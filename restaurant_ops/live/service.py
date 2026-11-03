"""The live service: one dinner, one event bus, five agents, the coordinator, the floor, the kitchen and the online
channel, all driven by the service clock.

`Service.advance(wall_dt)` moves simulated time by wall_dt x speed in steps of at most STEP_S sim seconds. Every
step: guests arrive and are seated, parties order and progress through their table states, online orders arrive,
the kitchen fires / cooks / finishes items, and per-minute work runs (kitchen load, quotes, KPIs, agent timers).
Everything runs on one thread and is seeded, so a headless run is reproducible; only LLM reviews (live mode) run on
worker threads, and their results are picked up on a later step.
"""
from __future__ import annotations

import logging
import math
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

from restaurant_ops import admin as ADMIN
from restaurant_ops.agents import llm
from restaurant_ops.config import settings, sim_start_from_db
from restaurant_ops.core import prep as prep_core
from restaurant_ops.db.connection import copy_db, get_conn, init_db
from restaurant_ops.live import events as E
from restaurant_ops.live import handlers as HANDLERS
from restaurant_ops.live import persistence as PERSIST
from restaurant_ops.live.agents.forecasting import ForecastingAgent
from restaurant_ops.live.agents.inventory import InventoryAgent
from restaurant_ops.live.agents.menu import MenuAgent
from restaurant_ops.live.agents.order import OrderAgent
from restaurant_ops.live.agents.prep import PrepAgent
from restaurant_ops.live.agents.procurement import ProcurementAgent
from restaurant_ops.live.bus import make_bus
from restaurant_ops.live.clock import ServiceClock
from restaurant_ops.live.coordinator import Coordinator
from restaurant_ops.live.crm import CRM
from restaurant_ops.live.floor import Floor
from restaurant_ops.live.floor_ops import FloorOps
from restaurant_ops.live.guests import GuestSim
from restaurant_ops.live.host import HostStand
from restaurant_ops.live.intraday import IntradayForecast
from restaurant_ops.live.kitchen import Kitchen
from restaurant_ops.live.llm_guard import LLMGuard
from restaurant_ops.live.menu_live import LiveMenu
from restaurant_ops.live.metrics import Metrics
from restaurant_ops.live.online import OnlineBook
from restaurant_ops.live.online_ops import OnlineOps
from restaurant_ops.live.order_flow import OrderFlow
from restaurant_ops.live.outputs import KitchenPrinter
from restaurant_ops.live.payments import Dispatcher, Payments
from restaurant_ops.live.processor import make_processor
from restaurant_ops.live.safety import FoodSafety
from restaurant_ops.live.stock import LiveStock
from restaurant_ops.live.tools import ToolBox
from restaurant_ops.live.views import Views
from restaurant_ops.live.world import (
    CAPACITY_30,
    DISH_VIEW,
    LAST_ONLINE_BEFORE_END_MIN,
    LAST_SEATING_BEFORE_END_MIN,
    STEP_S,
    Policy,
    ServiceError,
    apply_config,
)
from restaurant_ops.tools import registry as R

__all__ = ["Service", "ServiceError", "apply_config", "DISH_VIEW", "Policy"]  # world names re-exported here

log = logging.getLogger(__name__)


class Service(FloorOps, OrderFlow, OnlineOps, Views):
    """One service. The floor, order flow, online channel and read side live in mixins (live/floor_ops.py,
    order_flow.py, online_ops.py, views.py); restore paths in live/recovery.py; executors in live/handlers.py."""

    def __init__(self, seed: int | None = None, speed: int = 60, bus_kind: str | None = None, source_db: str | Path | None = None,
                 service_id: str | None = None, auto_policy: Policy | None = None, period: str | None = None,
                 service_date: "date | str | None" = None):
        src = Path(source_db or settings.DB_PATH)
        if not src.exists():
            raise ServiceError(f"{src} does not exist: seed it first (python -m restaurant_ops.data.seed)")
        self.seed = settings.SEED if seed is None else int(seed)
        self.master_path = src
        with self.master() as m:   # the master DB holds gift cards, users, audit, day close: make sure its tables exist
            ADMIN.seed_defaults(m)
        first_day = sim_start_from_db(src) or settings.SIM_START
        self.date = date.fromisoformat(str(service_date)) if service_date else first_day
        if self.date < first_day:
            raise ServiceError(f"service date {self.date} is inside the history (first live day {first_day})")
        self.period = period or settings.LIVE_PERIOD
        if self.period not in ("lunch", "dinner", "late"):
            raise ServiceError("meal period must be lunch, dinner or late")
        self.service_id = service_id or f"svc-{self.date:%Y%m%d}-{self.period[0]}{datetime.now():%H%M%S}-{uuid.uuid4().hex[:4]}"
        settings.LIVE_DIR.mkdir(parents=True, exist_ok=True)
        self.db_path = settings.LIVE_DIR / f"{self.service_id}.db"
        copy_db(src, self.db_path)
        self.conn = get_conn(self.db_path)
        self.conn.execute("PRAGMA synchronous = NORMAL")
        init_db(self.conn)
        ADMIN.seed_defaults(self.conn)
        hours = ADMIN.hours_for(self.conn, self.date, self.period)
        if hours and hours[2]:
            raise ServiceError(f"the restaurant is closed for {self.period} on {self.date:%A}s (opening hours)")
        R.set_db_path(self.db_path)
        R.set_sim_date(self.date)
        self.clock = ServiceClock(self.date, self.period, speed, hours=hours[:2] if hours else None)
        self.conn.execute("INSERT INTO service_runs (service_id, service_date, meal_period, seed, started_ts, bus, llm_mode) "
                          "VALUES (?,?,?,?,?,?,?)", (self.service_id, self.date.isoformat(), self.period, self.seed,
                                                     E.wall_now(), "pending", "mock" if llm.is_mock() else llm.describe()))
        self.conn.commit()
        self._build_world()
        self._init_runtime(bus_kind, auto_policy)
        self.conn.execute("UPDATE service_runs SET bus=? WHERE service_id=?", (self.bus.kind, self.service_id))
        regulars = self.crm.seed_regulars(self.seed)
        for b in self.guests.bookings:  # tonight's book goes to the host stand
            rid = self.guests.choice(regulars) if regulars and self.guests.aux_chance(0.3) else None
            g = self.crm.get(rid) if rid else None
            self.host.create(g["name"] if g else f"Booking {len(self.host.bookings) + 1}", b["size"], t=b["booked_t"],
                             guest_id=rid, source="sim", arrive_t=None if b["no_show"] else b["t"], allow_overbook=True,
                             publish=False)
        self.conn.commit()

    def _build_world(self) -> None:
        """The restaurant as configured in the admin tables (copied from the master DB when this service started)."""
        apply_config(self.conn)
        self.profile = ADMIN.get_profile(self.conn)
        self.zones = ADMIN.zones_dict(self.conn) or dict(settings.DELIVERY_ZONES)
        self.tax_rules = [r for r in ADMIN.list_taxes(self.conn) if r["active"]]
        self.tax_category = {r[0]: r[1] for r in self.conn.execute("SELECT id, tax_category FROM dishes")}
        staff = prep_core.station_staff(self.conn, self.date.isoformat(), self.period)
        self.kitchen = Kitchen(staff, self.seed)
        self.stock = LiveStock(self.conn, self.date)
        self.forecast = IntradayForecast(self.date, self.period)
        self.menu = LiveMenu(self.date)
        self.floor = Floor(ADMIN.list_tables(self.conn))
        self.online = OnlineBook()
        self.metrics = Metrics()
        res = [dict(r) for r in self.conn.execute("SELECT party_size, kind FROM reservations WHERE date=? AND meal_period=? "
                                                  "ORDER BY id", (self.date.isoformat(), self.period))]
        self.guests = GuestSim(self.seed, self.forecast, res, self.forecast.curve)
        self.host = HostStand(self)
        self.payments = Payments(self)
        self.dispatcher = Dispatcher(settings.DELIVERY_DRIVERS, self.zones)
        self.safety = FoodSafety(self)
        self.committed: dict[str, int] = {}
        self.item_skip: dict[str, list] = {}
        self.item_meta: dict[str, dict] = {}
        self.orders: dict[str, dict] = {}
        self.order_results: dict[str, dict] = {}
        self.shortages: dict[str, dict] = {}
        self.inventory_dirty = True
        self.large_fires: list[tuple[float, str]] = []
        self.order_fire: dict[str, float] = {}
        self.counted_items: set[str] = set()
        self._n_party = self._n_order = self._n_online = self._n_cust = 0
        self.feed: deque = deque(maxlen=250)
        self.status = "ready"
        self._last_quote_view: dict[str, tuple] = {}
        self.schedule: list[tuple[float, str]] = []  # (t_min, scenario) for headless runs
        self.sim_since_snapshot = 0.0
        self.recovered = None

    def _init_runtime(self, bus_kind: str | None, auto_policy: Policy | None) -> None:
        """Everything that is rebuilt (not restored) on resume: bus, tools, CRM, coordinator, agents, subscriptions."""
        self.ckpt_path = settings.LIVE_DIR / f"{self.service_id}.ckpt.sqlite"
        self.bus = make_bus(self.service_id, self.conn, clock=lambda: self.clock.iso(), kind=bus_kind)
        self.auto_policy = auto_policy
        self.tools = ToolBox(self)
        self.crm = CRM(self.conn, self.profile.get("loyalty"))
        self.processor = make_processor()
        self.printer = KitchenPrinter(self)
        if not hasattr(self, "llm_guard"):
            self.llm_guard = LLMGuard(self)
        self.llm_guard.svc = self
        for obj in (self.host, self.payments, self.safety):
            obj.svc = self
        self.stock.conn = self.conn
        self.pending_llm: list[dict] = []
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="llm")
        self.coordinator = Coordinator(self)
        self.order = OrderAgent(self)
        self.inventory = InventoryAgent(self)
        self.forecasting = ForecastingAgent(self)
        self.procurement = ProcurementAgent(self)
        self.menu_agent = MenuAgent(self)
        self.prep = PrepAgent(self)
        self.agents = {a.name: a for a in (self.order, self.inventory, self.forecasting, self.procurement, self.menu_agent, self.prep)}
        HANDLERS.register(self)
        self.bus.subscribe(E.ORDER_CREATED, self._on_order_created, "service.order_created")
        self.bus.subscribe(E.ITEM_FIRED, self._on_item_fired, "service.item_fired")
        self.bus.subscribe(E.ITEM_DONE, self._on_item_done, "service.item_done")
        self.bus.subscribe(E.DECISION_PROPOSED, self._on_decision, "service.snapshot_on_decision")
        self.bus.subscribe(E.DECISION_RESOLVED, self._on_decision, "service.snapshot_on_decision")
        self.bus.subscribe("*", self._to_feed, "service.feed")

    @classmethod
    def resume(cls, service_id: str, auto_policy: Policy | None = None, bus_kind: str | None = None) -> "Service":
        """Continue a service after a backend restart from its saved state (see live/recovery.py)."""
        from restaurant_ops.live import recovery

        return recovery.resume(cls, service_id, auto_policy=auto_policy, bus_kind=bus_kind)

    @contextmanager
    def master(self):
        """A short-lived connection to the master database (state that outlives a service: gift cards, users, day close)."""
        c = get_conn(self.master_path)
        try:
            init_db(c)
            yield c
            c.commit()
        finally:
            c.close()

    @classmethod
    def recover_from_log(cls, service_id: str, auto_policy: Policy | None = None, bus_kind: str | None = None) -> "Service":
        """Last-resort restore from the service DB and event log (see live/recovery.py)."""
        from restaurant_ops.live import recovery

        return recovery.recover_from_log(cls, service_id, auto_policy=auto_policy, bus_kind=bus_kind)

    def save_state(self) -> Path:
        self.flush()
        self.sim_since_snapshot = 0.0
        return PERSIST.save(self)

    def _on_decision(self, ev: E.Event) -> None:
        if self.status == "running":
            self.save_state()

    # ================================================================== lifecycle
    def start(self) -> None:
        if self.status != "ready":
            return
        self.bus.publish(E.SERVICE_STARTED, {"service": self.info(), "snapshot": self.snapshot()})
        self.status = "running"
        self.safety.create_checklist()
        self.safety.log()
        self.procurement.start_of_service()
        self.inventory.start_of_service()
        self.prep.start_of_service()
        self.forecasting.nightly()
        self.forecasting.publish("service start: model forecast for tonight")
        self.menu_agent.start_of_service()
        self.host.publish()
        self._publish_state()
        self.save_state()

    def advance(self, wall_dt: float) -> None:
        """Move the service forward by wall_dt real seconds at the clock speed (no-op while paused or finished)."""
        self._llm()
        if self.status != "running" or self.clock.speed == 0 or wall_dt <= 0:
            return
        sim = wall_dt * self.clock.speed
        while sim > 1e-9 and self.status == "running":
            dt = min(STEP_S, sim)
            self._step(dt)
            self.coordinator.tick(dt)
            sim -= dt
        self.sim_since_snapshot += wall_dt * self.clock.speed
        if self.status == "running" and self.sim_since_snapshot >= settings.SNAPSHOT_EVERY_MIN * 60:
            self.save_state()
        self.flush()

    def run_headless(self, until_end: bool = True, minutes: float | None = None, wall_step: float = 0.25) -> dict:
        """Run without wall-clock sleeps (tests, CLI). Approval timeouts count service minutes, as in the live server."""
        if self.status == "ready":
            self.start()
        target = self.clock.t + minutes * 60 if minutes is not None else math.inf
        while self.status == "running" and self.clock.t < target:
            self.advance(wall_step)
        return self.kpis()

    def end(self) -> None:
        if self.status == "ended":
            return
        self.status = "ended"
        fc = self.forecast.base
        actual = self.metrics.covers_arrived
        err = {"forecast_p50": fc.get("p50"), "actual": actual,
               "ape_pct": round(abs(actual - fc["p50"]) / max(actual, 1) * 100, 1) if fc.get("p50") else None,
               "in_p10_p90": bool(fc.get("p10", 0) <= actual <= fc.get("p90", 0)) if fc else None}
        self.forecast_error = err
        self.bus.publish(E.SERVICE_ENDED, {"kpis": self.kpis(), "forecast_error": err})
        self.forecasting.nightly()
        self.conn.execute("UPDATE service_runs SET status='ended' WHERE service_id=?", (self.service_id,))
        self.flush()
        try:
            PERSIST.save(self)
        except Exception:  # noqa: BLE001  the report and DB are what matter after the end
            pass

    def close(self) -> None:
        self.flush()
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.bus.close()
        self.coordinator.close()
        self.conn.close()

    def flush(self) -> None:
        self.conn.commit()

    # ================================================================== step
    def _step(self, dt: float) -> None:
        t0 = self.clock.t
        self.clock.advance(dt)
        t1 = self.clock.t
        end_min = (self.clock.end - self.clock.start).total_seconds() / 60
        for t_min, name in [s for s in self.schedule if s[0] * 60 <= t1]:
            self.schedule.remove((t_min, name))
            self.inject(name)
        self.host.step(t0, t1)
        if t1 / 60 <= end_min - LAST_SEATING_BEFORE_END_MIN:
            for a in self.guests.arrivals(t0, t1):
                self._arrive(a["size"], a["kind"], a["t"])
        self._seat_waiting(t1)
        self._party_timers(t1)
        if t1 / 60 <= end_min - LAST_ONLINE_BEFORE_END_MIN:
            for _ in range(self.guests.online_arrivals(t0, t1)):
                self._sim_online_order()
        res = self.kitchen.advance(t1)
        for it in res["fired"]:
            self.bus.publish(E.ITEM_FIRED, {"item_id": it.item_id, "order_id": it.order_id, "dish_id": it.dish_id,
                                            "station": it.station, "mods": it.mods, "large_party": it.large_party},
                             source="kitchen")
        for it in res["done"]:
            self.bus.publish(E.ITEM_DONE, {"item_id": it.item_id, "order_id": it.order_id, "dish_id": it.dish_id,
                                           "station": it.station, "cook_min": round((it.done_at - it.start_at) / 60, 1),
                                           "est_min": it.est_min}, source="kitchen")
        self._online_progress(t1)
        for m in range(int(t0 // 60) + 1, int(t1 // 60) + 1):
            self._minute(m)
        if self.clock.finished:
            self.end()

    def _minute(self, m: int) -> None:
        self.bus.publish(E.CLOCK_TICK, self.clock_view(), source="clock")
        load = self.kitchen_load()
        self.bus.publish(E.KITCHEN_LOAD, load, source="kitchen")
        self._refresh_quotes(load)
        for a in self.agents.values():
            a.on_tick(float(m))
        self.safety.on_minute(m)
        if m % 5 == 0 and self.floor.waitlist:
            self.host.publish()
        if self.inventory_dirty:
            self.inventory_dirty = False
            if self.menu_agent.refresh_flags():
                self.bus.publish(E.MENU_UPDATED, self.menu.snapshot(), source="menu")
            self.bus.publish(E.INVENTORY_SNAPSHOT, self.inventory_view(), source="inventory")
        if m % 2 == 0:
            self.bus.publish(E.KPI_UPDATED, self.kpis(), source="service")

    def _llm(self) -> None:
        now = time.monotonic()
        for entry in list(self.pending_llm):
            fut = entry["future"]
            if fut.done() or now > entry["deadline"]:
                self.pending_llm.remove(entry)
                entry["agent"].finish_review(entry["proposals"], fut if fut.done() else None, entry.get("t0"))

    # ================================================================== menu state (executors are in live/handlers.py)
    def _eighty_six(self, dish: str, note: str) -> None:
        if dish in self.menu.eighty_sixed:
            return
        self.menu.limits.pop(dish, None)
        self.menu.eighty_sixed[dish] = note
        self.menu.specials.pop(dish, None)
        self.metrics.eighty_six += 1
        if self.available_portions(dish) > 0:
            self.metrics.eighty_six_caught += 1
        self.conn.execute("INSERT OR REPLACE INTO menu_state (date, dish_id, status, note) VALUES (?,?,?,?)",
                          (self.date.isoformat(), dish, "86", note[:300]))
        self.inventory_dirty = True
        self.bus.publish(E.MENU_UPDATED, self.menu.snapshot(), source="menu")

    def on_limit_reached(self, dish: str) -> None:
        """'86 after N more' reached zero while taking an order."""
        self.menu.eighty_sixed.pop(dish, None)  # LiveMenu.sold marked it; count it through the regular path
        self._eighty_six(dish, "86 after limit reached")

    # ================================================================== queries used by agents
    def available_portions(self, dish_id: str) -> int:
        return max(0, self.stock.portions(dish_id) - self.committed.get(dish_id, 0))

    def available_map(self) -> dict[str, int]:
        return self.menu.available({d: self.available_portions(d) for d in self.menu.on_menu()})

    def expected_next_30(self) -> float:
        t = self.clock.t / 60
        return self.forecast.expected_between(t, t + 30)

    def kitchen_load(self) -> dict:
        load = self.kitchen.load(self.clock.t, self.expected_next_30(), CAPACITY_30)
        load["online_mode"] = self.online.mode
        load["explanation"] = self.order.explain(load) if hasattr(self, "order") else ""
        return load

    def recent_large_fires(self, minutes: float) -> list[str]:
        return [o for t, o in self.large_fires if self.clock.t - t <= minutes * 60]

    def open_pos(self, ingredient_id: str | None = None) -> list[dict]:
        self.flush()
        q = ("SELECT p.po_id, p.supplier_id, p.expected_delivery, p.status, l.ingredient_id, l.qty FROM purchase_orders p "
             "JOIN po_lines l ON l.po_id=p.po_id WHERE p.status IN ('sent','partial') AND p.expected_delivery>=?")
        args: list = [self.date.isoformat()]
        if ingredient_id:
            q += " AND l.ingredient_id=?"
            args.append(ingredient_id)
        return [dict(r) for r in self.conn.execute(q + " ORDER BY p.expected_delivery", args)]

    def next_delivery(self, ingredient_id: str) -> str | None:
        short = self.shortages.get(ingredient_id) or {}
        for p in self.open_pos(ingredient_id):
            if p["supplier_id"] != short.get("supplier_id") or p["expected_delivery"] > short.get("until", ""):
                return p["expected_delivery"]
        return None

    def stock_low_ingredients(self) -> list[str]:
        return list(self.inventory.low_sent)

    def quarantined_ingredients(self) -> set[str]:
        return {i for i, ls in self.stock.lots.items() for l in ls if l["lot_id"] in self.stock.quarantine}
