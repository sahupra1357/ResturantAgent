"""Menu agent: on stock.low, lot.expiring and quarantines it proposes 86-ing a dish ("86 salmon after 4 more") or a
special that uses at-risk stock, after checking remaining portions, tonight's forecast, margin and allergens.
Every 86 and every special goes to the manager. It also keeps the 'last N' flags that limit dishes online at once.
"""
from __future__ import annotations

from restaurant_ops.config import settings
from restaurant_ops.data import catalog as cat
from restaurant_ops.live import events as E
from restaurant_ops.live.agents.base import LiveAgent
from restaurant_ops.live.menu_live import LAST_N

BUFFER = 2                 # portions held back when 86-ing "after N more" (allergy swaps, refires)
MIN_MARGIN_PCT = 60.0
SPECIAL_MAX = 12
SPECIAL_MIN = 4
# active + pending specials at any time: config.Settings.MAX_SPECIALS


class MenuAgent(LiveAgent):
    name = "menu"
    llm_reviewed = True

    def __init__(self, svc):
        super().__init__(svc)
        self.special_for: set[str] = set()
        svc.bus.subscribe(E.STOCK_LOW, self.on_low, "menu.on_low")
        svc.bus.subscribe(E.LOT_EXPIRING, self.on_expiring, "menu.on_expiring")
        svc.bus.subscribe(E.LOT_QUARANTINED, self.on_quarantine, "menu.on_quarantine")
        svc.bus.subscribe(E.STOCK_OUT, self.on_out, "menu.on_out")

    # ------------------------------------------------------------------ helpers
    def _special_candidates(self, category: str | None, trace: list, prefer_ingredients=(), exclude=(), need: float = 0.0,
                            check_shortages: bool = True) -> list[dict]:
        svc = self.svc
        blocked_ings = set(svc.stock_low_ingredients()) | set(svc.shortages) if check_shortages else set()
        out = []
        for d in cat.DISHES:
            if d.id in exclude or (category and d.category != category) or not svc.menu.orderable(d.id) or d.id in svc.menu.specials:
                continue
            if check_shortages and set(d.recipe) & blocked_ings:
                continue
            rp = self.tool(trace, "remaining_portions", dish_id=d.id)
            if rp["portions"] < max(SPECIAL_MIN, need):
                continue
            m = self.tool(trace, "margin_calc", dish_id=d.id)
            if m.get("margin_pct", 0) < MIN_MARGIN_PCT:
                continue
            score = m["margin_pct"] + (40 if set(d.recipe) & set(prefer_ingredients) else 0)
            out.append({"dish_id": d.id, "portions": rp["portions"], "margin_pct": m["margin_pct"], "score": score})
        return sorted(out, key=lambda c: -c["score"])

    def _special(self, cand: dict, reason: str, trace: list, key: str, title_extra: str = "") -> dict:
        d = cat.DISHES_BY_ID[cand["dish_id"]]
        r = self.tool(trace, "recipes", dish_id=d.id)
        allergens = r[0]["allergens"] if r else []
        portions = int(min(cand["portions"], SPECIAL_MAX))
        return self.proposal(
            "add_special", {"dish_id": d.id, "portions": portions, "price": d.price, "allergens_declared": allergens,
                            "date": self.svc.date.isoformat(), "meal_period": self.svc.forecast.period},
            f"Special: {d.name} ({portions} portions){title_extra}", reason, trace, key=f"special:{d.id}",
            confidence=0.75, timeout_default="reject")

    def room_for_special(self, ending: str | None = None) -> bool:
        """Fewer than MAX_SPECIALS active or pending (a special being 86'd frees its slot)."""
        svc = self.svc
        pending = sum(1 for d in svc.coordinator.decisions.values() if d["action_type"] == "add_special" and d["status"] == "pending")
        active = sum(1 for d in svc.menu.specials if d != ending)
        return active + pending < settings.MAX_SPECIALS

    # ------------------------------------------------------------------ triggers
    def on_low(self, ev: E.Event) -> None:
        p, svc = ev.payload, self.svc
        dish = p["dish_id"]
        if not svc.menu.orderable(dish) or dish in svc.menu.limits:
            return
        trace: list = []
        rp = self.tool(trace, "remaining_portions", dish_id=dish)
        fc = self.tool(trace, "remaining_forecast").get(dish, 0.0)
        left = rp["portions"]
        after = max(0, left - BUFFER)
        d = cat.DISHES_BY_ID[dish]
        props = [self.proposal(
            "86_dish", {"dish_id": dish, "date": svc.date.isoformat(), "meal_period": svc.forecast.period, "after_portions": after,
                        "remaining_portions": left},
            f"86 {d.name} after {after} more" if after else f"86 {d.name} now",
            f"{left} portions left ({p['ingredient']} {p['on_hand']} {p['unit']}) vs {fc:.0f} still forecast tonight"
            + (f"; {p['shortage']['supplier_id']} short until {p['shortage']['until']}" if p.get("shortage") else "")
            + f"; keep {BUFFER} back for refires and allergy swaps", trace,
            key=f"86:{dish}", confidence=0.85, timeout_default="approve" if left <= BUFFER else "reject")]
        if dish not in self.special_for and self.room_for_special(ending=dish):
            gap = max(0.0, fc - after)
            cands = self._special_candidates(d.category, trace, prefer_ingredients=[l["ingredient_id"] for l in svc.stock.expiring(2)],
                                             exclude={dish}, need=min(gap, SPECIAL_MAX))
            if cands:
                self.special_for.add(dish)
                c = cands[0]
                props.append(self._special(c, f"replaces {d.name} for the ~{gap:.0f} guests still expected to want it; "
                                              f"{c['portions']} portions on hand, margin {c['margin_pct']}%", trace,
                                           key=f"special:{c['dish_id']}", title_extra=f" to replace {d.name}"))
        self.submit(props, situation={"stock_low": p, "forecast_remaining": fc})

    def on_expiring(self, ev: E.Event) -> None:
        l, svc = ev.payload, self.svc
        if l["ingredient_id"] in self.special_for or not self.room_for_special():
            return
        trace: list = []
        dishes = self.tool(trace, "dishes_using", ingredient_ids=[l["ingredient_id"]])
        cands = [c for c in self._special_candidates(None, trace, prefer_ingredients=[l["ingredient_id"]], check_shortages=False)
                 if c["dish_id"] in dishes]
        if not cands:
            return
        self.special_for.add(l["ingredient_id"])
        c = cands[0]
        self.submit([self._special(c, f"lot {l['lot_id']}: {l['qty']} {l['unit']} {l['ingredient']} (${l['value']:.2f}) expires "
                                      f"{l['expiry_date']}; {c['portions']} portions possible, margin {c['margin_pct']}%",
                                   trace, key=f"special:{c['dish_id']}", title_extra=f" to use expiring {l['ingredient'].lower()}")],
                    situation={"lot": l})

    def on_quarantine(self, ev: E.Event) -> None:
        svc, trace = self.svc, []
        props = []
        for dish in self.tool(trace, "dishes_using", ingredient_ids=ev.payload["ingredients"]):
            if not svc.menu.orderable(dish):
                continue
            rp = self.tool(trace, "remaining_portions", dish_id=dish)
            if rp["portions"] > 0:
                continue
            d = cat.DISHES_BY_ID[dish]
            props.append(self.proposal(
                "86_dish", {"dish_id": dish, "date": svc.date.isoformat(), "meal_period": svc.forecast.period, "after_portions": 0,
                            "remaining_portions": 0},
                f"86 {d.name} now: no usable {cat.INGREDIENTS_BY_ID[rp['limiting']].name.lower()}",
                f"{ev.payload['reason']}: {rp['limiting']} quarantined, 0 portions left", trace, key=f"86:{dish}",
                confidence=0.95, timeout_default="approve"))
        self.submit(props, situation={"quarantine": ev.payload["ingredients"]})

    def on_out(self, ev: E.Event) -> None:
        dish = ev.payload["dish_id"]
        if self.svc.menu.orderable(dish) and self.svc.available_portions(dish) <= 0:
            self.submit([self.proposal(
                "86_dish", {"dish_id": dish, "date": self.svc.date.isoformat(), "meal_period": self.svc.forecast.period,
                            "after_portions": 0, "remaining_portions": 0},
                f"86 {cat.DISHES_BY_ID[dish].name} now: out of stock", "a guest ordered it with no stock left", [],
                key=f"86:{dish}", confidence=0.99, timeout_default="approve")])

    # ------------------------------------------------------------------ menu engineering (prices take effect next service)
    def start_of_service(self) -> None:
        svc = self.svc
        eng = svc.menu_engineering()
        trace = [{"tool": "menu_engineering", "args": {"days": eng["days"]},
                  "result": {d["dish_id"]: d["class"] for d in eng["dishes"]}}]
        props = []
        for d in eng["dishes"]:
            sug = d["suggestion"]
            if sug["action"] == "price_increase":
                props.append(self.proposal(
                    "price_change", {"dish_id": d["dish_id"], "old_price": d["price"], "new_price": sug["new_price"],
                                     "effective": "next service", "reason": sug["why"]},
                    f"Price {d['name']} {d['price']:.2f} -> {sug['new_price']:.2f} from tomorrow ({d['class']})",
                    sug["why"], trace, key=f"price:{d['dish_id']}", confidence=0.6, timeout_default="reject"))
        for r in eng["rotation"]:
            self.log(f"seasonal rotation: {r['name']} {r['change']} season by {r['by']} ({r['why']})")
        props.sort(key=lambda x: -(x["payload"]["new_price"] - x["payload"]["old_price"]))
        self.submit(props[:2], situation={"engineering": [(d["name"], d["class"]) for d in eng["dishes"]]})

    # ------------------------------------------------------------------ last-N flags (no approval: informational)
    def refresh_flags(self) -> bool:
        svc = self.svc
        new = {}
        for d in svc.menu.on_menu():
            if not svc.menu.orderable(d):
                continue
            n = svc.available_portions(d)
            if d in svc.menu.limits:
                n = min(n, svc.menu.limits[d])
            if 0 < n <= LAST_N:
                new[d] = n
        if new != svc.menu.last_n:
            svc.menu.last_n = new
            return True
        return False
