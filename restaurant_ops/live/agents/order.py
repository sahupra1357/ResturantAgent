"""Order agent: parses mods and allergies, checks allergens against the recipe, routes each item to its station and
sets fire timing per course. Fully deterministic (the hot path never calls a model).

Asks the manager when:
  - an allergen conflict cannot be fixed by leaving garnish ingredients out (allergen_swap; timeout = drop the item)
  - the kitchen has been slammed for a while (throttle_online; timeout = no change)
  - an online order runs well past its quote (comp; timeout = no comp)
"""
from __future__ import annotations

import time

from restaurant_ops.data import catalog as cat
from restaurant_ops.live import events as E
from restaurant_ops.live.agents.base import LiveAgent

COURSE_GAP_MIN = 12.0       # mains fire this long after starters
LARGE_PARTY = 8             # parties this big fire everything at once
SLAMMED_STREAK_MIN = 4      # minutes slammed before asking to throttle online orders
CALM_STREAK_MIN = 10        # minutes calm before reopening
COMP_LATE_MIN = 10.0        # online order this late past the P90 quote -> propose a comp
COMP_SHARE = 0.15


class OrderAgent(LiveAgent):
    name = "order"

    def __init__(self, svc):
        super().__init__(svc)
        self.slammed_for = 0
        self.calm_for = 0
        self.comped: set[str] = set()
        svc.bus.subscribe(E.ORDER_SUBMITTED, self.on_order, "order.on_order")
        svc.bus.subscribe(E.KITCHEN_LOAD, self.on_load, "order.on_load")
        svc.bus.subscribe(E.QUOTE_UPDATED, self.on_quote, "order.on_quote")

    # ------------------------------------------------------------------ orders
    def on_order(self, ev: E.Event) -> None:
        svc, p = self.svc, ev.payload
        t0 = p.get("t0") or time.perf_counter()
        now = svc.clock.t
        order_id = p["order_id"]
        trace: list = []
        notes = self.tool(trace, "parse_order", text=p.get("notes", ""))
        order_allergens = set(notes["allergens"])
        large = int(p.get("party_size") or 0) >= LARGE_PARTY or bool(p.get("large"))
        has_starter = any(i.get("course") == "starter" for i in p["items"])
        out_items, held, rejected = [], [], []
        for k, it in enumerate(p["items"]):
            dish = it["dish_id"]
            d = cat.DISHES_BY_ID.get(dish)
            if d is None:
                rejected.append({"dish_id": dish, "reason": "unknown dish"})
                continue
            mods = self.tool(trace, "parse_order", text=it.get("mods", "")) if it.get("mods") else {"allergens": [], "remove": [], "text": ""}
            guest = sorted(order_allergens | set(mods["allergens"]))
            if not svc.menu.orderable(dish) or svc.available_portions(dish) < 1:
                caught = not svc.menu.orderable(dish)
                rejected.append({"dish_id": dish, "reason": "86'd" if caught else "out of stock"})
                if not caught:
                    svc.metrics.stockouts += 1
                    svc.bus.publish(E.STOCK_OUT, {"dish_id": dish, "order_id": order_id, "name": d.name}, source=self.name)
                continue
            res = self.tool(trace, "allergen_check", dish_id=dish, guest_allergens=guest) if guest else {"conflict": [], "resolvable": True, "leave_out": []}
            item_id = f"{order_id}-{k + 1}"
            skip = sorted(set(res.get("leave_out", [])) | set(mods["remove"]))
            course = it.get("course") or ("main" if d.category == "main" else d.category)
            if res["conflict"] and not res["resolvable"]:
                held.append({"item_id": item_id, "dish_id": dish, "course": course, "guest_allergens": guest,
                             "conflict": res["conflict"], "blocking": res["blocking"]})
                continue
            route = self.tool(trace, "route_to_station", dish_id=dish)
            if p["channel"] == "online" or large or course in ("starter", "dessert") or not has_starter:
                fire = now
            else:
                fire = now + COURSE_GAP_MIN * 60
            mod_text = []
            if res.get("leave_out"):
                mod_text.append("ALLERGY " + "/".join(res["conflict"]) + ": no " + ", ".join(cat.INGREDIENTS_BY_ID[i].name for i in res["leave_out"]))
            if mods["remove"]:
                mod_text.append("no " + ", ".join(cat.INGREDIENTS_BY_ID[i].name for i in mods["remove"]))
            kit = svc.kitchen.add_item(item_id, order_id, dish, fire, mods=mod_text, large_party=large)
            svc.committed[dish] = svc.committed.get(dish, 0) + 1
            svc.item_skip[item_id] = skip
            svc.item_meta[item_id] = {"course": course, "channel": p["channel"], "party_id": p.get("party_id")}
            if svc.menu.sold(dish):
                svc.on_limit_reached(dish)
            out_items.append({"item_id": item_id, "dish_id": dish, "name": d.name, "station": route["station"], "course": course,
                              "fire_at": svc.clock.iso(fire), "mods": mod_text, "allergy_resolved": bool(res.get("leave_out"))})
        latency_ms = round((time.perf_counter() - t0) * 1000, 3)
        svc.metrics.order_latency_ms.append(latency_ms)
        order = {"order_id": order_id, "channel": p["channel"], "table_ids": p.get("table_ids", []), "party_id": p.get("party_id"),
                 "items": out_items, "held": held, "rejected": rejected, "allergens": sorted(order_allergens),
                 "latency_ms": latency_ms, "course_gap_min": COURSE_GAP_MIN if has_starter and not large else 0,
                 "stations": sorted({i["station"] for i in out_items}), "kind": p.get("kind", "food"),
                 "customer": p.get("customer", ""), "source": p.get("source", "sim")}
        svc.orders[order_id] = order
        svc.bus.publish(E.ORDER_CREATED, order, source=self.name)
        for h in held:
            self._ask_allergen(order, h)
        svc.order_results[order_id] = order

    def _ask_allergen(self, order: dict, h: dict) -> None:
        svc, trace = self.svc, []
        avail = svc.menu.available(svc.stock.all_portions())
        from restaurant_ops.live.tools import safe_alternatives

        alts = safe_alternatives(h["dish_id"], h["guest_allergens"], avail)
        to = alts[0] if alts else None
        trace.append({"tool": "allergen_check", "args": {"dish_id": h["dish_id"], "guest_allergens": h["guest_allergens"]},
                      "result": {"conflict": h["conflict"], "core_ingredients_with_allergen": h["blocking"], "safe_alternatives": alts[:4]}})
        d = cat.DISHES_BY_ID[h["dish_id"]]
        title = (f"Allergy: {d.name} has {'/'.join(h['conflict'])} in its core ({', '.join(h['blocking'])}); "
                 + (f"swap to {cat.DISHES_BY_ID[to].name}?" if to else "drop the item?"))
        svc.bus.publish(E.ITEM_HELD, {"order_id": order["order_id"], **h}, source=self.name)
        self.submit([self.proposal(
            "allergen_swap",
            {"order_id": order["order_id"], "item_id": h["item_id"], "from_dish": h["dish_id"], "to_dish": to,
             "guest_allergens": h["guest_allergens"], "course": h["course"], "alternatives": alts[:4]},
            title, f"guest declared {', '.join(h['guest_allergens'])}; the allergen comes from core ingredients that cannot be "
                   f"left out, so the kitchen holds the item until the manager decides", trace,
            key=f"allergen:{h['item_id']}", confidence=0.95, timeout_default="reject")])

    def apply_swap(self, payload: dict, approved: bool) -> dict:
        """Coordinator executes an allergen_swap: fire the safe dish (approved) or drop the item."""
        svc = self.svc
        order = svc.orders.get(payload["order_id"])
        if order is None:
            return {"error": "order gone"}
        order["held"] = [h for h in order["held"] if h["item_id"] != payload["item_id"]]
        to = payload.get("to_dish") if approved else None
        if not to:
            order.setdefault("rejected", []).append({"dish_id": payload["from_dish"], "reason": "allergen conflict: dropped"})
            svc.bus.publish(E.ORDER_CREATED, order, source=self.name)
            svc.after_item_change(order)
            return {"dropped": payload["item_id"]}
        item_id = payload["item_id"] + "s"
        d = cat.DISHES_BY_ID[to]
        svc.kitchen.add_item(item_id, order["order_id"], to, svc.clock.t, mods=["ALLERGY: swapped from " + cat.DISHES_BY_ID[payload["from_dish"]].name])
        svc.committed[to] = svc.committed.get(to, 0) + 1
        svc.item_skip[item_id] = []
        svc.item_meta[item_id] = {"course": payload.get("course", "main"), "channel": order["channel"], "party_id": order.get("party_id")}
        if svc.menu.sold(to):
            svc.on_limit_reached(to)
        order["items"].append({"item_id": item_id, "dish_id": to, "name": d.name, "station": d.station,
                               "course": payload.get("course", "main"), "fire_at": svc.clock.iso(), "mods": ["allergen swap"],
                               "allergy_resolved": True})
        svc.bus.publish(E.ORDER_CREATED, order, source=self.name)
        svc.after_item_change(order)
        return {"fired": item_id, "dish_id": to}

    def on_resolved(self, d: dict) -> None:
        if d["action_type"] == "allergen_swap" and d["status"] != "executed":
            self.apply_swap(d["payload"], approved=False)  # rejected / timed out / blocked: never serve the conflict

    # ------------------------------------------------------------------ kitchen load -> throttle online
    def explain(self, load: dict) -> str:
        """One-line explanation of kitchen pace for the manager / online page."""
        svc = self.svc
        worst = max(load["stations"], key=lambda r: r["wait_min"])
        recent = [o for o in svc.recent_large_fires(15)]
        bits = [f"{worst['station']} is {worst['wait_min']:.0f} min behind" if worst["wait_min"] >= 3 else "kitchen is keeping pace"]
        if recent:
            bits.append(f"{len(recent)} large table{'s' if len(recent) > 1 else ''} just fired")
        nxt = load.get("expected_next_30", 0)
        if nxt >= 15:
            bits.append(f"{nxt:.0f} covers expected in the next 30 min")
        return ", ".join(bits)

    def on_load(self, ev: E.Event) -> None:
        load, svc = ev.payload, self.svc
        if load["busyness"] == "slammed":
            self.slammed_for += 1
            self.calm_for = 0
        elif load["busyness"] == "calm":
            self.calm_for += 1
            self.slammed_for = 0
        else:
            self.slammed_for = 0
            self.calm_for = 0
        mode = svc.online.mode
        if self.slammed_for >= SLAMMED_STREAK_MIN and mode == "open":
            trace = [{"tool": "station_queue", "args": {}, "result": [
                {k: r[k] for k in ("station", "queue_depth", "wait_min", "utilisation")} for r in load["stations"]]}]
            others_calm = all(r["wait_min"] < 6 for r in load["stations"] if r["station"] in ("cold", "pastry"))
            new = "quick_only" if others_calm else "paused"
            why = self.explain(load)
            self.submit([self.proposal(
                "throttle_online", {"mode": new, "reason": why, "busyness": load["busyness"], "score": load["score"]},
                f"{'Limit online orders to quick items' if new == 'quick_only' else 'Pause online orders'}: {why}",
                f"kitchen slammed for {self.slammed_for} min (score {load['score']}); {why}", trace,
                key="throttle_online", confidence=0.8, timeout_default="reject")])
        elif self.calm_for >= CALM_STREAK_MIN and mode != "open":
            self.calm_for = 0
            self.submit([self.proposal("throttle_online", {"mode": "open", "reason": "kitchen calm again"},
                                       "Reopen online orders: kitchen calm again",
                                       f"busyness calm for {CALM_STREAK_MIN} min", [], key="reopen_online", confidence=0.9)])

    # ------------------------------------------------------------------ late online orders -> comp
    def on_quote(self, ev: E.Event) -> None:
        o = ev.payload
        if o.get("status") in ("ready", "picked_up") or o["order_id"] in self.comped:
            return
        late = float(o.get("late_by_min") or 0)
        if late < COMP_LATE_MIN:
            return
        self.comped.add(o["order_id"])
        amt = round(o.get("total", 0) * COMP_SHARE, 2)
        if amt <= 0:
            return
        self.submit([self.proposal(
            "comp", {"order_id": o["order_id"], "amount": amt, "order_total": o.get("total", 0),
                     "reason": f"{late:.0f} min past the quoted range"},
            f"Comp {COMP_SHARE:.0%} (${amt:.2f}) on online order {o['order_id']}: {late:.0f} min late",
            f"order is {late:.0f} min past its P90 quote; {o.get('reason') or ''}".strip(), [], key=f"comp:{o['order_id']}",
            confidence=0.7, timeout_default="reject")])
