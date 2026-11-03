"""Guests and tables: arrivals, check-in and the waiting area, seating, a party's timers, leaving. Mixed into `live.service.Service`."""
from __future__ import annotations

from restaurant_ops.live import events as E
from restaurant_ops.live.floor import Party
from restaurant_ops.live.world import BAR_SHARE, CHECK_IN_MIN, EARLY_HOLD_MAX_MIN, INCOMPLETE_MIN, INCOMPLETE_SHARE


class FloorOps:
    # ================================================================== floor
    def _arrive(self, size: int, kind: str, t: float, preorder: bool = False, guest_id: int | None = None,
                booking_id: str | None = None, name: str = "", patience: object = "default") -> Party:
        self._n_party += 1
        p = Party(f"P{self._n_party:03d}", int(size), kind, t, preorder=preorder, guest_id=guest_id, booking_id=booking_id,
                  name=name)
        self.floor.parties[p.id] = p
        self.metrics.parties += 1
        self.metrics.covers_arrived += p.size
        seats = self.floor.find_seating(p.size, prefer_bar=kind == "walk_in" and p.size <= 2 and self.guests.chance(BAR_SHARE))
        if seats and not preorder and kind in ("walk_in", "reservation"):
            # guests check in with the host first (name, coats, the right table) and wait in the lobby meanwhile
            hold = self.guests.uniform(CHECK_IN_MIN[0], CHECK_IN_MIN[1])
            p.wait_reason = "checking in"
            b = self.host.bookings.get(booking_id) if booking_id else None
            if b is not None and (b.time_t - self.clock.t) / 60 - 2 > hold:
                hold = min(EARLY_HOLD_MAX_MIN, (b.time_t - self.clock.t) / 60 - 2)
                p.wait_reason = f"early for {self.host.hhmm(b.time_t)}"
            elif kind == "walk_in" and p.size >= 3 and self.guests.uniform(0, 1) < INCOMPLETE_SHARE:
                hold += self.guests.uniform(INCOMPLETE_MIN[0], INCOMPLETE_MIN[1])
                p.wait_reason = "waiting for the rest of the party"
            p.greet_until = self.clock.t + hold * 60
            p.patience_until = None
            p.prefer_tables = seats
            self.floor.waitlist.append(p.id)
            self.host.on_waitlisted(p)
            seats = None
            checking_in = True
        else:
            checking_in = False
        if seats:
            self._seat(p, seats, self.clock.t)
        elif not checking_in:
            if preorder or kind == "reservation" or patience is None:
                p.patience_until = None  # booked guests and named waitlist entries wait for their table
            else:
                p.patience_until = self.clock.t + self.guests.minutes("patience") * 60
            self.floor.waitlist.append(p.id)
            self.host.on_waitlisted(p)
        self.bus.publish(E.PARTY_ARRIVED, {"party_id": p.id, "size": p.size, "kind": kind, "seated": bool(seats), "checking_in": checking_in,
                                           "table_ids": p.table_ids, "waitlist": len(self.floor.waitlist),
                                           "guest_id": guest_id, "booking_id": booking_id}, source="floor")
        if not seats:
            self.host.publish()
        return p

    def _seat(self, p: Party, seats: list[str], t: float) -> None:
        self.floor.seat(p, seats, t)
        self.metrics.covers_seated += p.size
        self.host.on_seated(p)
        self.host.quotes.pop(p.id, None)
        if p.food_order:
            p.state = "eating" if p.food_done else "entrees_fired"
            self.floor.set_state(p, p.state)
            if p.food_done and p.check_at is None:
                p.check_at = t + self.guests.minutes("eat") * 60
        else:
            p.next_at = t + self.guests.minutes("to_order") * 60
        self._tables(p)

    def _tables(self, p: Party) -> None:
        self.bus.publish(E.TABLE_UPDATED, {"tables": [self.floor.table_view(tid, self.clock.t) for tid in p.table_ids],
                                           "party": {"party_id": p.id, "size": p.size, "state": p.state, "kind": p.kind}},
                         source="floor")

    def _seat_waiting(self, t: float) -> None:
        for pid in list(self.floor.waitlist):
            p = self.floor.parties[pid]
            if p.patience_until is not None and t > p.patience_until:
                self.floor.waitlist.remove(pid)
                p.state, p.gave_up = "left", True
                self.metrics.covers_lost += p.size
                self.host.quotes.pop(pid, None)
                self.bus.publish(E.PARTY_LEFT, {"party_id": pid, "size": p.size, "reason": "gave up waiting"}, source="floor")
                self.host.publish()
                continue
            if p.greet_until is not None and t < p.greet_until:
                continue  # still checking in at the host stand
            pref = getattr(p, "prefer_tables", None)
            seats = pref if pref and all(self.floor.free(x) for x in pref) else self.floor.find_seating(p.size)
            if seats:
                self._seat(p, seats, t)
                self.host.publish()

    def _party_timers(self, t: float) -> None:
        for p in list(self.floor.parties.values()):
            if p.state == "seated" and p.next_at is not None and p.next_at <= t and not p.food_order:
                p.next_at = None
                self._place_food_order(p)
            if p.dessert_at is not None and p.dessert_at <= t and p.state == "eating":
                p.dessert_at = None
                items = self.guests.dessert_order(p.size, self.available_map(), set(self.menu.specials))
                if items:
                    self.submit_order("dine_in", items, party=p, kind="dessert")
            if p.state == "eating" and p.check_at is not None and p.check_at <= t:
                p.check_at = None
                p.state = "check"
                c = self.payments.open_for_party(p)
                p.check_id = c.check_id
                p.leave_at = t + self.guests.minutes("pay") * 60
                if p.kind == "manual":
                    p.pay_by = t + 20 * 60  # a hand-placed table pays through the API (or at the terminal after 20 min)
                self.floor.set_state(p, "check")
                self._tables(p)
            elif p.state == "check" and p.leave_at is not None and p.leave_at <= t:
                c = self.payments.checks.get(p.check_id or "")
                if c is not None and c.status == "open":
                    if p.kind == "manual" and (p.pay_by or 0) > t:
                        continue
                    split = 2 if p.size >= 4 and self.guests.aux_chance(0.2) else 1
                    self.payments.settle_sim(c, tip_pct=self.guests.uniform(0.15, 0.22), split_ways=split)
                else:
                    self._party_leaves(p, t)
            elif p.state == "bussing" and p.bus_at is not None and p.bus_at <= t:
                tids = list(p.table_ids)
                for tid in tids:
                    self.floor.release(tid)
                p.state = "left"
                self.bus.publish(E.TABLE_UPDATED, {"tables": [self.floor.table_view(tid, t) for tid in tids]}, source="floor")

    def _party_leaves(self, p: Party, t: float) -> None:
        if p.state != "check":
            return
        p.state = "bussing"
        p.leave_at = None
        p.bus_at = t + self.guests.minutes("bus") * 60
        self.floor.set_state(p, "bussing")
        self.bus.publish(E.PARTY_LEFT, {"party_id": p.id, "size": p.size, "reason": "paid"}, source="floor")
        self._tables(p)

    def on_check_paid(self, c) -> None:
        """Payments closed a check: the party leaves (dine-in) and the guest's visit is on their profile."""
        p = self.floor.parties.get(c.party_id or "")
        if p is not None:
            self._party_leaves(p, self.clock.t)

    def _place_food_order(self, p: Party) -> dict:
        o = self.guests.party_order(p.size, self.available_map(), set(self.menu.specials), large=p.kind == "large")
        return self.submit_order("dine_in", o["items"], notes=o["notes"], party=p, kind="food")

    def walk_in(self, size: int, kind: str = "walk_in", preorder: bool = False) -> dict:
        p = self._arrive(size, kind, self.clock.t, preorder=preorder)
        out = {"party_id": p.id, "seated": p.state != "waiting", "table_ids": p.table_ids}
        if preorder:
            out["order"] = self._place_food_order(p)
        return out
