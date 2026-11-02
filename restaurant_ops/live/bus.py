"""Event bus: Redis Streams when reachable, an in-process queue otherwise. Same interface either way.

publish() appends the event to the append-only `event_log` table (SQLite, the replay source of truth) and to the
transport (Redis stream `ro:events:{service_id}` or an in-memory deque), then drains the transport to subscribers in
order. Handlers that publish while being dispatched do not recurse: their events are queued and dispatched after
the current one, so every subscriber sees events in sequence order. Dispatch is synchronous on the caller's thread,
which keeps the hot path deterministic (an order reaches its stations in microseconds) and makes seeded runs exact.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from collections import defaultdict, deque
from typing import Callable

from restaurant_ops.config import settings
from restaurant_ops.live.events import Event

log = logging.getLogger(__name__)
Handler = Callable[[Event], None]


class EventBus:
    """Base bus: subscriptions, sequencing, event log, non-recursive dispatch. Subclasses provide the transport."""

    kind = "memory"

    def __init__(self, service_id: str, conn: sqlite3.Connection | None = None, clock: Callable[[], str] | None = None):
        self.service_id = service_id
        self.conn = conn
        self.clock = clock or (lambda: "")
        self.seq = 0
        self._subs: dict[str, list[tuple[str, Handler]]] = defaultdict(list)
        self._any: list[tuple[str, Handler]] = []
        self._dispatching = False
        self.errors: list[str] = []

    # ------------------------------------------------------------------ subscriptions
    def subscribe(self, types: "str | list[str]", handler: Handler, name: str = "") -> None:
        """Subscribe to one or more event types; '*' = every event."""
        for t in [types] if isinstance(types, str) else types:
            (self._any if t == "*" else self._subs[t]).append((name or getattr(handler, "__qualname__", "handler"), handler))

    # ------------------------------------------------------------------ publish
    def publish(self, type_: str, payload: dict | None = None, source: str = "service") -> Event:
        self.seq += 1
        ev = Event(type=type_, payload=payload or {}, source=source, sim_ts=self.clock(), seq=self.seq)
        self._log(ev)
        self._push(ev)
        if not self._dispatching:
            self._drain()
        return ev

    def _log(self, ev: Event) -> None:
        if self.conn is None:
            return
        self.conn.execute("INSERT INTO event_log (service_id, seq, wall_ts, sim_ts, type, source, payload) VALUES (?,?,?,?,?,?,?)",
                          (self.service_id, ev.seq, ev.wall_ts, ev.sim_ts, ev.type, ev.source, json.dumps(ev.payload, default=str)))

    def _drain(self) -> None:
        self._dispatching = True
        try:
            while True:
                ev = self._pop()
                if ev is None:
                    break
                self._dispatch(ev)
        finally:
            self._dispatching = False

    def _dispatch(self, ev: Event) -> None:
        for name, h in [*self._subs.get(ev.type, ()), *self._any]:
            try:
                h(ev)
            except Exception as e:  # noqa: BLE001  one bad subscriber must not stop the floor
                msg = f"{name} failed on {ev.type}#{ev.seq}: {type(e).__name__}: {e}"
                log.exception(msg)
                self.errors.append(msg)

    # ------------------------------------------------------------------ transport (in-process)
    def _push(self, ev: Event) -> None:
        self._q.append(ev)

    def _pop(self) -> Event | None:
        return self._q.popleft() if self._q else None

    @property
    def _q(self) -> deque:
        if not hasattr(self, "_queue"):
            self._queue: deque = deque()
        return self._queue

    def close(self) -> None:
        pass


class InProcessBus(EventBus):
    kind = "memory"


class RedisStreamsBus(EventBus):
    """Transport = a Redis stream. publish XADDs; drain XREADs from the last id seen and dispatches in order.

    External consumers (another process, a real POS bridge) can read the same stream with XREAD / consumer groups.
    If Redis fails mid-service the bus degrades to the in-process queue (kind 'redis-degraded') and keeps dispatching.
    """

    kind = "redis"
    MAXLEN = 200_000

    def __init__(self, service_id: str, client, conn: sqlite3.Connection | None = None, clock=None):
        super().__init__(service_id, conn, clock)
        self.r = client
        self.stream = f"ro:events:{service_id}"
        self.last_id = "0-0"
        self.degraded = False

    def _push(self, ev: Event) -> None:
        if self.degraded:
            self._q.append(ev)
            return
        try:
            self.r.xadd(self.stream, {"e": json.dumps(ev.to_dict(), default=str)}, maxlen=self.MAXLEN, approximate=True)
        except Exception as e:  # noqa: BLE001  Redis went away: keep the floor running on the in-process queue
            self._degrade(e)
            self._q.append(ev)

    def _pop(self) -> Event | None:
        if self.degraded:
            return self._q.popleft() if self._q else None
        try:
            got = self.r.xread({self.stream: self.last_id}, count=1)
        except Exception as e:  # noqa: BLE001
            self._degrade(e)
            return self._q.popleft() if self._q else None
        if not got:
            return None
        _, entries = got[0]
        if not entries:
            return None
        eid, fields = entries[0]
        self.last_id = eid if isinstance(eid, str) else eid.decode()
        raw = fields.get("e") if "e" in fields else fields.get(b"e")
        return Event.from_dict(json.loads(raw if isinstance(raw, str) else raw.decode()))

    def _degrade(self, err: Exception) -> None:
        """Switch to the in-process queue. Events stay in event_log (SQLite), so replay and the UI are unaffected."""
        self.degraded = True
        self.kind = "redis-degraded"
        msg = f"Redis unavailable ({type(err).__name__}: {err}); continuing on the in-process queue"
        log.error(msg)
        self.errors.append(msg)

    def close(self) -> None:
        try:
            self.r.close()
        except Exception:  # noqa: BLE001
            pass


def redis_client(url: str | None = None, timeout: float = 0.3):
    """A connected Redis client, or None when redis-py is missing or the server does not answer."""
    try:
        import redis
    except ImportError:
        return None
    try:
        c = redis.Redis.from_url(url or settings.REDIS_URL, socket_connect_timeout=timeout, socket_timeout=2.0,
                                 decode_responses=True)
        c.ping()
        return c
    except Exception:  # noqa: BLE001
        return None


def make_bus(service_id: str, conn: sqlite3.Connection | None = None, clock=None, kind: str | None = None) -> EventBus:
    """settings.EVENT_BUS: 'redis' (required), 'memory', or 'auto' (Redis if reachable, else in-process)."""
    kind = (kind or settings.EVENT_BUS or "auto").lower()
    if kind in ("redis", "auto"):
        c = redis_client()
        if c is not None:
            return RedisStreamsBus(service_id, c, conn, clock)
        if kind == "redis":
            raise RuntimeError(f"EVENT_BUS=redis but Redis at {settings.REDIS_URL} is not reachable")
    return InProcessBus(service_id, conn, clock)
