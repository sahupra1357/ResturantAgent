"""Sign-in, sign-out, the current user and password changes."""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from restaurant_ops.config import settings
from restaurant_ops.live import auth as AUTH
from restaurant_ops.live.api.deps import ANY, COOKIE, _session_token, current_user, guard, master
from restaurant_ops.live.api.models import LoginReq, PasswordReq

log = logging.getLogger("restaurant_ops.live")
router = APIRouter()

# =========================================================================== auth endpoints
@router.post("/api/auth/login")
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


@router.post("/api/auth/logout")
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


@router.get("/api/auth/me")
async def me(request: Request):
    u = current_user(request)
    if u is None:
        raise HTTPException(401, "not signed in")
    return {k: v for k, v in u.items() if not k.startswith("_")} | {"auth_enabled": settings.AUTH_ENABLED}


@router.post("/api/auth/password")
async def change_password(req: PasswordReq, user: dict = ANY):
    m = master()
    try:
        r = m.execute("SELECT pw_hash FROM users WHERE id=?", (user["id"],)).fetchone()
        if r is None or not AUTH.verify_password(req.current_password, r[0]):
            raise HTTPException(400, "current password is wrong")
        return guard(lambda: AUTH.update_user(m, user["id"], password=req.new_password))
    finally:
        m.close()
