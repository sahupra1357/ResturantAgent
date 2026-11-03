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
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from restaurant_ops import admin as ADMIN
from restaurant_ops.config import PROJECT_ROOT, obsolete_settings, settings
from restaurant_ops.db.connection import init_db
from restaurant_ops.live import auth as AUTH
from restaurant_ops.live import ops as OPS
from restaurant_ops.live import persistence as PERSIST
from restaurant_ops.live.api import routes_admin, routes_auth, routes_floor, routes_kitchen, routes_office, routes_service, ws
from restaurant_ops.live.api.deps import WRITE_METHODS, master
from restaurant_ops.live.api.hub import Hub, hub  # noqa: F401  Hub is re-exported for tests and tools

log = logging.getLogger("restaurant_ops.live")
DIST = PROJECT_ROOT / "frontend" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    OPS.configure_logging()
    for key, hint in obsolete_settings():
        log.warning("setting %s is no longer used (%s); remove it from your environment / .env", key, hint)
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
for r in (routes_auth, routes_service, routes_floor, routes_kitchen, routes_office, routes_admin, ws):
    app.include_router(r.router)


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

