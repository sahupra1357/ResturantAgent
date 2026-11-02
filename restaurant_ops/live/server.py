"""FastAPI backend for the live service: REST, the staff WebSocket, a public WebSocket for the online-order page,
replay, Prometheus /metrics, and the built React app (frontend/dist) at /.

    python -m restaurant_ops.live.server [--fresh]          # http://127.0.0.1:8000

Identity: named users (live/auth.py). Browsers sign in at /api/auth/login and get an HttpOnly, SameSite=Strict
session cookie; scripts send `Authorization: Bearer <token>`. Tokens never travel in URLs. Cookie-authenticated
writes also need the header `X-Requested-With: ro-ui` (CSRF guard). Every write is in the audit log.
Roles: admin (setup, users, restore) > manager (service control, approvals, money, reports) > server (orders,
bookings, tables, checks, guests) and kitchen (KDS, HACCP). Everything except the public online-order endpoints,
/ws/public and /api/health needs a signed-in user.
"""
from __future__ import annotations

import asyncio
import csv
import html
import io
import json
import logging
import tempfile
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from restaurant_ops import admin as ADMIN
from restaurant_ops.config import PROJECT_ROOT, settings
from restaurant_ops.db.connection import get_conn, init_db
from restaurant_ops.live import auth as AUTH
from restaurant_ops.live import dayclose as DC
from restaurant_ops.live import events as E
from restaurant_ops.live import giftcards as GC
from restaurant_ops.live import ops as OPS
from restaurant_ops.live import outputs as OUT
from restaurant_ops.live import persistence as PERSIST
from restaurant_ops.live import po_channel as PO
from restaurant_ops.live import replay as RP
from restaurant_ops.live.events import Event
from restaurant_ops.live.host import HostError
from restaurant_ops.live.payments import PaymentError
from restaurant_ops.live.service import DISH_VIEW, Service, ServiceError

log = logging.getLogger("restaurant_ops.live")
DIST = PROJECT_ROOT / "frontend" / "dist"
TICK_S = 0.1
QUEUE_MAX = 5000
COOKIE = "ro_session"
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "ro-ui"
WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
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


def master():
    c = get_conn(settings.DB_PATH)
    return c


@asynccontextmanager
async def lifespan(app: FastAPI):
    OPS.configure_logging()
    try:
        OPS.cleanup()
    except Exception:  # noqa: BLE001
        log.exception("retention cleanup failed")
    try:
        hub._ensure_seeded()
        m = master()
        try:
            init_db(m)
            ADMIN.seed_defaults(m)
            hub.bootstrap = AUTH.bootstrap(m)
        finally:
            m.close()
        if hub.bootstrap.get("created"):
            log.warning("first run: created users %s%s", hub.bootstrap["created"],
                        f"; admin password in {hub.bootstrap['admin_password_file']}" if hub.bootstrap.get("admin_password_file") else "")
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


# =========================================================================== auth helpers


def _bearer(headers) -> str | None:
    h = headers.get("authorization", "")
    return h[7:].strip() if h.lower().startswith("bearer ") else None


def _session_token(request) -> tuple[str | None, str]:
    b = _bearer(request.headers)
    if b:
        return b, "bearer"
    c = request.cookies.get(COOKIE)
    return (c, "cookie") if c else (None, "")


def current_user(request: Request) -> dict | None:
    if not settings.AUTH_ENABLED:
        return {"id": 0, "username": "local", "display_name": "Local (auth off)", "role": "admin", "active": True}
    tok, how = _session_token(request)
    if not tok:
        return None
    m = master()
    try:
        u = AUTH.resolve(m, tok)
    finally:
        m.close()
    if u is not None:
        u["_via"] = how
    return u


def need(*roles: str):
    """Dependency: a signed-in user allowed by `roles` (see auth.allowed). Cookie writes need the CSRF header."""
    def dep(request: Request) -> dict:
        u = current_user(request)
        if u is None:
            raise HTTPException(401, "sign in required", headers={"WWW-Authenticate": "Bearer"})
        request.state.user = u  # the audit log names who was refused, too
        if not AUTH.allowed(u["role"], roles):
            raise HTTPException(403, f"{u['username']} ({u['role']}) may not do this")
        if (u.get("_via") == "cookie" and request.method in WRITE_METHODS
                and request.headers.get(CSRF_HEADER, "").lower() != CSRF_VALUE):
            raise HTTPException(403, "missing CSRF header")
        request.state.user = u
        return u
    return dep


ANY = Depends(need("any"))
SERVER = Depends(need("server"))
KITCHEN = Depends(need("kitchen"))
MANAGER = Depends(need())
ADMINONLY = Depends(need("admin"))


@app.middleware("http")
async def audit_mw(request: Request, call_next):
    response = await call_next(request)
    if request.method in WRITE_METHODS and request.url.path.startswith("/api/"):
        user = getattr(request.state, "user", None) or getattr(request.state, "login_user", None)
        try:
            m = master()
            try:
                AUTH.audit(m, user, request.method, request.url.path, response.status_code,
                           getattr(request.state, "audit", None) or dict(request.query_params))
            finally:
                m.close()
        except Exception:  # noqa: BLE001  auditing must not break the request
            log.exception("audit write failed")
    return response


def svc() -> Service:
    if hub.svc is None:
        raise HTTPException(503, hub.error or "service not started")
    return hub.svc


def guard(fn):
    try:
        out = fn()
    except (ServiceError, HostError, PaymentError, GC.GiftCardError, ADMIN.AdminError, AUTH.AuthError, DC.DayCloseError,
            KeyError, ValueError) as e:
        raise HTTPException(400, str(e).strip("'\"")) from e
    if hub.svc is not None:
        hub.svc.flush()
    return out


def idempotent(request: Request, endpoint: str, fn):
    """Replays the stored response for a repeated Idempotency-Key (offline order capture retries safely)."""
    key = request.headers.get("idempotency-key")
    if not key:
        return guard(fn)
    m = master()
    try:
        r = m.execute("SELECT response FROM idempotency WHERE key=? AND endpoint=?", (key[:120], endpoint)).fetchone()
        if r:
            return {**json.loads(r[0]), "replayed": True}
        out = guard(fn)
        m.execute("INSERT OR REPLACE INTO idempotency (key, endpoint, response, created) VALUES (?,?,?,?)",
                  (key[:120], endpoint, json.dumps(out, default=str), E.wall_now()))
        m.commit()
        return out
    finally:
        m.close()


# =========================================================================== models
class LoginReq(BaseModel):
    username: str
    password: str


class PasswordReq(BaseModel):
    current_password: str
    new_password: str


class UserReq(BaseModel):
    username: str
    password: str
    role: str
    display_name: str = ""


class UserPatch(BaseModel):
    role: str | None = None
    active: bool | None = None
    password: str | None = None
    display_name: str | None = None


class StartReq(BaseModel):
    seed: int | None = None
    speed: int = 10
    bus: str | None = None
    period: str | None = None
    date: str | None = None


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
    token: str | None = None          # card: a processor payment-method token from the terminal / Stripe.js
    gift_code: str | None = None


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


class BumpReq(BaseModel):
    order_id: str
    station: str


class GiftReq(BaseModel):
    amount: float
    note: str = ""


class VoidReq(BaseModel):
    reason: str


class DayCloseReq(BaseModel):
    counted_cash: float | None = None
    force: bool = False


class HoursReq(BaseModel):
    weekday: int
    meal_period: str
    open: str
    close: str
    closed: bool = False


class RosterReq(BaseModel):
    date: str
    meal_period: str
    entries: list[dict]


class ItemPriceReq(BaseModel):
    price: float
    pack_size: float


class ImportReq(BaseModel):
    csv: str
    dry_run: bool = False
    replace_stock: bool = False


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


# =========================================================================== auth endpoints
@app.post("/api/auth/login")
async def login(req: LoginReq, request: Request):
    m = master()
    try:
        try:
            token, user = AUTH.login(m, req.username, req.password)
        except AUTH.AuthError as e:
            request.state.login_user = {"username": req.username.strip().lower(), "role": None}
            request.state.audit = {"result": str(e)}
            raise HTTPException(429 if str(e) == "locked" else 401, "too many attempts, try again later" if str(e) == "locked"
                                else "invalid username or password") from e
    finally:
        m.close()
    request.state.login_user = user
    resp = JSONResponse({"user": user, "token": token, "token_type": "bearer"})
    resp.set_cookie(COOKIE, token, httponly=True, samesite="strict", secure=settings.COOKIE_SECURE,
                    max_age=int(settings.SESSION_HOURS * 3600), path="/")
    return resp


@app.post("/api/auth/logout")
async def logout(request: Request):
    tok, _ = _session_token(request)
    m = master()
    try:
        AUTH.logout(m, tok)
    finally:
        m.close()
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE, path="/")
    return resp


@app.get("/api/auth/me")
async def me(request: Request):
    u = current_user(request)
    if u is None:
        raise HTTPException(401, "not signed in")
    return {k: v for k, v in u.items() if not k.startswith("_")} | {"auth_enabled": settings.AUTH_ENABLED}


@app.post("/api/auth/password")
async def change_password(req: PasswordReq, user: dict = ANY):
    m = master()
    try:
        r = m.execute("SELECT pw_hash FROM users WHERE id=?", (user["id"],)).fetchone()
        if r is None or not AUTH.verify_password(req.current_password, r[0]):
            raise HTTPException(400, "current password is wrong")
        return guard(lambda: AUTH.update_user(m, user["id"], password=req.new_password))
    finally:
        m.close()


# =========================================================================== service + read endpoints
@app.get("/api/state")
async def state(user: dict = ANY):
    return svc().snapshot()


@app.get("/api/public/state")
async def public_state():
    return public_snapshot(svc())


@app.get("/api/health")
async def health():
    s = hub.svc
    return {"ok": s is not None, "error": hub.error, "status": s.status if s else None, "bus": s.bus.kind if s else None,
            "ws_clients": len(hub.clients), "public_clients": len(hub.public)}


@app.get("/metrics", response_class=PlainTextResponse)
async def metrics(user: dict = MANAGER):
    return OPS.prometheus(hub.svc, ws_clients=len(hub.clients) + len(hub.public), started=hub.started)


@app.post("/api/service/start")
async def start(req: StartReq, user: dict = MANAGER):
    s = guard(lambda: hub.start_service(req.seed, req.speed, req.bus, req.period, req.date))
    return s.info()


@app.post("/api/service/resume/{service_id}")
async def resume(service_id: str, user: dict = MANAGER):
    try:
        s = hub.resume_service(service_id)
    except (FileNotFoundError, ServiceError) as e:
        raise HTTPException(404, str(e)) from e
    return {**s.info(), "pending": len(s.coordinator.pending())}


@app.post("/api/clock")
async def clock(req: SpeedReq, user: dict = MANAGER):
    return guard(lambda: svc().set_speed(req.speed))


@app.post("/api/scenarios/{name}")
async def scenario(name: str, user: dict = MANAGER):
    return guard(lambda: svc().inject(name))


@app.post("/api/decisions/{decision_id}")
async def resolve(decision_id: int, req: ResolveReq, request: Request, user: dict = MANAGER):
    s = svc()
    request.state.audit = {"decision_id": decision_id, "approved": req.approved, "note": req.note}
    d = guard(lambda: s.coordinator.resolve(decision_id, req.approved, req.note or f"by {user['username']}", req.payload,
                                            by=user["username"]))
    return s.coordinator.view(d)


# =========================================================================== orders & online
@app.post("/api/orders")
async def table_order(req: TableOrderReq, request: Request, user: dict = SERVER):
    return idempotent(request, "orders", lambda: svc().manual_order(req.table_id, _expand(req.items), req.notes, req.party_size,
                                                                    req.guest_id))


@app.get("/api/online/menu")
async def online_menu():
    return svc().online_menu()


@app.post("/api/online/quote")
async def online_quote(req: QuoteReq):
    return guard(lambda: svc().quote_preview(req.dish_ids, req.fulfilment, req.zone))


@app.post("/api/online/orders")
async def online_order(req: OnlineOrderReq, request: Request):
    out = idempotent(request, "online", lambda: svc().place_online(_expand(req.items), req.customer, req.fulfilment, req.zone, req.phone))
    if out.get("online"):
        out = {**out, "online": public_order(out["online"])}
    return out


@app.get("/api/online/orders/{order_id}")
async def online_get(order_id: str):
    s = svc()
    o = s.online.orders.get(order_id)
    if o is None:
        raise HTTPException(404, "unknown order")
    return public_order(s.online.view(o, s.clock))


@app.post("/api/online/mode")
async def online_mode(req: ModeReq, user: dict = MANAGER):
    return guard(lambda: svc().set_online_mode(req.mode, f"manager {user['username']}"))


@app.post("/api/online/orders/{order_id}/override")
async def online_override(order_id: str, req: OverrideReq, user: dict = MANAGER):
    return guard(lambda: svc().override_quote(order_id, req.ready_hhmm))


@app.post("/api/online/orders/{order_id}/notify")
async def online_notify(order_id: str, req: NotifyReq, user: dict = MANAGER):
    return guard(lambda: svc().notify_customer(order_id, req.text))


# =========================================================================== host stand
@app.get("/api/reservations")
async def reservations(user: dict = ANY):
    return svc().host.bookings_view()


@app.post("/api/reservations")
async def create_booking(req: BookingReq, user: dict = SERVER):
    if req.allow_overbook and user["role"] not in ("manager", "admin"):
        raise HTTPException(403, "only a manager can allow an overbooking")
    s = svc()
    b = guard(lambda: s.host.create(req.name, req.party_size, req.time, phone=req.phone, notes=req.notes, source="phone",
                                    simulate=req.simulate_arrival, allow_overbook=req.allow_overbook))
    return next(x for x in s.host.bookings_view() if x["booking_id"] == b.booking_id)


@app.post("/api/reservations/{booking_id}/{action}")
async def booking_action(booking_id: str, action: str, user: dict = SERVER):
    s = svc()
    if action == "arrive":
        return guard(lambda: s.host.arrive(booking_id))
    if action in ("cancel", "no_show"):
        status = "cancelled" if action == "cancel" else "no_show"
        guard(lambda: s.host.set_status(booking_id, status))
        return next(x for x in s.host.bookings_view() if x["booking_id"] == booking_id)
    raise HTTPException(404, "action must be arrive, cancel or no_show")


@app.get("/api/waitlist")
async def waitlist(user: dict = ANY):
    s = svc()
    return {"waitlist": s.host.waitlist_view(), "quote_for": {n: s.host.quote_wait(n) for n in (2, 4, 6, 8)}}


@app.post("/api/waitlist")
async def add_waitlist(req: WaitlistReq, user: dict = SERVER):
    return guard(lambda: svc().host.add_to_waitlist(req.name, req.party_size, req.phone, req.notes))


@app.post("/api/waitlist/{party_id}/seat")
async def seat_waitlist(party_id: str, req: SeatReq, user: dict = SERVER):
    return guard(lambda: svc().host.seat_from_waitlist(party_id, req.table_ids))


@app.post("/api/tables/move")
async def tables_move(req: MoveReq, user: dict = SERVER):
    return guard(lambda: svc().host.move(req.party_id, req.table_ids))


@app.post("/api/tables/combine")
async def tables_combine(req: TableOpReq, user: dict = SERVER):
    return guard(lambda: svc().host.combine(req.party_id, req.table_id))


@app.post("/api/tables/split")
async def tables_split(req: TableOpReq, user: dict = SERVER):
    return guard(lambda: svc().host.split(req.party_id, req.table_id))


# =========================================================================== checks, payments, gift cards
@app.get("/api/checks")
async def checks(status: str | None = None, user: dict = ANY):
    s = svc()
    return [s.payments.view(c) for c in s.payments.checks.values() if status in (None, c.status)]


@app.post("/api/checks/{check_id}/split")
async def check_split(check_id: str, req: SplitReq, user: dict = SERVER):
    return guard(lambda: svc().payments.split(check_id, req.mode, req.ways))


@app.post("/api/checks/{check_id}/pay")
async def check_pay(check_id: str, req: PayReq, request: Request, user: dict = SERVER):
    request.state.audit = {"check_id": check_id, "amount": req.amount, "tip": req.tip, "method": req.method}
    return guard(lambda: svc().payments.pay(check_id, req.amount, req.tip, req.method, req.split_index, token=req.token,
                                            gift_code=req.gift_code))


@app.post("/api/checks/{check_id}/discount")
async def check_discount(check_id: str, req: DiscountReq, request: Request, user: dict = SERVER):
    request.state.audit = {"check_id": check_id, "pct": req.pct, "amount": req.amount, "reason": req.reason}
    return guard(lambda: svc().payments.discount(check_id, req.reason, req.pct, req.amount, by=user["username"], role=user["role"]))


@app.post("/api/checks/{check_id}/refund")
async def check_refund(check_id: str, req: RefundReq, request: Request, user: dict = MANAGER):
    request.state.audit = {"check_id": check_id, "amount": req.amount, "reason": req.reason}
    return guard(lambda: svc().payments.refund(check_id, req.amount, req.reason, by=user["username"]))


@app.post("/api/giftcards")
async def giftcard_issue(req: GiftReq, request: Request, user: dict = MANAGER):
    request.state.audit = {"amount": req.amount, "note": req.note}
    with svc().master() as m:
        return guard(lambda: GC.issue(m, req.amount, req.note or f"issued by {user['username']}", svc().service_id))


@app.get("/api/giftcards/{code}")
async def giftcard_get(code: str, user: dict = SERVER):
    with svc().master() as m:
        g = guard(lambda: GC.get(m, code))
        return {**g, "ledger": GC.ledger(m, code)}


@app.post("/api/giftcards/{code}/void")
async def giftcard_void(code: str, req: VoidReq, user: dict = MANAGER):
    with svc().master() as m:
        return guard(lambda: GC.void(m, code, req.reason))


# =========================================================================== guests (CRM)
@app.get("/api/guests")
async def guests(q: str = "", user: dict = SERVER):
    return svc().crm.search(q)


@app.post("/api/guests")
async def guest_create(req: GuestReq, user: dict = SERVER):
    return guard(lambda: svc().crm.upsert(req.name, req.phone, req.email, req.allergies, req.preferences, req.notes))


@app.get("/api/guests/{guest_id}")
async def guest_get(guest_id: int, user: dict = SERVER):
    s = svc()
    g = s.crm.get(guest_id)
    if g is None:
        raise HTTPException(404, "unknown guest")
    return {**g, "visits_history": s.crm.visits(guest_id)}


@app.patch("/api/guests/{guest_id}")
async def guest_patch(guest_id: int, req: GuestPatch, user: dict = SERVER):
    return guard(lambda: svc().crm.update(guest_id, **req.model_dump(exclude_none=True)))


# =========================================================================== kitchen: KDS, safety, prep
@app.get("/api/kds")
async def kds(station: str | None = None, include_bumped: bool = False, user: dict = KITCHEN):
    s = svc()
    return {"station": station, "clock": s.clock.hhmm(), "stations": list(s.kitchen.stations),
            "tickets": OUT.tickets(s, station, include_bumped)}


@app.post("/api/kds/bump")
async def kds_bump(req: BumpReq, user: dict = KITCHEN):
    return {"bumped": guard(lambda: OUT.bump(svc(), req.order_id, req.station, user["username"]))}


@app.get("/api/safety")
async def safety(user: dict = ANY):
    s = svc()
    return {**s.safety.view(), "temps": s.safety.temps()}


@app.post("/api/safety/checklist/{item_id}")
async def haccp_done(item_id: int, req: HaccpReq, user: dict = KITCHEN):
    return guard(lambda: svc().safety.complete(item_id, user["username"], req.ok, req.note))


@app.post("/api/safety/log")
async def temp_log_now(user: dict = KITCHEN):
    return guard(lambda: svc().safety.log())


@app.get("/api/prep")
async def prep(user: dict = ANY):
    return svc().prep.view()


# =========================================================================== purchasing outputs
@app.get("/api/purchase-orders")
async def purchase_orders(user: dict = MANAGER):
    s = svc()
    s.flush()
    return PO.outbox(s.conn)


@app.get("/api/purchase-orders/{po_id}.pdf")
async def purchase_order_pdf(po_id: str, user: dict = MANAGER):
    s = svc()
    s.flush()
    if s.conn.execute("SELECT 1 FROM purchase_orders WHERE po_id=?", (po_id,)).fetchone() is None:
        raise HTTPException(404, "unknown PO")
    _, lines = PO.po_lines(s.conn, po_id, s.profile)
    return Response(PO.text_pdf(lines, f"Purchase order {po_id}"), media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{po_id}.pdf"'})


@app.post("/api/purchase-orders/{po_id}/resend")
async def purchase_order_resend(po_id: str, user: dict = MANAGER):
    s = svc()
    s.flush()
    return guard(lambda: PO.send_po(s.conn, po_id, s.profile))


# =========================================================================== analytics, reports, replay
@app.get("/api/menu/engineering")
async def menu_engineering(days: int = 28, user: dict = ANY):
    return svc().menu_engineering(days)


@app.get("/api/waste")
async def waste(days: int = 30, user: dict = ANY):
    return svc().waste(days)


@app.get("/api/suppliers")
async def suppliers(user: dict = ANY):
    s = svc()
    s.flush()
    return [dict(r) for r in s.conn.execute("SELECT id, name, lead_time_days, min_order_value, fill_rate, delay_prob, email FROM suppliers")]


@app.get("/api/llm")
async def llm_stats(user: dict = ANY):
    s = svc()
    s.flush()
    calls = [dict(r) for r in s.conn.execute("SELECT ts, agent, latency_ms, ok, tokens_in, tokens_out, cost, error FROM llm_calls "
                                              "WHERE service_id=? ORDER BY id DESC LIMIT 30", (s.service_id,))]
    return {**s.llm_guard.view(), "recent": calls}


@app.get("/api/report")
async def report(user: dict = MANAGER):
    return svc().report()


@app.get("/api/report.csv")
async def report_csv(user: dict = MANAGER):
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
async def report_html(user: dict = MANAGER):
    r = svc().report()
    e = html.escape
    rows = "".join(f"<tr><td>{e(str(a))}</td><td>{e(str(b))}</td></tr>" for a, b in r["kpis"].items())
    pay = "".join(f"<tr><td>{e(a)}</td><td>{e(str(b))}</td></tr>" for a, b in r["payments"].items())
    sales = "".join(f"<tr><td>{e(d['name'])}</td><td>{d['qty']}</td><td>${d['sales']:.2f}</td></tr>" for d in r["sales_by_dish"])
    dec = "".join(f"<tr><td>{e((d['created_sim'] or '')[11:16])}</td><td>{e(d['agent'])}</td><td>{e(d['title'])}</td>"
                  f"<td>{e(d['status'])}</td></tr>" for d in r["decisions"]["items"])
    si = r["service"]
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Service report</title><style>
body{{font:14px/1.45 system-ui,sans-serif;max-width:900px;margin:24px auto;padding:0 16px;color:#1f2328;background:#fff}}
h1{{font-size:20px}} h2{{font-size:15px;margin-top:22px}} table{{border-collapse:collapse;width:100%}}
td,th{{border-bottom:1px solid #ddd;padding:4px 6px;text-align:left;font-size:13px}} .muted{{color:#666}}
@media print{{button{{display:none}}}}</style></head><body>
<button onclick="print()">Print / save as PDF</button>
<h1>{e(si.get('restaurant', ''))} · end-of-service report · {e(si['weekday'])} {e(si['date'])} {e(si['period'])}</h1>
<p class="muted">{e(si['service_id'])} · generated {e(r['generated'])} · bus {e(si['bus'])} · agents {e(si['llm_mode'])}</p>
<h2>KPIs</h2><table>{rows}</table>
<h2>Payments</h2><table>{pay}</table>
<h2>Sales by dish</h2><table><tr><th>Dish</th><th>Qty</th><th>Sales</th></tr>{sales}</table>
<h2>Decisions</h2><table><tr><th>Time</th><th>Agent</th><th>Decision</th><th>Status</th></tr>{dec}</table>
</body></html>"""
    return HTMLResponse(page)


@app.get("/api/services")
async def services(user: dict = ANY):
    return RP.list_services()


@app.get("/api/services/{service_id}/events")
async def service_events(service_id: str, after: int = 0, user: dict = ANY):
    return guard(lambda: RP.load_events(service_id, after))


@app.get("/api/baseline")
async def baseline(user: dict = ANY):
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


# =========================================================================== day close, backups, audit
@app.get("/api/dayclose/preview")
async def dayclose_preview(counted_cash: float | None = None, user: dict = MANAGER):
    return DC.reconcile(svc(), counted_cash)


@app.post("/api/dayclose")
async def dayclose(req: DayCloseReq, request: Request, user: dict = MANAGER):
    request.state.audit = {"counted_cash": req.counted_cash, "force": req.force}
    return guard(lambda: DC.close_day(svc(), req.counted_cash, user["username"], req.force))


@app.get("/api/dayclose/history")
async def dayclose_history(user: dict = MANAGER):
    m = master()
    try:
        return DC.history(m)
    finally:
        m.close()


@app.get("/api/ops/backups")
async def backups(user: dict = MANAGER):
    return OPS.list_backups()


@app.post("/api/ops/backups")
async def backup_now(user: dict = MANAGER):
    if hub.svc:
        hub.svc.save_state()
    return OPS.backup()


@app.post("/api/ops/backups/{backup_id}/verify")
async def backup_verify(backup_id: str, user: dict = MANAGER):
    return guard(lambda: OPS.verify_backup(backup_id))


@app.post("/api/ops/drill")
async def backup_drill(user: dict = MANAGER):
    if hub.svc:
        hub.svc.save_state()
    return OPS.drill()


@app.post("/api/ops/cleanup")
async def ops_cleanup(dry_run: bool = True, user: dict = MANAGER):
    keep = {hub.svc.service_id} if hub.svc else set()
    return OPS.cleanup(keep=keep, dry_run=dry_run)


@app.get("/api/admin/audit")
async def audit_log(limit: int = 200, username: str | None = None, user: dict = MANAGER):
    m = master()
    try:
        return AUTH.audit_rows(m, limit, username)
    finally:
        m.close()


# =========================================================================== admin: setup, users, restore
def _admin(fn):
    m = master()
    try:
        init_db(m)
        return guard(lambda: fn(m))
    finally:
        m.close()


@app.get("/api/admin/config")
async def admin_config(user: dict = ADMINONLY):
    return _admin(lambda m: {**ADMIN.snapshot(m), "applies_from": "the next service (Start a new service to apply)",
                             "current_service_started": hub.svc.info()["start"] if hub.svc else None})


@app.put("/api/admin/profile")
async def admin_profile(body: dict, user: dict = ADMINONLY):
    return _admin(lambda m: ADMIN.set_profile(m, body))


@app.post("/api/admin/taxes")
async def admin_tax_new(body: dict, user: dict = ADMINONLY):
    return _admin(lambda m: ADMIN.save_tax(m, body))


@app.put("/api/admin/taxes/{tax_id}")
async def admin_tax_put(tax_id: int, body: dict, user: dict = ADMINONLY):
    return _admin(lambda m: ADMIN.save_tax(m, body, tax_id))


@app.delete("/api/admin/taxes/{tax_id}")
async def admin_tax_del(tax_id: int, user: dict = ADMINONLY):
    return _admin(lambda m: ADMIN.delete_tax(m, tax_id) or {"deleted": tax_id})


@app.put("/api/admin/hours")
async def admin_hours(req: HoursReq, user: dict = ADMINONLY):
    return _admin(lambda m: ADMIN.set_hours(m, req.weekday, req.meal_period, req.open, req.close, req.closed))


@app.put("/api/admin/tables/{table_id}")
async def admin_table(table_id: str, body: dict, user: dict = ADMINONLY):
    return _admin(lambda m: ADMIN.save_table(m, {**body, "id": table_id}))


@app.delete("/api/admin/tables/{table_id}")
async def admin_table_del(table_id: str, user: dict = ADMINONLY):
    return _admin(lambda m: ADMIN.delete_table(m, table_id) or {"deleted": table_id})


@app.put("/api/admin/stations/{station_id}")
async def admin_station(station_id: str, body: dict, user: dict = ADMINONLY):
    return _admin(lambda m: ADMIN.save_station(m, {**body, "id": station_id}))


@app.delete("/api/admin/stations/{station_id}")
async def admin_station_del(station_id: str, user: dict = ADMINONLY):
    return _admin(lambda m: ADMIN.delete_station(m, station_id) or {"deleted": station_id})


@app.put("/api/admin/zones/{zone_id}")
async def admin_zone(zone_id: str, body: dict, user: dict = ADMINONLY):
    return _admin(lambda m: ADMIN.save_zone(m, {**body, "id": zone_id}))


@app.delete("/api/admin/zones/{zone_id}")
async def admin_zone_del(zone_id: str, user: dict = ADMINONLY):
    return _admin(lambda m: ADMIN.delete_zone(m, zone_id) or {"deleted": zone_id})


@app.put("/api/admin/ingredients/{ingredient_id}")
async def admin_ingredient(ingredient_id: str, body: dict, user: dict = ADMINONLY):
    return _admin(lambda m: ADMIN.save_ingredient(m, {**body, "id": ingredient_id}))


@app.delete("/api/admin/ingredients/{ingredient_id}")
async def admin_ingredient_del(ingredient_id: str, user: dict = ADMINONLY):
    return _admin(lambda m: ADMIN.delete_ingredient(m, ingredient_id) or {"deleted": ingredient_id})


@app.put("/api/admin/dishes/{dish_id}")
async def admin_dish(dish_id: str, body: dict, user: dict = ADMINONLY):
    return _admin(lambda m: ADMIN.save_dish(m, {**body, "id": dish_id}))


@app.post("/api/admin/dishes/{dish_id}/{action}")
async def admin_dish_action(dish_id: str, action: str, user: dict = ADMINONLY):
    if action not in ("retire", "restore"):
        raise HTTPException(404, "action must be retire or restore")
    return _admin(lambda m: ADMIN.retire_dish(m, dish_id, active=action == "restore"))


@app.put("/api/admin/suppliers/{supplier_id}")
async def admin_supplier(supplier_id: str, body: dict, user: dict = ADMINONLY):
    return _admin(lambda m: ADMIN.save_supplier(m, {**body, "id": supplier_id}))


@app.put("/api/admin/suppliers/{supplier_id}/items/{ingredient_id}")
async def admin_supplier_item(supplier_id: str, ingredient_id: str, req: ItemPriceReq, user: dict = ADMINONLY):
    return _admin(lambda m: ADMIN.save_supplier_item(m, supplier_id, ingredient_id, req.price, req.pack_size))


@app.delete("/api/admin/suppliers/{supplier_id}/items/{ingredient_id}")
async def admin_supplier_item_del(supplier_id: str, ingredient_id: str, user: dict = ADMINONLY):
    return _admin(lambda m: ADMIN.delete_supplier_item(m, supplier_id, ingredient_id) or {"deleted": ingredient_id})


@app.post("/api/admin/staff")
async def admin_staff_new(body: dict, user: dict = ADMINONLY):
    return _admin(lambda m: ADMIN.save_staff(m, body))


@app.put("/api/admin/staff/{staff_id}")
async def admin_staff(staff_id: int, body: dict, user: dict = ADMINONLY):
    return _admin(lambda m: ADMIN.save_staff(m, body, staff_id))


@app.get("/api/admin/roster")
async def admin_roster(date: str, meal_period: str = "dinner", user: dict = MANAGER):
    return _admin(lambda m: ADMIN.get_roster(m, date, meal_period))


@app.put("/api/admin/roster")
async def admin_roster_put(req: RosterReq, user: dict = MANAGER):
    return _admin(lambda m: ADMIN.set_roster(m, req.date, req.meal_period, req.entries))


@app.post("/api/admin/import/{kind}")
async def admin_import(kind: str, req: ImportReq, user: dict = ADMINONLY):
    from restaurant_ops.data import importer as IMP

    if kind not in ("menu", "inventory", "pos"):
        raise HTTPException(404, "kind must be menu, inventory or pos")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"{kind}.csv"
        path.write_text(req.csv)
        if kind == "menu":
            return guard(lambda: IMP.import_menu(path, settings.DB_PATH, req.dry_run))
        if kind == "inventory":
            return guard(lambda: IMP.import_inventory(path, settings.DB_PATH, replace=req.replace_stock, dry_run=req.dry_run))
        return guard(lambda: IMP.import_pos(path, settings.DB_PATH, dry_run=req.dry_run))


@app.get("/api/admin/users")
async def admin_users(user: dict = ADMINONLY):
    return _admin(AUTH.list_users)


@app.post("/api/admin/users")
async def admin_user_new(req: UserReq, user: dict = ADMINONLY):
    return _admin(lambda m: AUTH.create_user(m, req.username, req.password, req.role, req.display_name))


@app.patch("/api/admin/users/{user_id}")
async def admin_user_patch(user_id: int, req: UserPatch, user: dict = ADMINONLY):
    return _admin(lambda m: AUTH.update_user(m, user_id, req.role, req.active, req.password, req.display_name))


@app.post("/api/admin/restore/{backup_id}")
async def admin_restore(backup_id: str, user: dict = ADMINONLY):
    """Restore a verified backup in place: the current service is saved and stopped first, then a fresh one starts."""
    v = OPS.verify_backup(backup_id)
    if not v["ok"]:
        raise HTTPException(400, f"backup failed verification: {v['corrupt_or_missing']}")
    if hub.svc:
        hub.svc.save_state()
        hub.svc.close()
        hub.svc = None
    out = OPS.restore(backup_id)
    hub.start_service()
    return out


# =========================================================================== WebSockets
def _ws_user(websocket: WebSocket) -> dict | None:
    if not settings.AUTH_ENABLED:
        return {"username": "local", "role": "admin"}
    tok = _bearer(websocket.headers) or websocket.cookies.get(COOKIE)
    if not tok:
        return None
    m = master()
    try:
        return AUTH.resolve(m, tok)
    finally:
        m.close()


async def _pump(websocket: WebSocket, q: asyncio.Queue, first: dict) -> None:
    await websocket.send_text(json.dumps(first, default=str))
    while True:
        msg = await q.get()
        batch = [msg]
        while not q.empty() and len(batch) < 200:
            batch.append(q.get_nowait())
        snaps = [b for b in batch if b.get("type") == "snapshot"]
        if snaps:  # a new service replaced the state: send the snapshot alone
            await websocket.send_text(json.dumps(snaps[-1], default=str))
            batch = [b for b in batch if b.get("type") != "snapshot"]
            if not batch:
                continue
        await websocket.send_text(json.dumps({"type": "batch", "events": batch}, default=str))


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    if _ws_user(websocket) is None:
        await websocket.send_text(json.dumps({"type": "error", "payload": {"message": "sign in required", "code": 401}}))
        await websocket.close(code=4401)
        return
    q: asyncio.Queue = asyncio.Queue()
    hub.clients.add(q)
    try:
        first = {"type": "snapshot", "payload": hub.svc.snapshot()} if hub.svc else \
            {"type": "error", "payload": {"message": hub.error or "service not started"}}
        await _pump(websocket, q, first)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        hub.clients.discard(q)


@app.websocket("/ws/public")
async def ws_public(websocket: WebSocket):
    """The online-order page: menu availability, kitchen busyness, online mode and order progress (no names)."""
    await websocket.accept()
    q: asyncio.Queue = asyncio.Queue()
    hub.public.add(q)
    try:
        first = {"type": "snapshot", "payload": public_snapshot(hub.svc)} if hub.svc else \
            {"type": "error", "payload": {"message": "the restaurant is not open"}}
        await _pump(websocket, q, first)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        hub.public.discard(q)


@app.websocket("/ws/replay/{service_id}")
async def ws_replay(websocket: WebSocket, service_id: str, speed: float = 60.0):
    """Stream a logged service's events at `speed` x sim time (gaps capped at 1 s), for the UI's replay mode."""
    await websocket.accept()
    if _ws_user(websocket) is None:
        await websocket.close(code=4401)
        return
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


# =========================================================================== frontend
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
