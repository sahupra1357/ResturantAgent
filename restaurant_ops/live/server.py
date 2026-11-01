"""FastAPI backend for the live service: REST for actions, one WebSocket that streams every bus event, a replay
WebSocket, and the built React app (frontend/dist) at /.

    python -m restaurant_ops.live.server          # http://127.0.0.1:8000
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from restaurant_ops.config import PROJECT_ROOT, settings
from restaurant_ops.live import replay as RP
from restaurant_ops.live.events import Event
from restaurant_ops.live.service import Service, ServiceError

log = logging.getLogger("restaurant_ops.live")
DIST = PROJECT_ROOT / "frontend" / "dist"
TICK_S = 0.1
QUEUE_MAX = 5000


class Hub:
    """Holds the running service, the WebSocket clients and the clock loop."""

    def __init__(self):
        self.svc: Service | None = None
        self.clients: set[asyncio.Queue] = set()
        self.task: asyncio.Task | None = None
        self.error: str = ""

    def start_service(self, seed: int | None = None, speed: int = 10, bus: str | None = None) -> Service:
        if self.svc is not None:
            try:
                self.svc.close()
            except Exception:  # noqa: BLE001
                pass
        if not settings.DB_PATH.exists():
            from restaurant_ops.data import seed as seed_mod

            log.warning("no seeded database at %s: seeding now (first run trains the forecast models)", settings.DB_PATH)
            seed_mod.main([])
        svc = Service(seed=seed, speed=speed, bus_kind=bus)
        svc.bus.subscribe("*", self.broadcast, "hub.broadcast")
        self.svc = svc
        svc.start()
        self.broadcast_raw({"type": "snapshot", "payload": svc.snapshot()})
        return svc

    def broadcast(self, ev: Event) -> None:
        self.broadcast_raw(ev.to_dict())

    def broadcast_raw(self, msg: dict) -> None:
        for q in list(self.clients):
            if q.qsize() < QUEUE_MAX:
                q.put_nowait(msg)

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        hub.start_service()
    except Exception as e:  # noqa: BLE001  keep the server up so the UI can show the problem
        log.exception("could not start the service")
        hub.error = f"{type(e).__name__}: {e}"
    hub.task = asyncio.create_task(hub.loop())
    yield
    hub.task.cancel()
    if hub.svc:
        hub.svc.close()


app = FastAPI(title="Restaurant Ops Agents - live service", lifespan=lifespan)


def svc() -> Service:
    if hub.svc is None:
        raise HTTPException(503, hub.error or "service not started")
    return hub.svc


def guard(fn):
    try:
        return fn()
    except (ServiceError, KeyError, ValueError) as e:
        raise HTTPException(400, str(e).strip("'\"")) from e


# ------------------------------------------------------------------ models
class StartReq(BaseModel):
    seed: int | None = None
    speed: int = 10
    bus: str | None = None


class SpeedReq(BaseModel):
    speed: int


class Item(BaseModel):
    dish_id: str
    mods: str = ""
    course: str | None = None
    qty: int = Field(1, ge=1, le=12)


class TableOrderReq(BaseModel):
    table_id: str
    items: list[Item]
    notes: str = ""
    party_size: int | None = None


class OnlineOrderReq(BaseModel):
    items: list[Item]
    customer: str = "Web guest"


class QuoteReq(BaseModel):
    dish_ids: list[str]


class ResolveReq(BaseModel):
    approved: bool
    note: str = ""
    payload: dict | None = None


class ModeReq(BaseModel):
    mode: str


class OverrideReq(BaseModel):
    ready_hhmm: str


class NotifyReq(BaseModel):
    text: str = ""


def _expand(items: list[Item]) -> list[dict]:
    out = []
    for i in items:
        for _ in range(i.qty):
            d = {"dish_id": i.dish_id, "mods": i.mods}
            if i.course:
                d["course"] = i.course
            out.append(d)
    return out


# ------------------------------------------------------------------ REST
@app.get("/api/state")
async def state():
    return svc().snapshot()


@app.get("/api/health")
async def health():
    s = hub.svc
    return {"ok": s is not None, "error": hub.error, "service": s.info() if s else None,
            "bus_errors": s.bus.errors[-5:] if s else []}


@app.post("/api/service/start")
async def start(req: StartReq):
    s = guard(lambda: hub.start_service(req.seed, req.speed, req.bus))
    return s.info()


@app.post("/api/clock")
async def clock(req: SpeedReq):
    return guard(lambda: svc().set_speed(req.speed))


@app.post("/api/scenarios/{name}")
async def scenario(name: str):
    return guard(lambda: svc().inject(name))


@app.post("/api/orders")
async def table_order(req: TableOrderReq):
    return guard(lambda: svc().manual_order(req.table_id, _expand(req.items), req.notes, req.party_size))


@app.get("/api/online/menu")
async def online_menu():
    return svc().online_menu()


@app.post("/api/online/quote")
async def online_quote(req: QuoteReq):
    return guard(lambda: svc().quote_preview(req.dish_ids))


@app.post("/api/online/orders")
async def online_order(req: OnlineOrderReq):
    return guard(lambda: svc().place_online(_expand(req.items), req.customer))


@app.get("/api/online/orders/{order_id}")
async def online_get(order_id: str):
    s = svc()
    o = s.online.orders.get(order_id)
    if o is None:
        raise HTTPException(404, "unknown order")
    return s.online.view(o, s.clock)


@app.post("/api/online/mode")
async def online_mode(req: ModeReq):
    return guard(lambda: svc().set_online_mode(req.mode, "manager"))


@app.post("/api/online/orders/{order_id}/override")
async def online_override(order_id: str, req: OverrideReq):
    return guard(lambda: svc().override_quote(order_id, req.ready_hhmm))


@app.post("/api/online/orders/{order_id}/notify")
async def online_notify(order_id: str, req: NotifyReq):
    return guard(lambda: svc().notify_customer(order_id, req.text))


@app.post("/api/decisions/{decision_id}")
async def resolve(decision_id: int, req: ResolveReq):
    s = svc()
    d = guard(lambda: s.coordinator.resolve(decision_id, req.approved, req.note, req.payload))
    s.flush()
    return s.coordinator.view(d)


@app.get("/api/services")
async def services():
    return RP.list_services()


@app.get("/api/services/{service_id}/events")
async def service_events(service_id: str, after: int = 0):
    return guard(lambda: RP.load_events(service_id, after))


@app.get("/api/baseline")
async def baseline():
    """Agents vs fixed-par baseline from the latest batch simulation (python -m restaurant_ops.sim.run), if any."""
    runs = settings.DATA_DIR / "runs"
    agents = sorted(runs.glob("*_agent.db"), key=lambda p: p.stat().st_mtime, reverse=True) if runs.exists() else []
    for a in agents:
        b = a.with_name(a.name.replace("_agent.db", "_baseline.db"))
        if b.exists():
            from restaurant_ops.sim.metrics import compare

            run_id = a.name[: -len("_agent.db")]
            try:
                return {"run_id": run_id, "comparison": compare(a, b, run_id)}
            except Exception as e:  # noqa: BLE001
                return {"run_id": run_id, "error": str(e)}
    return {"run_id": None, "hint": "run: uv run python -m restaurant_ops.sim.run --days 7 --policy both --scenario all --auto-approve"}


# ------------------------------------------------------------------ WebSockets
@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    q: asyncio.Queue = asyncio.Queue()
    hub.clients.add(q)
    try:
        if hub.svc is not None:
            await websocket.send_text(json.dumps({"type": "snapshot", "payload": hub.svc.snapshot()}, default=str))
        else:
            await websocket.send_text(json.dumps({"type": "error", "payload": {"message": hub.error or "service not started"}}))
        while True:
            msg = await q.get()
            batch = [msg]
            while not q.empty() and len(batch) < 200:
                batch.append(q.get_nowait())
            await websocket.send_text(json.dumps({"type": "batch", "events": batch}, default=str))
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        hub.clients.discard(q)


@app.websocket("/ws/replay/{service_id}")
async def ws_replay(websocket: WebSocket, service_id: str, speed: float = 60.0):
    """Stream a logged service's events at `speed` x sim time (gaps capped at 1 s), for the UI's replay mode."""
    await websocket.accept()
    try:
        evs = RP.load_events(service_id)
    except FileNotFoundError as e:
        await websocket.send_text(json.dumps({"type": "error", "payload": {"message": str(e)}}))
        await websocket.close()
        return
    try:
        await websocket.send_text(json.dumps({"type": "replay.start", "payload": {"service_id": service_id, "events": len(evs)}}))
        prev = None
        batch: list = []
        for ev in evs:
            t = datetime.fromisoformat(ev["sim_ts"]) if ev["sim_ts"] else prev
            if prev is not None and t is not None and batch:
                gap = (t - prev).total_seconds() / max(speed, 0.1)
                if gap > 0.02:
                    await websocket.send_text(json.dumps({"type": "batch", "events": batch}, default=str))
                    batch = []
                    await asyncio.sleep(min(gap, 1.0))
            batch.append(ev)
            prev = t or prev
        if batch:
            await websocket.send_text(json.dumps({"type": "batch", "events": batch}, default=str))
        await websocket.send_text(json.dumps({"type": "replay.end", "payload": {"service_id": service_id}}))
    except (WebSocketDisconnect, RuntimeError):
        pass


# ------------------------------------------------------------------ frontend
if DIST.exists():
    app.mount("/", StaticFiles(directory=str(DIST), html=True), name="ui")
else:
    @app.get("/", response_class=HTMLResponse)
    async def no_ui():
        return ("<h1>Restaurant Ops Agents</h1><p>The UI is not built yet: <code>cd frontend &amp;&amp; npm install &amp;&amp; "
                "npm run build</code>, then reload. The API is live at <a href='/docs'>/docs</a>.</p>")


def main() -> None:
    import argparse

    import uvicorn

    ap = argparse.ArgumentParser(description="Run the live service backend + UI")
    ap.add_argument("--host", default=settings.LIVE_HOST)
    ap.add_argument("--port", type=int, default=settings.LIVE_PORT)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host=a.host, port=a.port, log_level="info")


if __name__ == "__main__":
    main()
