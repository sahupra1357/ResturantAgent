"""Shared pieces of the live service: tuning constants, the dish view used by the UI and agents, the service error,
and `apply_config` (loads the admin configuration into the runtime catalog)."""
from __future__ import annotations

import logging
from typing import Callable

from restaurant_ops.data import catalog as cat
from restaurant_ops.live import events as E
from restaurant_ops.live.kitchen import cook_minutes, is_quick

log = logging.getLogger(__name__)
STEP_S = 15.0
LAST_SEATING_BEFORE_END_MIN = 60
LAST_ONLINE_BEFORE_END_MIN = 30
CAPACITY_30 = 20.0
LATE_TICKET_MIN = 25.0
CHECK_IN_MIN = (2.0, 5.0)  # minutes a party spends at the host stand before being walked to a table
EARLY_HOLD_MAX_MIN = 15.0  # a reservation that turns up early waits in the lobby until ~2 min before its booked time
INCOMPLETE_SHARE = 0.3     # walk-in groups of 3+ that wait for the rest of their party before being seated
INCOMPLETE_MIN = (3.0, 10.0)
BAR_SHARE = 0.35
FEED_SKIP = {E.CLOCK_TICK, E.KITCHEN_LOAD, E.INVENTORY_SNAPSHOT, E.KPI_UPDATED, E.TABLE_UPDATED, E.QUOTE_UPDATED,
             E.MENU_UPDATED, E.ONLINE_ORDER, E.ITEM_FIRED, E.ITEM_DONE, E.ORDER_SUBMITTED, E.SERVICE_STARTED,
             E.FORECAST_UPDATED, E.HOST_UPDATED, E.CHECK_UPDATED, E.PREP_UPDATED, E.SAFETY_UPDATED}
def _dish_view() -> dict:
    return {d.id: {"dish_id": d.id, "name": d.name, "category": d.category, "price": d.price, "station": d.station,
                   "cook_min": cook_minutes(d.id), "quick": is_quick(d.id), "allergens": cat.dish_allergens(d.id)}
            for d in cat.DISHES}


DISH_VIEW = _dish_view()

Policy = Callable[[dict], "tuple | None"]


class ServiceError(RuntimeError):
    pass


def apply_config(conn) -> dict:
    """Load the admin configuration of `conn` into the runtime catalog (dishes, ingredients, suppliers, stations) and
    the forecaster's dish list. Called when a service starts."""
    from restaurant_ops import admin as ADMIN
    from restaurant_ops.core import forecasting

    if ADMIN.migrate_legacy_stations(conn):  # a service DB copied before the migration (resumed / recovered services)
        conn.commit()
    out = cat.reload_from_db(conn)
    forecasting.refresh_catalog()
    DISH_VIEW.clear()
    DISH_VIEW.update(_dish_view())
    return out
