"""Deterministic constraint checker: every agent action passes `check_action` before it executes.

An action is {"action_type", "payload", "rationale"?, "agent"?, "est_cost"?}. Payload shapes
(dates are ISO strings, quantities in ingredient units):

  place_po             {supplier_id, delivery_date, lines:[{ingredient_id, qty, unit_price}], total?}
  reschedule_po        {po_id, new_delivery_date}
  substitute_supplier  {supplier_id (the NEW supplier), delivery_date, lines:[{ingredient_id, qty, unit_price}],
                        from_supplier_id, po_id? (open PO being replaced), total?}
  add_special          {dish_id, portions, price?, allergens_declared:[str], uses_lots?:[{lot_id, qty}],
                        date?, meal_period?}
  86_dish              {dish_id, date, meal_period?}
  substitute_ingredient {dish_id, from_ingredient, to_ingredient, allergens_declared:[str], portions?}
  prep_list            {date, meal_period, items:[{station, dish_id, portions}]}
  discard_lot          {lot_id, reason ('expired'|'spoilage'|'discarded'), qty?}
  adjust_forecast      {date, meal_period ('lunch'|'dinner'|'late'|'all'), factor (0.5..1.6), reason}
  flag_variance        {ingredient_id, variance_pct?, flag? ('shrinkage'|'over_portioning'|...)}
  discard_lots         {lots:[{lot_id, reason, qty?}]}  (several discard_lot checks in one decision)
  throttle_online      {mode ('open'|'quick_only'|'paused'), reason}
  allergen_swap        {order_id, item_id, from_dish, to_dish (null = drop the item), guest_allergens:[str]}
  comp                 {order_id, amount, order_total, reason}
  add_staff            {station, add, hours, hourly_rate, surge_spent}   (labour budget: SURGE_LABOR_BUDGET)
  release_staff        {station, remove}
  prep_batch           {dish_id, portions}
  donate_lots          {lots:[{lot_id}], partner}
  discount             {check_id, pct?|amount?, subtotal, reason}       (<= DISCOUNT_MAX_PCT)
  refund               {check_id, amount, paid, reason}
  price_change         {dish_id, new_price, reason}                    (<= +/-15%, effective next service)

Rules (code -> severity): positive_quantities, supplier_carries_item, lead_time, min_order_value,
budget_daily, budget_weekly, shelf_life, fefo, stock_available, allergen_undeclared, station_capacity
are 'block'; softer advisories (pack_size, price_mismatch, usable_lot, low_margin, ...) are 'warn'.
Pure and deterministic: reads the DB, never writes. No LLM imports here.
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import asdict, dataclass
from datetime import date, timedelta

from restaurant_ops.config import Settings, settings as default_settings
from restaurant_ops.core import inventory, menu, prep
from restaurant_ops.core.util import to_date

ACTION_TYPES = ("adjust_forecast", "place_po", "reschedule_po", "substitute_supplier", "add_special",
                "86_dish", "substitute_ingredient", "prep_list", "discard_lot", "flag_variance",
                "discard_lots", "throttle_online", "allergen_swap", "comp", "add_staff", "release_staff",
                "prep_batch", "donate_lots", "discount", "refund", "price_change")
STAFF_STATIONS = ("grill", "saute", "cold", "pastry")
MAX_PRICE_CHANGE = 0.15
ONLINE_MODES = ("open", "quick_only", "paused")
MAX_COMP_SHARE = 0.5  # a comp may not exceed half the order total
FORECAST_FACTOR_RANGE = (0.5, 1.6)
DAILY_CEILING_FACTOR = 2.0  # daily PO spend ceiling = DAILY_BUDGET x this (a morning order covers ~2 days)
WEEKLY_DAYS = 7  # weekly ceiling = DAILY_BUDGET x 7
PRICE_TOLERANCE = 1.10  # unit_price above list x this -> warn
SHELF_TOLERANCE = 1.25  # buying more than rate x shelf_life x this (+1 pack) -> block
DISCARD_REASONS = ("expired", "spoilage", "discarded")
MAX_FOOD_COST_PCT = 35.0


@dataclass
class Violation:
    """One broken rule. severity 'block' stops the action; 'warn' is advisory."""

    code: str
    message: str
    severity: str = "block"  # 'block' | 'warn'

    def to_dict(self) -> dict:
        return asdict(self)


def _norm(action) -> dict:
    if hasattr(action, "model_dump"):
        return action.model_dump()
    return dict(action)


def _num(x, default: float | None = None) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def total_of(action: dict) -> float:
    """Order value of a PO-like action (payload total, else sum of lines, else est_cost)."""
    p = action.get("payload") or {}
    if p.get("total") is not None:
        return float(p["total"])
    if p.get("lines"):
        return float(sum((_num(l.get("qty"), 0) or 0) * (_num(l.get("unit_price"), 0) or 0) for l in p["lines"]))
    return float(action.get("est_cost") or 0.0)


def needs_approval(action, settings: Settings = default_settings) -> bool:
    """Chef approval required: PO / supplier swap above APPROVAL_PO_THRESHOLD, any add_special,
    86_dish or substitute_ingredient."""
    a = _norm(action)
    t = a.get("action_type")
    if t in ("add_special", "86_dish", "substitute_ingredient"):
        return True
    if t in ("place_po", "substitute_supplier"):
        return total_of(a) > settings.APPROVAL_PO_THRESHOLD
    return False


# =========================================================================== helpers


def _daily_rate(conn: sqlite3.Connection, ingredient_id: str, as_of: date, days: int = 28) -> float:
    """Average daily usage of an ingredient over the last `days` days of POS history."""
    r = conn.execute(
        "SELECT SUM(p.qty*l.qty_per_portion), COUNT(DISTINCT p.date) FROM pos_sales p "
        "JOIN recipe_lines l ON l.dish_id=p.dish_id WHERE l.ingredient_id=? AND p.date>=? AND p.date<?",
        (ingredient_id, (as_of - timedelta(days=days)).isoformat(), as_of.isoformat())).fetchone()
    return float(r[0]) / max(int(r[1]), 1) if r and r[0] else 0.0


def _inbound(conn: sqlite3.Connection, ingredient_id: str, upto: date) -> float:
    r = conn.execute("SELECT COALESCE(SUM(l.qty - COALESCE(l.qty_delivered,0)),0) FROM po_lines l "
                     "JOIN purchase_orders p ON p.po_id=l.po_id WHERE p.status='sent' AND l.ingredient_id=? "
                     "AND p.expected_delivery<=?", (ingredient_id, upto.isoformat())).fetchone()
    return float(r[0] or 0.0)


def shelf_life_limit(conn: sqlite3.Connection, ingredient_id: str, as_of: date, delivery: date) -> dict | None:
    """The shelf-life rule used by `place_po` checks: on hand + inbound + ordered may not exceed `usable` + one pack.
    Returns {rate, shelf_days, usable, have}, or None when the rule does not apply (unknown item or no usage history).
    The optimizer caps its order lines with the same numbers so its plans pass this check."""
    row = conn.execute("SELECT shelf_life_days FROM ingredients WHERE id=?", (ingredient_id,)).fetchone()
    if row is None:
        return None
    rate = _daily_rate(conn, ingredient_id, as_of)
    if rate <= 0:
        return None
    have = inventory.on_hand(ingredient_id, as_of.isoformat(), conn) + _inbound(conn, ingredient_id, delivery)
    return {"rate": rate, "shelf_days": int(row["shelf_life_days"]),
            "usable": rate * int(row["shelf_life_days"]) * SHELF_TOLERANCE, "have": have}


def _parse_date(v, field: str, out: list[Violation]) -> date | None:
    try:
        return to_date(v) if v else None
    except ValueError:
        out.append(Violation("invalid_date", f"{field} '{v}' is not an ISO date"))
        return None


def _missing(p: dict, keys: list[str], out: list[Violation]) -> bool:
    miss = [k for k in keys if p.get(k) in (None, "", [])]
    if miss:
        out.append(Violation("missing_field", f"payload is missing: {', '.join(miss)}"))
    return bool(miss)


# =========================================================================== PO rules


def _check_po(p: dict, conn: sqlite3.Connection, as_of: date, out: list[Violation], supplier_key: str,
              date_key: str, replacing_po: str | None = None) -> None:
    if _missing(p, [supplier_key, date_key, "lines"], out):
        return
    sid = p[supplier_key]
    sup = conn.execute("SELECT * FROM suppliers WHERE id=?", (sid,)).fetchone()
    if sup is None:
        out.append(Violation("unknown_supplier", f"supplier {sid} does not exist"))
        return
    delivery = _parse_date(p[date_key], date_key, out)
    if delivery is not None:
        earliest = as_of + timedelta(days=int(sup["lead_time_days"]))
        if delivery < earliest:
            out.append(Violation("lead_time", f"{sid} needs {sup['lead_time_days']}d lead time: earliest delivery "
                                 f"{earliest.isoformat()}, requested {delivery.isoformat()}"))
    total = 0.0
    for l in p["lines"]:
        ing, qty, price = l.get("ingredient_id"), _num(l.get("qty")), _num(l.get("unit_price"))
        if qty is None or qty <= 0 or price is None or price <= 0:
            out.append(Violation("positive_quantities", f"line {ing}: qty and unit_price must be > 0"))
            continue
        total += qty * price
        item = conn.execute("SELECT price, pack_size FROM supplier_items WHERE supplier_id=? AND ingredient_id=?",
                            (sid, ing)).fetchone()
        if item is None:
            out.append(Violation("supplier_carries_item", f"{sid} does not carry {ing}"))
            continue
        pack = float(item["pack_size"])
        if abs(qty / pack - round(qty / pack)) > 1e-6:
            out.append(Violation("pack_size", f"{ing}: qty {qty} is not a multiple of pack size {pack}", "warn"))
        if price > float(item["price"]) * PRICE_TOLERANCE:
            out.append(Violation("price_mismatch", f"{ing}: unit_price {price} is above list {item['price']}", "warn"))
        lim = shelf_life_limit(conn, ing, as_of, delivery or as_of)
        if lim is not None and qty + lim["have"] > lim["usable"] + pack:
            out.append(Violation("shelf_life", f"{ing}: {qty + lim['have']:.1f} on hand + ordered exceeds what can be "
                                 f"used within its {lim['shelf_days']}d shelf life (~{lim['usable'] + pack:.1f} at "
                                 f"{lim['rate']:.2f}/day)"))
    if total + 1e-9 < float(sup["min_order_value"]):
        out.append(Violation("min_order_value", f"order value {total:.2f} is below {sid} minimum {sup['min_order_value']}"))
    # budget: committed POs created today / this week (excluding a PO being replaced)
    excl = replacing_po or ""
    today = conn.execute("SELECT COALESCE(SUM(total),0) FROM purchase_orders WHERE created_date=? AND po_id<>? "
                         "AND status IN ('draft','pending_approval','sent','delivered','partial')",
                         (as_of.isoformat(), excl)).fetchone()[0]
    week = conn.execute("SELECT COALESCE(SUM(total),0) FROM purchase_orders WHERE created_date>? AND created_date<=? "
                        "AND po_id<>? AND status IN ('draft','pending_approval','sent','delivered','partial')",
                        ((as_of - timedelta(days=WEEKLY_DAYS)).isoformat(), as_of.isoformat(), excl)).fetchone()[0]
    daily_cap = default_settings.DAILY_BUDGET * DAILY_CEILING_FACTOR
    if today + total > daily_cap + 1e-9:
        out.append(Violation("budget_daily", f"today's PO spend {today + total:.0f} would exceed the daily ceiling {daily_cap:.0f}"))
    weekly_cap = default_settings.DAILY_BUDGET * WEEKLY_DAYS
    if week + total > weekly_cap + 1e-9:
        out.append(Violation("budget_weekly", f"7-day PO spend {week + total:.0f} would exceed the weekly budget {weekly_cap:.0f}"))


def _check_place_po(a, p, conn, as_of, out):
    _check_po(p, conn, as_of, out, "supplier_id", "delivery_date")


def _check_substitute_supplier(a, p, conn, as_of, out):
    if p.get("from_supplier_id") and p.get("from_supplier_id") == p.get("supplier_id"):
        out.append(Violation("same_supplier", "substitute supplier must differ from from_supplier_id"))
    if p.get("po_id"):
        po = conn.execute("SELECT status FROM purchase_orders WHERE po_id=?", (p["po_id"],)).fetchone()
        if po is None:
            out.append(Violation("po_not_found", f"PO {p['po_id']} does not exist"))
        elif po["status"] not in ("sent", "pending_approval", "draft", "partial"):
            out.append(Violation("po_not_open", f"PO {p['po_id']} is {po['status']}"))
    _check_po(p, conn, as_of, out, "supplier_id", "delivery_date", replacing_po=p.get("po_id"))


def _check_reschedule_po(a, p, conn, as_of, out):
    if _missing(p, ["po_id", "new_delivery_date"], out):
        return
    po = conn.execute("SELECT p.status, p.created_date, s.lead_time_days FROM purchase_orders p "
                      "JOIN suppliers s ON s.id=p.supplier_id WHERE p.po_id=?", (p["po_id"],)).fetchone()
    if po is None:
        out.append(Violation("po_not_found", f"PO {p['po_id']} does not exist"))
        return
    if po["status"] not in ("sent", "pending_approval", "draft"):
        out.append(Violation("po_not_open", f"PO {p['po_id']} is {po['status']} and cannot be rescheduled"))
    new = _parse_date(p["new_delivery_date"], "new_delivery_date", out)
    if new is not None:
        earliest = max(as_of, to_date(po["created_date"]) + timedelta(days=int(po["lead_time_days"])))
        if new < earliest:
            out.append(Violation("lead_time", f"earliest possible delivery is {earliest.isoformat()}, requested {new.isoformat()}"))


# =========================================================================== menu rules


def _dish(conn, dish_id):
    return conn.execute("SELECT * FROM dishes WHERE id=?", (dish_id,)).fetchone()


def _check_fefo(conn, as_of, lots_used, ingredient_of, out):
    """lots_used: [{lot_id, qty}]. Using a lot while an earlier-expiring active lot of the same item is not used."""
    used_ids = {l["lot_id"] for l in lots_used}
    for l in lots_used:
        lot = conn.execute("SELECT * FROM lots WHERE lot_id=?", (l.get("lot_id"),)).fetchone()
        if lot is None or lot["status"] != "active":
            out.append(Violation("stock_available", f"lot {l.get('lot_id')} does not exist or is not active"))
            continue
        if to_date(lot["expiry_date"]) < as_of:
            out.append(Violation("lot_expired", f"lot {lot['lot_id']} expired on {lot['expiry_date']}"))
        if ingredient_of is not None and lot["ingredient_id"] not in ingredient_of:
            out.append(Violation("lot_not_in_recipe", f"lot {lot['lot_id']} ({lot['ingredient_id']}) is not in the recipe"))
        if (_num(l.get("qty"), 0) or 0) > float(lot["qty"]) + 1e-9:
            out.append(Violation("stock_available", f"lot {lot['lot_id']} holds {lot['qty']}, requested {l.get('qty')}"))
        older = conn.execute("SELECT lot_id FROM lots WHERE ingredient_id=? AND status='active' AND qty>0 AND "
                             "expiry_date>=? AND expiry_date<? ", (lot["ingredient_id"], as_of.isoformat(),
                                                                   lot["expiry_date"])).fetchall()
        skipped = [r["lot_id"] for r in older if r["lot_id"] not in used_ids]
        if skipped:
            out.append(Violation("fefo", f"lot {lot['lot_id']} is used before earlier-expiring {', '.join(skipped)} (FEFO)"))


def _check_add_special(a, p, conn, as_of, out):
    if _missing(p, ["dish_id", "portions"], out):
        return
    d = _dish(conn, p["dish_id"])
    if d is None:
        out.append(Violation("unknown_dish", f"dish {p['dish_id']} does not exist"))
        return
    portions = _num(p["portions"])
    if portions is None or portions <= 0:
        out.append(Violation("positive_quantities", "portions must be > 0"))
        return
    if p.get("allergens_declared") is None:
        out.append(Violation("allergen_undeclared", "allergens_declared is required for a special"))
        declared = set()
    else:
        declared = {str(x).lower() for x in p["allergens_declared"]}
    actual = menu.dish_allergen_set(conn, p["dish_id"])
    missing = sorted(actual - declared)
    if missing:
        out.append(Violation("allergen_undeclared", f"special contains undeclared allergens: {', '.join(missing)}"))
    months = [int(m) for m in (d["seasonal_months"] or "").split(",") if m]
    if months and as_of.month not in months:
        out.append(Violation("out_of_season", f"{d['name']} is out of season (months {months})"))
    feasible = menu.feasible_portions(p["dish_id"], as_of.isoformat(), conn)
    if feasible < portions:
        out.append(Violation("stock_available", f"stock supports only {feasible} portions of {d['name']}, requested {int(portions)}"))
    if p.get("uses_lots"):
        recipe = set(menu._recipe_lines(conn, p["dish_id"]))
        _check_fefo(conn, as_of, p["uses_lots"], recipe, out)
    price = _num(p.get("price"), float(d["price"]))
    fc = menu.margin_calc(p["dish_id"], conn=conn)["food_cost"]
    if price is None or price <= 0:
        out.append(Violation("positive_quantities", "price must be > 0"))
    elif price < fc:
        out.append(Violation("negative_margin", f"price {price:.2f} is below food cost {fc:.2f}"))
    elif fc / price * 100 > MAX_FOOD_COST_PCT:
        out.append(Violation("low_margin", f"food cost is {fc / price * 100:.0f}% of price", "warn"))
    day = _parse_date(p.get("date"), "date", out) or as_of
    period = p.get("meal_period") or "dinner"
    if period in default_settings.MEAL_PERIODS:
        for r in prep.station_loads(conn, day.isoformat(), period, [{"station": d["station"], "dish_id": d["id"], "portions": portions}]):
            if r["station"] == d["station"] and r["utilisation"] > 1.0:
                out.append(Violation("station_capacity", f"{d['station']} cannot make {int(portions)} portions in {period}"))
    if conn.execute("SELECT 1 FROM menu_state WHERE date=? AND dish_id=? AND status='86'", (day.isoformat(), d["id"])).fetchone():
        out.append(Violation("dish_86d", f"{d['name']} is 86'd on {day.isoformat()}", "warn"))


def _check_86(a, p, conn, as_of, out):
    if _missing(p, ["dish_id", "date"], out):
        return
    d = _dish(conn, p["dish_id"])
    if d is None:
        out.append(Violation("unknown_dish", f"dish {p['dish_id']} does not exist"))
        return
    day = _parse_date(p["date"], "date", out)
    if day is not None and day < as_of:
        out.append(Violation("past_date", f"cannot 86 a dish on past date {day.isoformat()}"))
    if p.get("meal_period") and p["meal_period"] not in default_settings.MEAL_PERIODS:
        out.append(Violation("invalid_meal_period", f"unknown meal period {p['meal_period']}"))
    feasible = menu.feasible_portions(p["dish_id"], as_of.isoformat(), conn)
    if feasible >= 25:
        out.append(Violation("unnecessary_86", f"stock still covers {feasible} portions of {d['name']}", "warn"))
    if day and conn.execute("SELECT 1 FROM menu_state WHERE date=? AND dish_id=? AND status='86'", (day.isoformat(), d["id"])).fetchone():
        out.append(Violation("already_86d", f"{d['name']} is already 86'd", "warn"))


def _check_substitute_ingredient(a, p, conn, as_of, out):
    if _missing(p, ["dish_id", "from_ingredient", "to_ingredient"], out):
        return
    d = _dish(conn, p["dish_id"])
    if d is None:
        out.append(Violation("unknown_dish", f"dish {p['dish_id']} does not exist"))
        return
    lines = menu._recipe_lines(conn, p["dish_id"])
    if p["from_ingredient"] not in lines:
        out.append(Violation("not_in_recipe", f"{p['from_ingredient']} is not in {d['name']}"))
        return
    to = conn.execute("SELECT * FROM ingredients WHERE id=?", (p["to_ingredient"],)).fetchone()
    if to is None:
        out.append(Violation("unknown_ingredient", f"ingredient {p['to_ingredient']} does not exist"))
        return
    if p["to_ingredient"] == p["from_ingredient"]:
        out.append(Violation("same_ingredient", "substitute must differ from the original"))
    if p.get("allergens_declared") is None:
        out.append(Violation("allergen_undeclared", "allergens_declared is required for a substitution"))
        declared = set()
    else:
        declared = {str(x).lower() for x in p["allergens_declared"]}
    chk = menu.allergen_check(p["dish_id"], {p["from_ingredient"]: p["to_ingredient"]}, conn)
    added = sorted(c["allergen"] for c in chk["changed"] if c["change"] == "added")
    undeclared = [x for x in added if x not in declared]
    if undeclared:
        out.append(Violation("allergen_undeclared", f"substitution introduces undeclared allergens: {', '.join(undeclared)}"))
    frm = conn.execute("SELECT unit FROM ingredients WHERE id=?", (p["from_ingredient"],)).fetchone()
    if frm and frm["unit"] != to["unit"]:
        out.append(Violation("unit_mismatch", f"units differ ({frm['unit']} vs {to['unit']})", "warn"))
    need = lines[p["from_ingredient"]] * (_num(p.get("portions"), 10) or 10)
    have = inventory.on_hand(p["to_ingredient"], as_of.isoformat(), conn)
    if have + 1e-9 < need:
        out.append(Violation("stock_available", f"only {have:.2f} {to['unit']} of {to['name']} on hand, need {need:.2f}"))


# =========================================================================== prep / lots / forecast / variance


def _check_prep_list(a, p, conn, as_of, out):
    if _missing(p, ["date", "meal_period", "items"], out):
        return
    day = _parse_date(p["date"], "date", out)
    period = p["meal_period"]
    if period not in default_settings.MEAL_PERIODS:
        out.append(Violation("invalid_meal_period", f"unknown meal period {period}"))
        return
    if day is not None and day < as_of:
        out.append(Violation("past_date", f"prep list date {day.isoformat()} is in the past"))
    need: dict[str, float] = {}
    for it in p["items"]:
        d = _dish(conn, it.get("dish_id"))
        portions = _num(it.get("portions"))
        if d is None:
            out.append(Violation("unknown_dish", f"dish {it.get('dish_id')} does not exist"))
            continue
        if portions is None or portions <= 0:
            out.append(Violation("positive_quantities", f"{d['id']}: portions must be > 0"))
            continue
        if it.get("station") != d["station"]:
            out.append(Violation("station_mismatch", f"{d['id']} is made on {d['station']}, not {it.get('station')}"))
        if day and conn.execute("SELECT 1 FROM menu_state WHERE date=? AND dish_id=? AND status='86'", (day.isoformat(), d["id"])).fetchone():
            out.append(Violation("dish_86d", f"{d['id']} is 86'd on {day.isoformat()}", "warn"))
        for ing, q in menu._recipe_lines(conn, d["id"]).items():
            need[ing] = need.get(ing, 0.0) + q * portions
    if day is not None:
        for r in prep.station_loads(conn, day.isoformat(), period, [i for i in p["items"] if _dish(conn, i.get("dish_id")) is not None
                                                                     and (_num(i.get("portions"), 0) or 0) > 0]):
            if r["utilisation"] > 1.0 + 1e-9:
                out.append(Violation("station_capacity", f"{r['station']} load {r['load_minutes']:.0f} min exceeds capacity "
                                     f"{r['capacity_minutes']:.0f} min ({r['utilisation']:.0%})"))
            elif r["utilisation"] > 0.9:
                out.append(Violation("station_capacity", f"{r['station']} at {r['utilisation']:.0%} of capacity", "warn"))
        for ing, q in need.items():
            have = inventory.on_hand(ing, as_of.isoformat(), conn) + _inbound(conn, ing, day)
            if have + 1e-9 < q:
                sev = "block" if have < 0.85 * q else "warn"
                out.append(Violation("stock_available", f"{ing}: need {q:.2f}, only {have:.2f} on hand/inbound", sev))


def _check_discard_lot(a, p, conn, as_of, out):
    if _missing(p, ["lot_id"], out):
        return
    lot = conn.execute("SELECT * FROM lots WHERE lot_id=?", (p["lot_id"],)).fetchone()
    if lot is None or lot["status"] != "active":
        out.append(Violation("lot_not_found", f"lot {p['lot_id']} does not exist or is not active"))
        return
    reason = p.get("reason", "discarded")
    if reason not in DISCARD_REASONS:
        out.append(Violation("invalid_reason", f"reason must be one of {', '.join(DISCARD_REASONS)}"))
    days_left = (to_date(lot["expiry_date"]) - as_of).days
    if reason == "expired" and days_left >= 0:
        out.append(Violation("not_expired", f"lot {lot['lot_id']} is still usable until {lot['expiry_date']}"))
    if reason == "discarded" and days_left >= 2:
        out.append(Violation("usable_lot", f"lot {lot['lot_id']} still has {days_left} days left - consider a special", "warn"))
    q = _num(p.get("qty"))
    if p.get("qty") is not None and (q is None or q <= 0 or q > float(lot["qty"]) + 1e-9):
        out.append(Violation("positive_quantities", f"qty must be in (0, {lot['qty']}]"))


def _check_adjust_forecast(a, p, conn, as_of, out):
    if _missing(p, ["date", "meal_period", "factor"], out):
        return
    f = _num(p["factor"])
    lo, hi = FORECAST_FACTOR_RANGE
    if f is None or not (lo <= f <= hi):
        out.append(Violation("factor_out_of_range", f"factor must be within {lo}-{hi}, got {p['factor']}"))
    if p["meal_period"] not in (*default_settings.MEAL_PERIODS, "all"):
        out.append(Violation("invalid_meal_period", f"unknown meal period {p['meal_period']}"))
    if not str(p.get("reason", "")).strip():
        out.append(Violation("missing_reason", "adjust_forecast needs a written reason"))
    day = _parse_date(p["date"], "date", out)
    if day is not None:
        if day < as_of:
            out.append(Violation("past_date", "cannot adjust the forecast for a past date"))
        elif day > as_of + timedelta(days=14):
            out.append(Violation("far_horizon", "forecast adjustment is more than 14 days out", "warn"))


def _check_flag_variance(a, p, conn, as_of, out):
    if _missing(p, ["ingredient_id"], out):
        return
    if conn.execute("SELECT 1 FROM ingredients WHERE id=?", (p["ingredient_id"],)).fetchone() is None:
        out.append(Violation("unknown_ingredient", f"ingredient {p['ingredient_id']} does not exist"))
        return
    rows = inventory.variance_report(as_of.isoformat(), 7, conn, include_ok=True)
    row = next((r for r in rows if r["ingredient_id"] == p["ingredient_id"]), None)
    if row is None or row["flag"] == "ok":
        out.append(Violation("variance_not_supported", "stock counts do not show a persistent variance for this ingredient", "warn"))


def _check_discard_lots(a, p, conn, as_of, out):
    lots = p.get("lots")
    if not lots:
        out.append(Violation("missing_field", "payload is missing: lots"))
        return
    for lot in lots:
        _check_discard_lot(a, lot if isinstance(lot, dict) else {"lot_id": lot}, conn, as_of, out)


# =========================================================================== live service actions


def _check_throttle_online(a, p, conn, as_of, out):
    if _missing(p, ["mode"], out):
        return
    if p["mode"] not in ONLINE_MODES:
        out.append(Violation("invalid_mode", f"online mode must be one of {', '.join(ONLINE_MODES)}"))
    if p["mode"] != "open" and not str(p.get("reason", "")).strip():
        out.append(Violation("missing_reason", "throttling online orders needs a written reason"))


def _check_allergen_swap(a, p, conn, as_of, out):
    """The allergen guard: a replacement dish may never contain an allergen the guest declared."""
    if _missing(p, ["order_id", "item_id", "from_dish"], out):
        return
    guest = {str(x).lower() for x in (p.get("guest_allergens") or [])}
    to = p.get("to_dish")
    if not to:
        return  # dropping the item is always allowed
    d = _dish(conn, to)
    if d is None:
        out.append(Violation("unknown_dish", f"dish {to} does not exist"))
        return
    clash = sorted(menu.dish_allergen_set(conn, to) & guest)
    if clash:
        out.append(Violation("allergen_conflict", f"{d['name']} contains {', '.join(clash)}, which the guest cannot eat"))
    if menu.feasible_portions(to, as_of.isoformat(), conn) < 1:
        out.append(Violation("stock_available", f"no stock for {d['name']}"))


def _check_comp(a, p, conn, as_of, out):
    if _missing(p, ["order_id", "amount"], out):
        return
    amt, tot = _num(p["amount"]), _num(p.get("order_total"))
    if amt is None or amt <= 0:
        out.append(Violation("positive_quantities", "comp amount must be > 0"))
    elif tot is not None and amt > tot * MAX_COMP_SHARE + 1e-9:
        out.append(Violation("comp_too_large", f"comp {amt:.2f} exceeds {MAX_COMP_SHARE:.0%} of the order total {tot:.2f}"))


def _check_add_staff(a, p, conn, as_of, out):
    if _missing(p, ["station", "add", "hours", "hourly_rate"], out):
        return
    if p["station"] not in STAFF_STATIONS:
        out.append(Violation("unknown_station", f"unknown station {p['station']}"))
    add, hours, rate = _num(p["add"], 0) or 0, _num(p["hours"], 0) or 0, _num(p["hourly_rate"], 0) or 0
    if add <= 0 or hours <= 0 or rate <= 0:
        out.append(Violation("positive_quantities", "add, hours and hourly_rate must be > 0"))
        return
    cost = add * hours * rate
    spent = _num(p.get("surge_spent"), 0) or 0
    budget = default_settings.SURGE_LABOR_BUDGET
    if spent + cost > budget + 1e-9:
        out.append(Violation("labor_budget", f"call-in costs {cost:.0f}; with {spent:.0f} already spent tonight that exceeds "
                                             f"the surge labour budget {budget:.0f}"))
    if add > default_settings.MAX_EXTRA_COOKS:
        out.append(Violation("max_extra_cooks", f"at most {default_settings.MAX_EXTRA_COOKS} extra cook(s) per station"))


def _check_release_staff(a, p, conn, as_of, out):
    if _missing(p, ["station", "remove"], out):
        return
    if (_num(p["remove"], 0) or 0) <= 0:
        out.append(Violation("positive_quantities", "remove must be > 0"))


def _check_prep_batch(a, p, conn, as_of, out):
    if _missing(p, ["dish_id", "portions"], out):
        return
    d = _dish(conn, p["dish_id"])
    if d is None:
        out.append(Violation("unknown_dish", f"dish {p['dish_id']} does not exist"))
        return
    n = _num(p["portions"], 0) or 0
    if n <= 0:
        out.append(Violation("positive_quantities", "portions must be > 0"))
        return
    feasible = menu.feasible_portions(p["dish_id"], as_of.isoformat(), conn)
    if feasible < n:
        out.append(Violation("stock_available", f"stock supports only {feasible} portions of {d['name']}, prep asks {int(n)}"))


def _check_donate_lots(a, p, conn, as_of, out):
    lots = p.get("lots")
    if not lots:
        out.append(Violation("missing_field", "payload is missing: lots"))
        return
    for lot in lots:
        lid = lot["lot_id"] if isinstance(lot, dict) else lot
        r = conn.execute("SELECT status, expiry_date FROM lots WHERE lot_id=?", (lid,)).fetchone()
        if r is None or r["status"] != "active":
            out.append(Violation("lot_not_found", f"lot {lid} does not exist or is not active"))
        elif to_date(r["expiry_date"]) < as_of:
            out.append(Violation("lot_expired", f"lot {lid} expired on {r['expiry_date']}: it may not be donated"))
    if not str(p.get("partner", "")).strip():
        out.append(Violation("missing_field", "donation needs a partner"))


def _check_discount(a, p, conn, as_of, out):
    if _missing(p, ["check_id", "subtotal"], out):
        return
    sub = _num(p["subtotal"], 0) or 0
    amt = _num(p.get("amount"))
    if amt is None:
        amt = sub * (_num(p.get("pct"), 0) or 0) / 100
    if amt <= 0:
        out.append(Violation("positive_quantities", "discount must be > 0"))
    else:
        cap = _num(p.get("max_pct"), default_settings.DISCOUNT_MAX_PCT)
        if sub and amt / sub * 100 > cap + 1e-9:
            out.append(Violation("discount_too_large", f"discount {amt:.2f} is {amt / sub * 100:.0f}% of the bill (max {cap:.0f}% "
                                                       f"for this role)"))
    if not str(p.get("reason", "")).strip():
        out.append(Violation("missing_reason", "a discount needs a reason"))


def _check_refund(a, p, conn, as_of, out):
    if _missing(p, ["check_id", "amount"], out):
        return
    amt, paid = _num(p["amount"], 0) or 0, _num(p.get("paid"), 0) or 0
    if amt <= 0:
        out.append(Violation("positive_quantities", "refund must be > 0"))
    elif amt > paid + 1e-9:
        out.append(Violation("refund_exceeds_paid", f"refund {amt:.2f} exceeds the {paid:.2f} paid on the check"))
    if not str(p.get("reason", "")).strip():
        out.append(Violation("missing_reason", "a refund needs a reason"))


def _check_price_change(a, p, conn, as_of, out):
    if _missing(p, ["dish_id", "new_price"], out):
        return
    d = _dish(conn, p["dish_id"])
    if d is None:
        out.append(Violation("unknown_dish", f"dish {p['dish_id']} does not exist"))
        return
    new = _num(p["new_price"], 0) or 0
    old = float(d["price"])
    if new <= 0:
        out.append(Violation("positive_quantities", "new_price must be > 0"))
        return
    if abs(new / old - 1) > MAX_PRICE_CHANGE + 1e-9:
        out.append(Violation("price_step", f"{d['name']}: {old:.2f} -> {new:.2f} is more than {MAX_PRICE_CHANGE:.0%}"))
    fc = menu.margin_calc(p["dish_id"], conn=conn)["food_cost"]
    if new < fc:
        out.append(Violation("negative_margin", f"price {new:.2f} is below food cost {fc:.2f}"))


_CHECKS = {
    "place_po": _check_place_po, "reschedule_po": _check_reschedule_po,
    "substitute_supplier": _check_substitute_supplier, "add_special": _check_add_special,
    "86_dish": _check_86, "substitute_ingredient": _check_substitute_ingredient, "prep_list": _check_prep_list,
    "discard_lot": _check_discard_lot, "adjust_forecast": _check_adjust_forecast, "flag_variance": _check_flag_variance,
    "discard_lots": _check_discard_lots, "throttle_online": _check_throttle_online,
    "allergen_swap": _check_allergen_swap, "comp": _check_comp, "add_staff": _check_add_staff,
    "release_staff": _check_release_staff, "prep_batch": _check_prep_batch, "donate_lots": _check_donate_lots,
    "discount": _check_discount, "refund": _check_refund, "price_change": _check_price_change,
}


def check_action(action, conn: sqlite3.Connection, as_of: "str | date") -> list[Violation]:
    """Validate one action against every applicable rule. Returns [] when clean; any 'block' stops execution."""
    a = _norm(action)
    out: list[Violation] = []
    fn = _CHECKS.get(a.get("action_type"))
    if fn is None:
        return [Violation("unknown_action", f"unsupported action_type {a.get('action_type')!r}")]
    p = a.get("payload")
    if not isinstance(p, dict):
        return [Violation("missing_field", "payload must be an object")]
    fn(a, p, conn, to_date(as_of), out)
    return out


def blocking(violations: list[Violation]) -> list[Violation]:
    """Only the 'block' severity violations."""
    return [v for v in violations if v.severity == "block"]
