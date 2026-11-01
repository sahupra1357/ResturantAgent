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
from datetime import datetime
from pathlib import Path
from typing import Callable

from restaurant_ops.agents import decision_log as DL
from restaurant_ops.agents import llm
from restaurant_ops.agents import supervisor as SUP
from restaurant_ops.config import settings, sim_start_from_db
from restaurant_ops.core import prep
from restaurant_ops.data import catalog as cat
from restaurant_ops.db.connection import copy_db, get_conn, init_db
from restaurant_ops.live import events as E
from restaurant_ops.live import scenarios as SC
from restaurant_ops.live.agents.forecasting import ForecastingAgent
from restaurant_ops.live.agents.inventory import InventoryAgent
from restaurant_ops.live.agents.menu import MenuAgent
from restaurant_ops.live.agents.order import OrderAgent
from restaurant_ops.live.agents.procurement import ProcurementAgent
from restaurant_ops.live.bus import make_bus
from restaurant_ops.live.clock import ServiceClock
from restaurant_ops.live.coordinator import Coordinator
from restaurant_ops.live.floor import LAYOUT_FIXTURES, Floor, Party
from restaurant_ops.live.guests import GuestSim
from restaurant_ops.live.intraday import IntradayForecast
from restaurant_ops.live.kitchen import PACK_BUFFER_MIN, Kitchen, cook_minutes, is_quick
from restaurant_ops.live.menu_live import LiveMenu
from restaurant_ops.live.metrics import Metrics
from restaurant_ops.live.online import NOTIFY_SLIP_MIN, PICKUP_AFTER_MIN, OnlineBook, OnlineOrder, order_total
from restaurant_ops.live.stock import LiveStock
from restaurant_ops.live.tools import ToolBox
from restaurant_ops.tools import registry as R

log = logging.getLogger(__name__)
STEP_S = 15.0
LAST_SEATING_BEFORE_END_MIN = 60
LAST_ONLINE_BEFORE_END_MIN = 30
CAPACITY_30 = 20.0
LATE_TICKET_MIN = 25.0
BAR_SHARE = 0.35
FEED_SKIP = {E.CLOCK_TICK, E.KITCHEN_LOAD, E.INVENTORY_SNAPSHOT, E.KPI_UPDATED, E.TABLE_UPDATED, E.QUOTE_UPDATED,
             E.MENU_UPDATED, E.ONLINE_ORDER, E.ITEM_FIRED, E.ITEM_DONE, E.ORDER_SUBMITTED, E.SERVICE_STARTED,
             E.FORECAST_UPDATED}
DISH_VIEW = {d.id: {"dish_id": d.id, "name": d.name, "category": d.category, "price": d.price, "station": d.station,
                    "cook_min": cook_minutes(d.id), "quick": is_quick(d.id), "allergens": cat.dish_allergens(d.id)}
             for d in cat.DISHES}

Policy = Callable[[dict], "tuple | None"]


class ServiceError(RuntimeError):
    pass


class Service:
    def __init__(self, seed: int | None = None, speed: int = 60, bus_kind: str | None = None, source_db: str | Path | None = None,
                 service_id: str | None = None, auto_policy: Policy | None = None, period: str | None = None):
        src = Path(source_db or settings.DB_PATH)
        if not src.exists():
            raise ServiceError(f"{src} does not exist: seed it first (python -m restaurant_ops.data.seed)")
        self.seed = settings.SEED if seed is None else int(seed)
        self.date = sim_start_from_db(src) or settings.SIM_START
        self.period = period or settings.LIVE_PERIOD
        self.service_id = service_id or f"svc-{self.date:%Y%m%d}-{datetime.now():%H%M%S}-{uuid.uuid4().hex[:4]}"
        settings.LIVE_DIR.mkdir(parents=True, exist_ok=True)
        self.db_path = settings.LIVE_DIR / f"{self.service_id}.db"
        copy_db(src, self.db_path)
        self.conn = get_conn(self.db_path)
        self.conn.execute("PRAGMA synchronous = NORMAL")
        init_db(self.conn)
        R.set_db_path(self.db_path)
        R.set_sim_date(self.date)
        self.clock = ServiceClock(self.date, self.period, speed)
        self.bus = make_bus(self.service_id, self.conn, clock=lambda: self.clock.iso(), kind=bus_kind)
        self.conn.execute("INSERT INTO service_runs (service_id, service_date, meal_period, seed, started_ts, bus, llm_mode) "
                          "VALUES (?,?,?,?,?,?,?)", (self.service_id, self.date.isoformat(), self.period, self.seed,
                                                     E.wall_now(), self.bus.kind, "mock" if llm.is_mock() else llm.describe()))
        self.conn.commit()
        self.auto_policy = auto_policy
        # world
        staff = prep.station_staff(self.conn, self.date.isoformat(), self.period)
        self.kitchen = Kitchen(staff, self.seed)
        self.stock = LiveStock(self.conn, self.date)
        self.forecast = IntradayForecast(self.date, self.period)
        self.menu = LiveMenu(self.date)
        self.floor = Floor()
        self.online = OnlineBook()
        self.metrics = Metrics()
        res = [dict(r) for r in self.conn.execute("SELECT party_size, kind FROM reservations WHERE date=? AND meal_period=? "
                                                  "ORDER BY id", (self.date.isoformat(), self.period))]
        self.guests = GuestSim(self.seed, self.forecast, res, self.forecast.curve)
        self.tools = ToolBox(self)
        # bookkeeping
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
        self.pending_llm: list[dict] = []
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="llm")
        self.feed: deque = deque(maxlen=250)
        self.status = "ready"
        self._last_quote_view: dict[str, tuple] = {}
        self.schedule: list[tuple[float, str]] = []  # (t_min, scenario) for headless runs
        # coordinator + agents
        self.coordinator = Coordinator(self)
        self.order = OrderAgent(self)
        self.inventory = InventoryAgent(self)
        self.forecasting = ForecastingAgent(self)
        self.procurement = ProcurementAgent(self)
        self.menu_agent = MenuAgent(self)
        self.agents = {a.name: a for a in (self.order, self.inventory, self.forecasting, self.procurement, self.menu_agent)}
        self._register_handlers()
        self.bus.subscribe(E.ORDER_CREATED, self._on_order_created, "service.order_created")
        self.bus.subscribe(E.ITEM_FIRED, self._on_item_fired, "service.item_fired")
        self.bus.subscribe(E.ITEM_DONE, self._on_item_done, "service.item_done")
        self.bus.subscribe("*", self._to_feed, "service.feed")

    # ================================================================== lifecycle
    def start(self) -> None:
        if self.status != "ready":
            return
        self.bus.publish(E.SERVICE_STARTED, {"service": self.info(), "snapshot": self.snapshot()})
        self.status = "running"
        self.inventory.start_of_service()
        self.forecasting.nightly()
        self.forecasting.publish("service start: model forecast for tonight")
        self._publish_state()
        self.flush()

    def advance(self, wall_dt: float) -> None:
        """Move the service forward by wall_dt real seconds at the clock speed (no-op while paused or finished)."""
        self._llm()
        if self.status != "running" or self.clock.speed == 0 or wall_dt <= 0:
            return
        sim = wall_dt * self.clock.speed
        while sim > 1e-9 and self.status == "running":
            dt = min(STEP_S, sim)
            self._step(dt)
            sim -= dt
        self.coordinator.tick(wall_dt)
        self.flush()

    def run_headless(self, until_end: bool = True, minutes: float | None = None, wall_step: float = 0.25) -> dict:
        """Run without wall-clock sleeps (tests, CLI). Approval timeouts still count wall time at the clock speed."""
        if self.status == "ready":
            self.start()
        target = self.clock.t + minutes * 60 if minutes is not None else math.inf
        while self.status == "running" and self.clock.t < target:
            self.advance(wall_step)
        return self.metrics.kpis(self.forecast)

    def end(self) -> None:
        if self.status == "ended":
            return
        self.status = "ended"
        fc = self.forecast.base
        actual = self.metrics.covers_arrived
        err = {"forecast_p50": fc.get("p50"), "actual": actual,
               "ape_pct": round(abs(actual - fc["p50"]) / max(actual, 1) * 100, 1) if fc.get("p50") else None,
               "in_p10_p90": bool(fc.get("p10", 0) <= actual <= fc.get("p90", 0)) if fc else None}
        self.bus.publish(E.SERVICE_ENDED, {"kpis": self.metrics.kpis(self.forecast), "forecast_error": err})
        self.forecasting.nightly()
        self.conn.execute("UPDATE service_runs SET status='ended' WHERE service_id=?", (self.service_id,))
        self.flush()

    def close(self) -> None:
        self.flush()
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.bus.close()
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
        if self.inventory_dirty:
            self.inventory_dirty = False
            if self.menu_agent.refresh_flags():
                self.bus.publish(E.MENU_UPDATED, self.menu.snapshot(), source="menu")
            self.bus.publish(E.INVENTORY_SNAPSHOT, self.inventory_view(), source="inventory")
        if m % 2 == 0:
            self.bus.publish(E.KPI_UPDATED, self.metrics.kpis(self.forecast), source="service")

    def _llm(self) -> None:
        now = time.monotonic()
        for entry in list(self.pending_llm):
            fut = entry["future"]
            if fut.done() or now > entry["deadline"]:
                self.pending_llm.remove(entry)
                entry["agent"].finish_review(entry["proposals"], fut if fut.done() else None)

    # ================================================================== floor
    def _arrive(self, size: int, kind: str, t: float, preorder: bool = False) -> Party:
        self._n_party += 1
        p = Party(f"P{self._n_party:03d}", int(size), kind, t, preorder=preorder)
        self.floor.parties[p.id] = p
        self.metrics.parties += 1
        self.metrics.covers_arrived += p.size
        seats = self.floor.find_seating(p.size, prefer_bar=kind == "walk_in" and p.size <= 2 and self.guests.chance(BAR_SHARE))
        if seats:
            self._seat(p, seats, self.clock.t)
        else:
            p.patience_until = None if preorder else self.clock.t + self.guests.minutes("patience") * 60
            self.floor.waitlist.append(p.id)
        self.bus.publish(E.PARTY_ARRIVED, {"party_id": p.id, "size": p.size, "kind": kind, "seated": bool(seats),
                                           "table_ids": p.table_ids, "waitlist": len(self.floor.waitlist)}, source="floor")
        return p

    def _seat(self, p: Party, seats: list[str], t: float) -> None:
        self.floor.seat(p, seats, t)
        self.metrics.covers_seated += p.size
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
                self.bus.publish(E.PARTY_LEFT, {"party_id": pid, "size": p.size, "reason": "gave up waiting"}, source="floor")
                continue
            seats = self.floor.find_seating(p.size)
            if seats:
                self._seat(p, seats, t)

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
                p.leave_at = t + self.guests.minutes("pay") * 60
                self.floor.set_state(p, "check")
                self._tables(p)
            elif p.state == "check" and p.leave_at is not None and p.leave_at <= t:
                p.state = "bussing"
                p.bus_at = t + self.guests.minutes("bus") * 60
                self.floor.set_state(p, "bussing")
                self.bus.publish(E.PARTY_LEFT, {"party_id": p.id, "size": p.size, "reason": "paid"}, source="floor")
                self._tables(p)
            elif p.state == "bussing" and p.bus_at is not None and p.bus_at <= t:
                tids = list(p.table_ids)
                for tid in tids:
                    self.floor.release(tid)
                p.state = "left"
                self.bus.publish(E.TABLE_UPDATED, {"tables": [self.floor.table_view(tid, t) for tid in tids]}, source="floor")

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
                     customer: str = "", source: str = "sim") -> dict:
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
                   "customer": customer, "source": source, "t0": t0}
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

    def manual_order(self, table_id: str, items: list[dict], notes: str = "", party_size: int | None = None) -> dict:
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
            p = Party(f"P{self._n_party:03d}", size, "manual", self.clock.t)
            self.floor.parties[p.id] = p
            self.metrics.parties += 1
            self.metrics.covers_arrived += size
            self.floor.seat(p, [table_id], self.clock.t)
            self.metrics.covers_seated += size
            self._tables(p)
        kind = "food" if not p.food_order else "extra"
        if all(cat.DISHES_BY_ID[i["dish_id"]].category == "dessert" for i in items if i["dish_id"] in cat.DISHES_BY_ID):
            kind = "dessert" if p.food_order else "food"
        return self.submit_order("dine_in", items, notes=notes, party=p, kind=kind, source="manual")

    # ================================================================== online channel
    def _sim_online_order(self) -> None:
        allowed = self.online.allowed(self.available_map())
        if not allowed:
            self.online.lost += 1
            return
        cart = self.guests.online_cart(allowed, set(self.menu.specials))
        if cart:
            self._n_cust += 1
            self.submit_order("online", cart, customer=f"Guest {self._n_cust}", source="sim")

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
        return {"mode": self.online.mode, "reason": self.online.mode_reason, "dishes": rows}

    def quote_preview(self, dish_ids: list[str]) -> dict:
        load = self.kitchen_load()
        q = self.kitchen.quote(dish_ids, self.clock.t, PACK_BUFFER_MIN)
        return {**q, "range_hhmm": [self.clock.hhmm(self.clock.t + q["p50_min"] * 60), self.clock.hhmm(self.clock.t + q["p90_min"] * 60)],
                "busyness": load["busyness"], "explanation": self.order.explain(load), "mode": self.online.mode}

    def place_online(self, items: list[dict], customer: str = "Web guest") -> dict:
        allowed = self.online.allowed(self.available_map())
        if self.online.mode == "paused":
            raise ServiceError("online ordering is paused: the kitchen is slammed")
        bad = [i["dish_id"] for i in items if i["dish_id"] not in allowed]
        if bad:
            raise ServiceError(f"not available online right now: {', '.join(cat.DISHES_BY_ID[b].name for b in bad if b in cat.DISHES_BY_ID)}")
        return self.submit_order("online", items, customer=customer, source="web")

    def _online_register(self, o: dict) -> None:
        t = self.clock.t
        if o["order_id"] in self.online.orders:
            oo = self.online.orders[o["order_id"]]
            oo.item_ids = [i["item_id"] for i in o["items"]]
            oo.dishes = [i["dish_id"] for i in o["items"]]
            oo.total = order_total(oo.dishes)
            return
        items = [self.kitchen.items[i["item_id"]] for i in o["items"]]
        if not items:
            return
        est = self.kitchen.estimate_items(items, t, PACK_BUFFER_MIN)
        load = self.kitchen_load()
        oo = OnlineOrder(o["order_id"], self._customer_of(o), [i.item_id for i in items], [i.dish_id for i in items],
                         order_total([i.dish_id for i in items]), t, t + est["p50_min"] * 60, t + est["p90_min"] * 60,
                         t + est["p50_min"] * 60, t + est["p90_min"] * 60, reason=self.order.explain(load),
                         source=self._source_of(o))
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

    def _online_progress(self, t: float) -> None:
        for o in self.online.active():
            if o.status == "cooking":
                its = [self.kitchen.items[i] for i in o.item_ids if i in self.kitchen.items]
                if its and all(i.status == "done" for i in its) and not self.orders.get(o.order_id, {}).get("held"):
                    ready = max(i.done_at for i in its) + PACK_BUFFER_MIN * 60
                    if t >= ready:
                        o.status, o.ready_t = "ready", ready
                        within = (o.quote_p50_t - 5 * 60) <= ready <= (o.quote_p90_t + 30)
                        err = (ready - o.quote_p50_t) / 60
                        self.metrics.quotes.append({"order_id": o.order_id, "within": within, "error_min": round(err, 1)})
                        self.conn.execute("UPDATE quote_log SET actual_ready=?, within_range=?, error_min=? WHERE service_id=? AND order_id=?",
                                          (self.clock.iso(ready), int(within), round(err, 2), self.service_id, o.order_id))
                        o.notifications.append({"at": self.clock.hhmm(t), "text": "Your order is ready for pickup"})
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
            o.cur_p50_t, o.cur_p90_t = t + est["p50_min"] * 60, t + est["p90_min"] * 60
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

    def _h_po(self, d: dict) -> dict:
        self.flush()
        p = d["payload"]
        res = R.send_po(supplier_id=p["supplier_id"], delivery_date=p["delivery_date"], lines=p["lines"], decision_id=d["id"])
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
                "approval_timeout_s": settings.APPROVAL_TIMEOUT_S, "po_threshold": settings.LIVE_PO_APPROVAL_THRESHOLD,
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
                "layout": {"width": 1000, "height": 640, "fixtures": LAYOUT_FIXTURES},
                "tables": {tid: self.floor.table_view(tid, t) for tid in self.floor.tables},
                "kitchen": self.kitchen_load(), "inventory": self.inventory_view(), "forecast": self.forecast.snapshot(),
                "menu": self.menu.snapshot(),
                "online": {"mode": self.online.mode, "reason": self.online.mode_reason,
                           "orders": {o.order_id: self.online.view(o, self.clock) for o in self.online.orders.values()}},
                "decisions": {str(d["id"]): self.coordinator.view(d) for d in self.coordinator.decisions.values()},
                "kpis": self.metrics.kpis(self.forecast), "feed": list(self.feed), "dishes": DISH_VIEW,
                "scenarios": [{"name": k, "label": v[1]} for k, v in SC.SCENARIOS.items()]}

    def _publish_state(self) -> None:
        self.bus.publish(E.KITCHEN_LOAD, self.kitchen_load(), source="kitchen")
        self.bus.publish(E.INVENTORY_SNAPSHOT, self.inventory_view(), source="inventory")
        self.bus.publish(E.MENU_UPDATED, self.menu.snapshot(), source="menu")
        self.bus.publish(E.KPI_UPDATED, self.metrics.kpis(self.forecast), source="service")
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
