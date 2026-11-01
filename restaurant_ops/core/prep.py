"""Prep planning: quantities per station from the forecast mix, and station load vs rostered capacity.

No LLM imports here.
"""
from __future__ import annotations

import math
import sqlite3

from restaurant_ops.core.forecasting import forecast_dish_mix
from restaurant_ops.core.util import conn_ctx, iso, r2
from restaurant_ops.data import catalog as cat


def prep_quantities(date: str, meal_period: str, forecast_quantile: str = "p50",
                    conn: sqlite3.Connection | None = None, adjust: bool = True) -> list[dict]:
    """Portions to prep per dish for one meal period: forecast mix at `forecast_quantile`.

    Dishes 86'd in menu_state for the date get 0 portions (omitted). Specials stay on the list (flagged
    `special`) because their portions come out of the same forecast. Portions are rounded up.
    Returns [{station, dish_id, portions, prep_minutes}] where prep_minutes is the total for the line.
    """
    d = iso(date)
    with conn_ctx(conn) as c:
        mix = [r for r in forecast_dish_mix(d, 1, conn=c, adjust=adjust) if r["meal_period"] == meal_period]
        state = {r["dish_id"]: r["status"] for r in
                 c.execute("SELECT dish_id, status FROM menu_state WHERE date=?", (d,))}
        info = {r["id"]: (r["station"], float(r["prep_minutes"])) for r in c.execute("SELECT id, station, prep_minutes FROM dishes")}
    out = []
    for r in mix:
        if state.get(r["dish_id"]) == "86":
            continue
        portions = int(math.ceil(r[forecast_quantile] - 1e-9))
        if portions <= 0:
            continue
        station, pm = info[r["dish_id"]]
        row = {"station": station, "dish_id": r["dish_id"], "portions": portions, "prep_minutes": r2(portions * pm, 1)}
        if state.get(r["dish_id"]) == "special":
            row["special"] = True
        out.append(row)
    out.sort(key=lambda x: (x["station"], x["dish_id"]))
    return out


def station_staff(c: sqlite3.Connection, date: str, meal_period: str) -> dict[str, int]:
    """Rostered staff per station (staff_shifts), falling back to default staffing."""
    got = {r["station"]: int(r["staff"]) for r in
           c.execute("SELECT station, staff FROM staff_shifts WHERE date=? AND meal_period=?", (iso(date), meal_period))}
    return {s: got.get(s, cat.DEFAULT_STAFFING[s][meal_period]) for s in cat.STATIONS}


def station_loads(c: sqlite3.Connection, date: str, meal_period: str, items: list[dict]) -> list[dict]:
    """Capacity vs load for explicit prep items [{station, dish_id, portions}] (prep minutes from the dishes table)."""
    pm = {r["id"]: float(r["prep_minutes"]) for r in c.execute("SELECT id, prep_minutes FROM dishes")}
    load: dict[str, float] = {}
    for it in items:
        load[it["station"]] = load.get(it["station"], 0.0) + float(it["portions"]) * pm.get(it["dish_id"], 0.0)
    out = []
    for s, staff in station_staff(c, date, meal_period).items():
        cap = cat.station_capacity_minutes(s, meal_period, staff)
        out.append({"station": s, "staff": staff, "capacity_minutes": r2(cap, 1), "load_minutes": r2(load.get(s, 0.0), 1),
                    "utilisation": r2(load.get(s, 0.0) / cap, 2) if cap > 0 else (0.0 if not load.get(s) else 9.99)})
    return out


def station_capacity(date: str, meal_period: str, conn: sqlite3.Connection | None = None,
                     forecast_quantile: str = "p50", items: list[dict] | None = None) -> list[dict]:
    """Per station: [{station, staff, capacity_minutes, load_minutes, utilisation}].

    Capacity = rostered staff x period minutes x efficiency (catalog); load = prep minutes of the forecast
    prep list (or of explicit `items`). utilisation > 1.0 means the station cannot finish prep.
    """
    with conn_ctx(conn) as c:
        its = items if items is not None else prep_quantities(date, meal_period, forecast_quantile, conn=c)
        return station_loads(c, date, meal_period, its)
