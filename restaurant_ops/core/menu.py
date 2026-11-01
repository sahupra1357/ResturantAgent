"""Menu maths: recipes, margins, allergens, feasibility, and the day's menu status. No LLM imports here."""
from __future__ import annotations

import math
import sqlite3
from collections import defaultdict

from restaurant_ops.core.inventory import on_hand
from restaurant_ops.core.util import conn_ctx, iso, r2


def _split(s: str | None) -> list[str]:
    return [x for x in (s or "").split(",") if x]


def _dish_row(c: sqlite3.Connection, dish_id: str) -> sqlite3.Row | None:
    return c.execute("SELECT * FROM dishes WHERE id=?", (dish_id,)).fetchone()


def _recipe_lines(c: sqlite3.Connection, dish_id: str) -> dict[str, float]:
    return {r["ingredient_id"]: float(r["qty_per_portion"]) for r in
            c.execute("SELECT ingredient_id, qty_per_portion FROM recipe_lines WHERE dish_id=?", (dish_id,))}


def _ing_allergens(c: sqlite3.Connection) -> dict[str, set[str]]:
    return {r["id"]: set(_split(r["allergens"])) for r in c.execute("SELECT id, allergens FROM ingredients")}


def dish_allergen_set(c: sqlite3.Connection, dish_id: str, substitutions: dict[str, str] | None = None) -> set[str]:
    """Allergens of a dish (union over its ingredients), optionally after ingredient substitutions."""
    ia = _ing_allergens(c)
    out: set[str] = set()
    for ing in _recipe_lines(c, dish_id):
        out |= ia.get((substitutions or {}).get(ing, ing), set())
    return out


def recipes(dish_id: str | None = None, conn: sqlite3.Connection | None = None) -> list[dict]:
    """Recipes with BOM lines and allergens: [{dish_id, name, lines:[{ingredient_id, qty_per_portion}], allergens, ...}]."""
    with conn_ctx(conn) as c:
        rows = c.execute("SELECT * FROM dishes" + (" WHERE id=?" if dish_id else "") + " ORDER BY id",
                         (dish_id,) if dish_id else ()).fetchall()
        return [{"dish_id": r["id"], "name": r["name"], "category": r["category"], "station": r["station"],
                 "price": r["price"], "prep_minutes": r["prep_minutes"], "is_cold": bool(r["is_cold"]),
                 "seasonal_months": [int(m) for m in _split(r["seasonal_months"])],
                 "lines": [{"ingredient_id": i, "qty_per_portion": q} for i, q in _recipe_lines(c, r["id"]).items()],
                 "allergens": sorted(dish_allergen_set(c, r["id"]))} for r in rows]


def margin_calc(dish_id: str, ingredient_cost_overrides: dict[str, float] | None = None,
                conn: sqlite3.Connection | None = None) -> dict:
    """Food cost / margin per portion at ingredient reference cost (or per-unit cost overrides)."""
    with conn_ctx(conn) as c:
        d = _dish_row(c, dish_id)
        if d is None:
            return {"error": f"unknown dish {dish_id}"}
        cost = {r["id"]: float(r["ref_cost"]) for r in c.execute("SELECT id, ref_cost FROM ingredients")}
        cost.update(ingredient_cost_overrides or {})
        fc = sum(cost[i] * q for i, q in _recipe_lines(c, dish_id).items())
        price = float(d["price"])
        return {"dish_id": dish_id, "price": r2(price), "food_cost": r2(fc), "margin": r2(price - fc),
                "margin_pct": r2((price - fc) / price * 100, 1), "food_cost_pct": r2(fc / price * 100, 1)}


def allergen_check(dish_id: str, substitutions: dict[str, str] | None = None,
                   conn: sqlite3.Connection | None = None) -> dict:
    """Allergens after optional substitutions {from_ingredient: to_ingredient}.

    Returns {dish_id, allergens (resulting), original_allergens, changed:[{allergen, change}], ok}.
    ok = the substitution introduces no new allergen.
    """
    with conn_ctx(conn) as c:
        if _dish_row(c, dish_id) is None:
            return {"error": f"unknown dish {dish_id}"}
        orig = dish_allergen_set(c, dish_id)
        new = dish_allergen_set(c, dish_id, substitutions)
        changed = [{"allergen": a, "change": "added"} for a in sorted(new - orig)] + \
                  [{"allergen": a, "change": "removed"} for a in sorted(orig - new)]
        return {"dish_id": dish_id, "allergens": sorted(new), "original_allergens": sorted(orig),
                "changed": changed, "ok": not (new - orig)}


def dishes_using(ingredient_ids: "str | list[str]", conn: sqlite3.Connection | None = None) -> list[str]:
    """Dish ids whose recipe uses any of the ingredients."""
    ids = [ingredient_ids] if isinstance(ingredient_ids, str) else list(ingredient_ids)
    if not ids:
        return []
    with conn_ctx(conn) as c:
        q = ",".join("?" * len(ids))
        return [r[0] for r in c.execute(f"SELECT DISTINCT dish_id FROM recipe_lines WHERE ingredient_id IN ({q}) "
                                        "ORDER BY dish_id", ids)]


def feasible_portions(dish_id: str, as_of: str | None = None, conn: sqlite3.Connection | None = None) -> int:
    """How many portions the usable on-hand stock supports (limited by the scarcest ingredient)."""
    with conn_ctx(conn) as c:
        lines = _recipe_lines(c, dish_id)
        if not lines:
            return 0
        return int(max(0, min(math.floor(on_hand(i, as_of, c) / q + 1e-9) for i, q in lines.items())))


def menu_status(date: str | None = None, conn: sqlite3.Connection | None = None) -> dict:
    """The day's menu from menu_state (absence = active): {date, specials, eighty_sixed, active}."""
    d = iso(date)
    with conn_ctx(conn) as c:
        st = {r["dish_id"]: (r["status"], r["note"]) for r in
              c.execute("SELECT dish_id, status, note FROM menu_state WHERE date=?", (d,))}
        ids = [r[0] for r in c.execute("SELECT id FROM dishes WHERE active=1 ORDER BY id")]
    out: dict = {"date": d, "specials": [], "eighty_sixed": [], "active": []}
    for i in ids:
        s, note = st.get(i, ("active", None))
        if s == "special":
            out["specials"].append({"dish_id": i, "note": note})
        elif s == "86":
            out["eighty_sixed"].append({"dish_id": i, "note": note})
        else:
            out["active"].append(i)
    return out
