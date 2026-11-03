"""WebSockets: the staff stream (/ws, signed in), the public online-order stream (/ws/public) and replay."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from restaurant_ops.config import settings
from restaurant_ops.live import auth as AUTH
from restaurant_ops.live import replay as RP
from restaurant_ops.live.api.deps import COOKIE, _bearer, master
from restaurant_ops.live.api.hub import hub, public_snapshot

log = logging.getLogger("restaurant_ops.live")
router = APIRouter()

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


@router.websocket("/ws")
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


@router.websocket("/ws/public")
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


@router.websocket("/ws/replay/{service_id}")
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
