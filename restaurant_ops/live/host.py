"""Host stand: bookings (create / cancel / no-show / arrive), overbooking control, the waitlist with quoted waits,
and table operations (move a party, push tables together, split them apart).

Simulated bookings for tonight come from the `reservations` table (via GuestSim) and arrive by themselves;
bookings taken through the API arrive when the host marks them (or by themselves with simulate=True).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from restaurant_ops.config import settings
from restaurant_ops.live import events as E
from restaurant_ops.live.events import wall_now

if TYPE_CHECKING:  # pragma: no cover
    from restaurant_ops.live.service import Service

# expected minutes until a table frees, by its state (dinner averages)
REMAINING_MIN = {"seated": 75, "ordered": 62, "entrees_fired": 48, "eating": 28, "check": 9, "bussing": 4}
WINDOW_MIN = 45  # bookings within +/- this many minutes compete for the same seats
STATUSES = ("booked", "arrived", "seated", "no_show", "cancelled")


@dataclass
class Booking:
    booking_id: str
    name: str
    size: int
    time_t: float                 # sim seconds from service start (the booked time)
    status: str = "booked"
    phone: str = ""
    guest_id: int | None = None
    notes: str = ""
    source: str = "phone"
    arrive_t: float | None = None  # when the guest will turn up (simulation); None = not simulated / no-show
    party_id: str | None = None
    overbooked: bool = False


class HostError(ValueError):
    pass


class HostStand:
    def __init__(self, svc: "Service"):
        self.svc = svc
        self.bookings: dict[str, Booking] = {}
        self.quotes: dict[str, dict] = {}   # party_id -> {quoted_min, at}
        self._n = 0

    def __getstate__(self):
        return {k: v for k, v in self.__dict__.items() if k != "svc"}

    # ------------------------------------------------------------------ time helpers
    def t_of(self, hhmm: str) -> float:
        hh, mm = (int(x) for x in hhmm.split(":"))
        start = self.svc.clock.start
        at = datetime.combine(start.date(), datetime.min.time()).replace(hour=hh, minute=mm)
        return (at - start).total_seconds()

    def hhmm(self, t: float) -> str:
        return (self.svc.clock.start + timedelta(seconds=t)).strftime("%H:%M")

    # ------------------------------------------------------------------ bookings
    def booked_covers_near(self, t: float, exclude: str | None = None) -> int:
        return sum(b.size for b in self.bookings.values() if b.booking_id != exclude and b.status in ("booked", "arrived", "seated")
                   and abs(b.time_t - t) <= WINDOW_MIN * 60)

    def capacity_check(self, t: float, size: int) -> dict:
        near = self.booked_covers_near(t)
        cap = int(self.svc.floor.seats * settings.OVERBOOK_FACTOR)
        return {"booked_near": near, "limit": cap, "ok": near + size <= cap}

    def create(self, name: str, size: int, hhmm: str | None = None, t: float | None = None, phone: str = "", notes: str = "",
               guest_id: int | None = None, source: str = "phone", simulate: bool = False, arrive_t: float | None = None,
               allow_overbook: bool = False, publish: bool = True) -> Booking:
        if size < 1 or size > 20:
            raise HostError("party size must be 1-20")
        t = self.t_of(hhmm) if hhmm else float(t if t is not None else self.svc.clock.t)
        end = (self.svc.clock.end - self.svc.clock.start).total_seconds()
        if t < 0 or t > end:
            raise HostError(f"booking time must be within tonight's service ({self.hhmm(0)}-{self.hhmm(end)})")
        cap = self.capacity_check(t, size)
        if not cap["ok"] and not allow_overbook:
            raise HostError(f"overbooked: {cap['booked_near']} covers already booked around {self.hhmm(t)} "
                            f"(limit {cap['limit']}); a manager can allow it")
        if phone and guest_id is None:
            g = self.svc.crm.by_phone(phone)
            guest_id = g["guest_id"] if g else self.svc.crm.upsert(name, phone=phone)["guest_id"]
        self._n += 1
        b = Booking(f"B{self._n:03d}", name, int(size), t, phone=phone or "", guest_id=guest_id, notes=notes or "", source=source,
                    arrive_t=arrive_t if arrive_t is not None else (t if simulate else None), overbooked=not cap["ok"])
        self.bookings[b.booking_id] = b
        self._save(b)
        if publish:
            self.publish()
        return b

    def _save(self, b: Booking) -> None:
        self.svc.conn.execute("INSERT OR REPLACE INTO bookings (booking_id, service_id, name, phone, party_size, time, status, guest_id, "
                              "notes, source, created_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                              (b.booking_id, self.svc.service_id, b.name, b.phone, b.size, self.hhmm(b.time_t), b.status,
                               b.guest_id, b.notes, b.source, wall_now()))

    def _get(self, booking_id: str) -> Booking:
        b = self.bookings.get(booking_id)
        if b is None:
            raise HostError(f"unknown booking {booking_id}")
        return b

    def set_status(self, booking_id: str, status: str) -> Booking:
        b = self._get(booking_id)
        if status not in STATUSES:
            raise HostError(f"status must be one of {STATUSES}")
        if b.status in ("seated", "cancelled", "no_show") and status in ("cancelled", "no_show"):
            raise HostError(f"booking {booking_id} is already {b.status}")
        b.status = status
        b.arrive_t = None if status in ("cancelled", "no_show") else b.arrive_t
        self._save(b)
        self.publish()
        return b

    def arrive(self, booking_id: str) -> dict:
        """The guest is here: the party enters the floor (seated now or put on the waitlist)."""
        b = self._get(booking_id)
        if b.status != "booked":
            raise HostError(f"booking {booking_id} is {b.status}")
        b.status = "arrived"
        b.arrive_t = None
        p = self.svc._arrive(b.size, "reservation", self.svc.clock.t, guest_id=b.guest_id, booking_id=b.booking_id)
        b.party_id = p.id
        if p.state != "waiting":
            b.status = "seated"
        self._save(b)
        self.publish()
        return {"booking_id": b.booking_id, "party_id": p.id, "status": b.status, "table_ids": p.table_ids}

    def on_seated(self, party) -> None:
        if getattr(party, "booking_id", None) and party.booking_id in self.bookings:
            b = self.bookings[party.booking_id]
            b.status = "seated"
            self._save(b)

    def step(self, t0: float, t1: float) -> None:
        """Simulated arrivals and automatic no-shows in (t0, t1]."""
        changed = False
        for b in sorted(self.bookings.values(), key=lambda x: (x.arrive_t or 0, x.booking_id)):
            if b.status == "booked" and b.arrive_t is not None and t0 < b.arrive_t <= t1:
                self.arrive(b.booking_id)
                changed = True
            elif b.status == "booked" and b.arrive_t is None and b.source == "sim" and t1 > b.time_t + settings.NO_SHOW_GRACE_MIN * 60:
                b.status = "no_show"
                self._save(b)
                self.svc.bus.publish(E.AGENT_LOG, {"agent": "host", "message": f"{b.name} ({b.size}) no-show for {self.hhmm(b.time_t)}"},
                                     source="host")
                changed = True
        if changed:
            self.publish()

    # ------------------------------------------------------------------ waitlist
    def quote_wait(self, size: int, ahead: list | None = None) -> float:
        """Minutes until a fitting table should free up, after the parties already waiting for one."""
        fl = self.svc.floor
        if fl.find_seating(size):
            return 0.0
        now = self.svc.clock.t
        frees = []
        for t in fl.tables.values():
            if t.shape == "bar" and size > 2:
                continue
            if t.seats < size and t.shape != "bar":
                continue
            if t.state == "available":
                frees.append(0.0)
                continue
            p = fl.parties.get(t.party_id) if t.party_id else None
            rem = REMAINING_MIN.get(t.state, 30)
            if p is not None and t.state == "seated":
                rem = max(5.0, rem - (now - (p.seated_at or now)) / 60)
            frees.append(float(rem))
        if size > 6:
            frees = [max(a, b) for a, b in zip(sorted(frees), sorted(frees)[1:])]  # two tables pushed together
        frees.sort()
        waiting = ahead if ahead is not None else [fl.parties[pid] for pid in fl.waitlist if fl.parties[pid].size <= size + 2]
        k = min(len(frees) - 1, len(waiting)) if frees else 0
        return round(frees[k] + 3, 0) if frees else 30.0

    def add_to_waitlist(self, name: str, size: int, phone: str = "", notes: str = "") -> dict:
        guest_id = None
        if phone:
            g = self.svc.crm.by_phone(phone) or self.svc.crm.upsert(name, phone=phone)
            guest_id = g["guest_id"]
        quote = self.quote_wait(size)
        p = self.svc._arrive(size, "walk_in", self.svc.clock.t, guest_id=guest_id, name=name, patience=None)
        if p.state == "waiting":
            self.quotes[p.id] = {"quoted_min": quote, "at": self.svc.clock.t}
        self.publish()
        return {"party_id": p.id, "seated": p.state != "waiting", "table_ids": p.table_ids, "quoted_min": quote if p.state == "waiting" else 0}

    def on_waitlisted(self, party) -> None:
        if party.greet_until is not None:
            self.quotes[party.id] = {"quoted_min": round((party.greet_until - self.svc.clock.t) / 60, 0), "at": self.svc.clock.t}
            return
        if party.id not in self.quotes:
            ahead = [self.svc.floor.parties[pid] for pid in self.svc.floor.waitlist
                     if pid != party.id and self.svc.floor.parties[pid].size <= party.size + 2]
            self.quotes[party.id] = {"quoted_min": self.quote_wait(party.size, ahead), "at": self.svc.clock.t}

    def waitlist_view(self) -> list[dict]:
        fl, now = self.svc.floor, self.svc.clock.t
        out = []
        for i, pid in enumerate(fl.waitlist):
            p = fl.parties[pid]
            q = self.quotes.get(pid, {})
            waited = (now - p.arrive_at) / 60
            checking_in = p.greet_until is not None and now < p.greet_until
            out.append({"party_id": pid, "size": p.size, "name": p.name or f"Party {pid}", "kind": p.kind, "position": i + 1,
                        "checking_in": checking_in, "reason": (getattr(p, "wait_reason", "") or "checking in") if checking_in else "no table yet",
                        "waited_min": round(waited, 0), "quoted_min": q.get("quoted_min"),
                        "remaining_min": max(0.0, round((q.get("quoted_min") or 0) - waited, 0)),
                        "guest_id": p.guest_id, "preorder": p.preorder})
        return out

    def seat_from_waitlist(self, party_id: str, table_ids: list[str] | None = None) -> dict:
        fl = self.svc.floor
        if party_id not in fl.waitlist:
            raise HostError(f"party {party_id} is not waiting")
        p = fl.parties[party_id]
        seats = table_ids or fl.find_seating(p.size)
        if not seats:
            raise HostError(f"no free table fits a party of {p.size}")
        self._check_tables(seats, p.size)
        self.svc._seat(p, list(seats), self.svc.clock.t)
        self.publish()
        return {"party_id": party_id, "table_ids": p.table_ids}

    # ------------------------------------------------------------------ table operations
    def _check_tables(self, table_ids: list[str], size: int, party_id: str | None = None) -> None:
        fl = self.svc.floor
        for tid in table_ids:
            if tid not in fl.tables:
                raise HostError(f"unknown table {tid}")
            t = fl.tables[tid]
            if t.state != "available" and t.party_id != party_id:
                raise HostError(f"{tid} is {t.state}")
        if sum(fl.tables[t].seats for t in table_ids) < size:
            raise HostError(f"{'+'.join(table_ids)} seat {sum(fl.tables[t].seats for t in table_ids)}, party is {size}")

    def move(self, party_id: str, table_ids: list[str]) -> dict:
        fl = self.svc.floor
        p = fl.parties.get(party_id)
        if p is None or not p.table_ids:
            raise HostError(f"party {party_id} is not seated")
        self._check_tables(table_ids, p.size, party_id)
        old = list(p.table_ids)
        state = p.state
        for tid in old:
            if tid not in table_ids:
                fl.release(tid)
        for tid in table_ids:
            t = fl.tables[tid]
            t.state, t.party_id, t.since = state if state != "left" else "bussing", p.id, p.seated_at or self.svc.clock.t
        p.table_ids = list(table_ids)
        self._publish_tables(set(old) | set(table_ids), p)
        return {"party_id": party_id, "from": old, "to": p.table_ids}

    def combine(self, party_id: str, table_id: str) -> dict:
        p = self.svc.floor.parties.get(party_id)
        if p is None or not p.table_ids:
            raise HostError(f"party {party_id} is not seated")
        if table_id in p.table_ids:
            raise HostError(f"{table_id} is already part of the party's tables")
        return self.move(party_id, [*p.table_ids, table_id])

    def split(self, party_id: str, table_id: str) -> dict:
        p = self.svc.floor.parties.get(party_id)
        if p is None or table_id not in (p.table_ids or []):
            raise HostError(f"{table_id} is not one of party {party_id}'s tables")
        if len(p.table_ids) < 2:
            raise HostError("a single table cannot be split off")
        rest = [t for t in p.table_ids if t != table_id]
        if sum(self.svc.floor.tables[t].seats for t in rest) < p.size:
            raise HostError(f"party of {p.size} does not fit {'+'.join(rest)}")
        return self.move(party_id, rest)

    def _publish_tables(self, tids, p) -> None:
        self.svc.bus.publish(E.TABLE_UPDATED, {"tables": [self.svc.floor.table_view(t, self.svc.clock.t) for t in sorted(tids)],
                                               "party": {"party_id": p.id, "size": p.size, "state": p.state, "kind": p.kind}},
                             source="host")

    # ------------------------------------------------------------------ views
    def bookings_view(self) -> list[dict]:
        out = []
        for b in sorted(self.bookings.values(), key=lambda x: (x.time_t, x.booking_id)):
            d = asdict(b)
            d["time"] = self.hhmm(b.time_t)
            d.pop("arrive_t", None)
            d.pop("time_t", None)
            out.append(d)
        return out

    def publish(self) -> None:
        self.svc.bus.publish(E.HOST_UPDATED, {"bookings": self.bookings_view(), "waitlist": self.waitlist_view()}, source="host")
