"""Live disruptions, injected from the floor (buttons) or scheduled in a headless run ("salmon_short@19:00")."""
from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from restaurant_ops.data import catalog as cat
from restaurant_ops.live import events as E
from restaurant_ops.tools import registry as R

if TYPE_CHECKING:  # pragma: no cover
    from restaurant_ops.live.service import Service

SALMON_LEFT = 6
SALMON_SUPPLIER = "sup_freshcatch"
SALMON_FILL = 0.3
HEAT_C = 34.0
MISS_WALKIN_MULT = 1.75
MISS_ONLINE_MULT = 1.3


def party_of_12(svc: "Service") -> dict:
    return svc.walk_in(12, kind="large", preorder=True)


def salmon_short(svc: "Service") -> dict:
    ing = "salmon"
    per = cat.DISHES_BY_ID["seared_salmon"].recipe[ing]
    keep = SALMON_LEFT * per + 0.01 + svc.committed.get("seared_salmon", 0) * per
    removed = svc.stock.set_on_hand(ing, keep)
    until = (svc.date + timedelta(days=2)).isoformat()
    svc.conn.execute("INSERT INTO supply_overrides (supplier_id, ingredient_id, start_date, end_date, fill_rate, note) "
                     "VALUES (?,?,?,?,?,?)", (SALMON_SUPPLIER, ing, svc.date.isoformat(), until, SALMON_FILL,
                                              "live: salmon delivery short"))
    svc.shortages[ing] = {"supplier_id": SALMON_SUPPLIER, "fill_rate": SALMON_FILL, "until": until}
    svc.inventory_dirty = True
    info = {"ingredient_id": ing, "supplier_id": SALMON_SUPPLIER, "fill_rate": SALMON_FILL, "until": until,
            "removed_qty": removed, "remaining_portions": svc.available_portions("seared_salmon")}
    svc.bus.publish(E.SUPPLIER_SHORTFALL, info, source="scenario")
    return info


def cooler_failure(svc: "Service") -> dict:
    return svc.inventory.cooler_failure("dairy")


def heatwave(svc: "Service") -> dict:
    d0 = svc.date
    svc.flush()
    with R.connection() as c:
        for i in range(3):
            c.execute("INSERT OR REPLACE INTO weather (date, temp_c, alert) VALUES (?,?,?)",
                      ((d0 + timedelta(days=i)).isoformat(), HEAT_C, "heatwave"))
        c.commit()
    R.clear_cache()
    info = {"alert": "heatwave", "temp_c": HEAT_C, "days": 3}
    svc.bus.publish(E.WEATHER_ALERT, info, source="scenario")
    return info


def forecast_miss(svc: "Service") -> dict:
    svc.guests.walkin_mult = MISS_WALKIN_MULT
    svc.guests.online_mult = MISS_ONLINE_MULT
    return {"walkin_mult": MISS_WALKIN_MULT, "online_mult": MISS_ONLINE_MULT,
            "note": "unannounced demand surge; the forecast does not know"}


SCENARIOS = {
    "party_of_12": (party_of_12, "Walk-in party of 12 at peak"),
    "salmon_short": (salmon_short, "Salmon delivery short: FreshCatch fills 30%"),
    "cooler_failure": (cooler_failure, "Walk-in cooler fails: dairy unsafe"),
    "heatwave": (heatwave, "Heatwave: 34 C for 3 days"),
    "forecast_miss": (forecast_miss, "Forecast miss: covers run ~30% above forecast"),
}


def inject(svc: "Service", name: str) -> dict:
    if name not in SCENARIOS:
        raise KeyError(f"unknown scenario {name!r}; choose from {', '.join(SCENARIOS)}")
    fn, label = SCENARIOS[name]
    svc.bus.publish(E.SCENARIO_INJECTED, {"name": name, "label": label, "at": svc.clock.hhmm()}, source="scenario")
    res = fn(svc)
    brief = {k: v for k, v in res.items() if not isinstance(v, (dict, list))} if isinstance(res, dict) else res
    svc.bus.publish(E.AGENT_LOG, {"agent": "scenario", "message": f"{label}: " + ", ".join(f"{k} {v}" for k, v in brief.items())},
                    source="scenario")
    return {"name": name, "label": label, "result": res}
