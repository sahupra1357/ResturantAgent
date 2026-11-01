"""Inventory maths: lots, FEFO, expected usage from the forecast mix x BOM, waste risk, variance.

Lots are usable through their `expiry_date` (inclusive); they are expired from the next day.
No LLM imports here.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import date, timedelta

from restaurant_ops.core.forecasting import forecast_dish_mix
from restaurant_ops.core.util import conn_ctx, daterange, iso, r2, rq, to_date
from restaurant_ops.db.connection import query_df

EPS = 1e-6
VAR_SHRINKAGE_PCT = 4.0  # mean variance worse than -4% (persistent) -> shrinkage
VAR_OVERPORTION_PCT = 2.0  # between -2% and -4% (persistent) -> over_portioning
VAR_PERSISTENCE = 0.7  # share of count days that must be negative


def _bom(conn: sqlite3.Connection) -> dict[str, dict[str, float]]:
    """dish_id -> {ingredient_id: qty_per_portion}."""
    out: dict[str, dict[str, float]] = defaultdict(dict)
    for dish_id, ing, q in conn.execute("SELECT dish_id, ingredient_id, qty_per_portion FROM recipe_lines"):
        out[dish_id][ing] = float(q)
    return out


# =========================================================================== lots


def stock_by_lot(ingredient_id: str | None = None, as_of: str | None = None,
                 conn: sqlite3.Connection | None = None, include_expired: bool = False) -> list[dict]:
    """Active lots (FEFO order) with days_left = expiry - as_of. Expired lots are hidden unless asked for."""
    a = iso(as_of)
    sql = ("SELECT lot_id, ingredient_id, qty, received_date, expiry_date, unit_cost FROM lots "
           "WHERE status='active' AND qty>? AND received_date<=?")
    params: list = [EPS, a]
    if not include_expired:
        sql += " AND expiry_date>=?"
        params.append(a)
    if ingredient_id:
        sql += " AND ingredient_id=?"
        params.append(ingredient_id)
    sql += " ORDER BY expiry_date, received_date, lot_id"
    with conn_ctx(conn) as c:
        rows = c.execute(sql, params).fetchall()
    d0 = to_date(a)
    return [{"lot_id": r["lot_id"], "ingredient_id": r["ingredient_id"], "qty": rq(r["qty"]),
             "expiry_date": r["expiry_date"], "days_left": (to_date(r["expiry_date"]) - d0).days,
             "unit_cost": r2(r["unit_cost"], 3)} for r in rows]


def on_hand(ingredient_id: str, as_of: str | None = None, conn: sqlite3.Connection | None = None) -> float:
    """Usable (active, unexpired, received) quantity of an ingredient."""
    a = iso(as_of)
    with conn_ctx(conn) as c:
        r = c.execute("SELECT COALESCE(SUM(qty),0) FROM lots WHERE ingredient_id=? AND status='active' "
                      "AND qty>? AND expiry_date>=? AND received_date<=?", (ingredient_id, EPS, a, a)).fetchone()
    return rq(r[0])


def fefo_pick(ingredient_id: str, qty: float, as_of: str | None = None,
              conn: sqlite3.Connection | None = None) -> list[dict]:
    """Lots to draw `qty` from, earliest expiry first: [{lot_id, qty}]. Short stock -> fewer than asked."""
    out, need = [], float(qty)
    for lot in stock_by_lot(ingredient_id, as_of, conn):
        if need <= EPS:
            break
        take = min(lot["qty"], need)
        out.append({"lot_id": lot["lot_id"], "qty": rq(take)})
        need -= take
    return out


def consume_fefo(conn: sqlite3.Connection, ingredient_id: str, qty: float, as_of: str | None = None,
                 commit: bool = False) -> dict:
    """Deplete `qty` of an ingredient FEFO (updates lots; marks emptied lots 'depleted').

    Returns {picked: [{lot_id, qty, unit_cost}], consumed, shortfall, cost}. The caller commits unless commit=True.
    """
    need, picked, cost = float(qty), [], 0.0
    rows = conn.execute("SELECT lot_id, qty, unit_cost FROM lots WHERE ingredient_id=? AND status='active' AND qty>? "
                        "AND expiry_date>=? AND received_date<=? ORDER BY expiry_date, received_date, lot_id",
                        (ingredient_id, EPS, iso(as_of), iso(as_of))).fetchall()
    for r in rows:
        if need <= EPS:
            break
        take = min(float(r["qty"]), need)
        left = float(r["qty"]) - take
        conn.execute("UPDATE lots SET qty=?, status=? WHERE lot_id=?",
                     (max(left, 0.0), "depleted" if left <= EPS else "active", r["lot_id"]))
        picked.append({"lot_id": r["lot_id"], "qty": rq(take), "unit_cost": r2(r["unit_cost"], 3)})
        cost += take * float(r["unit_cost"])
        need -= take
    if commit:
        conn.commit()
    return {"picked": picked, "consumed": rq(float(qty) - max(need, 0.0)), "shortfall": rq(max(need, 0.0)),
            "cost": r2(cost)}


# =========================================================================== usage & waste risk


def expected_usage(start_date: str, days: int = 3, quantile: str = "p50", dish_overrides: dict | None = None,
                   conn: sqlite3.Connection | None = None, adjust: bool = True,
                   apply_menu_state: bool = True, as_of: str | None = None) -> list[dict]:
    """Ingredient usage = forecast dish mix (at `quantile`) x BOM, per ingredient per date.

    dish_overrides: {dish_id: factor} multiplies that dish's forecast portions (0 = remove the dish,
    1.5 = +50%). Dishes 86'd in menu_state for a date are removed when apply_menu_state=True.
    Returns [{ingredient_id, date, qty}].
    """
    if quantile not in ("p10", "p50", "p90"):
        raise ValueError("quantile must be p10, p50 or p90")
    dates = daterange(start_date, days)
    with conn_ctx(conn) as c:
        mix = forecast_dish_mix(iso(start_date), days, conn=c, adjust=adjust, as_of=as_of)
        bom = _bom(c)
        eighty = set()
        if apply_menu_state:
            eighty = {(r[0], r[1]) for r in c.execute(
                "SELECT date, dish_id FROM menu_state WHERE status='86' AND date BETWEEN ? AND ?",
                (dates[0].isoformat(), dates[-1].isoformat()))}
        agg: dict[tuple[str, str], float] = defaultdict(float)
        for r in mix:
            if (r["date"], r["dish_id"]) in eighty:
                continue
            portions = r[quantile] * float((dish_overrides or {}).get(r["dish_id"], 1.0))
            for ing, q in bom.get(r["dish_id"], {}).items():
                agg[(ing, r["date"])] += portions * q
    return [{"ingredient_id": ing, "date": d, "qty": rq(v)} for (ing, d), v in sorted(agg.items(), key=lambda kv: (kv[0][1], kv[0][0]))]


def usage_by_ingredient(usage: list[dict]) -> dict[str, dict[str, float]]:
    """Reshape expected_usage output to {ingredient_id: {date: qty}}."""
    out: dict[str, dict[str, float]] = defaultdict(dict)
    for u in usage:
        out[u["ingredient_id"]][u["date"]] = u["qty"]
    return out


def waste_risk(as_of: str | None = None, horizon_days: int = 3, conn: sqlite3.Connection | None = None,
               quantile: str = "p50", dish_overrides: dict | None = None) -> list[dict]:
    """Lots that will expire (within `horizon_days`) before forecast usage consumes them.

    FEFO-simulates daily usage over as_of .. as_of+horizon-1 against active lots; whatever is left of a lot
    at the end of its expiry day is at risk. Already-expired active lots are included in full.
    Returns [{lot_id, ingredient_id, qty_at_risk, value_at_risk, expiry_date, days_left}] sorted by value.
    """
    a = to_date(as_of)
    last = a + timedelta(days=horizon_days - 1)
    with conn_ctx(conn) as c:
        lots = c.execute("SELECT lot_id, ingredient_id, qty, expiry_date, unit_cost, received_date FROM lots "
                         "WHERE status='active' AND qty>? AND received_date<=? ORDER BY expiry_date, received_date, lot_id",
                         (EPS, a.isoformat())).fetchall()
        risky = {r["ingredient_id"] for r in lots if to_date(r["expiry_date"]) <= last}
        if not risky:
            return []
        usage = usage_by_ingredient(expected_usage(a.isoformat(), horizon_days, quantile, dish_overrides, conn=c))
    by_ing: dict[str, list] = defaultdict(list)
    for r in lots:
        if r["ingredient_id"] in risky:
            by_ing[r["ingredient_id"]].append({"lot_id": r["lot_id"], "qty": float(r["qty"]), "unit_cost": float(r["unit_cost"]),
                                               "exp": to_date(r["expiry_date"])})
    out = []

    def flag(ing: str, lot: dict) -> None:
        if lot["qty"] > 1e-4:
            out.append({"lot_id": lot["lot_id"], "ingredient_id": ing, "qty_at_risk": rq(lot["qty"]),
                        "value_at_risk": r2(lot["qty"] * lot["unit_cost"]), "expiry_date": lot["exp"].isoformat(),
                        "days_left": (lot["exp"] - a).days})
            lot["qty"] = 0.0

    for ing, ls in by_ing.items():
        for lot in ls:
            if lot["exp"] < a:
                flag(ing, lot)
        for t in range(horizon_days):
            day = a + timedelta(days=t)
            need = usage.get(ing, {}).get(day.isoformat(), 0.0)
            for lot in ls:
                if need <= 1e-9:
                    break
                if lot["exp"] >= day and lot["qty"] > 0:
                    take = min(lot["qty"], need)
                    lot["qty"] -= take
                    need -= take
            for lot in ls:
                if lot["exp"] <= day:
                    flag(ing, lot)
    out.sort(key=lambda x: -x["value_at_risk"])
    return out


# =========================================================================== variance


def variance_report(as_of: str | None = None, days: int = 7, conn: sqlite3.Connection | None = None,
                    include_ok: bool = False) -> list[dict]:
    """Counted vs system stock over the last `days` count days.

    variance_pct = sum(counted - system) / sum(system) x 100. Flags:
      'shrinkage'       mean <= -4% and >= 70% of count days negative (theft / unrecorded loss)
      'over_portioning' mean between -2% and -4% and persistently negative (portions run heavy)
      'surplus'         mean >= +4% (receiving / counting error)
    Only flagged ingredients are returned unless include_ok=True. Sorted worst first.
    """
    a = to_date(as_of)
    df = query_df("SELECT date, ingredient_id, counted_qty, system_qty FROM stock_counts WHERE date>=? AND date<=?",
                  ((a - timedelta(days=days)).isoformat(), a.isoformat()), conn)
    if df.empty:
        return []
    out = []
    for ing, g in df.groupby("ingredient_id"):
        exp, act = float(g["system_qty"].sum()), float(g["counted_qty"].sum())
        if exp <= EPS:
            continue
        pct = (act - exp) / exp * 100
        neg = float(((g["counted_qty"] - g["system_qty"]) < 0).mean())
        if pct <= -VAR_SHRINKAGE_PCT and neg >= VAR_PERSISTENCE:
            flag = "shrinkage"
        elif -VAR_SHRINKAGE_PCT < pct <= -VAR_OVERPORTION_PCT and neg >= VAR_PERSISTENCE:
            flag = "over_portioning"
        elif pct >= VAR_SHRINKAGE_PCT:
            flag = "surplus"
        else:
            flag = "ok"
        if flag != "ok" or include_ok:
            out.append({"ingredient_id": ing, "expected": rq(exp), "actual": rq(act), "variance_pct": r2(pct),
                        "negative_day_share": r2(neg), "n_counts": int(len(g)), "flag": flag})
    out.sort(key=lambda x: x["variance_pct"])
    return out
