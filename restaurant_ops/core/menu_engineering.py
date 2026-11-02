"""Menu engineering: mix x margin analysis (Kasavana-Smith), price suggestions, seasonal rotation. No LLM here.

- Popularity: a dish's share of portions sold; "popular" = share >= 70% of an equal share (1/N).
- Contribution margin (CM) = price - food cost; "profitable" = CM >= the sales-weighted average CM.
- Classes: Star (popular, profitable), Plowhorse (popular, low CM), Puzzle (unpopular, high CM), Dog (neither).
- Price suggestions: the synthetic history never changes prices, so elasticity cannot be estimated from it; the
  suggestion assumes a constant elasticity (ASSUMED_ELASTICITY) and says so. Plowhorses (popular, below-average
  margin) get an increase that moves their margin toward the average, between MIN_PRICE_STEP_PCT and
  MAX_PRICE_STEP_PCT (more when food cost is above TARGET_FOOD_COST_PCT); Puzzles get "promote / reposition";
  Dogs are replacement candidates.
- Rotation: dishes whose season ends (or starts) within ROTATION_WINDOW_DAYS, from `seasonal_months`.
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from restaurant_ops.core.menu import margin_calc
from restaurant_ops.core.util import iso, r2, to_date
from restaurant_ops.data import catalog as cat

POPULARITY_FACTOR = 0.7
ASSUMED_ELASTICITY = -0.8
TARGET_FOOD_COST_PCT = 30.0
MAX_PRICE_STEP_PCT = 8.0
MIN_PRICE_STEP_PCT = 3.0
ROTATION_WINDOW_DAYS = 30


def _in_season(d: cat.Dish, day: date) -> bool:
    return cat.in_season(d.seasonal_months, day.month)


def analyze(conn: sqlite3.Connection, as_of: "str | date", days: int = 28, extra_sales: dict[str, float] | None = None) -> dict:
    """Menu-engineering table for dishes on the menu at `as_of`, from the last `days` days of POS sales
    (+ `extra_sales`, e.g. tonight's live sales)."""
    a = to_date(as_of)
    rows = conn.execute("SELECT dish_id, SUM(qty) FROM pos_sales WHERE date>=? AND date<? GROUP BY dish_id",
                        ((a - timedelta(days=days)).isoformat(), a.isoformat())).fetchall()
    sold = {r[0]: float(r[1]) for r in rows}
    for k, v in (extra_sales or {}).items():
        sold[k] = sold.get(k, 0.0) + float(v)
    on_menu = [d for d in cat.DISHES if _in_season(d, a)]
    total = sum(sold.get(d.id, 0.0) for d in on_menu) or 1.0
    info = []
    for d in on_menu:
        m = margin_calc(d.id, conn=conn)
        q = sold.get(d.id, 0.0)
        info.append({"dish_id": d.id, "name": d.name, "category": d.category, "price": d.price, "sold": int(q),
                     "mix_pct": r2(q / total * 100, 1), "food_cost": m["food_cost"], "food_cost_pct": m["food_cost_pct"],
                     "cm": m["margin"], "cm_total": r2(m["margin"] * q)})
    avg_cm = sum(i["cm"] * i["sold"] for i in info) / max(sum(i["sold"] for i in info), 1)
    pop_cut = 100.0 / max(len(info), 1) * POPULARITY_FACTOR
    for i in info:
        popular, profitable = i["mix_pct"] >= pop_cut, i["cm"] >= avg_cm
        i["class"] = {(True, True): "star", (True, False): "plowhorse", (False, True): "puzzle", (False, False): "dog"}[(popular, profitable)]
        i["suggestion"] = suggest(i, avg_cm)
    info.sort(key=lambda i: -i["cm_total"])
    return {"as_of": iso(a), "days": days, "avg_cm": r2(avg_cm), "popularity_cut_pct": r2(pop_cut, 1),
            "assumed_elasticity": ASSUMED_ELASTICITY, "dishes": info, "rotation": rotation(a)}


def suggest(i: dict, avg_cm: float | None = None) -> dict:
    c = i["class"]
    if c == "plowhorse":
        gap = ((avg_cm - i["cm"]) / i["price"]) if avg_cm else 0.0
        if i["food_cost_pct"] > TARGET_FOOD_COST_PCT:
            gap = max(gap, (i["food_cost"] / (TARGET_FOOD_COST_PCT / 100)) / i["price"] - 1)
        step = min(MAX_PRICE_STEP_PCT / 100, max(MIN_PRICE_STEP_PCT / 100, gap))
        new = round(i["price"] * (1 + step) * 2) / 2  # to the half dollar
        if new <= i["price"]:
            new = i["price"] + 0.5
        dq = ASSUMED_ELASTICITY * (new / i["price"] - 1)
        rev_change = (1 + dq) * new / i["price"] - 1
        return {"action": "price_increase", "new_price": new,
                "why": f"popular with a below-average margin (${i['cm']:.2f} vs ${avg_cm or 0:.2f}); +{(new / i['price'] - 1) * 100:.0f}% "
                       f"at assumed elasticity {ASSUMED_ELASTICITY}: volume {dq * 100:+.1f}%, revenue {rev_change * 100:+.1f}%"}
    if c == "puzzle":
        return {"action": "promote", "why": "high margin, low sales: reposition on the menu, server suggestion, special"}
    if c == "dog":
        return {"action": "replace", "why": "low sales and low margin: candidate to rotate out"}
    return {"action": "keep", "why": "star: keep quality and placement"}


def rotation(as_of: "str | date", window_days: int = ROTATION_WINDOW_DAYS) -> list[dict]:
    """Seasonal changes coming up: dishes leaving or entering season within the window."""
    a = to_date(as_of)
    later = a + timedelta(days=window_days)
    out = []
    for d in cat.DISHES:
        if not d.seasonal_months:
            continue
        now_in, then_in = _in_season(d, a), _in_season(d, later)
        if now_in and not then_in:
            out.append({"dish_id": d.id, "name": d.name, "change": "leaving", "by": later.isoformat(),
                        "why": f"in season months {list(d.seasonal_months)} only; plan a replacement"})
        elif not now_in and then_in:
            out.append({"dish_id": d.id, "name": d.name, "change": "entering", "by": later.isoformat(),
                        "why": f"comes into season (months {list(d.seasonal_months)}); prep the recipe and suppliers"})
    return out
