"""Inventory agent: on every item.fired it takes the recipe BOM from the right lots (FEFO) and checks the dishes that
use those ingredients against tonight's remaining forecast. No LLM: this is the hot path (target < 500 ms; it takes
well under a millisecond).

Publishes stock.low (debounced per ingredient), stock.out, lot.expiring (hourly sweep) and inventory snapshots.
Asks the manager before discarding stock worth more than LIVE_DISCARD_APPROVAL_VALUE (a quarantine for food
safety applies at once; the discard itself waits, and its timeout default is to discard).
"""
from __future__ import annotations

import math
import time

from restaurant_ops.config import settings
from restaurant_ops.data import catalog as cat
from restaurant_ops.live import events as E
from restaurant_ops.live.agents.base import LiveAgent
from restaurant_ops.live.menu_live import LAST_N

LOW_DEBOUNCE_MIN = 30.0
EXPIRY_MIN_VALUE = 20.0


class InventoryAgent(LiveAgent):
    name = "inventory"

    def __init__(self, svc):
        super().__init__(svc)
        self.low_sent: dict[str, dict] = {}   # ingredient -> {t, remaining}
        self.expiry_sent: set[str] = set()
        svc.bus.subscribe(E.ITEM_FIRED, self.on_fired, "inventory.on_fired")
        svc.bus.subscribe(E.FORECAST_UPDATED, self.on_forecast, "inventory.on_forecast")
        svc.bus.subscribe(E.SUPPLIER_SHORTFALL, self.on_shortfall, "inventory.on_shortfall")

    # ------------------------------------------------------------------ hot path
    def on_fired(self, ev: E.Event) -> None:
        svc, p = self.svc, ev.payload
        t0 = time.perf_counter()
        dish, item_id = p["dish_id"], p["item_id"]
        res = svc.stock.deplete(dish, 1, svc.item_skip.get(item_id, ()))
        svc.committed[dish] = max(0, svc.committed.get(dish, 0) - 1)
        svc.metrics.food_cost += res["cost"]
        if res["shortfall"]:
            svc.metrics.short_items += 1
            svc.bus.publish(E.STOCK_OUT, {"dish_id": dish, "item_id": item_id, "shortfall": res["shortfall"],
                                          "name": cat.DISHES_BY_ID[dish].name}, source=self.name)
        self.check_dishes(svc.stock.uses_any(res["ingredients"]))
        svc.inventory_dirty = True
        svc.metrics.depletion_ms.append(round((time.perf_counter() - t0) * 1000, 3))

    def check_dishes(self, dish_ids, force: bool = False, reason: str = "") -> list[dict]:
        """stock.low for each orderable dish whose remaining portions fall below tonight's remaining forecast (par)."""
        svc = self.svc
        t_min = svc.clock.t / 60
        remaining_fc = svc.forecast.dish_remaining(t_min)
        out = []
        for d in dish_ids:
            if not svc.menu.orderable(d) or d in svc.menu.limits:
                continue
            left = svc.available_portions(d)
            need = remaining_fc.get(d, 0.0)
            par = max(LAST_N, math.ceil(need))
            if need < 2 or left >= par:
                continue
            ing = svc.stock.limiting(d)
            prev = self.low_sent.get(ing)
            if prev and not force and (t_min - prev["t"] < LOW_DEBOUNCE_MIN and left > prev["remaining"] / 2):
                continue
            self.low_sent[ing] = {"t": t_min, "remaining": left}
            ev = {"ingredient_id": ing, "ingredient": cat.INGREDIENTS_BY_ID[ing].name, "dish_id": d,
                  "dish": cat.DISHES_BY_ID[d].name, "remaining_portions": left, "forecast_remaining": need, "par": par,
                  "on_hand": round(svc.stock.on_hand(ing), 3), "unit": cat.INGREDIENTS_BY_ID[ing].unit,
                  "shortage": svc.shortages.get(ing), "reason": reason or "depletion"}
            out.append(ev)
            svc.bus.publish(E.STOCK_LOW, ev, source=self.name)
        return out

    def on_forecast(self, ev: E.Event) -> None:
        if ev.payload.get("recheck"):
            self.check_dishes(self.svc.menu.on_menu(), force=True, reason=ev.payload.get("reason", "forecast revised"))

    def on_shortfall(self, ev: E.Event) -> None:
        ing = ev.payload["ingredient_id"]
        self.check_dishes(self.svc.stock.uses_any([ing]), force=True, reason="supplier shortfall")

    # ------------------------------------------------------------------ sweeps
    def sweep_expiry(self) -> list[dict]:
        trace: list = []
        lots = self.tool(trace, "expiry_risk", within_days=1)
        out = []
        for l in lots:
            if l["lot_id"] in self.expiry_sent or l["value"] < EXPIRY_MIN_VALUE or l["quarantined"]:
                continue
            self.expiry_sent.add(l["lot_id"])
            l = {**l, "ingredient": cat.INGREDIENTS_BY_ID[l["ingredient_id"]].name,
                 "dishes": self.svc.stock.uses_any([l["ingredient_id"]])}
            out.append(l)
            self.svc.bus.publish(E.LOT_EXPIRING, l, source=self.name)
        return out

    def start_of_service(self) -> None:
        """Variance check (stock counts vs system) at the start of service; flags are informational, auto-executed."""
        trace: list = []
        flags = [v for v in self.tool(trace, "variance_report", days=7) if v["flag"] in ("shrinkage", "over_portioning")]
        props = [self.proposal("flag_variance", {"ingredient_id": v["ingredient_id"], "variance_pct": v["variance_pct"], "flag": v["flag"]},
                               f"Variance: {cat.INGREDIENTS_BY_ID[v['ingredient_id']].name} {v['variance_pct']}% ({v['flag']})",
                               f"counted {v['actual']} vs system {v['expected']} over {v['n_counts']} counts", trace,
                               key=f"variance:{v['ingredient_id']}", confidence=0.75) for v in flags]
        self.submit(props)
        self.sweep_expiry()

    # ------------------------------------------------------------------ cooler failure
    def cooler_failure(self, category: str = "dairy") -> dict:
        svc, trace = self.svc, []
        lots = [l for l in svc.stock.lots_where(category=category, storage="cooler")]
        trace.append({"tool": "stock_by_lot", "args": {"category": category, "storage": "cooler"},
                      "result": {"n": len(lots), "value": round(sum(l["value"] for l in lots), 2)}})
        ids = [l["lot_id"] for l in lots]
        svc.stock.quarantine.update(ids)
        svc.inventory_dirty = True
        value = round(sum(l["value"] for l in lots), 2)
        ings = sorted({l["ingredient_id"] for l in lots})
        svc.bus.publish(E.LOT_QUARANTINED, {"lots": lots, "ingredients": ings, "value": value, "reason": "walk-in cooler failure"},
                        source=self.name)
        self.submit([self.proposal(
            "discard_lots", {"lots": [{"lot_id": i, "reason": "spoilage"} for i in ids], "value": value, "ingredients": ings},
            f"Discard {len(ids)} {category} lots (${value:.2f}): walk-in cooler failed",
            f"{len(ids)} {category} lots held above safe temperature are quarantined; food safety requires discarding them",
            trace, key=f"discard:cooler:{category}", confidence=0.95, timeout_default="approve", est_cost=value)])
        return {"quarantined": len(ids), "value": value, "ingredients": ings}

    def on_resolved(self, d: dict) -> None:
        if d["action_type"] == "discard_lots" and d["status"] in ("rejected",) and d.get("resolved_by") != "timeout":
            ids = [l["lot_id"] if isinstance(l, dict) else l for l in d["payload"]["lots"]]
            self.svc.stock.quarantine.difference_update(ids)
            self.svc.inventory_dirty = True
            self.log(f"manager kept {len(ids)} quarantined lots ('{d.get('note')}'); back in stock")

    def on_tick(self, t_min: float) -> None:
        if int(t_min) % 60 == 0 and t_min > 0:
            self.sweep_expiry()


def discard_needs_approval(value: float) -> bool:
    return value > settings.LIVE_DISCARD_APPROVAL_VALUE
