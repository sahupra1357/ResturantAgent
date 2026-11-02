"""Tools the live agents call. Each returns numbers from the deterministic layer; agents never compute them.

Names follow the plan: Order (parse_order, allergen_check, route_to_station, station_queue), Inventory
(deplete_by_recipe, stock_by_lot, expiry_risk, variance_report), Forecasting (forecast_covers, forecast_dish_mix,
reservations, weather, signal_report), Procurement (optimize_orders, supplier_quotes, draft_po; send_po only runs in
the coordinator's execute step), Menu (recipes, remaining_portions, margin_calc, allergen_check).
Stock-dependent registry tools are called uncached because the live service changes stock every few seconds.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import re
from typing import TYPE_CHECKING, Callable

from restaurant_ops.data import catalog as cat
from restaurant_ops.tools import registry as R

if TYPE_CHECKING:  # pragma: no cover
    from restaurant_ops.live.service import Service

# --------------------------------------------------------------------------- order parsing

ALLERGY_WORDS = {
    "nut": ["nuts"], "nuts": ["nuts"], "peanut": ["nuts", "peanuts"], "tree nut": ["nuts"],
    "dairy": ["milk"], "milk": ["milk"], "lactose": ["milk"], "cheese": ["milk"],
    "gluten": ["gluten"], "wheat": ["gluten"], "coeliac": ["gluten"], "celiac": ["gluten"],
    "shellfish": ["crustaceans", "molluscs"], "crustacean": ["crustaceans"], "mollusc": ["molluscs"],
    "fish": ["fish"], "egg": ["eggs"], "eggs": ["eggs"], "soy": ["soy"], "soya": ["soy"],
    "sulphite": ["sulphites"], "sulfite": ["sulphites"], "sesame": ["sesame"],
}
_ING_WORDS = {}
for _i in cat.INGREDIENTS:
    for w in {_i.id.replace("_", " "), _i.name.lower(), _i.name.lower().split(" (")[0]}:
        _ING_WORDS[w] = _i.id
    _ING_WORDS.setdefault(_i.id.split("_")[0], _i.id)


def parse_mods(text: str) -> dict:
    """'no dairy, nut allergy, no onion' -> {allergens: [milk, nuts], remove: [onion], notes: [...]}."""
    t = (text or "").lower()
    allergens: set[str] = set()
    remove: set[str] = set()
    for part in re.split(r"[,;/]| and ", t):
        p = part.strip()
        if not p:
            continue
        hit = False
        for word, al in sorted(ALLERGY_WORDS.items(), key=lambda kv: -len(kv[0])):
            if re.search(rf"\b{re.escape(word)}\b", p) and re.search(r"allerg|free|no |without|intoleran|coeliac|celiac|avoid", p + " "):
                allergens.update(al)
                hit = True
                break
        if hit:
            continue
        m = re.search(r"(?:no|without|hold the|hold)\s+(.+)", p)
        if m:
            word = m.group(1).replace("please", "").strip()
            for w, ing in sorted(_ING_WORDS.items(), key=lambda kv: -len(kv[0])):
                if w and (word == w or word.startswith(w) or w.startswith(word)) and len(word) >= 3:
                    remove.add(ing)
                    break
    return {"allergens": sorted(allergens), "remove": sorted(remove), "text": text or ""}


def dish_allergen_sources(dish_id: str) -> dict[str, list[str]]:
    """allergen -> ingredients of the dish that carry it."""
    out: dict[str, list[str]] = {}
    for ing in cat.DISHES_BY_ID[dish_id].recipe:
        for a in cat.INGREDIENTS_BY_ID[ing].allergens:
            out.setdefault(a, []).append(ing)
    return out


CORE_COST_SHARE = 0.2  # ingredients above this share of the dish's food cost cannot be left out


def core_ingredients(dish_id: str) -> set[str]:
    d = cat.DISHES_BY_ID[dish_id]
    cost = {i: cat.INGREDIENTS_BY_ID[i].ref_cost * q for i, q in d.recipe.items()}
    tot = sum(cost.values()) or 1.0
    core = {i for i, c in cost.items() if c / tot >= CORE_COST_SHARE}
    core.add(max(d.recipe, key=lambda i: cost[i]))
    return core


def allergen_resolution(dish_id: str, guest_allergens: list[str]) -> dict:
    """Can the kitchen make this dish safe by leaving ingredients out? {conflict, resolvable, leave_out, blocking}."""
    src = dish_allergen_sources(dish_id)
    conflict = sorted(set(src) & set(guest_allergens))
    if not conflict:
        return {"conflict": [], "resolvable": True, "leave_out": [], "blocking": []}
    leave = sorted({i for a in conflict for i in src[a]})
    core = core_ingredients(dish_id)
    blocking = sorted(i for i in leave if i in core)
    return {"conflict": conflict, "resolvable": not blocking, "leave_out": leave if not blocking else [], "blocking": blocking}


def safe_alternatives(dish_id: str, guest_allergens: list[str], available: dict[str, int]) -> list[str]:
    d = cat.DISHES_BY_ID[dish_id]
    out = []
    for o in cat.DISHES:
        if o.id == dish_id or o.category != d.category or available.get(o.id, 0) <= 0:
            continue
        if not allergen_resolution(o.id, guest_allergens)["conflict"]:
            out.append(o.id)
    return sorted(out, key=lambda x: abs(cat.DISHES_BY_ID[x].price - d.price))


# --------------------------------------------------------------------------- toolbox


def fresh(name: str, **kw):
    """Call a registry tool bypassing its result cache (stock moves continuously during service)."""
    fn = R.FUNCTIONS[name]
    raw = getattr(fn, "__wrapped__", fn)
    return R._jsonable(raw(**kw))


def compact(obj, limit: int = 600):
    s = json.dumps(obj, default=str)
    if len(s) <= limit:
        return obj
    if isinstance(obj, list):
        return {"n": len(obj), "head": [compact(x, limit // 4) for x in obj[:3]]}
    if isinstance(obj, dict):
        return {k: compact(v, max(60, limit // max(1, len(obj)))) for k, v in list(obj.items())[:12]}
    return s[:limit] + "..."


class ToolBox:
    def __init__(self, svc: "Service"):
        self.svc = svc
        s = svc
        self.fns: dict[str, Callable] = {
            # order agent
            "parse_order": lambda text="": parse_mods(text),
            "allergen_check": lambda dish_id, guest_allergens=(): allergen_resolution(dish_id, list(guest_allergens)),
            "route_to_station": lambda dish_id: {"dish_id": dish_id, "station": cat.DISHES_BY_ID[dish_id].station},
            "station_queue": lambda: s.kitchen.load(s.clock.t, s.expected_next_30())["stations"],
            # inventory agent
            "deplete_by_recipe": lambda dish_id, qty=1, skip=(): s.stock.deplete(dish_id, qty, skip),
            "stock_by_lot": lambda ingredient_id=None: [dict(l, ingredient_id=i) for i, ls in s.stock.lots.items()
                                                        if ingredient_id in (None, i) for l in ls],
            "expiry_risk": lambda within_days=1: s.stock.expiring(within_days),
            "variance_report": lambda days=7: R.variance_report(days=days),
            # forecasting agent
            "forecast_covers": lambda start_date=None, days=1: R.forecast_covers(start_date=start_date, days=days),
            "forecast_dish_mix": lambda start_date=None, days=1, meal_period=None: R.forecast_dish_mix(
                start_date=start_date, days=days, meal_period=meal_period),
            "reservations": lambda start_date=None, days=1: R.reservations_summary(start_date=start_date, days=days),
            "weather": lambda start_date=None, days=2: R.weather(start_date=start_date, days=days),
            "signal_report": lambda: R.signal_report(),
            # procurement agent
            "optimize_orders": lambda **kw: fresh("optimize_orders", **kw),
            "supplier_quotes": lambda ingredient_id, qty, needed_by: fresh("supplier_quotes", ingredient_id=ingredient_id,
                                                                          qty=qty, needed_by=needed_by),
            "expected_usage": lambda **kw: fresh("expected_usage", **kw),
            "open_pos": lambda ingredient_id=None: s.open_pos(ingredient_id),
            "negotiate": lambda **kw: R.negotiate(**kw),
            "supplier_scorecard": lambda: s.supplier_scorecard(),
            "learn_supplier": lambda supplier_id, kind, fill=None, late=None, note="": s.learn_supplier(supplier_id, kind, fill, late, note),
            "draft_po": lambda supplier_id, delivery_date, lines: {
                "supplier_id": supplier_id, "delivery_date": delivery_date, "lines": lines,
                "total": round(sum(float(l["qty"]) * float(l["unit_price"]) for l in lines), 2)},
            # menu agent
            "recipes": lambda dish_id=None: R.recipes(dish_id=dish_id),
            "remaining_portions": lambda dish_id: {"dish_id": dish_id, "portions": s.available_portions(dish_id),
                                                   "limiting": s.stock.limiting(dish_id)},
            "margin_calc": lambda dish_id: R.margin_calc(dish_id=dish_id),
            "dishes_using": lambda ingredient_ids: [d for d in s.stock.uses_any(ingredient_ids)],
            "remaining_forecast": lambda: s.forecast.dish_remaining(s.clock.t / 60),
        }

    def bind(self) -> None:
        """Point the shared tool registry at this service's database and date. The registry is process-global, so with
        several services in one process (tests, a resumed service next to a new one) each call re-binds it."""
        svc = self.svc
        if R._DB_PATH != Path(svc.db_path):
            R.set_db_path(svc.db_path)
        if R.get_sim_date() != svc.date.isoformat():
            R.set_sim_date(svc.date)

    def call(self, trace: list | None, name: str, **kw):
        self.bind()
        res = self.fns[name](**kw)
        if trace is not None:
            trace.append({"tool": name, "args": compact(kw, 200), "result": compact(res)})
        return res


def ceil_packs(qty: float, pack: float) -> float:
    return math.ceil(qty / pack - 1e-9) * pack
