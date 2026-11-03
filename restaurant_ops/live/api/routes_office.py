"""Back office: day close, backups and the restore drill, retention cleanup, the audit log."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from restaurant_ops.live import auth as AUTH
from restaurant_ops.live import dayclose as DC
from restaurant_ops.live import ops as OPS
from restaurant_ops.live.api.deps import MANAGER, guard, master, svc
from restaurant_ops.live.api.hub import hub
from restaurant_ops.live.api.models import DayCloseReq

log = logging.getLogger("restaurant_ops.live")
router = APIRouter()

# =========================================================================== day close, backups, audit
@router.get("/api/dayclose/preview")
async def dayclose_preview(counted_cash: float | None = None, user: dict = MANAGER):
    return DC.reconcile(svc(), counted_cash)


@router.post("/api/dayclose")
async def dayclose(req: DayCloseReq, request: Request, user: dict = MANAGER):
    request.state.audit = {"counted_cash": req.counted_cash, "force": req.force}
    return guard(lambda: DC.close_day(svc(), req.counted_cash, user["username"], req.force))


@router.get("/api/dayclose/history")
async def dayclose_history(user: dict = MANAGER):
    m = master()
    try:
        return DC.history(m)
    finally:
        m.close()


@router.get("/api/ops/backups")
async def backups(user: dict = MANAGER):
    return OPS.list_backups()


@router.post("/api/ops/backups")
async def backup_now(user: dict = MANAGER):
    if hub.svc:
        hub.svc.save_state()
    return OPS.backup()


@router.post("/api/ops/backups/{backup_id}/verify")
async def backup_verify(backup_id: str, user: dict = MANAGER):
    return guard(lambda: OPS.verify_backup(backup_id))


@router.post("/api/ops/drill")
async def backup_drill(user: dict = MANAGER):
    if hub.svc:
        hub.svc.save_state()
    return OPS.drill()


@router.post("/api/ops/cleanup")
async def ops_cleanup(dry_run: bool = True, user: dict = MANAGER):
    keep = {hub.svc.service_id} if hub.svc else set()
    return OPS.cleanup(keep=keep, dry_run=dry_run)


@router.get("/api/admin/audit")
async def audit_log(limit: int = 200, username: str | None = None, user: dict = MANAGER):
    m = master()
    try:
        return AUTH.audit_rows(m, limit, username)
    finally:
        m.close()
