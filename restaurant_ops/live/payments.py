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

METHODS = ("card", "cash", "wallet", "online", "gift_card", "points")
PROCESSOR_METHODS = ("card", "wallet", "online")


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
    def _category(self, line: dict) -> str:
        if line.get("is_fee"):
            return "delivery_fee"
        return (getattr(self.svc, "tax_category", {}) or {}).get(line.get("dish_id"), "food")

    def tax_lines(self, c: Check, sub: float, disc: float) -> list[dict]:
        """Tax per rule: each line's share of the bill (after discounts, pro rata) x every active rule that covers its
        category ('all' covers everything) and channel. Without rules: TAX_RATE on food."""
        share = (1 - disc / sub) if sub > 0 else 0.0
        by_cat: dict[str, float] = {}
        for l in c.lines:
            by_cat[self._category(l)] = by_cat.get(self._category(l), 0.0) + l["price"] * share
        rules = getattr(self.svc, "tax_rules", None)
        if not rules:
            return [{"name": "Sales tax", "jurisdiction": "", "rate": settings.TAX_RATE,
                     "amount": _r(by_cat.get("food", 0.0) * settings.TAX_RATE)}]
        out = []
        for r in rules:
            if r["channel"] not in ("all", c.channel):
                continue
            base = sum(v for k, v in by_cat.items() if r["applies_to"] in ("all", k))
            if base > 0:
                out.append({"name": r["name"], "jurisdiction": r["jurisdiction"], "rate": r["rate"], "amount": _r(base * r["rate"])})
        return out

    def totals(self, c: Check) -> dict:
        sub = sum(l["price"] for l in c.lines)
        disc = min(sub, sum(d["amount"] for d in c.discounts) + sum(x["amount"] for x in c.comps))
        taxable = max(0.0, sub - disc)
        tlines = self.tax_lines(c, sub, disc)
        tax = sum(t["amount"] for t in tlines)
        due = taxable + tax
        paid = sum(p["amount"] for p in c.payments)
        tips = sum(p.get("tip", 0.0) for p in c.payments)
        refunded = sum(r["amount"] for r in c.refunds)
        return {"subtotal": _r(sub), "discounts": _r(disc), "tax": _r(tax), "tax_lines": tlines, "due": _r(due), "paid": _r(paid),
                "tips": _r(tips), "refunded": _r(refunded), "balance": _r(max(0.0, due - paid))}

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

    def _record(self, c: Check, kind: str, amount: float, tip: float = 0.0, method: str = "", note: str = "", ref: str | None = None) -> None:
        self.svc.conn.execute("INSERT INTO payments (service_id, check_id, ts, kind, amount, tip, method, note, processor_ref) "
                              "VALUES (?,?,?,?,?,?,?,?,?)", (self.svc.service_id, c.check_id, self.svc.clock.iso(), kind, _r(amount),
                                                             _r(tip), method, note, ref))

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
            publish: bool = True, token: str | None = None, gift_code: str | None = None) -> dict:
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
        if split_index is not None and (not (0 <= split_index < len(c.splits)) or c.splits[split_index]["paid"]):
            raise PaymentError("invalid or already paid split")
        ref = self._take_money(c, method, amount, tip, token, gift_code)
        if split_index is not None:
            c.splits[split_index]["paid"] = True
        c.payments.append({"amount": _r(amount), "tip": _r(tip), "method": method, "at": self.svc.clock.hhmm(), "split": split_index,
                           "ref": ref})
        self.collected += amount
        self.tips += tip
        self._record(c, "payment", amount, tip, method, ref=ref)
        if self.totals(c)["balance"] <= 0.009:
            self._close(c)
        if publish:
            self.publish(c)
        return self.view(c)

    def _take_money(self, c: Check, method: str, amount: float, tip: float, token: str | None, gift_code: str | None) -> str | None:
        """Move the money: card / wallet / online through the processor, gift card from its balance, points from the
        guest's loyalty balance; cash needs nothing. Returns the external reference."""
        from restaurant_ops.live import giftcards as GC
        from restaurant_ops.live.processor import ProcessorError

        if method in PROCESSOR_METHODS:
            try:
                return self.svc.processor.charge(amount + tip, settings.CURRENCY, token, f"{self.svc.service_id} {c.check_id}")["id"]
            except ProcessorError as e:
                raise PaymentError(str(e)) from e
        if method == "gift_card":
            if not gift_code:
                raise PaymentError("gift card code required")
            if tip:
                raise PaymentError("tips can't go on a gift card")
            with self.svc.master() as m:
                try:
                    GC.redeem(m, gift_code, amount, ref=c.check_id, service_id=self.svc.service_id)
                except GC.GiftCardError as e:
                    raise PaymentError(str(e)) from e
            return f"gc:{gift_code.strip().upper()}"
        if method == "points":
            if not c.guest_id:
                raise PaymentError("points need a guest profile on the check")
            value = float(self.svc.profile.get("loyalty", {}).get("redeem_value_per_point", 0.01))
            need = int(round(amount / value)) if value else 10 ** 9
            g = self.svc.crm.get(c.guest_id)
            if g is None or g["points"] < need:
                raise PaymentError(f"needs {need} points; the guest has {g['points'] if g else 0}")
            self.svc.crm.spend_points(c.guest_id, need)
            return f"pts:{need}"
        return None

    def _close(self, c: Check) -> None:
        c.status, c.closed_t = "paid", self.svc.clock.t
        if c.guest_id:
            t = self.totals(c)
            p = self.svc.floor.parties.get(c.party_id or "")
            self.svc.crm.record_visit(c.guest_id, self.svc.service_id, self.svc.date.isoformat(), t["paid"],
                                      p.size if p else 1, [l["dish_id"] for l in c.lines if l.get("dish_id")])
        self.svc.on_check_paid(c)

    def discount(self, check_id: str, reason: str, pct: float | None = None, amount: float | None = None, by: str = "manager",
                 role: str = "manager") -> dict:
        c = self._get(check_id)
        if c.status != "open":
            raise PaymentError(f"check {check_id} is {c.status}")
        t = self.totals(c)
        amt = float(amount) if amount is not None else t["subtotal"] * float(pct or 0) / 100
        already = sum(d["amount"] for d in c.discounts)
        caps = getattr(self.svc, "profile", {}).get("discount_max_pct", {}) if hasattr(self.svc, "profile") else {}
        cap = float(caps.get(role, caps.get("manager", settings.DISCOUNT_MAX_PCT))) if role != "admin" else float(caps.get("manager", 100))
        self._guard("discount", {"check_id": check_id, "amount": amt + already, "subtotal": t["subtotal"], "reason": reason,
                                 "max_pct": cap})
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
        ref = self._return_money(c, float(amount))
        c.refunds.append({"amount": _r(amount), "reason": reason, "by": by, "at": self.svc.clock.hhmm(), "ref": ref})
        self.refunded += float(amount)
        if self.totals(c)["refunded"] >= t["paid"] - 0.009:
            c.status = "refunded"
        self._record(c, "refund", float(amount), note=reason, ref=ref)
        self.publish(c)
        return self.view(c)

    def _return_money(self, c: Check, amount: float) -> str | None:
        """Refund to the most recent payment's instrument (processor refund, gift card credit, or cash)."""
        from restaurant_ops.live import giftcards as GC
        from restaurant_ops.live.processor import ProcessorError

        last = next((p for p in reversed(c.payments) if p.get("method") != "points"), None)
        if last is None:
            return None
        ref = last.get("ref") or ""
        if last["method"] in PROCESSOR_METHODS and ref:
            try:
                return self.svc.processor.refund(ref, amount)["id"]
            except ProcessorError as e:
                raise PaymentError(str(e)) from e
        if last["method"] == "gift_card" and ref.startswith("gc:"):
            with self.svc.master() as m:
                GC.credit(m, ref[3:], amount, ref=c.check_id, service_id=self.svc.service_id)
            return ref
        return "cash"

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

    def __init__(self, n: int, zones: dict | None = None):
        self.drivers = [Driver(f"D{i + 1}") for i in range(max(1, n))]
        self.zones = dict(zones or settings.DELIVERY_ZONES)

    def zone(self, z: str) -> dict:
        if z not in self.zones:
            raise PaymentError(f"delivery zone must be one of {', '.join(self.zones)}")
        return self.zones[z]

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
