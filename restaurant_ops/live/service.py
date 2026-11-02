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
from contextlib import contextmanager
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path
from typing import Callable

from restaurant_ops.agents import decision_log as DL
from restaurant_ops.agents import llm
from restaurant_ops import admin as ADMIN
from restaurant_ops.agents import supervisor as SUP
from restaurant_ops.config import settings, sim_start_from_db
from restaurant_ops.core import menu_engineering as ME
from restaurant_ops.core import prep as prep_core
from restaurant_ops.core import suppliers as SUPL
from restaurant_ops.data import catalog as cat
from restaurant_ops.db.connection import copy_db, get_conn, init_db
from restaurant_ops.live import events as E
from restaurant_ops.live import persistence as PERSIST
from restaurant_ops.live import scenarios as SC
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
from restaurant_ops.live.floor import LAYOUT_FIXTURES, PLAN_H, PLAN_W, Floor, Party
from restaurant_ops.live.guests import GuestSim
from restaurant_ops.live.host import HostError, HostStand
from restaurant_ops.live.intraday import IntradayForecast
from restaurant_ops.live.kitchen import PACK_BUFFER_MIN, Kitchen, cook_minutes, is_quick
from restaurant_ops.live.menu_live import LiveMenu
from restaurant_ops.live.metrics import Metrics
from restaurant_ops.live.online import NOTIFY_SLIP_MIN, PICKUP_AFTER_MIN, OnlineBook, OnlineOrder, order_total
from restaurant_ops.live.payments import Dispatcher, PaymentError, Payments
from restaurant_ops.live.llm_guard import LLMGuard
from restaurant_ops.live.outputs import KitchenPrinter
from restaurant_ops.live.po_channel import send_po as po_send
from restaurant_ops.live.processor import make_processor
from restaurant_ops.live.safety import FoodSafety, record_donation, waste_analytics
from restaurant_ops.live.stock import LiveStock
from restaurant_ops.live.tools import ToolBox
from restaurant_ops.tools import registry as R

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
    from restaurant_ops.core import forecasting

    out = cat.reload_from_db(conn)
    forecasting.refresh_catalog()
    global DISH_VIEW
    DISH_VIEW.clear()
    DISH_VIEW.update(_dish_view())
    return out


class Service:
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
        self._register_handlers()
        self.bus.subscribe(E.ORDER_CREATED, self._on_order_created, "service.order_created")
        self.bus.subscribe(E.ITEM_FIRED, self._on_item_fired, "service.item_fired")
        self.bus.subscribe(E.ITEM_DONE, self._on_item_done, "service.item_done")
        self.bus.subscribe(E.DECISION_PROPOSED, self._on_decision, "service.snapshot_on_decision")
        self.bus.subscribe(E.DECISION_RESOLVED, self._on_decision, "service.snapshot_on_decision")
        self.bus.subscribe("*", self._to_feed, "service.feed")

    @classmethod
    def resume(cls, service_id: str, auto_policy: Policy | None = None, bus_kind: str | None = None) -> "Service":
        """Continue a service after a backend restart from its saved state, DB and approval checkpoints."""
        blob = PERSIST.load(service_id)
        svc = cls.__new__(cls)
        for k, v in blob["world"].items():
            setattr(svc, k, v)
        svc.db_path = settings.LIVE_DIR / f"{service_id}.db"
        svc.conn = get_conn(svc.db_path)
        svc.conn.execute("PRAGMA synchronous = NORMAL")
        init_db(svc.conn)
        R.set_db_path(svc.db_path)
        R.set_sim_date(svc.date)
        svc._init_runtime(bus_kind, auto_policy)
        svc.bus.seq = int(svc.conn.execute("SELECT COALESCE(MAX(seq),0) FROM event_log WHERE service_id=?", (service_id,)).fetchone()[0])
        for name, state in blob["agents"].items():
            if name in svc.agents:
                svc.agents[name].__dict__.update(state)
        svc.prep.attach()
        c = blob["coordinator"]
        svc.coordinator.decisions, svc.coordinator.open_by_key, svc.coordinator.resolved_by_key = (
            c["decisions"], c["open_by_key"], c["resolved_by_key"])
        svc.bus.publish(E.AGENT_LOG, {"agent": "service", "message": f"service resumed at {svc.clock.hhmm()} with "
                                      f"{len(svc.coordinator.pending())} approval(s) still pending"})
        svc.bus.publish(E.SERVICE_STARTED, {"service": svc.info(), "snapshot": svc.snapshot(), "resumed": True})
        svc.flush()
        return svc

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
        """Last-resort restore when no snapshot is readable: continue the service on its database (stock, menu state,
        bookings, payments already recorded) and rebuild the decisions from the event log, so pending approvals still
        resume from their LangGraph checkpoints. Guests on the floor, kitchen queues and online orders in progress
        cannot be rebuilt from the log and are reported as lost."""
        import json as _json

        db_path = settings.LIVE_DIR / f"{service_id}.db"
        if not db_path.exists():
            raise ServiceError(f"no database for {service_id}")
        svc = cls.__new__(cls)
        svc.service_id, svc.db_path = service_id, db_path
        svc.conn = get_conn(db_path)
        svc.conn.execute("PRAGMA synchronous = NORMAL")
        init_db(svc.conn)
        run = svc.conn.execute("SELECT * FROM service_runs WHERE service_id=?", (service_id,)).fetchone()
        if run is None:
            raise ServiceError(f"{service_id}: no service_runs row")
        svc.seed, svc.date, svc.period = int(run["seed"]), date.fromisoformat(run["service_date"]), run["meal_period"]
        svc.master_path = settings.DB_PATH
        R.set_db_path(db_path)
        R.set_sim_date(svc.date)
        hours = ADMIN.hours_for(svc.conn, svc.date, svc.period)
        svc.clock = ServiceClock(svc.date, svc.period, 10, hours=hours[:2] if hours else None)
        svc._build_world()
        last = svc.conn.execute("SELECT seq, sim_ts FROM event_log WHERE service_id=? ORDER BY seq DESC LIMIT 1", (service_id,)).fetchone()
        if last and last["sim_ts"]:
            svc.clock.now = datetime.fromisoformat(last["sim_ts"])
        svc._init_runtime(bus_kind, auto_policy)
        svc.bus.seq = int(last["seq"]) if last else 0
        for dish, status, note in svc.conn.execute("SELECT dish_id, status, note FROM menu_state WHERE date=?", (svc.date.isoformat(),)):
            if status == "86":
                svc.menu.eighty_sixed[dish] = note or "86"
            elif status == "special":
                svc.menu.specials[dish] = {"portions": 0, "note": note or "special"}
        views: dict[int, dict] = {}
        kpi = None
        for t, payload in svc.conn.execute("SELECT type, payload FROM event_log WHERE service_id=? AND type IN "
                                           "('decision.proposed','decision.resolved','kpi.updated') ORDER BY seq", (service_id,)):
            p = _json.loads(payload)
            if t == E.KPI_UPDATED:
                kpi = p
            else:
                views[int(p["id"])] = {**views.get(int(p["id"]), {}), **p}
        for did, v in views.items():
            v.setdefault("context", {})
            if v["status"] == "auto":
                v["status"] = "executed"
            svc.coordinator.decisions[did] = v
            if v["status"] == "pending":
                svc.coordinator.open_by_key[v["key"]] = did
        if kpi:
            for k in ("covers", "covers_arrived", "orders", "online_orders", "revenue", "food_cost", "waste_value", "eighty_six"):
                attr = {"covers": "covers_seated"}.get(k, k)
                if kpi.get(k) is not None and hasattr(svc.metrics, attr):
                    setattr(svc.metrics, attr, kpi[k])
        for b in svc.conn.execute("SELECT * FROM bookings WHERE service_id=?", (service_id,)):
            from restaurant_ops.live.host import Booking

            svc.host.bookings[b["booking_id"]] = Booking(b["booking_id"], b["name"], int(b["party_size"]), svc.host.t_of(b["time"]),
                                                        status=b["status"], phone=b["phone"] or "", guest_id=b["guest_id"],
                                                        notes=b["notes"] or "", source=b["source"] or "phone")
        svc.host._n = len(svc.host.bookings)
        svc.prep.attach()
        svc.status = "running"
        svc.recovered = {"from": "event log", "at": svc.clock.hhmm(),
                         "lost": "guests on the floor, kitchen queues and online orders in progress",
                         "kept": f"stock, menu state, {len(svc.host.bookings)} bookings, {len(views)} decisions "
                                 f"({len(svc.coordinator.pending())} pending), KPIs"}
        svc.bus.publish(E.AGENT_LOG, {"agent": "service", "message": f"recovered from the event log at {svc.clock.hhmm()}: kept "
                                      f"{svc.recovered['kept']}; lost {svc.recovered['lost']}"})
        svc.bus.publish(E.SERVICE_STARTED, {"service": svc.info(), "snapshot": svc.snapshot(), "resumed": True, "recovered": True})
        svc.flush()
        return svc

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

    # ================================================================== floor
    def _arrive(self, size: int, kind: str, t: float, preorder: bool = False, guest_id: int | None = None,
                booking_id: str | None = None, name: str = "", patience: object = "default") -> Party:
        self._n_party += 1
        p = Party(f"P{self._n_party:03d}", int(size), kind, t, preorder=preorder, guest_id=guest_id, booking_id=booking_id,
                  name=name)
        self.floor.parties[p.id] = p
        self.metrics.parties += 1
        self.metrics.covers_arrived += p.size
        seats = self.floor.find_seating(p.size, prefer_bar=kind == "walk_in" and p.size <= 2 and self.guests.chance(BAR_SHARE))
        if seats and not preorder and kind in ("walk_in", "reservation"):
            # guests check in with the host first (name, coats, the right table) and wait in the lobby meanwhile
            hold = self.guests.uniform(CHECK_IN_MIN[0], CHECK_IN_MIN[1])
            p.wait_reason = "checking in"
            b = self.host.bookings.get(booking_id) if booking_id else None
            if b is not None and (b.time_t - self.clock.t) / 60 - 2 > hold:
                hold = min(EARLY_HOLD_MAX_MIN, (b.time_t - self.clock.t) / 60 - 2)
                p.wait_reason = f"early for {self.host.hhmm(b.time_t)}"
            elif kind == "walk_in" and p.size >= 3 and self.guests.uniform(0, 1) < INCOMPLETE_SHARE:
                hold += self.guests.uniform(INCOMPLETE_MIN[0], INCOMPLETE_MIN[1])
                p.wait_reason = "waiting for the rest of the party"
            p.greet_until = self.clock.t + hold * 60
            p.patience_until = None
            p.prefer_tables = seats
            self.floor.waitlist.append(p.id)
            self.host.on_waitlisted(p)
            seats = None
            checking_in = True
        else:
            checking_in = False
        if seats:
            self._seat(p, seats, self.clock.t)
        elif not checking_in:
            if preorder or kind == "reservation" or patience is None:
                p.patience_until = None  # booked guests and named waitlist entries wait for their table
            else:
                p.patience_until = self.clock.t + self.guests.minutes("patience") * 60
            self.floor.waitlist.append(p.id)
            self.host.on_waitlisted(p)
        self.bus.publish(E.PARTY_ARRIVED, {"party_id": p.id, "size": p.size, "kind": kind, "seated": bool(seats), "checking_in": checking_in,
                                           "table_ids": p.table_ids, "waitlist": len(self.floor.waitlist),
                                           "guest_id": guest_id, "booking_id": booking_id}, source="floor")
        if not seats:
            self.host.publish()
        return p

    def _seat(self, p: Party, seats: list[str], t: float) -> None:
        self.floor.seat(p, seats, t)
        self.metrics.covers_seated += p.size
        self.host.on_seated(p)
        self.host.quotes.pop(p.id, None)
        if p.food_order:
            p.state = "eating" if p.food_done else "entrees_fired"
            self.floor.set_state(p, p.state)
            if p.food_done and p.check_at is None:
                p.check_at = t + self.guests.minutes("eat") * 60
        else:
            p.next_at = t + self.guests.minutes("to_order") * 60
        self._tables(p)

    def _tables(self, p: Party) -> None:
        self.bus.publish(E.TABLE_UPDATED, {"tables": [self.floor.table_view(tid, self.clock.t) for tid in p.table_ids],
                                           "party": {"party_id": p.id, "size": p.size, "state": p.state, "kind": p.kind}},
                         source="floor")

    def _seat_waiting(self, t: float) -> None:
        for pid in list(self.floor.waitlist):
            p = self.floor.parties[pid]
            if p.patience_until is not None and t > p.patience_until:
                self.floor.waitlist.remove(pid)
                p.state, p.gave_up = "left", True
                self.metrics.covers_lost += p.size
                self.host.quotes.pop(pid, None)
                self.bus.publish(E.PARTY_LEFT, {"party_id": pid, "size": p.size, "reason": "gave up waiting"}, source="floor")
                self.host.publish()
                continue
            if p.greet_until is not None and t < p.greet_until:
                continue  # still checking in at the host stand
            pref = getattr(p, "prefer_tables", None)
            seats = pref if pref and all(self.floor.free(x) for x in pref) else self.floor.find_seating(p.size)
            if seats:
                self._seat(p, seats, t)
                self.host.publish()

    def _party_timers(self, t: float) -> None:
        for p in list(self.floor.parties.values()):
            if p.state == "seated" and p.next_at is not None and p.next_at <= t and not p.food_order:
                p.next_at = None
                self._place_food_order(p)
            if p.dessert_at is not None and p.dessert_at <= t and p.state == "eating":
                p.dessert_at = None
                items = self.guests.dessert_order(p.size, self.available_map(), set(self.menu.specials))
                if items:
                    self.submit_order("dine_in", items, party=p, kind="dessert")
            if p.state == "eating" and p.check_at is not None and p.check_at <= t:
                p.check_at = None
                p.state = "check"
                c = self.payments.open_for_party(p)
                p.check_id = c.check_id
                p.leave_at = t + self.guests.minutes("pay") * 60
                if p.kind == "manual":
                    p.pay_by = t + 20 * 60  # a hand-placed table pays through the API (or at the terminal after 20 min)
                self.floor.set_state(p, "check")
                self._tables(p)
            elif p.state == "check" and p.leave_at is not None and p.leave_at <= t:
                c = self.payments.checks.get(p.check_id or "")
                if c is not None and c.status == "open":
                    if p.kind == "manual" and (p.pay_by or 0) > t:
                        continue
                    split = 2 if p.size >= 4 and self.guests.aux_chance(0.2) else 1
                    self.payments.settle_sim(c, tip_pct=self.guests.uniform(0.15, 0.22), split_ways=split)
                else:
                    self._party_leaves(p, t)
            elif p.state == "bussing" and p.bus_at is not None and p.bus_at <= t:
                tids = list(p.table_ids)
                for tid in tids:
                    self.floor.release(tid)
                p.state = "left"
                self.bus.publish(E.TABLE_UPDATED, {"tables": [self.floor.table_view(tid, t) for tid in tids]}, source="floor")

    def _party_leaves(self, p: Party, t: float) -> None:
        if p.state != "check":
            return
        p.state = "bussing"
        p.leave_at = None
        p.bus_at = t + self.guests.minutes("bus") * 60
        self.floor.set_state(p, "bussing")
        self.bus.publish(E.PARTY_LEFT, {"party_id": p.id, "size": p.size, "reason": "paid"}, source="floor")
        self._tables(p)

    def on_check_paid(self, c) -> None:
        """Payments closed a check: the party leaves (dine-in) and the guest's visit is on their profile."""
        p = self.floor.parties.get(c.party_id or "")
        if p is not None:
            self._party_leaves(p, self.clock.t)

    def _place_food_order(self, p: Party) -> dict:
        o = self.guests.party_order(p.size, self.available_map(), set(self.menu.specials), large=p.kind == "large")
        return self.submit_order("dine_in", o["items"], notes=o["notes"], party=p, kind="food")

    def walk_in(self, size: int, kind: str = "walk_in", preorder: bool = False) -> dict:
        p = self._arrive(size, kind, self.clock.t, preorder=preorder)
        out = {"party_id": p.id, "seated": p.state != "waiting", "table_ids": p.table_ids}
        if preorder:
            out["order"] = self._place_food_order(p)
        return out

    # ================================================================== orders
    def submit_order(self, channel: str, items: list[dict], notes: str = "", party: Party | None = None, kind: str = "food",
                     customer: str = "", source: str = "sim", guest_id: int | None = None, fulfilment: str = "pickup",
                     zone: str | None = None) -> dict:
        t0 = time.perf_counter()
        if channel == "online":
            self._n_online += 1
            oid = f"W{self._n_online:04d}"
        else:
            self._n_order += 1
            tag = party.table_ids[0] if party and party.table_ids else "HOST"
            oid = f"{tag}-{self._n_order:04d}"
        if party is not None and kind == "food":
            party.food_order = oid
        payload = {"order_id": oid, "channel": channel, "items": items, "notes": notes, "kind": kind,
                   "party_id": party.id if party else None, "party_size": party.size if party else 0,
                   "table_ids": list(party.table_ids) if party else [], "large": bool(party and party.kind == "large"),
                   "customer": customer, "source": source, "t0": t0,
                   "guest_id": guest_id if guest_id is not None else (party.guest_id if party else None),
                   "fulfilment": fulfilment if channel == "online" else None, "zone": zone if channel == "online" else None}
        self.bus.publish(E.ORDER_SUBMITTED, payload, source="online" if channel == "online" else "floor")
        res = self.order_results.get(oid, {"order_id": oid, "items": [], "held": [], "rejected": []})
        if channel == "online":
            o = self.online.orders.get(oid)
            res = {**res, "online": self.online.view(o, self.clock) if o else None}
        return res

    def _on_order_created(self, ev: E.Event) -> None:
        o, t = ev.payload, self.clock.t
        new = [i for i in o["items"] if i["item_id"] not in self.counted_items]
        for i in new:
            self.counted_items.add(i["item_id"])
            self.metrics.revenue += cat.DISHES_BY_ID[i["dish_id"]].price
        if o["order_id"] not in self.order_fire:
            self.order_fire[o["order_id"]] = math.inf
            self.metrics.orders += 1
        if o["channel"] == "online":
            self._online_register(o)
            return
        p = self.floor.parties.get(o.get("party_id") or "")
        if p is None or o.get("kind") != "food":
            return
        if not o["items"] and not o.get("held"):
            p.food_done = True
            if p.state not in ("waiting",):
                p.state = "eating"
                p.check_at = t + 10 * 60
                self.floor.set_state(p, "eating")
                self._tables(p)
            return
        if p.state == "seated":
            p.state = "ordered"
            self.floor.set_state(p, "ordered")
            self._tables(p)

    def _on_item_fired(self, ev: E.Event) -> None:
        it = ev.payload
        meta = self.item_meta.get(it["item_id"], {})
        self.order_fire[it["order_id"]] = min(self.order_fire.get(it["order_id"], math.inf), self.clock.t)
        if it.get("large_party") and not any(o == it["order_id"] for _, o in self.large_fires):
            self.large_fires.append((self.clock.t, it["order_id"]))
        p = self.floor.parties.get(meta.get("party_id") or "")
        if p and meta.get("course") == "main" and p.state == "ordered":
            p.state = "entrees_fired"
            self.floor.set_state(p, "entrees_fired")
            self._tables(p)

    def _on_item_done(self, ev: E.Event) -> None:
        it = ev.payload
        meta = self.item_meta.get(it["item_id"], {})
        p = self.floor.parties.get(meta.get("party_id") or "")
        if p is None or meta.get("channel") != "dine_in" or p.food_order != it["order_id"]:
            return
        self._maybe_food_done(p, it["order_id"])

    def _maybe_food_done(self, p: Party, order_id: str) -> None:
        """When nothing of a party's food order is held or still cooking, the party starts eating."""
        t = self.clock.t
        order = self.orders.get(order_id, {})
        ids = [i["item_id"] for i in order.get("items", [])]
        if p.food_done or order.get("held") or any(self.kitchen.items[i].status != "done" for i in ids if i in self.kitchen.items):
            return
        p.food_done = True
        mains = [self.kitchen.items[i] for i in ids if i in self.kitchen.items and self.item_meta.get(i, {}).get("course") == "main"]
        if mains:
            ticket = (max(m.done_at for m in mains) - min(m.fire_at for m in mains)) / 60
            self.metrics.ticket_min.append(round(ticket, 1))
            if ticket > LATE_TICKET_MIN:
                self.metrics.late_tickets += 1
        if p.state in ("ordered", "entrees_fired", "seated"):
            p.state = "eating"
            p.check_at = t + self.guests.minutes("eat") * 60
            if self.guests.chance(0.6):
                p.dessert_at = t + self.guests.minutes("dessert_after") * 60
            self.floor.set_state(p, "eating")
            self._tables(p)

    def after_item_change(self, order: dict) -> None:
        """An item was swapped or dropped after a manager decision: refresh the online order / the party's progress."""
        if order["channel"] == "online":
            self._online_register(order)
            return
        p = self.floor.parties.get(order.get("party_id") or "")
        if p is not None and p.food_order == order["order_id"]:
            self._maybe_food_done(p, order["order_id"])

    def manual_order(self, table_id: str, items: list[dict], notes: str = "", party_size: int | None = None,
                     guest_id: int | None = None) -> dict:
        """A person clicks a table on the floor and enters an order (seats a party first if the table is free)."""
        if table_id not in self.floor.tables:
            raise ServiceError(f"unknown table {table_id}")
        t = self.floor.tables[table_id]
        p = self.floor.party_of_table(table_id)
        if p is None:
            if t.state != "available":
                raise ServiceError(f"{table_id} is {t.state}")
            self._n_party += 1
            size = int(party_size or min(t.seats, max(1, len(items))))
            p = Party(f"P{self._n_party:03d}", size, "manual", self.clock.t, guest_id=guest_id)
            self.floor.parties[p.id] = p
            self.metrics.parties += 1
            self.metrics.covers_arrived += size
            self.floor.seat(p, [table_id], self.clock.t)
            self.metrics.covers_seated += size
            self._tables(p)
        kind = "food" if not p.food_order else "extra"
        if all(cat.DISHES_BY_ID[i["dish_id"]].category == "dessert" for i in items if i["dish_id"] in cat.DISHES_BY_ID):
            kind = "dessert" if p.food_order else "food"
        if guest_id and not p.guest_id:
            p.guest_id = guest_id
        return self.submit_order("dine_in", items, notes=notes, party=p, kind=kind, source="manual", guest_id=guest_id or p.guest_id)

    # ================================================================== online channel
    def _sim_online_order(self) -> None:
        allowed = self.online.allowed(self.available_map())
        if not allowed:
            self.online.lost += 1
            return
        cart = self.guests.online_cart(allowed, set(self.menu.specials))
        if cart:
            self._n_cust += 1
            delivery = self.guests.aux_chance(settings.DELIVERY_SHARE)
            zone = self.guests.choice(sorted(self.zones)) if delivery and self.zones else None
            self.submit_order("online", cart, customer=f"Guest {self._n_cust}", source="sim",
                              fulfilment="delivery" if delivery else "pickup", zone=zone)

    def online_menu(self) -> dict:
        avail = self.available_map()
        allowed = self.online.allowed(avail)
        rows = []
        for d in self.menu.on_menu():
            v = dict(DISH_VIEW[d])
            v["available"] = d in allowed
            v["eighty_sixed"] = d in self.menu.eighty_sixed
            v["last_n"] = self.menu.last_n.get(d)
            v["special"] = d in self.menu.specials
            v["max_qty"] = 1 if v["last_n"] else 6
            if d in self.menu.eighty_sixed:
                continue  # hidden online at once
            rows.append(v)
        return {"mode": self.online.mode, "reason": self.online.mode_reason, "dishes": rows,
                "zones": self.zones, "taxes": [{k: r[k] for k in ("name", "rate", "applies_to", "channel")} for r in self.tax_rules]}

    def _delivery_extra(self, zone: str, ready_t: float) -> float:
        z = self.dispatcher.zone(zone)
        return self.dispatcher.driver_wait_min(ready_t) + z["drive_min"]

    def quote_preview(self, dish_ids: list[str], fulfilment: str = "pickup", zone: str | None = None) -> dict:
        load = self.kitchen_load()
        q = self.kitchen.quote(dish_ids, self.clock.t, PACK_BUFFER_MIN)
        extra = 0.0
        if fulfilment == "delivery":
            extra = self._delivery_extra(zone or next(iter(self.zones)), self.clock.t + q["p50_min"] * 60)
        p50, p90 = q["p50_min"] + extra, q["p90_min"] + extra * 1.15
        fee = self.dispatcher.zone(zone or next(iter(self.zones)))["fee"] if fulfilment == "delivery" else 0.0
        return {**q, "p50_min": round(p50, 1), "p90_min": round(p90, 1), "kitchen_p50_min": q["p50_min"], "delivery_min": round(extra, 1),
                "range_hhmm": [self.clock.hhmm(self.clock.t + p50 * 60), self.clock.hhmm(self.clock.t + p90 * 60)],
                "busyness": load["busyness"], "explanation": self.order.explain(load), "mode": self.online.mode,
                "fulfilment": fulfilment, "zone": zone, "fee": fee}

    def place_online(self, items: list[dict], customer: str = "Web guest", fulfilment: str = "pickup", zone: str | None = None,
                     phone: str = "") -> dict:
        allowed = self.online.allowed(self.available_map())
        if self.online.mode == "paused":
            raise ServiceError("online ordering is paused: the kitchen is slammed")
        if fulfilment not in ("pickup", "delivery"):
            raise ServiceError("fulfilment must be pickup or delivery")
        if fulfilment == "delivery":
            if not zone:
                raise ServiceError("delivery needs a zone")
            mino = self.zones.get(zone, {}).get("min_order", 0) or 0
            sub = sum(cat.DISHES_BY_ID[i["dish_id"]].price for i in items if i["dish_id"] in cat.DISHES_BY_ID)
            if sub < mino:
                raise ServiceError(f"zone {zone} needs a minimum order of ${mino:.2f}")
            try:
                self.dispatcher.zone(zone)
            except PaymentError as e:
                raise ServiceError(str(e)) from e
        bad = [i["dish_id"] for i in items if i["dish_id"] not in allowed]
        if bad:
            raise ServiceError(f"not available online right now: {', '.join(cat.DISHES_BY_ID[b].name for b in bad if b in cat.DISHES_BY_ID)}")
        guest_id = None
        if phone:
            g = self.crm.by_phone(phone) or self.crm.upsert(customer, phone=phone)
            guest_id = g["guest_id"]
        return self.submit_order("online", items, customer=customer, source="web", guest_id=guest_id, fulfilment=fulfilment,
                                 zone=zone if fulfilment == "delivery" else None)

    def _online_register(self, o: dict) -> None:
        t = self.clock.t
        if o["order_id"] in self.online.orders:
            oo = self.online.orders[o["order_id"]]
            oo.item_ids = [i["item_id"] for i in o["items"]]
            oo.dishes = [i["dish_id"] for i in o["items"]]
            oo.total = round(order_total(oo.dishes) + oo.fee, 2)
            return
        items = [self.kitchen.items[i["item_id"]] for i in o["items"]]
        if not items:
            return
        est = self.kitchen.estimate_items(items, t, PACK_BUFFER_MIN)
        load = self.kitchen_load()
        fulfil = o.get("fulfilment") or "pickup"
        zone = o.get("zone")
        extra = self._delivery_extra(zone, t + est["p50_min"] * 60) if fulfil == "delivery" else 0.0
        fee = self.dispatcher.zone(zone)["fee"] if fulfil == "delivery" else 0.0
        p50, p90 = t + (est["p50_min"] + extra) * 60, t + (est["p90_min"] + extra * 1.15) * 60
        dishes = [i.dish_id for i in items]
        oo = OnlineOrder(o["order_id"], self._customer_of(o), [i.item_id for i in items], dishes,
                         round(order_total(dishes) + fee, 2), t, p50, p90, p50, p90, reason=self.order.explain(load),
                         source=self._source_of(o), fulfilment=fulfil, zone=zone, fee=fee, guest_id=o.get("guest_id"),
                         extra_min=extra)
        oo.check_id = self.payments.open_online(o, fee, o.get("guest_id")).check_id
        self.online.orders[oo.order_id] = oo
        self.metrics.online_orders += 1
        self.conn.execute("INSERT OR REPLACE INTO quote_log (service_id, order_id, placed_sim_ts, quoted_p50, quoted_p90) VALUES (?,?,?,?,?)",
                          (self.service_id, oo.order_id, self.clock.iso(t), self.clock.iso(oo.quote_p50_t), self.clock.iso(oo.quote_p90_t)))
        self.bus.publish(E.ONLINE_ORDER, self.online.view(oo, self.clock), source="online")

    @staticmethod
    def _customer_of(o: dict) -> str:
        return o.get("customer") or "Guest"

    @staticmethod
    def _source_of(o: dict) -> str:
        return o.get("source") or "sim"

    def _quote_closed(self, o, promised_t: float) -> None:
        within = (o.quote_p50_t - 5 * 60) <= promised_t <= (o.quote_p90_t + 30)
        err = (promised_t - o.quote_p50_t) / 60
        self.metrics.quotes.append({"order_id": o.order_id, "within": within, "error_min": round(err, 1), "fulfilment": o.fulfilment})
        self.conn.execute("UPDATE quote_log SET actual_ready=?, within_range=?, error_min=? WHERE service_id=? AND order_id=?",
                          (self.clock.iso(promised_t), int(within), round(err, 2), self.service_id, o.order_id))

    def _online_progress(self, t: float) -> None:
        for o in self.online.active():
            if o.status == "cooking":
                its = [self.kitchen.items[i] for i in o.item_ids if i in self.kitchen.items]
                if its and all(i.status == "done" for i in its) and not self.orders.get(o.order_id, {}).get("held"):
                    ready = max(i.done_at for i in its) + PACK_BUFFER_MIN * 60
                    if t >= ready:
                        o.ready_t = ready
                        if o.fulfilment == "delivery":
                            a = self.dispatcher.assign(o.order_id, ready, o.zone)
                            o.status, o.driver_id, o.depart_t, o.delivered_t = "out_for_delivery", a["driver_id"], a["depart_t"], a["arrive_t"]
                            o.notifications.append({"at": self.clock.hhmm(t), "text": f"Out for delivery with {a['driver_id']}, "
                                                    f"arriving about {self.clock.hhmm(a['arrive_t'])}"})
                        else:
                            o.status = "ready"
                            self._quote_closed(o, ready)
                            o.notifications.append({"at": self.clock.hhmm(t), "text": "Your order is ready for pickup"})
                        self.bus.publish(E.ONLINE_ORDER, self.online.view(o, self.clock), source="online")
            elif o.status == "out_for_delivery" and o.delivered_t is not None and t >= o.delivered_t:
                o.status = "delivered"
                self._quote_closed(o, o.delivered_t)
                o.notifications.append({"at": self.clock.hhmm(o.delivered_t), "text": "Delivered. Enjoy!"})
                self.bus.publish(E.ONLINE_ORDER, self.online.view(o, self.clock), source="online")
            elif o.status == "ready" and o.ready_t is not None and t >= o.ready_t + PICKUP_AFTER_MIN * 60:
                o.status = "picked_up"
                self.bus.publish(E.ONLINE_ORDER, self.online.view(o, self.clock), source="online")

    def _refresh_quotes(self, load: dict) -> None:
        t = self.clock.t
        for o in self.online.active():
            if o.status != "cooking":
                continue
            its = [self.kitchen.items[i] for i in o.item_ids if i in self.kitchen.items]
            est = self.kitchen.estimate_items(its, t, PACK_BUFFER_MIN)
            extra = self._delivery_extra(o.zone, t + est["p50_min"] * 60) if o.fulfilment == "delivery" else 0.0
            o.cur_p50_t, o.cur_p90_t = t + (est["p50_min"] + extra) * 60, t + (est["p90_min"] + extra * 1.15) * 60
            notify = False
            ref = o.notified_p90_t or o.quote_p90_t
            if o.cur_p90_t > ref + NOTIFY_SLIP_MIN * 60 and o.override_t is None:
                o.reason = self.order.explain(load)
                o.notified_p90_t = o.cur_p90_t
                o.notifications.append({"at": self.clock.hhmm(t), "text": f"Running late: now ready "
                                        f"{self.clock.hhmm(o.cur_p50_t)}-{self.clock.hhmm(o.cur_p90_t)} ({o.reason})"})
                notify = True
            view = self.online.view(o, self.clock)
            sig = (tuple(view["current_hhmm"]), view["late"])
            if notify or self._last_quote_view.get(o.order_id) != sig:
                self._last_quote_view[o.order_id] = sig
                self.bus.publish(E.QUOTE_UPDATED, {**view, "notify": notify}, source="order")

    def override_quote(self, order_id: str, ready_hhmm: str) -> dict:
        o = self.online.orders[order_id]
        hh, mm = (int(x) for x in ready_hhmm.split(":"))
        target = datetime.combine(self.date, datetime.min.time()).replace(hour=hh, minute=mm)
        o.override_t = (target - self.clock.start).total_seconds()
        o.notifications.append({"at": self.clock.hhmm(), "text": f"Manager updated your ready time to {ready_hhmm}"})
        v = self.online.view(o, self.clock)
        self.bus.publish(E.QUOTE_UPDATED, {**v, "notify": True, "override": True}, source="manager")
        return v

    def notify_customer(self, order_id: str, text: str = "") -> dict:
        o = self.online.orders[order_id]
        o.notifications.append({"at": self.clock.hhmm(), "text": text or f"Update: ready {self.online.view(o, self.clock)['current_hhmm'][0]}"})
        v = self.online.view(o, self.clock)
        self.bus.publish(E.QUOTE_UPDATED, {**v, "notify": True}, source="manager")
        return v

    def set_online_mode(self, mode: str, reason: str = "manager") -> dict:
        if mode not in ("open", "quick_only", "paused"):
            raise ServiceError("mode must be open, quick_only or paused")
        self.online.mode, self.online.mode_reason = mode, reason
        self.bus.publish(E.ONLINE_MODE, {"mode": mode, "reason": reason}, source="manager" if reason == "manager" else "order")
        return {"mode": mode}

    # ================================================================== handlers (coordinator execute step)
    def _register_handlers(self) -> None:
        h = self.coordinator.handlers
        h["place_po"] = self._h_po
        h["substitute_supplier"] = self._h_po
        h["add_special"] = self._h_special
        h["86_dish"] = self._h_86
        h["discard_lots"] = self._h_discard
        h["discard_lot"] = lambda d: self._h_discard({**d, "payload": {"lots": [d["payload"]]}})
        h["adjust_forecast"] = lambda d: {"recorded": True, "factor": d["payload"]["factor"]}
        h["flag_variance"] = lambda d: {"recorded": True}
        h["throttle_online"] = lambda d: self.set_online_mode(d["payload"]["mode"], d["payload"].get("reason") or d["title"])
        h["allergen_swap"] = lambda d: self.order.apply_swap(d["payload"], approved=True)
        h["comp"] = self._h_comp
        h["add_staff"] = lambda d: {"called_in": d["payload"]["station"], "arrives_in_min": settings.CALL_IN_DELAY_MIN}
        h["release_staff"] = lambda d: self.prep.release(d["payload"])
        h["prep_batch"] = lambda d: self.prep.run_batch(d["payload"])
        h["donate_lots"] = self._h_donate
        h["price_change"] = self._h_price

    def _h_donate(self, d: dict) -> dict:
        self.flush()
        p = d["payload"]
        res = record_donation(self.conn, self.date, p["lots"], p["partner"])
        self.stock.reload(p.get("ingredients") or None)
        self.inventory_dirty = True
        return res

    def _h_price(self, d: dict) -> dict:
        """Price changes take effect from the next service (tonight's checks keep tonight's prices)."""
        p = d["payload"]
        self.conn.execute("UPDATE dishes SET price=? WHERE id=?", (float(p["new_price"]), p["dish_id"]))
        return {"dish_id": p["dish_id"], "new_price": p["new_price"], "effective": "next service"}

    def _h_po(self, d: dict) -> dict:
        self.flush()
        p = d["payload"]
        res = R.send_po(supplier_id=p["supplier_id"], delivery_date=p["delivery_date"], lines=p["lines"], decision_id=d["id"])
        try:  # PDF + e-mail to the supplier (or the outbox); a mail problem never undoes the PO
            res["outbox"] = po_send(self.conn, res["po_id"], self.profile)["status"]
        except Exception as e:  # noqa: BLE001
            res["outbox"] = f"failed: {type(e).__name__}"
        self.bus.publish(E.PO_SENT, {**res, "title": d["title"]}, source="procurement")
        return res

    def _h_special(self, d: dict) -> dict:
        p = d["payload"]
        self.menu.specials[p["dish_id"]] = {"portions": int(p["portions"]), "note": d["title"]}
        self.conn.execute("INSERT OR REPLACE INTO menu_state (date, dish_id, status, note) VALUES (?,?,?,?)",
                          (self.date.isoformat(), p["dish_id"], "special", d["title"][:300]))
        self.bus.publish(E.MENU_UPDATED, self.menu.snapshot(), source="menu")
        return {"special": p["dish_id"], "portions": p["portions"]}

    def _h_86(self, d: dict) -> dict:
        p = d["payload"]
        dish = p["dish_id"]
        after = int(p.get("after_portions") or 0)
        left = self.available_portions(dish)
        if after > 0 and left > after:
            self.menu.limits[dish] = after
            self.inventory_dirty = True
            self.bus.publish(E.MENU_UPDATED, self.menu.snapshot(), source="menu")
            return {"dish_id": dish, "limit": after}
        self._eighty_six(dish, d["title"])
        return {"dish_id": dish, "eighty_sixed": True}

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

    def _h_discard(self, d: dict) -> dict:
        self.flush()
        total, ids = 0.0, []
        for lot in d["payload"]["lots"]:
            lp = lot if isinstance(lot, dict) else {"lot_id": lot}
            row = self.conn.execute("SELECT qty, unit_cost, ingredient_id, status FROM lots WHERE lot_id=?", (lp["lot_id"],)).fetchone()
            if row is None or row["status"] != "active":
                continue
            SUP._exec_discard_lot({"reason": "spoilage", **lp}, d, d["id"], self.date.isoformat())
            total += float(row["qty"]) * float(row["unit_cost"])
            ids.append(lp["lot_id"])
        ings = sorted({r[0] for r in self.conn.execute(
            f"SELECT ingredient_id FROM lots WHERE lot_id IN ({','.join('?' * len(ids))})", ids)}) if ids else []
        self.stock.quarantine.difference_update(ids)
        self.stock.reload(ings)
        self.metrics.waste_value += total
        self.inventory_dirty = True
        return {"discarded": len(ids), "value": round(total, 2)}

    def _h_comp(self, d: dict) -> dict:
        p = d["payload"]
        self.metrics.comps += float(p["amount"])
        o = self.online.orders.get(p["order_id"])
        if o and o.check_id:
            self.payments.comp(o.check_id, float(p["amount"]), p.get("reason", "late order"))
        if o:
            o.notifications.append({"at": self.clock.hhmm(), "text": f"Sorry for the wait: ${p['amount']:.2f} credited"})
            self.bus.publish(E.QUOTE_UPDATED, {**self.online.view(o, self.clock), "notify": True}, source="order")
        return {"comp": p["amount"]}

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

    # ================================================================== analytics / suppliers / reports
    def supplier_scorecard(self) -> list[dict]:
        self.flush()
        rows = SUPL.scorecard(self.conn)
        R.clear_cache()
        return rows

    def learn_supplier(self, supplier_id: str, kind: str, fill: float | None = None, late: bool | None = None, note: str = "") -> dict:
        self.flush()
        res = SUPL.observe(self.conn, supplier_id, self.clock.iso(), kind, fill, late, weight=2.0, note=note)
        R.clear_cache()
        return res

    def tonight_sales(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for o in self.orders.values():
            for i in o["items"]:
                out[i["dish_id"]] = out.get(i["dish_id"], 0) + 1
        return out

    def menu_engineering(self, days: int = 28) -> dict:
        self.flush()
        return ME.analyze(self.conn, self.date, days, self.tonight_sales())

    def waste(self, days: int = 30) -> dict:
        self.flush()
        return waste_analytics(self.conn, self.date, days)

    def kpis(self) -> dict:
        k = self.metrics.kpis(self.forecast)
        k.update({f"pay_{key}": v for key, v in self.payments.summary().items()})
        k["surge_labor"] = round(self.prep.surge_spent, 2) if hasattr(self, "prep") else 0.0
        k["scratch_items"] = self.prep.scratch_items if hasattr(self, "prep") else 0
        k["waitlist"] = len(self.floor.waitlist)
        k["no_shows"] = sum(1 for b in self.host.bookings.values() if b.status == "no_show")
        dq = [q for q in self.metrics.quotes if q.get("fulfilment") == "delivery"]
        k["delivery_accuracy_pct"] = round(100 * sum(q["within"] for q in dq) / len(dq), 1) if dq else None
        return k

    def report(self) -> dict:
        """End-of-service report: KPIs, payments, decisions, waste, forecast error, menu mix."""
        ds = list(self.coordinator.decisions.values())
        by_status: dict[str, int] = {}
        for d in ds:
            by_status[d["status"]] = by_status.get(d["status"], 0) + 1
        sales = self.tonight_sales()
        return {"service": self.info(), "generated": E.wall_now(), "kpis": self.kpis(), "payments": self.payments.summary(),
                "forecast": {"base": self.forecast.base, "revised": self.forecast.revised,
                             "error": getattr(self, "forecast_error", None)},
                "decisions": {"by_status": by_status,
                              "items": [{k: d.get(k) for k in ("id", "created_sim", "agent", "action_type", "title", "status", "note")}
                                        for d in ds]},
                "sales_by_dish": sorted(({"dish_id": k, "name": cat.DISHES_BY_ID[k].name, "qty": v,
                                          "sales": round(v * cat.DISHES_BY_ID[k].price, 2)} for k, v in sales.items()),
                                        key=lambda x: -x["sales"]),
                "waste": self.waste(1), "staffing": self.prep.view(), "bookings": self.host.bookings_view(),
                "safety": {"checklist": self.safety.checklist(), "alerts": self.safety.alerts}}

    def inject(self, name: str) -> dict:
        res = SC.inject(self, name)
        self.flush()
        return res

    def set_speed(self, speed: int) -> dict:
        self.clock.set_speed(speed)
        self.bus.publish(E.CLOCK_TICK, self.clock_view(), source="clock")
        return self.clock_view()

    # ================================================================== views
    def info(self) -> dict:
        return {"service_id": self.service_id, "date": self.date.isoformat(), "weekday": self.date.strftime("%A"),
                "period": self.period, "start": self.clock.start.isoformat(timespec="minutes"),
                "end": self.clock.end.isoformat(timespec="minutes"), "seed": self.seed, "bus": self.bus.kind,
                "llm_mode": "mock" if llm.is_mock() else llm.describe(), "status": self.status,
                "approval_timeout_min": settings.APPROVAL_TIMEOUT_SIM_MIN, "po_threshold": settings.LIVE_PO_APPROVAL_THRESHOLD,
                "auth_enabled": settings.AUTH_ENABLED, "recovered": getattr(self, "recovered", None),
                "restaurant": (getattr(self, "profile", None) or {}).get("name", "Bistro 80"),
                "processor": getattr(getattr(self, "processor", None), "name", "simulated"),
                "reservations": len(self.guests.bookings), "booked_covers": self.guests.booked_covers}

    def clock_view(self) -> dict:
        return {"sim_ts": self.clock.iso(), "hhmm": self.clock.hhmm(), "t": round(self.clock.t, 1), "speed": self.clock.speed,
                "minutes_left": round(self.clock.minutes_left, 1), "status": self.status, **self.floor.occupancy()}

    def inventory_view(self) -> dict:
        t_min = self.clock.t / 60
        rem = self.forecast.dish_remaining(t_min)
        portions = {d: self.available_portions(d) for d in self.menu.on_menu()}
        low = [{"dish_id": d, "name": cat.DISHES_BY_ID[d].name, "remaining": n, "forecast_remaining": rem.get(d, 0.0)}
               for d, n in portions.items() if self.menu.orderable(d) and rem.get(d, 0) >= 2 and n < math.ceil(rem.get(d, 0))]
        return {"portions": portions, "forecast_remaining": rem, "low": sorted(low, key=lambda x: x["remaining"]),
                "expiring": self.stock.expiring(1)[:15], "quarantined": len(self.stock.quarantine),
                "shortages": self.shortages, "food_cost": round(self.metrics.food_cost, 2)}

    def snapshot(self) -> dict:
        t = self.clock.t
        return {"service": self.info(), "clock": self.clock_view(),
                "layout": {"width": PLAN_W, "height": PLAN_H, "fixtures": LAYOUT_FIXTURES},
                "tables": {tid: self.floor.table_view(tid, t) for tid in self.floor.tables},
                "kitchen": self.kitchen_load(), "inventory": self.inventory_view(), "forecast": self.forecast.snapshot(),
                "menu": self.menu.snapshot(),
                "online": {"mode": self.online.mode, "reason": self.online.mode_reason,
                           "orders": {o.order_id: self.online.view(o, self.clock) for o in self.online.orders.values()}},
                "decisions": {str(d["id"]): self.coordinator.view(d) for d in self.coordinator.decisions.values()},
                "kpis": self.kpis(), "feed": list(self.feed), "dishes": DISH_VIEW,
                "host": {"bookings": self.host.bookings_view(), "waitlist": self.host.waitlist_view()},
                "checks": {c.check_id: self.payments.view(c) for c in self.payments.checks.values()
                           if c.status == "open" or (self.clock.t - (c.closed_t or 0)) < 30 * 60},
                "prep": self.prep.view(), "safety": self.safety.view(), "drivers": self.dispatcher.view(self.clock.t),
                "zones": self.zones,
                "scenarios": [{"name": k, "label": v[1]} for k, v in SC.SCENARIOS.items()]}

    def _publish_state(self) -> None:
        self.bus.publish(E.KITCHEN_LOAD, self.kitchen_load(), source="kitchen")
        self.bus.publish(E.INVENTORY_SNAPSHOT, self.inventory_view(), source="inventory")
        self.bus.publish(E.MENU_UPDATED, self.menu.snapshot(), source="menu")
        self.bus.publish(E.KPI_UPDATED, self.kpis(), source="service")
        self.bus.publish(E.CLOCK_TICK, self.clock_view(), source="clock")

    def _to_feed(self, ev: E.Event) -> None:
        if ev.type in FEED_SKIP and not (ev.type == E.QUOTE_UPDATED and ev.payload.get("notify")):
            return
        self.feed.append(feed_entry(ev))


def feed_entry(ev: E.Event) -> dict:
    p = ev.payload
    keep = {k: p[k] for k in list(p)[:14] if not isinstance(p[k], (list, dict)) or k in ("items", "held", "table_ids", "violations")}
    if ev.type in (E.DECISION_PROPOSED, E.DECISION_RESOLVED):
        keep = {k: p.get(k) for k in ("id", "agent", "action_type", "title", "status", "note", "needs_approval")}
    if ev.type == E.ORDER_CREATED:
        keep = {"order_id": p["order_id"], "channel": p["channel"], "table_ids": p.get("table_ids"), "n_items": len(p["items"]),
                "stations": p.get("stations"), "allergens": p.get("allergens"), "held": len(p.get("held", [])),
                "latency_ms": p.get("latency_ms"), "kind": p.get("kind")}
    return {"seq": ev.seq, "type": ev.type, "source": ev.source, "sim_ts": ev.sim_ts, "payload": keep}
