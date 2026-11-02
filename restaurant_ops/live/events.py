"""Event types on the bus. Every event is {seq, type, source, sim_ts, wall_ts, payload}.

Events carry enough state (snapshots for tables, kitchen load, inventory, decisions) for a client to rebuild the
whole service by folding them in order, which is how the UI and the replay work.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

# order flow
ORDER_SUBMITTED = "order.submitted"   # raw order from the floor, a server, or the online page (before parsing)
ORDER_CREATED = "order.created"       # parsed, allergen-checked, routed (Order agent)
ORDER_REJECTED = "order.rejected"
ITEM_FIRED = "item.fired"             # item sent to its station queue (kitchen)
ITEM_DONE = "item.done"
ITEM_HELD = "item.held"               # waiting on a manager decision (unresolved allergen conflict)
# inventory
STOCK_LOW = "stock.low"
STOCK_OUT = "stock.out"
LOT_EXPIRING = "lot.expiring"
LOT_QUARANTINED = "lot.quarantined"
INVENTORY_SNAPSHOT = "inventory.snapshot"
SUPPLIER_SHORTFALL = "supplier.shortfall"
PO_SENT = "po.sent"
# forecast / menu / kitchen / online
FORECAST_UPDATED = "forecast.updated"
FORECAST_MISS = "forecast.miss"
WEATHER_ALERT = "weather.alert"
MENU_UPDATED = "menu.updated"
KITCHEN_LOAD = "kitchen.load"
QUOTE_UPDATED = "quote.updated"
ONLINE_ORDER = "online.order"
ONLINE_MODE = "online.mode"
# floor
TABLE_UPDATED = "table.updated"
PARTY_ARRIVED = "party.arrived"
PARTY_LEFT = "party.left"
# front of house / payments / safety / staffing
HOST_UPDATED = "host.updated"          # bookings + waitlist snapshot
CHECK_UPDATED = "check.updated"
TEMP_ALERT = "temp.alert"
SAFETY_UPDATED = "safety.updated"
PREP_UPDATED = "prep.updated"          # mise-en-place burn-down + staffing snapshot
STAFF_UPDATED = "staff.updated"
SUPPLIER_LEARNED = "supplier.learned"
# decisions
DECISION_PROPOSED = "decision.proposed"
DECISION_RESOLVED = "decision.resolved"
# service
SERVICE_STARTED = "service.started"
SERVICE_ENDED = "service.ended"
CLOCK_TICK = "clock.tick"
KPI_UPDATED = "kpi.updated"
SCENARIO_INJECTED = "scenario.injected"
AGENT_LOG = "agent.log"

ALL_TYPES = [v for k, v in dict(globals()).items() if k.isupper() and isinstance(v, str) and "." in v]


def wall_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class Event:
    type: str
    payload: dict = field(default_factory=dict)
    source: str = "service"
    sim_ts: str = ""
    wall_ts: str = field(default_factory=wall_now)
    seq: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        return cls(type=d["type"], payload=d.get("payload") or {}, source=d.get("source", "service"),
                   sim_ts=d.get("sim_ts", ""), wall_ts=d.get("wall_ts") or wall_now(), seq=int(d.get("seq", 0)))
