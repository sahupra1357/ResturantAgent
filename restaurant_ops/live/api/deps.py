"""Request dependencies: sessions (cookie or Bearer), roles, the CSRF check, the service, error mapping and idempotent writes."""
from __future__ import annotations

import json
import logging

from fastapi import Depends, HTTPException, Request

from restaurant_ops import admin as ADMIN
from restaurant_ops.config import settings
from restaurant_ops.db.connection import get_conn, init_db
from restaurant_ops.live import auth as AUTH
from restaurant_ops.live import dayclose as DC
from restaurant_ops.live import events as E
from restaurant_ops.live import giftcards as GC
from restaurant_ops.live.api.hub import hub
from restaurant_ops.live.host import HostError
from restaurant_ops.live.payments import PaymentError
from restaurant_ops.live.service import Service, ServiceError

log = logging.getLogger("restaurant_ops.live")

COOKIE = "ro_session"
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "ro-ui"
WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")


def master():
    c = get_conn(settings.DB_PATH)
    return c



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




def _admin(fn):
    m = master()
    try:
        init_db(m)
        return guard(lambda: fn(m))
    finally:
        m.close()
