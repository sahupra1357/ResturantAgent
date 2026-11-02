"""Checks and payments (dine-in bills, splits, tips, discounts, refunds) and delivery dispatch for online orders.

A check opens when a party asks for the bill: every item of the party's orders that reached the kitchen, at
menu price. due = subtotal - discounts - comps + tax. Payments may be split evenly or by seat; each payment can carry
a tip. Discounts above DISCOUNT_MAX_PCT and refunds above what was paid are refused by the constraint checker
(core/constraints.py). Online orders are prepaid; deliveries add a zone fee and go to the first free driver once the
food is ready.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING

from restaurant_ops.config import settings
from restaurant_ops.core import constraints as C
from restaurant_ops.data import catalog as cat
from restaurant_ops.live import events as E
from restaurant_ops.live.events import wall_now

if TYPE_CHECKING:  # pragma: no cover
    from restaurant_ops.live.service import Service

METHODS = ("card", "cash", "wallet", "online")


class PaymentError(ValueError):
    pass


@dataclass
class Check:
    check_id: str
    party_id: str | None
    table_ids: list[str]
    lines: list[dict]
    guest_id: int | None = None
    discounts: list[dict] = field(default_factory=list)
    comps: list[dict] = field(default_factory=list)
    payments: list[dict] = field(default_factory=list)
    refunds: list[dict] = field(default_factory=list)
    splits: list[dict] = field(default_factory=list)
    status: str = "open"            # open -> paid (-> refunded)
    opened_t: float = 0.0
    closed_t: float | None = None
    channel: str = "dine_in"


def _r(x: float) -> float:
    return round(x + 1e-9, 2)


class Payments:
    def __init__(self, svc: "Service"):
        self.svc = svc
        self.checks: dict[str, Check] = {}
        self._n = 0
        self.tips = 0.0
        self.collected = 0.0
        self.refunded = 0.0
        self.discounted = 0.0

    def __getstate__(self):
        return {k: v for k, v in self.__dict__.items() if k != "svc"}

    # ------------------------------------------------------------------ totals
    @staticmethod
    def totals(c: Check) -> dict:
        sub = sum(l["price"] for l in c.lines)
        disc = sum(d["amount"] for d in c.discounts) + sum(x["amount"] for x in c.comps)
        taxable = max(0.0, sub - disc)
        fee = sum(l.get("fee", 0.0) for l in c.lines if l.get("is_fee"))
        tax = taxable * settings.TAX_RATE if c.channel == "dine_in" else (taxable - fee) * settings.TAX_RATE
        due = taxable + tax
        paid = sum(p["amount"] for p in c.payments)
        tips = sum(p.get("tip", 0.0) for p in c.payments)
        refunded = sum(r["amount"] for r in c.refunds)
        return {"subtotal": _r(sub), "discounts": _r(disc), "tax": _r(tax), "due": _r(due), "paid": _r(paid), "tips": _r(tips),
                "refunded": _r(refunded), "balance": _r(max(0.0, due - paid))}

    def view(self, c: Check) -> dict:
        d = asdict(c)
        d.update(self.totals(c))
        d["opened"] = self.svc.clock.iso(c.opened_t)
        d["closed"] = self.svc.clock.iso(c.closed_t) if c.closed_t is not None else None
        return d

    def _get(self, check_id: str) -> Check:
        c = self.checks.get(check_id)
        if c is None:
            raise PaymentError(f"unknown check {check_id}")
        return c

    def _record(self, c: Check, kind: str, amount: float, tip: float = 0.0, method: str = "", note: str = "") -> None:
        self.svc.conn.execute("INSERT INTO payments (service_id, check_id, ts, kind, amount, tip, method, note) VALUES (?,?,?,?,?,?,?,?)",
                              (self.svc.service_id, c.check_id, self.svc.clock.iso(), kind, _r(amount), _r(tip), method, note))

    def publish(self, c: Check) -> None:
        self.svc.bus.publish(E.CHECK_UPDATED, self.view(c), source="payments")

    # ------------------------------------------------------------------ open
    def open_for_party(self, party) -> Check:
        svc = self.svc
        for c in self.checks.values():
            if c.party_id == party.id and c.status == "open":
                return c
        lines = []
        for o in svc.orders.values():
            if o.get("party_id") != party.id:
                continue
            for it in o["items"]:
                lines.append({"item_id": it["item_id"], "dish_id": it["dish_id"], "name": it["name"],
                              "price": cat.DISHES_BY_ID[it["dish_id"]].price, "seat": it.get("seat")})
        self._n += 1
        c = Check(f"C{self._n:04d}", party.id, list(party.table_ids), lines, guest_id=party.guest_id, opened_t=svc.clock.t)
        self.checks[c.check_id] = c
        self.publish(c)
        return c

    def open_online(self, order: dict, fee: float, guest_id: int | None, method: str = "online") -> Check:
        """Online orders are paid at checkout (items + delivery fee + tax)."""
        lines = [{"item_id": i["item_id"], "dish_id": i["dish_id"], "name": i["name"], "price": cat.DISHES_BY_ID[i["dish_id"]].price}
                 for i in order["items"]]
        if fee:
            lines.append({"item_id": "fee", "dish_id": None, "name": "Delivery fee", "price": fee, "is_fee": True, "fee": fee})
        self._n += 1
        c = Check(f"C{self._n:04d}", None, [], lines, guest_id=guest_id, opened_t=self.svc.clock.t, channel="online")
        self.checks[c.check_id] = c
        t = self.totals(c)
        self.pay(c.check_id, t["due"], tip=0.0, method=method, publish=False)
        self.publish(c)
        return c

    def add_lines(self, check_id: str, items: list[dict]) -> None:
        c = self._get(check_id)
        for it in items:
            c.lines.append({"item_id": it["item_id"], "dish_id": it["dish_id"], "name": it["name"],
                            "price": cat.DISHES_BY_ID[it["dish_id"]].price})

    # ------------------------------------------------------------------ operations
    def split(self, check_id: str, mode: str = "even", ways: int = 2) -> dict:
        c = self._get(check_id)
        if c.status != "open":
            raise PaymentError(f"check {check_id} is {c.status}")
        t = self.totals(c)
        if mode == "even":
            if ways < 2 or ways > 20:
                raise PaymentError("split into 2-20 ways")
            each = math.floor(t["balance"] / ways * 100) / 100
            amounts = [each] * (ways - 1) + [_r(t["balance"] - each * (ways - 1))]
            c.splits = [{"label": f"{i + 1}/{ways}", "amount": a, "paid": False} for i, a in enumerate(amounts)]
        elif mode == "seat":
            seats: dict = {}
            for l in c.lines:
                seats.setdefault(l.get("seat") or 0, 0.0)
                seats[l.get("seat") or 0] += l["price"]
            factor = t["due"] / t["subtotal"] if t["subtotal"] else 1.0
            c.splits = [{"label": f"seat {s}" if s else "shared", "amount": _r(v * factor), "paid": False} for s, v in sorted(seats.items())]
        else:
            raise PaymentError("mode must be 'even' or 'seat'")
        self.publish(c)
        return self.view(c)

    def pay(self, check_id: str, amount: float, tip: float = 0.0, method: str = "card", split_index: int | None = None,
            publish: bool = True) -> dict:
        c = self._get(check_id)
        if c.status != "open":
            raise PaymentError(f"check {check_id} is {c.status}")
        if method not in METHODS:
            raise PaymentError(f"method must be one of {METHODS}")
        amount, tip = float(amount), float(tip or 0)
        if amount <= 0 or tip < 0:
            raise PaymentError("amount must be > 0 and tip >= 0")
        bal = self.totals(c)["balance"]
        if amount > bal + 0.01:
            raise PaymentError(f"amount {amount:.2f} exceeds the balance {bal:.2f}")
        if split_index is not None:
            if not (0 <= split_index < len(c.splits)) or c.splits[split_index]["paid"]:
                raise PaymentError("invalid or already paid split")
            c.splits[split_index]["paid"] = True
        c.payments.append({"amount": _r(amount), "tip": _r(tip), "method": method, "at": self.svc.clock.hhmm(), "split": split_index})
        self.collected += amount
        self.tips += tip
        self._record(c, "payment", amount, tip, method)
        if self.totals(c)["balance"] <= 0.009:
            self._close(c)
        if publish:
            self.publish(c)
        return self.view(c)

    def _close(self, c: Check) -> None:
        c.status, c.closed_t = "paid", self.svc.clock.t
        if c.guest_id:
            t = self.totals(c)
            p = self.svc.floor.parties.get(c.party_id or "")
            self.svc.crm.record_visit(c.guest_id, self.svc.service_id, self.svc.date.isoformat(), t["paid"],
                                      p.size if p else 1, [l["dish_id"] for l in c.lines if l.get("dish_id")])
        self.svc.on_check_paid(c)

    def discount(self, check_id: str, reason: str, pct: float | None = None, amount: float | None = None, by: str = "manager") -> dict:
        c = self._get(check_id)
        if c.status != "open":
            raise PaymentError(f"check {check_id} is {c.status}")
        t = self.totals(c)
        amt = float(amount) if amount is not None else t["subtotal"] * float(pct or 0) / 100
        already = sum(d["amount"] for d in c.discounts)
        self._guard("discount", {"check_id": check_id, "amount": amt + already, "subtotal": t["subtotal"], "reason": reason})
        c.discounts.append({"amount": _r(amt), "reason": reason, "by": by, "at": self.svc.clock.hhmm()})
        self.discounted += amt
        self._record(c, "discount", amt, note=reason)
        self.publish(c)
        return self.view(c)

    def comp(self, check_id: str, amount: float, reason: str) -> None:
        c = self._get(check_id)
        c.comps.append({"amount": _r(amount), "reason": reason})
        self._record(c, "comp", amount, note=reason)
        self.publish(c)

    def refund(self, check_id: str, amount: float, reason: str, by: str = "manager") -> dict:
        c = self._get(check_id)
        t = self.totals(c)
        self._guard("refund", {"check_id": check_id, "amount": float(amount), "paid": t["paid"] - t["refunded"], "reason": reason})
        c.refunds.append({"amount": _r(amount), "reason": reason, "by": by, "at": self.svc.clock.hhmm()})
        self.refunded += float(amount)
        if self.totals(c)["refunded"] >= t["paid"] - 0.009:
            c.status = "refunded"
        self._record(c, "refund", float(amount), note=reason)
        self.publish(c)
        return self.view(c)

    def _guard(self, action_type: str, payload: dict) -> None:
        self.svc.flush()
        viol = C.blocking(C.check_action({"action_type": action_type, "payload": payload}, self.svc.conn, self.svc.date))
        if viol:
            raise PaymentError("; ".join(v.message for v in viol))

    def settle_sim(self, c: Check, tip_pct: float, split_ways: int = 1) -> None:
        """A simulated party pays: optional even split, card, tip on the pre-tax subtotal."""
        t = self.totals(c)
        if t["balance"] <= 0.009:  # nothing to pay (every item was rejected or comped)
            self._close(c)
            self.publish(c)
            return
        if split_ways > 1:
            self.split(c.check_id, "even", split_ways)
            for i, s in enumerate(list(c.splits)):
                self.pay(c.check_id, s["amount"], tip=_r(t["subtotal"] * tip_pct / split_ways), method="card", split_index=i,
                         publish=False)
        else:
            self.pay(c.check_id, t["balance"], tip=_r(t["subtotal"] * tip_pct), method="card", publish=False)
        self.publish(c)

    def open_checks(self) -> list[dict]:
        return [self.view(c) for c in self.checks.values() if c.status == "open"]

    def summary(self) -> dict:
        return {"collected": _r(self.collected), "tips": _r(self.tips), "refunded": _r(self.refunded),
                "discounted": _r(self.discounted), "open_checks": sum(1 for c in self.checks.values() if c.status == "open"),
                "closed_checks": sum(1 for c in self.checks.values() if c.status != "open")}


# =========================================================================== delivery dispatch


@dataclass
class Driver:
    driver_id: str
    free_t: float = 0.0
    trips: int = 0
    current: str | None = None


class Dispatcher:
    """Drivers take ready delivery orders first come, first served; a trip is out and back (2 x drive time)."""

    def __init__(self, n: int):
        self.drivers = [Driver(f"D{i + 1}") for i in range(max(1, n))]

    @staticmethod
    def zone(z: str) -> dict:
        if z not in settings.DELIVERY_ZONES:
            raise PaymentError(f"delivery zone must be one of {', '.join(settings.DELIVERY_ZONES)}")
        return settings.DELIVERY_ZONES[z]

    def driver_wait_min(self, at_t: float) -> float:
        return max(0.0, (min(d.free_t for d in self.drivers) - at_t) / 60)

    def assign(self, order_id: str, ready_t: float, zone: str) -> dict:
        z = self.zone(zone)
        d = min(self.drivers, key=lambda x: (x.free_t, x.driver_id))
        depart = max(ready_t, d.free_t)
        arrive = depart + z["drive_min"] * 60
        d.free_t = arrive + z["drive_min"] * 60
        d.trips += 1
        d.current = order_id
        return {"driver_id": d.driver_id, "depart_t": depart, "arrive_t": arrive}

    def view(self, now: float) -> list[dict]:
        return [{"driver_id": d.driver_id, "busy": d.free_t > now, "back_in_min": round(max(0.0, (d.free_t - now) / 60), 0),
                 "trips": d.trips, "current": d.current if d.free_t > now else None} for d in self.drivers]


def now_ts() -> str:
    return wall_now()
