"""The four demo disruptions as data, plus how each one mutates the simulated world and becomes a supervisor trigger.

A disruption is {"kind", "day_offset" (days after SIM_START = when it lands), "params"}. `SCENARIOS[name]` is a list
of them; dates in params are given as offsets ("@<n>") and resolved against settings.SIM_START when a scenario is
loaded, so a scenario works for any start date and weekday. No LLM imports here.

Timeline (day 1 = SIM_START, whatever weekday that is; `describe()` prints the real weekdays):
  day 3  cooler_failure       - all dairy lots are discarded (waste_log reason 'spoilage')
  day 3  heatwave             - notice: days 4-6 will be 34 C (weather rows written now)
  day 4  supplier_shortfall   - notice: day 5's salmon delivery from the salmon supplier will be filled at 30%
  day 4  private_booking      - 40 covers booked for day 6 dinner
"""
from __future__ import annotations

import copy
from datetime import timedelta

from restaurant_ops.config import settings
from restaurant_ops.data import catalog as cat
from restaurant_ops.sim import vendors


def _date(offset: int):
    return settings.SIM_START + timedelta(days=offset)


def _day(offset: int) -> str:
    return _date(offset).isoformat()


def _label(offset: int) -> str:
    """'Wed 2026-11-04' for SIM_START + offset."""
    d = _date(offset)
    return f"{d:%a} {d.isoformat()}"


def salmon_supplier_id() -> str:
    """Primary salmon supplier from the catalog: shortest lead time, then cheapest (sup_freshcatch)."""
    return min(cat.suppliers_for("salmon"),
               key=lambda si: (cat.SUPPLIERS_BY_ID[si.supplier_id].lead_time_days, si.price)).supplier_id


SCENARIO_NAMES = ("salmon_shortfall", "private_booking", "cooler_failure", "heatwave", "all", "none")

# date params are "@<offset from SIM_START>" and are resolved in get_scenario()
_SHORTFALL = {"kind": "supplier_shortfall", "day_offset": 3,
              "params": {"supplier_id": salmon_supplier_id(), "ingredient_id": "salmon", "fill_rate": 0.3,
                         "delivery_date": "@4"}}
_BOOKING = {"kind": "private_booking", "day_offset": 3,
            "params": {"date": "@5", "meal_period": "dinner", "party_size": 40}}
_COOLER = {"kind": "cooler_failure", "day_offset": 2, "params": {"category": "dairy"}}
_HEAT = {"kind": "heatwave", "day_offset": 2, "params": {"start_date": "@3", "days": 3, "temp_c": 34.0}}

SCENARIOS: dict[str, list[dict]] = {
    "salmon_shortfall": [_SHORTFALL],
    "private_booking": [_BOOKING],
    "cooler_failure": [_COOLER],
    "heatwave": [_HEAT],
    "all": [_COOLER, _HEAT, _SHORTFALL, _BOOKING],
    "none": [],
}


def describe(name: str) -> str:
    """One-line description of a scenario with its real dates and weekdays for the current SIM_START."""
    if name == "salmon_shortfall":
        return f"{_label(3)}: notice that the {_label(4)} salmon delivery from the salmon supplier is only 30% filled"
    if name == "private_booking":
        return f"{_label(3)}: a 40-cover private dinner lands for {_label(5)}"
    if name == "cooler_failure":
        return f"{_label(2)}: the cooler fails and all dairy lots are discarded"
    if name == "heatwave":
        return f"{_label(2)}: heatwave notice, 34 C {_label(3)} to {_label(5)} (cold dishes up, hot mains down)"
    if name == "all":
        return "all four disruptions in one week"
    return "no disruptions"


def __getattr__(attr: str):
    if attr == "DESCRIPTIONS":  # kept for older callers; computed so it follows SIM_START
        return {n: describe(n) for n in SCENARIO_NAMES}
    raise AttributeError(attr)


def _resolve(v):
    return _day(int(v[1:])) if isinstance(v, str) and v.startswith("@") else v


def get_scenario(name: str) -> list[dict]:
    """Deep copies of a scenario's disruptions with concrete dates: 'date' = SIM_START + day_offset, '@n' params."""
    if name not in SCENARIOS:
        raise ValueError(f"unknown scenario {name!r}; choose from {', '.join(SCENARIO_NAMES)}")
    out = []
    for d in copy.deepcopy(SCENARIOS[name]):
        d["date"] = _day(int(d["day_offset"]))
        d["params"] = {k: _resolve(v) for k, v in (d.get("params") or {}).items()}
        out.append(d)
    return sorted(out, key=lambda x: x["day_offset"])


def to_trigger(disruption: dict) -> dict:
    """Supervisor event trigger: {"kind": "event", "event": {"kind": K, "params": {...}}}."""
    return {"kind": "event", "event": {"kind": disruption["kind"], "params": dict(disruption.get("params") or {})}}


# =========================================================================== world mutations


def _trace(world, content: dict) -> None:
    world.trace("event", content)


def _supplier_shortfall(world, p: dict) -> dict:
    sup = p.get("supplier_id") or salmon_supplier_id()
    ing = p.get("ingredient_id")
    fill = float(p.get("fill_rate", 0.3))
    first = str(p.get("delivery_date") or world.today.isoformat())
    last = str(p.get("end_date") or first)
    note = vendors.vendor_message(sup, "shortfall", ing, fill, first)
    world.conn.execute("INSERT INTO supply_overrides (supplier_id, ingredient_id, start_date, end_date, fill_rate, note) "
                       "VALUES (?,?,?,?,?,?)", (sup, ing, first, last, fill, note))
    p.setdefault("supplier_id", sup)
    p.setdefault("fill_rate", fill)
    _trace(world, {"event": "supplier_shortfall", "supplier_id": sup, "ingredient_id": ing, "fill_rate": fill,
                   "window": [first, last], "message": note})
    return {"supplier_id": sup, "ingredient_id": ing, "fill_rate": fill, "window": [first, last], "message": note}


def _private_booking(world, p: dict) -> dict:
    day, period, size = str(p["date"]), p.get("meal_period", "dinner"), int(p.get("party_size", 40))
    world.conn.execute("INSERT INTO reservations (date, meal_period, party_size, kind, created_date) VALUES (?,?,?,?,?)",
                       (day, period, size, "private", world.today.isoformat()))
    world.conn.execute("INSERT INTO events (date, name, type, meal_period, impact_hint) VALUES (?,?,?,?,?)",
                       (day, f"Private party ({size} covers)", "private", period, f"{size} extra covers at {period}"))
    _trace(world, {"event": "private_booking", "date": day, "meal_period": period, "party_size": size})
    return {"date": day, "meal_period": period, "party_size": size}


def _cooler_failure(world, p: dict) -> dict:
    category = p.get("category") or "dairy"
    today = world.today.isoformat()
    rows = world.conn.execute(
        "SELECT l.lot_id, l.ingredient_id, l.qty, l.unit_cost FROM lots l JOIN ingredients i ON i.id=l.ingredient_id "
        "WHERE l.status='active' AND l.qty>0 AND i.category=? ORDER BY l.lot_id", (category,)).fetchall()
    value = 0.0
    for r in rows:
        v = round(float(r["qty"]) * float(r["unit_cost"]), 2)
        value += v
        world.conn.execute("INSERT INTO waste_log (date, lot_id, ingredient_id, qty, value, reason) VALUES (?,?,?,?,?,?)",
                           (today, r["lot_id"], r["ingredient_id"], r["qty"], v, "spoilage"))
        world.conn.execute("UPDATE lots SET status='discarded' WHERE lot_id=?", (r["lot_id"],))
    ings = sorted({r["ingredient_id"] for r in rows})
    p["ingredient_ids"] = ings
    p["lot_ids"] = [r["lot_id"] for r in rows]
    _trace(world, {"event": "cooler_failure", "category": category, "lots": len(rows), "value": round(value, 2),
                   "ingredients": ings})
    return {"category": category, "lots_discarded": len(rows), "value": round(value, 2), "ingredient_ids": ings}


def _heatwave(world, p: dict) -> dict:
    start = world.today if not p.get("start_date") else world.today.fromisoformat(str(p["start_date"]))
    days, temp = int(p.get("days", 3)), float(p.get("temp_c", 34.0))
    p.setdefault("start_date", start.isoformat())
    dates = [(start + timedelta(days=i)).isoformat() for i in range(days)]
    for d in dates:
        world.conn.execute("INSERT OR REPLACE INTO weather (date, temp_c, alert) VALUES (?,?,?)", (d, temp, "heatwave"))
        world.conn.execute("INSERT INTO events (date, name, type, meal_period, impact_hint) VALUES (?,?,?,?,?)",
                           (d, "Heatwave warning", "weather", None, f"{temp:g} C: cold dishes up, hot mains down"))
    _trace(world, {"event": "heatwave", "dates": dates, "temp_c": temp})
    return {"dates": dates, "temp_c": temp}


_APPLY = {"supplier_shortfall": _supplier_shortfall, "private_booking": _private_booking,
          "cooler_failure": _cooler_failure, "heatwave": _heatwave}


def apply_to_world(world, disruption: dict) -> dict:
    """Mutate the world for a disruption (the caller passes a copy; params may be enriched, e.g. dairy ingredient_ids).

    - supplier_shortfall: adds a `supply_overrides` row (fill rate for that supplier/ingredient on the delivery date);
      the PO stays 'sent' and is short-delivered by the simulator on that day.
    - private_booking: private reservation + private event row; the simulator adds the covers when realising that day.
    - cooler_failure: discards every active lot of the category (default dairy) into waste_log as 'spoilage'.
    - heatwave: writes weather rows (34 C, alert 'heatwave') plus a weather event row for each day.
    Returns a small summary dict.
    """
    kind = disruption["kind"]
    if kind not in _APPLY:
        raise ValueError(f"unknown disruption kind {kind!r}")
    disruption.setdefault("params", {})
    res = _APPLY[kind](world, disruption["params"])
    world.conn.commit()
    return {"kind": kind, **res}
