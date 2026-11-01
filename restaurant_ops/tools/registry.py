"""Tools layer: plain functions over the deterministic core, LangChain `@tool` wrappers, TOOLSETS.

- The simulated "today" comes from a context var (`set_sim_date`) so agents never pass `as_of`.
- Every call goes through `_registered`: per-(name, args, sim date, db) result cache, JSON-safe results,
  and an optional trace hook `fn(tool_name, args, result)` (Phase 3 writes these to the `trace` table).
- `send_po` mutates the world: it is for the execute node only and is in NO toolset.
- Call `clear_cache()` after anything mutates the DB (the simulator should do this when it advances).
"""
from __future__ import annotations

import copy
import functools
import inspect
import json
import math
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Iterator

from langchain_core.tools import tool

from restaurant_ops.config import settings
from restaurant_ops.core import constraints as C
from restaurant_ops.core import forecasting as F
from restaurant_ops.core import inventory as I
from restaurant_ops.core import menu as M
from restaurant_ops.core import optimizer as O
from restaurant_ops.core import prep as P
from restaurant_ops.core.util import iso, r2, to_date, with_weekday
from restaurant_ops.data import generator as gen
from restaurant_ops.db.connection import get_conn

# =========================================================================== context, cache, hook

_SIM_DATE: ContextVar[str | None] = ContextVar("sim_date", default=None)
_GLOBAL_SIM_DATE: str | None = None  # fallback when the context var was set in another thread
_DB_PATH: Path | None = None
_CACHE: dict[str, object] = {}
_TRACE_HOOK: Callable[[str, dict, object], None] | None = None


def set_sim_date(d: "str | date") -> str:
    """Set the current simulated date (context var + thread-safe fallback). Clears the cache on change."""
    global _GLOBAL_SIM_DATE
    s = iso(d)
    if s != get_sim_date():
        _CACHE.clear()
    _SIM_DATE.set(s)
    _GLOBAL_SIM_DATE = s
    return s


def get_sim_date() -> str:
    """Current simulated date (ISO); defaults to config.SIM_START."""
    return _SIM_DATE.get() or _GLOBAL_SIM_DATE or settings.SIM_START.isoformat()


@contextmanager
def use_sim_date(d: "str | date | None") -> Iterator[str]:
    """Temporarily run with a given sim date (None = leave unchanged)."""
    if d is None:
        yield get_sim_date()
        return
    token = _SIM_DATE.set(iso(d))
    try:
        yield get_sim_date()
    finally:
        _SIM_DATE.reset(token)


def set_db_path(path: "str | Path | None") -> None:
    """Point the tools at another database (e.g. a simulation copy). Clears the cache."""
    global _DB_PATH
    _DB_PATH = Path(path) if path else None
    _CACHE.clear()


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    """Open (and close) a connection to the tools' database."""
    c = get_conn(_DB_PATH)
    try:
        yield c
    finally:
        c.close()


def set_trace_hook(fn: Callable[[str, dict, object], None] | None) -> None:
    """Register fn(tool_name, args, result), called after every tool call (including cache hits)."""
    global _TRACE_HOOK
    _TRACE_HOOK = fn


def clear_cache() -> None:
    """Drop cached tool results (call after the world changes)."""
    _CACHE.clear()


def _default(o):
    if is_dataclass(o) and not isinstance(o, type):
        return asdict(o)
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    if hasattr(o, "item"):
        return o.item()
    if hasattr(o, "tolist"):
        return o.tolist()
    return str(o)


def _jsonable(x):
    """Round-trip through JSON so results are plain dict/list/str/float (NaN -> null)."""
    x = json.loads(json.dumps(x, default=_default))

    def clean(v):
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            return None
        if isinstance(v, dict):
            return {k: clean(e) for k, e in v.items()}
        if isinstance(v, list):
            return [clean(e) for e in v]
        return v
    return clean(x)


def _registered(name: str, cache: bool = True):
    """Decorator: sim-date cache + JSON-safe result + trace hook."""
    def deco(fn):
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            bound = sig.bind(*args, **kwargs)
            call_args = {k: v for k, v in bound.arguments.items()}
            key = None
            if cache:
                key = json.dumps([name, call_args, get_sim_date(), str(_DB_PATH or settings.DB_PATH)],
                                 sort_keys=True, default=_default)
            if key is not None and key in _CACHE:
                result = copy.deepcopy(_CACHE[key])
            else:
                result = _jsonable(fn(*args, **kwargs))
                if key is not None:
                    _CACHE[key] = copy.deepcopy(result)
            if _TRACE_HOOK is not None:
                try:
                    _TRACE_HOOK(name, _jsonable(call_args), result)
                except Exception:  # noqa: BLE001  tracing must never break a tool
                    pass
            return result
        wrapper.tool_name = name  # type: ignore[attr-defined]
        return wrapper
    return deco


def _d(x: str | None) -> str:
    return iso(x) if x else get_sim_date()


# =========================================================================== forecasting


@_registered("forecast_covers")
def forecast_covers(start_date: str | None = None, days: int = 3) -> list[dict]:
    """P10/P50/P90 covers per meal period: [{date, weekday, meal_period, p10, p50, p90, capacity_cap}]."""
    with connection() as c:
        return with_weekday(F.forecast_covers(_d(start_date), days, conn=c, as_of=get_sim_date()))  # bookings visible today only


@_registered("forecast_dish_mix")
def forecast_dish_mix(start_date: str | None = None, days: int = 3, meal_period: str | None = None) -> list[dict]:
    """Portions per dish with P10/P50/P90: [{date, meal_period, dish_id, p10, p50, p90}]."""
    with connection() as c:
        rows = F.forecast_dish_mix(_d(start_date), days, conn=c, as_of=get_sim_date())
    return [r for r in rows if meal_period in (None, r["meal_period"])]


@_registered("signal_report")
def signal_report() -> list[dict]:
    """Which signals matter: [{signal, effect_pct, significant, note, ...}]."""
    with connection() as c:
        return F.signal_report(c)


@_registered("forecast_accuracy")
def forecast_accuracy() -> dict:
    """Held-out accuracy of the forecast model: {mape, p90_coverage, by_period, ...}."""
    with connection() as c:
        return F.evaluate(c)


@_registered("event_calendar")
def event_calendar(start_date: str | None = None, days: int = 7) -> list[dict]:
    """Holidays, sports, local and private events in the window: [{date, name, type, meal_period, impact_hint}]."""
    s = to_date(_d(start_date))
    end = s + timedelta(days=days - 1)
    with connection() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT date, name, type, meal_period, impact_hint FROM events WHERE date BETWEEN ? AND ? "
            "ORDER BY date, type, name", (s.isoformat(), end.isoformat()))]
    have = {(r["date"], r["type"]) for r in rows}
    for i in range(days):  # holidays from the calendar when the events table has no row
        d = s + timedelta(days=i)
        h = gen.holiday_for(d)
        if h and (d.isoformat(), "holiday") not in have:
            rows.append({"date": d.isoformat(), "name": h, "type": "holiday", "meal_period": None,
                         "impact_hint": "holiday traffic pattern"})
    return with_weekday(sorted(rows, key=lambda r: (r["date"], r["type"], r["name"])))


@_registered("reservations_summary")
def reservations_summary(start_date: str | None = None, days: int = 7) -> list[dict]:
    """Bookings visible today per date and meal period: counts, booked covers, private covers, % of seat cap."""
    s = to_date(_d(start_date))
    end = s + timedelta(days=days - 1)
    with connection() as c:
        rows = c.execute(
            "SELECT date, meal_period, COUNT(*) AS bookings, SUM(party_size) AS booked_covers, "
            "SUM(CASE WHEN kind='private' THEN party_size ELSE 0 END) AS private_covers, MAX(party_size) AS largest_party "
            "FROM reservations WHERE date BETWEEN ? AND ? AND created_date<=? GROUP BY date, meal_period ORDER BY date, meal_period",
            (s.isoformat(), end.isoformat(), get_sim_date())).fetchall()
    return [{"date": r["date"], "weekday": to_date(r["date"]).strftime("%a"), "meal_period": r["meal_period"], "bookings": r["bookings"],
             "booked_covers": r["booked_covers"], "private_covers": r["private_covers"],
             "largest_party": r["largest_party"], "seat_cap": settings.seat_cap(r["meal_period"]),
             "booked_pct_of_cap": r2(r["booked_covers"] / settings.seat_cap(r["meal_period"]) * 100, 1)} for r in rows]


@_registered("weather")
def weather(start_date: str | None = None, days: int = 7) -> list[dict]:
    """Daily temperature and alerts: [{date, temp_c, alert, heat}] (heat = above 30 C)."""
    s = to_date(_d(start_date))
    with connection() as c:
        rows = c.execute("SELECT date, temp_c, alert FROM weather WHERE date BETWEEN ? AND ? ORDER BY date",
                         (s.isoformat(), (s + timedelta(days=days - 1)).isoformat())).fetchall()
    return [{"date": r["date"], "weekday": to_date(r["date"]).strftime("%a"), "temp_c": r["temp_c"], "alert": r["alert"], "heat": r["temp_c"] > gen.HEAT_C} for r in rows]


# =========================================================================== inventory


@_registered("stock_by_lot")
def stock_by_lot(ingredient_id: str | None = None) -> list[dict]:
    """Active unexpired lots in FEFO order: [{lot_id, ingredient_id, qty, expiry_date, days_left, unit_cost}]."""
    with connection() as c:
        return I.stock_by_lot(ingredient_id, get_sim_date(), c)


@_registered("expected_usage")
def expected_usage(start_date: str | None = None, days: int = 3, quantile: str = "p50",
                   dish_overrides: dict | None = None, ingredient_ids: list[str] | None = None) -> list[dict]:
    """Ingredient usage from forecast mix x BOM: [{ingredient_id, date, qty}]."""
    with connection() as c:
        rows = I.expected_usage(_d(start_date), days, quantile, dish_overrides, conn=c, as_of=get_sim_date())
    return [r for r in rows if not ingredient_ids or r["ingredient_id"] in ingredient_ids]


@_registered("waste_risk")
def waste_risk(horizon_days: int = 3) -> list[dict]:
    """Lots expiring before forecast usage consumes them: [{lot_id, ingredient_id, qty_at_risk, value_at_risk, expiry_date, days_left}]."""
    with connection() as c:
        return I.waste_risk(get_sim_date(), horizon_days, c)


@_registered("variance_report")
def variance_report(days: int = 7) -> list[dict]:
    """Flagged stock-count variances: [{ingredient_id, expected, actual, variance_pct, flag}]."""
    with connection() as c:
        return I.variance_report(get_sim_date(), days, c)


# =========================================================================== purchasing


@_registered("optimize_orders")
def optimize_orders(horizon_days: int = 3, budget: float | None = None, service_quantile: str = "p90",
                    exclude_suppliers: list[str] | None = None, ingredient_ids: list[str] | None = None) -> dict:
    """MILP order plan: {status, solver, orders:[{supplier_id, delivery_date, lines, total}], total_cost, notes, shortfalls}."""
    with connection() as c:
        return O.optimize_orders(get_sim_date(), horizon_days, budget, service_quantile,
                                 tuple(exclude_suppliers or ()), ingredient_ids, conn=c)


@_registered("supplier_quotes")
def supplier_quotes(ingredient_id: str, qty: float, needed_by: str) -> list[dict]:
    """Quotes from every supplier carrying the item, feasible-first then cheapest."""
    today = to_date(get_sim_date())
    need_by = to_date(needed_by)
    with connection() as c:
        rows = c.execute("SELECT s.*, i.price, i.pack_size FROM supplier_items i JOIN suppliers s ON s.id=i.supplier_id "
                         "WHERE i.ingredient_id=?", (ingredient_id,)).fetchall()
    out = []
    for r in rows:
        packs = max(1, math.ceil(qty / r["pack_size"] - 1e-9))
        oq = packs * r["pack_size"]
        total = oq * r["price"]
        earliest = today + timedelta(days=int(r["lead_time_days"]))
        out.append({"supplier_id": r["id"], "supplier": r["name"], "unit_price": r2(r["price"], 3), "pack_size": r["pack_size"],
                    "order_qty": oq, "line_total": r2(total), "min_order_value": r["min_order_value"],
                    "meets_min_order": total >= r["min_order_value"], "earliest_delivery": earliest.isoformat(),
                    "meets_deadline": earliest <= need_by, "fill_rate": r["fill_rate"], "delay_prob": r["delay_prob"],
                    "risk_adjusted_price": r2(r["price"] * (1 + O.FILL_RISK_WEIGHT * ((1 - r["fill_rate"]) + r["delay_prob"])), 3)})
    out.sort(key=lambda q: (not q["meets_deadline"], q["risk_adjusted_price"]))
    return out


@_registered("negotiate", cache=False)
def negotiate(supplier_id: str, ingredient_id: str, qty: float, target_price: float) -> dict:
    """Ask a (mock) vendor agent for a better unit price. Returns the vendor's counter-offer."""
    try:
        from restaurant_ops.sim.vendors import negotiate as _neg  # Phase 4 module, imported lazily
    except Exception as e:  # noqa: BLE001
        return {"error": f"negotiation unavailable: restaurant_ops.sim.vendors.negotiate could not be imported ({e})",
                "available": False}
    try:
        return _neg(supplier_id, ingredient_id, qty, target_price)
    except Exception as e:  # noqa: BLE001
        return {"error": f"negotiation failed: {type(e).__name__}: {e}", "available": True}


@_registered("send_po", cache=False)
def send_po(supplier_id: str, delivery_date: str, lines: list[dict], decision_id: int | None = None) -> dict:
    """EXECUTE NODE ONLY (in no toolset): write a purchase order with status 'sent'. Validate with check_action first."""
    today = get_sim_date()
    with connection() as c:
        n = c.execute("SELECT COUNT(*) FROM purchase_orders WHERE po_id LIKE ?", (f"PO-{today.replace('-', '')[2:]}-%",)).fetchone()[0]
        po_id = f"PO-{today.replace('-', '')[2:]}-{n + 1:03d}"
        total = round(sum(float(l["qty"]) * float(l["unit_price"]) for l in lines), 2)
        c.execute("INSERT INTO purchase_orders (po_id, supplier_id, created_date, expected_delivery, status, total, decision_id) "
                  "VALUES (?,?,?,?,?,?,?)", (po_id, supplier_id, today, iso(delivery_date), "sent", total, decision_id))
        c.executemany("INSERT INTO po_lines (po_id, ingredient_id, qty, unit_price, qty_delivered) VALUES (?,?,?,?,NULL)",
                      [(po_id, l["ingredient_id"], float(l["qty"]), float(l["unit_price"])) for l in lines])
        c.commit()
    clear_cache()
    return {"po_id": po_id, "supplier_id": supplier_id, "created_date": today, "expected_delivery": iso(delivery_date),
            "status": "sent", "total": total, "lines": len(lines)}


# =========================================================================== menu & prep


@_registered("recipes")
def recipes(dish_id: str | None = None) -> list[dict]:
    """Recipes with BOM lines, price, station and allergens."""
    with connection() as c:
        return M.recipes(dish_id, c)


@_registered("margin_calc")
def margin_calc(dish_id: str, ingredient_cost_overrides: dict | None = None) -> dict:
    """{dish_id, price, food_cost, margin, margin_pct} at reference costs or per-unit overrides."""
    with connection() as c:
        return M.margin_calc(dish_id, ingredient_cost_overrides, c)


@_registered("allergen_check")
def allergen_check(dish_id: str, substitutions: dict | None = None) -> dict:
    """Allergens of a dish, optionally after {from_ingredient: to_ingredient} swaps: {allergens, changed, ok}."""
    with connection() as c:
        return M.allergen_check(dish_id, substitutions, c)


@_registered("dishes_using")
def dishes_using(ingredient_ids: list[str]) -> list[str]:
    """Dish ids that use any of the given ingredients."""
    with connection() as c:
        return M.dishes_using(ingredient_ids, c)


@_registered("feasible_portions")
def feasible_portions(dish_id: str) -> int:
    """Portions of a dish the usable on-hand stock supports today."""
    with connection() as c:
        return M.feasible_portions(dish_id, get_sim_date(), c)


@_registered("menu_status")
def menu_status(date: str | None = None) -> dict:
    """Today's menu state: {date, specials, eighty_sixed, active}."""
    with connection() as c:
        return M.menu_status(_d(date), c)


@_registered("prep_quantities")
def prep_quantities(date: str, meal_period: str, forecast_quantile: str = "p50") -> list[dict]:
    """Prep portions per dish/station: [{station, dish_id, portions, prep_minutes}]."""
    with connection() as c:
        return P.prep_quantities(date, meal_period, forecast_quantile, c)


@_registered("station_capacity")
def station_capacity(date: str, meal_period: str) -> list[dict]:
    """Per station: [{station, staff, capacity_minutes, load_minutes, utilisation}]."""
    with connection() as c:
        return P.station_capacity(date, meal_period, c)


# =========================================================================== constraint checker (not an agent tool)


@_registered("check_action", cache=False)
def check_action(action: dict) -> list[dict]:
    """Run the deterministic constraint checker on an action for the current sim date."""
    with connection() as c:
        return [v.to_dict() for v in C.check_action(action, c, get_sim_date())]


@_registered("needs_approval", cache=False)
def needs_approval(action: dict) -> bool:
    """Whether the chef must approve this action (PO over threshold, specials, 86's, substitutions)."""
    return C.needs_approval(action, settings)


# =========================================================================== LangChain tools (the LLM never sees as_of)


def _safe(fn: Callable, **kw):
    try:
        return fn(**kw)
    except Exception as e:  # noqa: BLE001  tool errors go back to the agent as data
        return {"error": f"{type(e).__name__}: {e}"}


@tool("forecast_covers")
def forecast_covers_tool(start_date: str | None = None, days: int = 3) -> list | dict:
    """Forecast covers (guests) per meal period (lunch/dinner/late) with P10/P50/P90 bands, capped at seat capacity.
    Args: start_date ISO date (default today), days 1-7 (default 3)."""
    return _safe(forecast_covers, start_date=start_date, days=days)


@tool("forecast_dish_mix")
def forecast_dish_mix_tool(start_date: str | None = None, days: int = 3, meal_period: str | None = None) -> list | dict:
    """Forecast portions sold per dish per meal period with P10/P50/P90. Optionally filter to one meal_period.
    Args: start_date ISO date (default today), days (default 3), meal_period lunch|dinner|late."""
    return _safe(forecast_dish_mix, start_date=start_date, days=days, meal_period=meal_period)


@tool("event_calendar")
def event_calendar_tool(start_date: str | None = None, days: int = 7) -> list | dict:
    """List holidays, sports games, local events and private bookings in a date window, with expected impact hints."""
    return _safe(event_calendar, start_date=start_date, days=days)


@tool("signal_report")
def signal_report_tool() -> list | dict:
    """Report which demand signals (payday, start_of_month, day_of_week, season, events, heat, reservations)
    measurably move the forecast here: effect_pct and significance from a held-out ablation."""
    return _safe(signal_report)


@tool("weather")
def weather_tool(start_date: str | None = None, days: int = 7) -> list | dict:
    """Forecast temperature (C) and weather alerts per day; heat = above 30 C shifts demand toward cold dishes."""
    return _safe(weather, start_date=start_date, days=days)


@tool("reservations_summary")
def reservations_summary_tool(start_date: str | None = None, days: int = 7) -> list | dict:
    """Bookings already on the books per date and meal period: counts, booked covers, private covers, % of seat cap."""
    return _safe(reservations_summary, start_date=start_date, days=days)


@tool("stock_by_lot")
def stock_by_lot_tool(ingredient_id: str | None = None) -> list | dict:
    """Current stock lots (FEFO order) with quantity, expiry date, days left and unit cost. Optionally one ingredient."""
    return _safe(stock_by_lot, ingredient_id=ingredient_id)


@tool("expected_usage")
def expected_usage_tool(start_date: str | None = None, days: int = 3, quantile: str = "p50",
                        dish_overrides: dict[str, float] | None = None, ingredient_ids: list[str] | None = None) -> list | dict:
    """Expected ingredient usage per day = forecast dish mix x recipes. quantile p10|p50|p90.
    dish_overrides maps dish_id to a multiplier on forecast portions (0 removes the dish). Optionally filter ingredient_ids."""
    return _safe(expected_usage, start_date=start_date, days=days, quantile=quantile,
                 dish_overrides=dish_overrides, ingredient_ids=ingredient_ids)


@tool("variance_report")
def variance_report_tool(days: int = 7) -> list | dict:
    """Stock-count variances over the last N days. Flags 'shrinkage' (persistent loss, possible theft),
    'over_portioning' or 'surplus' per ingredient."""
    return _safe(variance_report, days=days)


@tool("waste_risk")
def waste_risk_tool(horizon_days: int = 3) -> list | dict:
    """Lots that will expire within the horizon before forecast usage consumes them (FEFO-simulated), with
    quantity and dollar value at risk. Use to propose specials or discounts."""
    return _safe(waste_risk, horizon_days=horizon_days)


@tool("optimize_orders")
def optimize_orders_tool(horizon_days: int = 3, budget: float | None = None, service_quantile: str = "p90",
                         exclude_suppliers: list[str] | None = None, ingredient_ids: list[str] | None = None) -> dict:
    """Run the order optimizer (MILP with greedy fallback) under lead time, minimum order value, budget and shelf life.
    Returns suggested purchase orders per supplier and delivery date, total cost, notes and remaining shortfalls.
    Use exclude_suppliers to plan around an unreliable supplier."""
    return _safe(optimize_orders, horizon_days=horizon_days, budget=budget, service_quantile=service_quantile,
                 exclude_suppliers=exclude_suppliers, ingredient_ids=ingredient_ids)


@tool("supplier_quotes")
def supplier_quotes_tool(ingredient_id: str, qty: float, needed_by: str) -> list | dict:
    """Compare every supplier for one ingredient and quantity needed by a date: price, pack rounding, earliest
    delivery, minimum order, fill rate. Feasible suppliers first."""
    return _safe(supplier_quotes, ingredient_id=ingredient_id, qty=qty, needed_by=needed_by)


@tool("negotiate")
def negotiate_tool(supplier_id: str, ingredient_id: str, qty: float, target_price: float) -> dict:
    """Negotiate a unit price with a supplier's (mock) vendor agent. Returns their counter-offer or an error."""
    return _safe(negotiate, supplier_id=supplier_id, ingredient_id=ingredient_id, qty=qty, target_price=target_price)


@tool("recipes")
def recipes_tool(dish_id: str | None = None) -> list | dict:
    """Recipes (ingredient quantities per portion), price, station and allergens for one dish or all dishes."""
    return _safe(recipes, dish_id=dish_id)


@tool("margin_calc")
def margin_calc_tool(dish_id: str, ingredient_cost_overrides: dict[str, float] | None = None) -> dict:
    """Food cost, margin and margin % per portion for a dish; optionally override ingredient unit costs."""
    return _safe(margin_calc, dish_id=dish_id, ingredient_cost_overrides=ingredient_cost_overrides)


@tool("allergen_check")
def allergen_check_tool(dish_id: str, substitutions: dict[str, str] | None = None) -> dict:
    """Allergens in a dish, and whether swapping ingredients ({from_ingredient: to_ingredient}) adds any new allergen."""
    return _safe(allergen_check, dish_id=dish_id, substitutions=substitutions)


@tool("dishes_using")
def dishes_using_tool(ingredient_ids: list[str]) -> list | dict:
    """Which dishes use any of these ingredients (e.g. to find dishes affected by a shortage or at-risk lot)."""
    return _safe(dishes_using, ingredient_ids=ingredient_ids)


@tool("feasible_portions")
def feasible_portions_tool(dish_id: str) -> int | dict:
    """How many portions of a dish today's usable stock can support."""
    return _safe(feasible_portions, dish_id=dish_id)


@tool("prep_quantities")
def prep_quantities_tool(date: str, meal_period: str, forecast_quantile: str = "p50") -> list | dict:
    """Prep list for one meal period: portions per dish with station and total prep minutes, from the forecast mix.
    86'd dishes are excluded. forecast_quantile p50 (default) or p90 for a safer par."""
    return _safe(prep_quantities, date=date, meal_period=meal_period, forecast_quantile=forecast_quantile)


@tool("station_capacity")
def station_capacity_tool(date: str, meal_period: str) -> list | dict:
    """Per-station prep capacity (rostered staff x minutes) vs forecast load; utilisation above 1.0 means overload."""
    return _safe(station_capacity, date=date, meal_period=meal_period)


TOOLSETS: dict[str, list] = {
    "forecast_analyst": [forecast_covers_tool, forecast_dish_mix_tool, event_calendar_tool, signal_report_tool,
                         weather_tool, reservations_summary_tool],
    "inventory": [stock_by_lot_tool, expected_usage_tool, variance_report_tool, waste_risk_tool],
    "purchasing": [optimize_orders_tool, supplier_quotes_tool, negotiate_tool],
    "menu": [recipes_tool, margin_calc_tool, allergen_check_tool, dishes_using_tool, feasible_portions_tool,
             waste_risk_tool],
    "prep": [prep_quantities_tool, station_capacity_tool],
}
TOOLSET_NAMES: dict[str, list[str]] = {k: [t.name for t in v] for k, v in TOOLSETS.items()}

# plain functions by name (API, mock policies, execute node); send_po / check_action are here but NOT in TOOLSETS
FUNCTIONS: dict[str, Callable] = {
    f.tool_name: f for f in (  # type: ignore[attr-defined]
        forecast_covers, forecast_dish_mix, signal_report, forecast_accuracy, event_calendar, reservations_summary,
        weather, stock_by_lot, expected_usage, waste_risk, variance_report, optimize_orders, supplier_quotes,
        negotiate, send_po, recipes, margin_calc, allergen_check, dishes_using, feasible_portions, menu_status,
        prep_quantities, station_capacity, check_action, needs_approval)}
