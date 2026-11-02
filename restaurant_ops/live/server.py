"""FastAPI backend for the live service: REST for actions, one WebSocket that streams every bus event, a replay
WebSocket, Prometheus /metrics, and the built React app (frontend/dist) at /.

    python -m restaurant_ops.live.server          # http://127.0.0.1:8000

Auth: mutating endpoints need a role token (header `Authorization: Bearer <token>`, `X-Api-Token`, or `?token=`
for links). Roles: manager (everything), server (orders, bookings, waitlist, tables, checks, guests), kitchen
(HACCP checklist, temperatures, tickets). Reads, the public online-order page and the WebSocket are open.
Set AUTH_ENABLED=false for a local demo without tokens; change the default tokens anywhere else.
"""
from __future__ import annotations

import asyncio
import csv
import html
import io
import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from restaurant_ops.config import PROJECT_ROOT, settings
from restaurant_ops.live import ops as OPS
from restaurant_ops.live import persistence as PERSIST
from restaurant_ops.live import replay as RP
from restaurant_ops.live.events import Event
from restaurant_ops.live.host import HostError
from restaurant_ops.live.payments import PaymentError
from restaurant_ops.live.service import DISH_VIEW, Service, ServiceError

log = logging.getLogger("restaurant_ops.live")
DIST = PROJECT_ROOT / "frontend" / "dist"
TICK_S = 0.1
QUEUE_MAX = 5000
ROLES = ("manager", "server", "kitchen")


class Hub:
    """Holds the running service, the WebSocket clients and the clock loop."""

    def __init__(self):
        self.svc: Service | None = None
        self.clients: set[asyncio.Queue] = set()
        self.task: asyncio.Task | None = None
        self.error: str = ""
        self.started = time.time()
        self.dropped = 0

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

    def start_service(self, seed: int | None = None, speed: int = 10, bus: str | None = None) -> Service:
        if self.svc is not None:
            try:
                self.svc.save_state()
                self.svc.close()
            except Exception:  # noqa: BLE001
                pass
            self.svc = None
        if not settings.DB_PATH.exists():
            from restaurant_ops.data import seed as seed_mod

            log.warning("no seeded database at %s: seeding now (first run trains the forecast models)", settings.DB_PATH)
            seed_mod.main([])
        svc = self._attach(Service(seed=seed, speed=speed, bus_kind=bus))
        svc.start()
        self.broadcast_raw({"type": "snapshot", "payload": svc.snapshot()})
        log.info("service started", extra={"service_id": svc.service_id, "bus": svc.bus.kind})
        return svc

    def resume_service(self, service_id: str, bus: str | None = None) -> Service:
        if self.svc is not None:
            self.svc.close()
            self.svc = None
        svc = self._attach(Service.resume(service_id, bus_kind=bus))
        self.broadcast_raw({"type": "snapshot", "payload": svc.snapshot()})
        log.info("service resumed", extra={"service_id": service_id, "pending": len(svc.coordinator.pending())})
        return svc

    def broadcast(self, ev: Event) -> None:
        self.broadcast_raw(ev.to_dict())

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    OPS.configure_logging()
    try:
        OPS.cleanup()
    except Exception:  # noqa: BLE001
        log.exception("retention cleanup failed")
    try:
        sid = PERSIST.latest_resumable() if settings.RESUME_ON_START else None
        if sid:
            hub.resume_service(sid)
        else:
            hub.start_service()
    except Exception as e:  # noqa: BLE001  keep the server up so the UI can show the problem
        log.exception("could not start the service")
        hub.error = f"{type(e).__name__}: {e}"
        if hub.svc is None:
            try:
                hub.start_service()
            except Exception as e2:  # noqa: BLE001
                hub.error = f"{type(e2).__name__}: {e2}"
    hub.task = asyncio.create_task(hub.loop())
    yield
    hub.task.cancel()
    if hub.svc:
        try:
            hub.svc.save_state()
        except Exception:  # noqa: BLE001
            pass
        hub.svc.close()


app = FastAPI(title="Restaurant Ops Agents - live service", lifespan=lifespan)


# ------------------------------------------------------------------ auth
def role_of_token(token: str | None) -> str | None:
    if not token:
        return None
    for role, t in (("manager", settings.MANAGER_TOKEN), ("server", settings.SERVER_TOKEN), ("kitchen", settings.KITCHEN_TOKEN)):
        if t and token == t:
            return role
    return None


def _token(request: Request) -> str | None:
    h = request.headers.get("authorization", "")
    if h.lower().startswith("bearer "):
        return h[7:].strip()
    return request.headers.get("x-api-token") or request.query_params.get("token")


def require(*allowed: str):
    """Dependency: the caller's role must be one of `allowed` (manager is always allowed)."""
    def dep(request: Request) -> str:
        if not settings.AUTH_ENABLED:
            return "manager"
        tok = _token(request)
        role = role_of_token(tok)
        if role is None:
            raise HTTPException(401, "missing or invalid token", headers={"WWW-Authenticate": "Bearer"})
        if role != "manager" and role not in allowed:
            raise HTTPException(403, f"role '{role}' may not do this (needs {' or '.join(('manager',) + allowed)})")
        return role
    return dep


MANAGER = Depends(require())
SERVER = Depends(require("server"))
KITCHEN = Depends(require("kitchen"))


def svc() -> Service:
    if hub.svc is None:
        raise HTTPException(503, hub.error or "service not started")
    return hub.svc


def guard(fn):
    try:
        out = fn()
    except (ServiceError, HostError, PaymentError, KeyError, ValueError) as e:
        raise HTTPException(400, str(e).strip("'\"")) from e
    if hub.svc is not None:
        hub.svc.flush()
    return out


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
    seat: int | None = None
    qty: int = Field(1, ge=1, le=12)


class TableOrderReq(BaseModel):
    table_id: str
    items: list[Item]
    notes: str = ""
    party_size: int | None = None
    guest_id: int | None = None


class OnlineOrderReq(BaseModel):
    items: list[Item]
    customer: str = "Web guest"
    phone: str = ""
    fulfilment: str = "pickup"
    zone: str | None = None


class QuoteReq(BaseModel):
    dish_ids: list[str]
    fulfilment: str = "pickup"
    zone: str | None = None


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


class BookingReq(BaseModel):
    name: str
    party_size: int = Field(ge=1, le=20)
    time: str = Field(pattern=r"^\d\d:\d\d$")
    phone: str = ""
    notes: str = ""
    simulate_arrival: bool = False
    allow_overbook: bool = False


class WaitlistReq(BaseModel):
    name: str
    party_size: int = Field(ge=1, le=20)
    phone: str = ""
    notes: str = ""


class SeatReq(BaseModel):
    table_ids: list[str] | None = None


class MoveReq(BaseModel):
    party_id: str
    table_ids: list[str]


class TableOpReq(BaseModel):
    party_id: str
    table_id: str


class SplitReq(BaseModel):
    mode: str = "even"
    ways: int = 2


class PayReq(BaseModel):
    amount: float
    tip: float = 0.0
    method: str = "card"
    split_index: int | None = None


class DiscountReq(BaseModel):
    reason: str
    pct: float | None = None
    amount: float | None = None


class RefundReq(BaseModel):
    amount: float
    reason: str


class GuestReq(BaseModel):
    name: str
    phone: str | None = None
    email: str | None = None
    allergies: list[str] | str | None = None
    preferences: str | None = None
    notes: str | None = None


class GuestPatch(BaseModel):
    name: str | None = None
    phone: str | None = None
    email: str | None = None
    allergies: list[str] | str | None = None
    preferences: str | None = None
    notes: str | None = None


class HaccpReq(BaseModel):
    ok: bool = True
    note: str = ""


def _expand(items: list[Item]) -> list[dict]:
    out = []
    for i in items:
        for _ in range(i.qty):
            d = {"dish_id": i.dish_id, "mods": i.mods}
            if i.course:
                d["course"] = i.course
            if i.seat:
                d["seat"] = i.seat
            out.append(d)
    return out


# ------------------------------------------------------------------ service + read endpoints (open)
@app.get("/api/state")
async def state():
    return svc().snapshot()


@app.get("/api/health")
async def health():
    s = hub.svc
    return {"ok": s is not None, "error": hub.error, "service": s.info() if s else None,
            "bus_errors": s.bus.errors[-5:] if s else [], "ws_clients": len(hub.clients)}


@app.get("/api/whoami")
async def whoami(request: Request):
    role = "manager" if not settings.AUTH_ENABLED else role_of_token(_token(request))
    return {"role": role, "auth_enabled": settings.AUTH_ENABLED}


@app.get("/metrics", response_class=PlainTextResponse)
async def metrics():
    return OPS.prometheus(hub.svc, ws_clients=len(hub.clients), started=hub.started)


@app.post("/api/service/start")
async def start(req: StartReq, role: str = MANAGER):
    s = guard(lambda: hub.start_service(req.seed, req.speed, req.bus))
    return s.info()


@app.post("/api/service/resume/{service_id}")
async def resume(service_id: str, role: str = MANAGER):
    try:
        s = hub.resume_service(service_id)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    return {**s.info(), "pending": len(s.coordinator.pending())}


@app.post("/api/clock")
async def clock(req: SpeedReq, role: str = MANAGER):
    return guard(lambda: svc().set_speed(req.speed))


@app.post("/api/scenarios/{name}")
async def scenario(name: str, role: str = MANAGER):
    return guard(lambda: svc().inject(name))


@app.post("/api/decisions/{decision_id}")
async def resolve(decision_id: int, req: ResolveReq, role: str = MANAGER):
    s = svc()
    d = guard(lambda: s.coordinator.resolve(decision_id, req.approved, req.note, req.payload))
    return s.coordinator.view(d)


# ------------------------------------------------------------------ orders & online
@app.post("/api/orders")
async def table_order(req: TableOrderReq, role: str = SERVER):
    return guard(lambda: svc().manual_order(req.table_id, _expand(req.items), req.notes, req.party_size, req.guest_id))


@app.get("/api/online/menu")
async def online_menu():
    return svc().online_menu()


@app.post("/api/online/quote")
async def online_quote(req: QuoteReq):
    return guard(lambda: svc().quote_preview(req.dish_ids, req.fulfilment, req.zone))


@app.post("/api/online/orders")
async def online_order(req: OnlineOrderReq):
    return guard(lambda: svc().place_online(_expand(req.items), req.customer, req.fulfilment, req.zone, req.phone))


@app.get("/api/online/orders/{order_id}")
async def online_get(order_id: str):
    s = svc()
    o = s.online.orders.get(order_id)
    if o is None:
        raise HTTPException(404, "unknown order")
    return s.online.view(o, s.clock)


@app.post("/api/online/mode")
async def online_mode(req: ModeReq, role: str = MANAGER):
    return guard(lambda: svc().set_online_mode(req.mode, "manager"))


@app.post("/api/online/orders/{order_id}/override")
async def online_override(order_id: str, req: OverrideReq, role: str = MANAGER):
    return guard(lambda: svc().override_quote(order_id, req.ready_hhmm))


@app.post("/api/online/orders/{order_id}/notify")
async def online_notify(order_id: str, req: NotifyReq, role: str = MANAGER):
    return guard(lambda: svc().notify_customer(order_id, req.text))


# ------------------------------------------------------------------ host stand
@app.get("/api/reservations")
async def reservations():
    return svc().host.bookings_view()


@app.post("/api/reservations")
async def create_booking(req: BookingReq, role: str = SERVER):
    if req.allow_overbook and role != "manager":
        raise HTTPException(403, "only a manager can allow an overbooking")
    s = svc()
    b = guard(lambda: s.host.create(req.name, req.party_size, req.time, phone=req.phone, notes=req.notes, source="phone",
                                    simulate=req.simulate_arrival, allow_overbook=req.allow_overbook))
    return next(x for x in s.host.bookings_view() if x["booking_id"] == b.booking_id)


@app.post("/api/reservations/{booking_id}/{action}")
async def booking_action(booking_id: str, action: str, role: str = SERVER):
    s = svc()
    if action == "arrive":
        return guard(lambda: s.host.arrive(booking_id))
    if action in ("cancel", "no_show"):
        status = "cancelled" if action == "cancel" else "no_show"
        guard(lambda: s.host.set_status(booking_id, status))
        return next(x for x in s.host.bookings_view() if x["booking_id"] == booking_id)
    raise HTTPException(404, "action must be arrive, cancel or no_show")


@app.get("/api/waitlist")
async def waitlist():
    s = svc()
    return {"waitlist": s.host.waitlist_view(), "quote_for": {n: s.host.quote_wait(n) for n in (2, 4, 6, 8)}}


@app.post("/api/waitlist")
async def add_waitlist(req: WaitlistReq, role: str = SERVER):
    return guard(lambda: svc().host.add_to_waitlist(req.name, req.party_size, req.phone, req.notes))


@app.post("/api/waitlist/{party_id}/seat")
async def seat_waitlist(party_id: str, req: SeatReq, role: str = SERVER):
    return guard(lambda: svc().host.seat_from_waitlist(party_id, req.table_ids))


@app.post("/api/tables/move")
async def tables_move(req: MoveReq, role: str = SERVER):
    return guard(lambda: svc().host.move(req.party_id, req.table_ids))


@app.post("/api/tables/combine")
async def tables_combine(req: TableOpReq, role: str = SERVER):
    return guard(lambda: svc().host.combine(req.party_id, req.table_id))


@app.post("/api/tables/split")
async def tables_split(req: TableOpReq, role: str = SERVER):
    return guard(lambda: svc().host.split(req.party_id, req.table_id))


# ------------------------------------------------------------------ checks & payments
@app.get("/api/checks")
async def checks(status: str | None = None):
    s = svc()
    return [s.payments.view(c) for c in s.payments.checks.values() if status in (None, c.status)]


@app.post("/api/checks/{check_id}/split")
async def check_split(check_id: str, req: SplitReq, role: str = SERVER):
    return guard(lambda: svc().payments.split(check_id, req.mode, req.ways))


@app.post("/api/checks/{check_id}/pay")
async def check_pay(check_id: str, req: PayReq, role: str = SERVER):
    return guard(lambda: svc().payments.pay(check_id, req.amount, req.tip, req.method, req.split_index))


@app.post("/api/checks/{check_id}/discount")
async def check_discount(check_id: str, req: DiscountReq, role: str = MANAGER):
    return guard(lambda: svc().payments.discount(check_id, req.reason, req.pct, req.amount))


@app.post("/api/checks/{check_id}/refund")
async def check_refund(check_id: str, req: RefundReq, role: str = MANAGER):
    return guard(lambda: svc().payments.refund(check_id, req.amount, req.reason))


# ------------------------------------------------------------------ guests (CRM)
@app.get("/api/guests")
async def guests(q: str = "", role: str = SERVER):
    return svc().crm.search(q)


@app.post("/api/guests")
async def guest_create(req: GuestReq, role: str = SERVER):
    return guard(lambda: svc().crm.upsert(req.name, req.phone, req.email, req.allergies, req.preferences, req.notes))


@app.get("/api/guests/{guest_id}")
async def guest_get(guest_id: int, role: str = SERVER):
    s = svc()
    g = s.crm.get(guest_id)
    if g is None:
        raise HTTPException(404, "unknown guest")
    return {**g, "visits_history": s.crm.visits(guest_id)}


@app.patch("/api/guests/{guest_id}")
async def guest_patch(guest_id: int, req: GuestPatch, role: str = SERVER):
    return guard(lambda: svc().crm.update(guest_id, **req.model_dump(exclude_none=True)))


# ------------------------------------------------------------------ kitchen: safety, prep, tickets
@app.get("/api/safety")
async def safety():
    s = svc()
    return {**s.safety.view(), "temps": s.safety.temps()}


@app.post("/api/safety/checklist/{item_id}")
async def haccp_done(item_id: int, req: HaccpReq, role: str = KITCHEN):
    return guard(lambda: svc().safety.complete(item_id, role, req.ok, req.note))


@app.post("/api/safety/log")
async def temp_log_now(role: str = KITCHEN):
    return guard(lambda: svc().safety.log())


@app.get("/api/kitchen/tickets")
async def tickets(role: str = KITCHEN):
    s = svc()
    out = []
    for st in s.kitchen.stations.values():
        for it in [*st.cooking, *st.queue]:
            out.append({"item_id": it.item_id, "order_id": it.order_id, "dish": DISH_VIEW[it.dish_id]["name"], "dish_id": it.dish_id, "station": it.station, "status": it.status, "mods": it.mods, "scratch": it.scratch})
    return out


@app.get("/api/prep")
async def prep():
    return svc().prep.view()


# ------------------------------------------------------------------ analytics, reports, replay
@app.get("/api/menu/engineering")
async def menu_engineering(days: int = 28):
    return svc().menu_engineering(days)


@app.get("/api/waste")
async def waste(days: int = 30):
    return svc().waste(days)


@app.get("/api/suppliers")
async def suppliers():
    s = svc()
    s.flush()
    return [dict(r) for r in s.conn.execute("SELECT id, name, lead_time_days, min_order_value, fill_rate, delay_prob FROM suppliers")]


@app.get("/api/report")
async def report(role: str = MANAGER):
    return svc().report()


@app.get("/api/report.csv")
async def report_csv(role: str = MANAGER):
    r = svc().report()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["section", "key", "value"])
    for k, v in r["kpis"].items():
        w.writerow(["kpi", k, v])
    for k, v in r["payments"].items():
        w.writerow(["payments", k, v])
    for d in r["sales_by_dish"]:
        w.writerow(["sales", d["name"], f"{d['qty']} / {d['sales']}"])
    for d in r["decisions"]["items"]:
        w.writerow(["decision", f"{(d['created_sim'] or '')[11:16]} {d['agent']}", f"{d['title']} -> {d['status']}"])
    for x in r["waste"]["by_reason"]:
        w.writerow(["waste", x["reason"], x["value"]])
    name = f"service-report-{r['service']['service_id']}.csv"
    return Response(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/api/report.html", response_class=HTMLResponse)
async def report_html(role: str = MANAGER):
    r = svc().report()
    e = html.escape
    k = r["kpis"]
    rows = "".join(f"<tr><td>{e(str(a))}</td><td>{e(str(b))}</td></tr>" for a, b in k.items())
    pay = "".join(f"<tr><td>{e(a)}</td><td>{e(str(b))}</td></tr>" for a, b in r["payments"].items())
    sales = "".join(f"<tr><td>{e(d['name'])}</td><td>{d['qty']}</td><td>${d['sales']:.2f}</td></tr>" for d in r["sales_by_dish"])
    dec = "".join(f"<tr><td>{e((d['created_sim'] or '')[11:16])}</td><td>{e(d['agent'])}</td><td>{e(d['title'])}</td>"
                  f"<td>{e(d['status'])}</td></tr>" for d in r["decisions"]["items"])
    svc_info = r["service"]
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Service report</title><style>
body{{font:14px/1.45 system-ui,sans-serif;max-width:900px;margin:24px auto;padding:0 16px;color:#1f2328;background:#fff}}
h1{{font-size:20px}} h2{{font-size:15px;margin-top:22px}} table{{border-collapse:collapse;width:100%}}
td,th{{border-bottom:1px solid #ddd;padding:4px 6px;text-align:left;font-size:13px}} .muted{{color:#666}}
@media print{{button{{display:none}}}}</style></head><body>
<button onclick="print()">Print / save as PDF</button>
<h1>End-of-service report · {e(svc_info['weekday'])} {e(svc_info['date'])} {e(svc_info['period'])}</h1>
<p class="muted">{e(svc_info['service_id'])} · generated {e(r['generated'])} · bus {e(svc_info['bus'])} · agents {e(svc_info['llm_mode'])}</p>
<h2>KPIs</h2><table>{rows}</table>
<h2>Payments</h2><table>{pay}</table>
<h2>Sales by dish</h2><table><tr><th>Dish</th><th>Qty</th><th>Sales</th></tr>{sales}</table>
<h2>Decisions</h2><table><tr><th>Time</th><th>Agent</th><th>Decision</th><th>Status</th></tr>{dec}</table>
</body></html>"""
    return HTMLResponse(page)


@app.get("/api/services")
async def services():
    return RP.list_services()


@app.get("/api/services/{service_id}/events")
async def service_events(service_id: str, after: int = 0):
    return guard(lambda: RP.load_events(service_id, after))


@app.post("/api/ops/cleanup")
async def ops_cleanup(dry_run: bool = True, role: str = MANAGER):
    keep = {hub.svc.service_id} if hub.svc else set()
    return OPS.cleanup(keep=keep, dry_run=dry_run)


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
    ap.add_argument("--fresh", action="store_true", help="start a new service instead of resuming an interrupted one")
    a = ap.parse_args()
    if a.fresh:
        settings.RESUME_ON_START = False
    OPS.configure_logging()
    uvicorn.run(app, host=a.host, port=a.port, log_level=settings.LOG_LEVEL.lower(), log_config=None)


if __name__ == "__main__":
    main()
