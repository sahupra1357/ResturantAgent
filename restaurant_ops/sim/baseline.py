"""Baseline policy: fixed par levels, order-up-to every morning, no specials / 86s / re-planning.

par = mean daily usage over the last 28 days (POS sales x recipes) x (lead time + 2). Each morning, for every ingredient
whose position (usable on-hand + inbound 'sent' POs) has fallen below usage x (lead time + 1), order up to par from
the cheapest supplier that can deliver it fresh (lead time <= half its shelf life, at least one day), rounded up to
packs. Per-supplier orders below the minimum order value are scaled up to it (or skipped when that would need more
than MAX_TOPUP x the lines). Disruptions are ignored; they only hit the baseline through the world. Every PO is
written to `decisions` with agent='baseline', status 'executed' (no approvals). No LLM imports here.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from restaurant_ops.core import inventory as inv

LOOKBACK_DAYS = 28
PAR_BUFFER_DAYS = 2  # par = usage x (lead time + this)
MAX_TOPUP = 2.5  # max scale-up to reach a supplier's minimum order value


class BaselinePolicy:
    """Same daily interface as the agent policy: call `plan(world)` each morning."""

    name = "baseline"

    def __init__(self, lookback_days: int = LOOKBACK_DAYS, buffer_days: int = PAR_BUFFER_DAYS):
        self.lookback_days = lookback_days
        self.buffer_days = buffer_days

    # ------------------------------------------------------------------ inputs

    def mean_daily_usage(self, world) -> dict[str, float]:
        """Ingredient usage per day over the last `lookback_days` days before today (from pos_sales x recipe_lines)."""
        end = world.today.isoformat()
        start = (world.today - timedelta(days=self.lookback_days)).isoformat()
        rows = world.conn.execute(
            "SELECT r.ingredient_id, SUM(p.qty * r.qty_per_portion) FROM pos_sales p JOIN recipe_lines r ON r.dish_id=p.dish_id "
            "WHERE p.date>=? AND p.date<? GROUP BY r.ingredient_id", (start, end)).fetchall()
        return {r[0]: float(r[1]) / self.lookback_days for r in rows}

    def _choose_supplier(self, world, ingredient_id: str) -> dict | None:
        rows = world.conn.execute(
            "SELECT s.id, s.lead_time_days, s.min_order_value, i.price, i.pack_size FROM supplier_items i "
            "JOIN suppliers s ON s.id=i.supplier_id WHERE i.ingredient_id=? ORDER BY i.price, s.lead_time_days, s.id",
            (ingredient_id,)).fetchall()
        if not rows:
            return None
        shelf = int(world.ingredients[ingredient_id]["shelf_life_days"])
        fresh = [r for r in rows if r["lead_time_days"] <= max(1, shelf // 2)]
        return dict((fresh or rows)[0])

    # ------------------------------------------------------------------ plan

    def plan(self, world) -> dict:
        """Place today's par-level orders. Returns {"orders": [{po_id, supplier_id, delivery_date, total, lines}], "skipped": [...]}."""
        usage = self.mean_daily_usage(world)
        today = world.today
        inbound = defaultdict(float)
        for r in world.conn.execute("SELECT l.ingredient_id, l.qty FROM po_lines l JOIN purchase_orders p ON p.po_id=l.po_id "
                                    "WHERE p.status='sent'"):
            inbound[r[0]] += float(r[1])
        by_sup: dict[str, dict] = {}
        for ing, per_day in sorted(usage.items()):
            if per_day <= 1e-9:
                continue
            sup = self._choose_supplier(world, ing)
            if sup is None:
                continue
            lead = int(sup["lead_time_days"])
            position = inv.on_hand(ing, today.isoformat(), world.conn) + inbound.get(ing, 0.0)
            if position >= per_day * (lead + 1):
                continue
            par = per_day * (lead + self.buffer_days)
            pack = float(sup["pack_size"])
            qty = math.ceil((par - position) / pack - 1e-9) * pack
            if qty <= 0:
                continue
            g = by_sup.setdefault(sup["id"], {"lead": lead, "min": float(sup["min_order_value"]), "lines": []})
            g["lines"].append({"ingredient_id": ing, "qty": qty, "unit_price": float(sup["price"]), "pack": pack})
        orders, skipped = [], []
        for sid, g in sorted(by_sup.items()):
            lines = g["lines"]
            total = sum(l["qty"] * l["unit_price"] for l in lines)
            if total < g["min"]:
                scale = g["min"] / total
                if scale > MAX_TOPUP:
                    skipped.append({"supplier_id": sid, "total": round(total, 2), "min_order_value": g["min"]})
                    continue
                for l in lines:
                    l["qty"] = math.ceil(l["qty"] * scale / l["pack"] - 1e-9) * l["pack"]
            po_lines = [{"ingredient_id": l["ingredient_id"], "qty": round(l["qty"], 3), "unit_price": l["unit_price"]} for l in lines]
            delivery = (today + timedelta(days=g["lead"])).isoformat()
            total = round(sum(l["qty"] * l["unit_price"] for l in po_lines), 2)
            did = self._log(world, sid, delivery, po_lines, total)
            po = world.create_po(sid, delivery, po_lines, decision_id=did)
            orders.append({**po, "lines": po_lines})
        world.trace("event", {"event": "baseline_plan", "orders": len(orders), "skipped": skipped}, node="baseline")
        world.conn.commit()
        return {"orders": orders, "skipped": skipped}

    def _log(self, world, supplier_id: str, delivery: str, lines: list[dict], total: float) -> int:
        payload = {"supplier_id": supplier_id, "delivery_date": delivery, "lines": lines, "total": total}
        cur = world.conn.execute(
            "INSERT INTO decisions (run_id, sim_date, ts, agent, action_type, payload, rationale, status, violations, chef_note) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (world.run_id, world.today.isoformat(), datetime.now(timezone.utc).isoformat(timespec="seconds"), "baseline",
             "place_po", json.dumps(payload), f"par-level order-up-to from {supplier_id}", "executed", "[]", None))
        return int(cur.lastrowid)
