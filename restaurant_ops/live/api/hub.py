"""The running service, its WebSocket clients (staff and public) and the clock loop; the public view of the state."""
from __future__ import annotations

import asyncio
import logging
import time

from restaurant_ops.config import settings
from restaurant_ops.live import events as E
from restaurant_ops.live import persistence as PERSIST
from restaurant_ops.live.events import Event
from restaurant_ops.live.service import Service

log = logging.getLogger("restaurant_ops.live")

TICK_S = 0.1
QUEUE_MAX = 5000
PUBLIC_TYPES = {E.MENU_UPDATED, E.INVENTORY_SNAPSHOT, E.KITCHEN_LOAD, E.ONLINE_MODE, E.CLOCK_TICK, E.ONLINE_ORDER, E.QUOTE_UPDATED,
                E.SERVICE_STARTED}


# =========================================================================== public (online page) view


def public_order(o: dict) -> dict:
    return {k: v for k, v in o.items() if k not in ("customer", "guest_id", "check_id", "source")}


def public_snapshot(s: Service) -> dict:
    snap = s.snapshot()
    k = snap["kitchen"]
    return {"public": True, "ready": True,
            "service": {k2: snap["service"].get(k2) for k2 in ("restaurant", "date", "weekday", "period", "status")},
            "clock": {k2: snap["clock"].get(k2) for k2 in ("hhmm", "speed", "status")}, "dishes": snap["dishes"], "menu": snap["menu"],
            "inventory": {"portions": snap["inventory"]["portions"]},
            "kitchen": {"busyness": k["busyness"], "explanation": k["explanation"], "score": k["score"]},
            "online": {"mode": snap["online"]["mode"], "reason": snap["online"]["reason"],
                       "orders": {oid: public_order(o) for oid, o in snap["online"]["orders"].items()}},
            "zones": snap["zones"], "decisions": {}, "tables": {}, "feed": []}


def public_event(ev: dict, s: Service) -> dict | None:
    t, p = ev["type"], ev["payload"]
    if t not in PUBLIC_TYPES:
        return None
    if t == E.SERVICE_STARTED:
        p = {"snapshot": public_snapshot(s)}
    elif t == E.INVENTORY_SNAPSHOT:
        p = {"portions": p.get("portions", {})}
    elif t == E.KITCHEN_LOAD:
        p = {"busyness": p["busyness"], "explanation": p.get("explanation", ""), "score": p["score"]}
    elif t == E.CLOCK_TICK:
        p = {k: p.get(k) for k in ("hhmm", "speed", "status")}
    elif t in (E.ONLINE_ORDER, E.QUOTE_UPDATED):
        p = public_order(p)
    return {**ev, "payload": p}


# =========================================================================== hub


class Hub:
    """Holds the running service, the WebSocket clients (staff and public) and the clock loop."""

    def __init__(self):
        self.svc: Service | None = None
        self.clients: set[asyncio.Queue] = set()
        self.public: set[asyncio.Queue] = set()
        self.task: asyncio.Task | None = None
        self.error: str = ""
        self.started = time.time()
        self.dropped = 0
        self.bootstrap: dict = {}

    def _attach(self, svc: Service) -> Service:
        if self.svc is not None and self.svc is not svc:
            try:
                self.svc.close()
            except Exception:  # noqa: BLE001
                pass
        svc.bus.subscribe("*", self.broadcast, "hub.broadcast")
        self.svc = svc
        self.error = ""
        return svc

    def _ensure_seeded(self) -> None:
        if not settings.DB_PATH.exists():
            from restaurant_ops.data import seed as seed_mod

            log.warning("no seeded database at %s: seeding now (first run trains the forecast models)", settings.DB_PATH)
            seed_mod.main([])

    def start_service(self, seed: int | None = None, speed: int = 10, bus: str | None = None, period: str | None = None,
                      service_date: str | None = None) -> Service:
        if self.svc is not None:
            try:
                self.svc.save_state()
                self.svc.close()
            except Exception:  # noqa: BLE001
                pass
            self.svc = None
        self._ensure_seeded()
        svc = self._attach(Service(seed=seed, speed=speed, bus_kind=bus, period=period, service_date=service_date))
        svc.start()
        self.broadcast_snapshot()
        log.info("service started", extra={"service_id": svc.service_id, "bus": svc.bus.kind, "period": svc.period})
        return svc

    def resume_service(self, service_id: str, bus: str | None = None) -> Service:
        if self.svc is not None:
            self.svc.close()
            self.svc = None
        try:
            svc = Service.resume(service_id, bus_kind=bus)
            how = "snapshot"
        except (PERSIST.SnapshotError, FileNotFoundError, ValueError) as e:
            log.warning("snapshot restore failed (%s); recovering from the event log", e)
            svc = Service.recover_from_log(service_id, bus_kind=bus)
            how = "event log"
        self._attach(svc)
        self.broadcast_snapshot()
        log.info("service resumed", extra={"service_id": service_id, "from": how, "pending": len(svc.coordinator.pending())})
        return svc

    def broadcast_snapshot(self) -> None:
        if self.svc is None:
            return
        self.broadcast_raw({"type": "snapshot", "payload": self.svc.snapshot()})
        pub = {"type": "snapshot", "payload": public_snapshot(self.svc)}
        for q in list(self.public):
            q.put_nowait(pub)

    def broadcast(self, ev: Event) -> None:
        d = ev.to_dict()
        self.broadcast_raw(d)
        if self.public and self.svc is not None:
            p = public_event(d, self.svc)
            if p is not None:
                for q in list(self.public):
                    if q.qsize() < QUEUE_MAX:
                        q.put_nowait(p)

    def broadcast_raw(self, msg: dict) -> None:
        for q in list(self.clients):
            if q.qsize() < QUEUE_MAX:
                q.put_nowait(msg)
            else:
                self.dropped += 1

    async def loop(self) -> None:
        last = time.monotonic()
        while True:
            await asyncio.sleep(TICK_S)
            now = time.monotonic()
            dt, last = now - last, now
            if self.svc is not None:
                try:
                    self.svc.advance(dt)
                except Exception as e:  # noqa: BLE001
                    log.exception("service step failed")
                    self.error = f"{type(e).__name__}: {e}"


hub = Hub()
