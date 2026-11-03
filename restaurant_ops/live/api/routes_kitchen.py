"""Kitchen: the kitchen display, food safety, prep and purchase-order output."""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from restaurant_ops.live import outputs as OUT
from restaurant_ops.live import po_channel as PO
from restaurant_ops.live.api.deps import ANY, KITCHEN, MANAGER, guard, svc
from restaurant_ops.live.api.models import BumpReq, HaccpReq

log = logging.getLogger("restaurant_ops.live")
router = APIRouter()

# =========================================================================== kitchen: KDS, safety, prep
@router.get("/api/kds")
async def kds(station: str | None = None, include_bumped: bool = False, user: dict = KITCHEN):
    s = svc()
    return {"station": station, "clock": s.clock.hhmm(), "stations": list(s.kitchen.stations),
            "tickets": OUT.tickets(s, station, include_bumped)}


@router.post("/api/kds/bump")
async def kds_bump(req: BumpReq, user: dict = KITCHEN):
    return {"bumped": guard(lambda: OUT.bump(svc(), req.order_id, req.station, user["username"]))}


@router.get("/api/safety")
async def safety(user: dict = ANY):
    s = svc()
    return {**s.safety.view(), "temps": s.safety.temps()}


@router.post("/api/safety/checklist/{item_id}")
async def haccp_done(item_id: int, req: HaccpReq, user: dict = KITCHEN):
    return guard(lambda: svc().safety.complete(item_id, user["username"], req.ok, req.note))


@router.post("/api/safety/log")
async def temp_log_now(user: dict = KITCHEN):
    return guard(lambda: svc().safety.log())


@router.get("/api/prep")
async def prep(user: dict = ANY):
    return svc().prep.view()


# =========================================================================== purchasing outputs
@router.get("/api/purchase-orders")
async def purchase_orders(user: dict = MANAGER):
    s = svc()
    s.flush()
    return PO.outbox(s.conn)


@router.get("/api/purchase-orders/{po_id}.pdf")
async def purchase_order_pdf(po_id: str, user: dict = MANAGER):
    s = svc()
    s.flush()
    if s.conn.execute("SELECT 1 FROM purchase_orders WHERE po_id=?", (po_id,)).fetchone() is None:
        raise HTTPException(404, "unknown PO")
    _, lines = PO.po_lines(s.conn, po_id, s.profile)
    return Response(PO.text_pdf(lines, f"Purchase order {po_id}"), media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{po_id}.pdf"'})


@router.post("/api/purchase-orders/{po_id}/resend")
async def purchase_order_resend(po_id: str, user: dict = MANAGER):
    s = svc()
    s.flush()
    return guard(lambda: PO.send_po(s.conn, po_id, s.profile))
