"""Front of house: table orders, the public online-order endpoints, bookings, waitlist, tables, checks and payments, gift cards and guest profiles."""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from restaurant_ops.live import giftcards as GC
from restaurant_ops.live.api.deps import ANY, MANAGER, SERVER, guard, idempotent, svc
from restaurant_ops.live.api.hub import public_order
from restaurant_ops.live.api.models import (
    BookingReq,
    DiscountReq,
    GiftReq,
    GuestPatch,
    GuestReq,
    ModeReq,
    MoveReq,
    NotifyReq,
    OnlineOrderReq,
    OverrideReq,
    PayReq,
    QuoteReq,
    RefundReq,
    SeatReq,
    SplitReq,
    TableOpReq,
    TableOrderReq,
    VoidReq,
    WaitlistReq,
    _expand,
)

log = logging.getLogger("restaurant_ops.live")
router = APIRouter()

# =========================================================================== orders & online
@router.post("/api/orders")
async def table_order(req: TableOrderReq, request: Request, user: dict = SERVER):
    return idempotent(request, "orders", lambda: svc().manual_order(req.table_id, _expand(req.items), req.notes, req.party_size,
                                                                    req.guest_id))


@router.get("/api/online/menu")
async def online_menu():
    return svc().online_menu()


@router.post("/api/online/quote")
async def online_quote(req: QuoteReq):
    return guard(lambda: svc().quote_preview(req.dish_ids, req.fulfilment, req.zone))


@router.post("/api/online/orders")
async def online_order(req: OnlineOrderReq, request: Request):
    out = idempotent(request, "online", lambda: svc().place_online(_expand(req.items), req.customer, req.fulfilment, req.zone, req.phone))
    if out.get("online"):
        out = {**out, "online": public_order(out["online"])}
    return out


@router.get("/api/online/orders/{order_id}")
async def online_get(order_id: str):
    s = svc()
    o = s.online.orders.get(order_id)
    if o is None:
        raise HTTPException(404, "unknown order")
    return public_order(s.online.view(o, s.clock))


@router.post("/api/online/mode")
async def online_mode(req: ModeReq, user: dict = MANAGER):
    return guard(lambda: svc().set_online_mode(req.mode, f"manager {user['username']}"))


@router.post("/api/online/orders/{order_id}/override")
async def online_override(order_id: str, req: OverrideReq, user: dict = MANAGER):
    return guard(lambda: svc().override_quote(order_id, req.ready_hhmm))


@router.post("/api/online/orders/{order_id}/notify")
async def online_notify(order_id: str, req: NotifyReq, user: dict = MANAGER):
    return guard(lambda: svc().notify_customer(order_id, req.text))


# =========================================================================== host stand
@router.get("/api/reservations")
async def reservations(user: dict = ANY):
    return svc().host.bookings_view()


@router.post("/api/reservations")
async def create_booking(req: BookingReq, user: dict = SERVER):
    if req.allow_overbook and user["role"] not in ("manager", "admin"):
        raise HTTPException(403, "only a manager can allow an overbooking")
    s = svc()
    b = guard(lambda: s.host.create(req.name, req.party_size, req.time, phone=req.phone, notes=req.notes, source="phone",
                                    simulate=req.simulate_arrival, allow_overbook=req.allow_overbook))
    return next(x for x in s.host.bookings_view() if x["booking_id"] == b.booking_id)


@router.post("/api/reservations/{booking_id}/{action}")
async def booking_action(booking_id: str, action: str, user: dict = SERVER):
    s = svc()
    if action == "arrive":
        return guard(lambda: s.host.arrive(booking_id))
    if action in ("cancel", "no_show"):
        status = "cancelled" if action == "cancel" else "no_show"
        guard(lambda: s.host.set_status(booking_id, status))
        return next(x for x in s.host.bookings_view() if x["booking_id"] == booking_id)
    raise HTTPException(404, "action must be arrive, cancel or no_show")


@router.get("/api/waitlist")
async def waitlist(user: dict = ANY):
    s = svc()
    return {"waitlist": s.host.waitlist_view(), "quote_for": {n: s.host.quote_wait(n) for n in (2, 4, 6, 8)}}


@router.post("/api/waitlist")
async def add_waitlist(req: WaitlistReq, user: dict = SERVER):
    return guard(lambda: svc().host.add_to_waitlist(req.name, req.party_size, req.phone, req.notes))


@router.post("/api/waitlist/{party_id}/seat")
async def seat_waitlist(party_id: str, req: SeatReq, user: dict = SERVER):
    return guard(lambda: svc().host.seat_from_waitlist(party_id, req.table_ids))


@router.post("/api/tables/move")
async def tables_move(req: MoveReq, user: dict = SERVER):
    return guard(lambda: svc().host.move(req.party_id, req.table_ids))


@router.post("/api/tables/combine")
async def tables_combine(req: TableOpReq, user: dict = SERVER):
    return guard(lambda: svc().host.combine(req.party_id, req.table_id))


@router.post("/api/tables/split")
async def tables_split(req: TableOpReq, user: dict = SERVER):
    return guard(lambda: svc().host.split(req.party_id, req.table_id))


# =========================================================================== checks, payments, gift cards
@router.get("/api/checks")
async def checks(status: str | None = None, user: dict = ANY):
    s = svc()
    return [s.payments.view(c) for c in s.payments.checks.values() if status in (None, c.status)]


@router.post("/api/checks/{check_id}/split")
async def check_split(check_id: str, req: SplitReq, user: dict = SERVER):
    return guard(lambda: svc().payments.split(check_id, req.mode, req.ways))


@router.post("/api/checks/{check_id}/pay")
async def check_pay(check_id: str, req: PayReq, request: Request, user: dict = SERVER):
    request.state.audit = {"check_id": check_id, "amount": req.amount, "tip": req.tip, "method": req.method}
    return guard(lambda: svc().payments.pay(check_id, req.amount, req.tip, req.method, req.split_index, token=req.token,
                                            gift_code=req.gift_code))


@router.post("/api/checks/{check_id}/discount")
async def check_discount(check_id: str, req: DiscountReq, request: Request, user: dict = SERVER):
    request.state.audit = {"check_id": check_id, "pct": req.pct, "amount": req.amount, "reason": req.reason}
    return guard(lambda: svc().payments.discount(check_id, req.reason, req.pct, req.amount, by=user["username"], role=user["role"]))


@router.post("/api/checks/{check_id}/refund")
async def check_refund(check_id: str, req: RefundReq, request: Request, user: dict = MANAGER):
    request.state.audit = {"check_id": check_id, "amount": req.amount, "reason": req.reason}
    return guard(lambda: svc().payments.refund(check_id, req.amount, req.reason, by=user["username"]))


@router.post("/api/giftcards")
async def giftcard_issue(req: GiftReq, request: Request, user: dict = MANAGER):
    request.state.audit = {"amount": req.amount, "note": req.note}
    with svc().master() as m:
        return guard(lambda: GC.issue(m, req.amount, req.note or f"issued by {user['username']}", svc().service_id))


@router.get("/api/giftcards/{code}")
async def giftcard_get(code: str, user: dict = SERVER):
    with svc().master() as m:
        g = guard(lambda: GC.get(m, code))
        return {**g, "ledger": GC.ledger(m, code)}


@router.post("/api/giftcards/{code}/void")
async def giftcard_void(code: str, req: VoidReq, user: dict = MANAGER):
    with svc().master() as m:
        return guard(lambda: GC.void(m, code, req.reason))


# =========================================================================== guests (CRM)
@router.get("/api/guests")
async def guests(q: str = "", user: dict = SERVER):
    return svc().crm.search(q)


@router.post("/api/guests")
async def guest_create(req: GuestReq, user: dict = SERVER):
    return guard(lambda: svc().crm.upsert(req.name, req.phone, req.email, req.allergies, req.preferences, req.notes))


@router.get("/api/guests/{guest_id}")
async def guest_get(guest_id: int, user: dict = SERVER):
    s = svc()
    g = s.crm.get(guest_id)
    if g is None:
        raise HTTPException(404, "unknown guest")
    return {**g, "visits_history": s.crm.visits(guest_id)}


@router.patch("/api/guests/{guest_id}")
async def guest_patch(guest_id: int, req: GuestPatch, user: dict = SERVER):
    return guard(lambda: svc().crm.update(guest_id, **req.model_dump(exclude_none=True)))
