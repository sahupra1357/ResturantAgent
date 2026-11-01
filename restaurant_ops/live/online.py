"""Online orders: quote before checkout (P50-P90 range from the kitchen-load model), live updates after the order is
placed, late flags, manager overrides, and the online mode (open / quick_only / paused)."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from restaurant_ops.data import catalog as cat
from restaurant_ops.live.kitchen import PACK_BUFFER_MIN, is_quick

NOTIFY_SLIP_MIN = 3.0   # notify the customer when the P90 moves out by this much
PICKUP_AFTER_MIN = 6.0


@dataclass
class OnlineOrder:
    order_id: str
    customer: str
    item_ids: list[str]
    dishes: list[str]
    total: float
    placed_t: float
    quote_p50_t: float
    quote_p90_t: float
    cur_p50_t: float
    cur_p90_t: float
    status: str = "cooking"          # cooking -> ready -> picked_up
    ready_t: float | None = None
    override_t: float | None = None
    notified_p90_t: float | None = None
    notifications: list[dict] = field(default_factory=list)
    reason: str = ""
    source: str = "sim"              # sim | web

    def to_dict(self) -> dict:
        return asdict(self)


class OnlineBook:
    def __init__(self):
        self.orders: dict[str, OnlineOrder] = {}
        self.mode = "open"
        self.mode_reason = ""
        self.lost = 0  # orders not placed because ordering was paused / the item was unavailable

    def allowed(self, available: dict[str, int]) -> dict[str, int]:
        if self.mode == "paused":
            return {}
        if self.mode == "quick_only":
            return {d: n for d, n in available.items() if is_quick(d)}
        return dict(available)

    def active(self) -> list[OnlineOrder]:
        return [o for o in self.orders.values() if o.status != "picked_up"]

    def view(self, o: OnlineOrder, clock, late_by: float | None = None) -> dict:
        shown50 = o.override_t if o.override_t is not None else o.cur_p50_t
        shown90 = o.override_t + 5 * 60 if o.override_t is not None else o.cur_p90_t
        now = clock.t
        late = o.status == "cooking" and now > o.quote_p90_t
        return {"order_id": o.order_id, "customer": o.customer, "status": o.status, "total": o.total,
                "items": [{"dish_id": d, "name": cat.DISHES_BY_ID[d].name} for d in o.dishes],
                "item_ids": o.item_ids, "placed": clock.iso(o.placed_t),
                "quoted": [clock.iso(o.quote_p50_t), clock.iso(o.quote_p90_t)],
                "quoted_hhmm": [clock.hhmm(o.quote_p50_t), clock.hhmm(o.quote_p90_t)],
                "current": [clock.iso(shown50), clock.iso(shown90)],
                "current_hhmm": [clock.hhmm(shown50), clock.hhmm(shown90)],
                "ready": clock.iso(o.ready_t) if o.ready_t is not None else None,
                "late": late, "late_by_min": round(max(0.0, (now - o.quote_p90_t) / 60), 1) if late else 0.0,
                "slip_min": round((o.cur_p90_t - o.quote_p90_t) / 60, 1), "override": o.override_t is not None,
                "notifications": o.notifications[-6:], "reason": o.reason, "source": o.source}


def order_total(dishes: list[str]) -> float:
    return round(sum(cat.DISHES_BY_ID[d].price for d in dishes), 2)


BUFFER_MIN = PACK_BUFFER_MIN
