"""Request bodies of the REST API."""
from __future__ import annotations

from pydantic import BaseModel, Field


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
