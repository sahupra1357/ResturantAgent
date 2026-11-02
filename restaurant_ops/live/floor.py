"""Virtual dining floor of the 80-seat bistro: 20 tables + an 8-seat bar, parties and their table states.

Table states (color-coded in the UI):
    available -> seated -> ordered -> entrees_fired -> eating -> check -> bussing -> available
A party may span several tables (a party of 12 takes two pushed-together 6-tops) or a pair of bar seats.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

STATES = ["available", "seated", "ordered", "entrees_fired", "eating", "check", "bussing"]
SERVERS = ["Ana", "Ben", "Chloe", "Dev"]


@dataclass
class Table:
    id: str
    seats: int
    x: float
    y: float
    w: float
    h: float
    shape: str          # 'round' | 'rect' | 'bar'
    section: str        # server
    state: str = "available"
    party_id: str | None = None
    since: float = 0.0  # sim seconds when the party sat (for elapsed time)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Party:
    id: str
    size: int
    kind: str                  # 'reservation' | 'walk_in' | 'large' | 'manual'
    arrive_at: float
    table_ids: list[str] = field(default_factory=list)
    state: str = "waiting"     # waiting -> seated ... -> left
    seated_at: float | None = None
    next_at: float | None = None
    order_ids: list[str] = field(default_factory=list)
    allergies: list[str] = field(default_factory=list)
    preorder: bool = False     # orders straight away (party of 12)
    gave_up: bool = False
    patience_until: float | None = None
    dessert_at: float | None = None
    check_at: float | None = None
    leave_at: float | None = None
    bus_at: float | None = None
    food_order: str | None = None
    food_done: bool = False
    guest_id: int | None = None
    booking_id: str | None = None
    name: str = ""
    check_id: str | None = None
    pay_by: float | None = None   # manual parties: auto-settle at the terminal after this

    def to_dict(self) -> dict:
        return asdict(self)


def build_layout() -> list[Table]:
    """20 tables (8 two-tops, 8 four-tops, 4 six-tops = 72 seats) + 8 bar seats = 80 seats. Coordinates in a 1000x640 plan."""
    t: list[Table] = []
    n = 1
    # two-tops along the window (top row)
    for i in range(8):
        t.append(Table(f"T{n}", 2, 70 + i * 105, 40, 62, 62, "round", SERVERS[0] if i < 4 else SERVERS[1]))
        n += 1
    # four-tops, two rows in the middle
    for r in range(2):
        for i in range(4):
            t.append(Table(f"T{n}", 4, 80 + i * 150, 170 + r * 120, 96, 72, "rect", SERVERS[1 + r]))
            n += 1
    # six-tops on the right (pairs can be pushed together)
    for r in range(2):
        for i in range(2):
            t.append(Table(f"T{n}", 6, 690 + i * 140, 160 + r * 140, 120, 84, "rect", SERVERS[3]))
            n += 1
    # bar (bottom left), 8 seats
    for i in range(8):
        t.append(Table(f"B{i + 1}", 1, 80 + i * 58, 470, 44, 44, "bar", SERVERS[2]))
    return t


LAYOUT_FIXTURES = {
    "host_stand": {"x": 900, "y": 470, "w": 80, "h": 50, "label": "Host"},
    "kitchen_pass": {"x": 640, "y": 560, "w": 340, "h": 50, "label": "Kitchen pass"},
    "bar_counter": {"x": 70, "y": 525, "w": 470, "h": 26, "label": "Bar"},
}
# tables that can be pushed together for big parties
COMBOS = [("T17", "T18"), ("T19", "T20"), ("T17", "T19"), ("T18", "T20"), ("T13", "T14"), ("T15", "T16"),
          ("T9", "T10"), ("T11", "T12")]


class Floor:
    def __init__(self):
        self.tables: dict[str, Table] = {t.id: t for t in build_layout()}
        self.parties: dict[str, Party] = {}
        self.waitlist: list[str] = []

    @property
    def seats(self) -> int:
        return sum(t.seats for t in self.tables.values())

    def free(self, tid: str) -> bool:
        return self.tables[tid].state == "available"

    def find_seating(self, size: int, prefer_bar: bool = False) -> list[str] | None:
        """Smallest table that fits; bar pairs for 1-2; pushed-together tables for big parties."""
        dining = sorted((t for t in self.tables.values() if t.shape != "bar" and t.state == "available"),
                        key=lambda t: (t.seats, t.id))
        bar = [t for t in sorted(self.tables.values(), key=lambda t: int(t.id[1:])) if t.shape == "bar"]
        if size <= 2 and prefer_bar:
            got = self._bar(bar, size)
            if got:
                return got
        for t in dining:
            if t.seats >= size and t.seats - size <= 2:
                return [t.id]
        for t in dining:
            if t.seats >= size:
                return [t.id]
        if size <= 2:
            got = self._bar(bar, size)
            if got:
                return got
        for combo in COMBOS:
            if all(self.free(c) for c in combo) and sum(self.tables[c].seats for c in combo) >= size:
                return list(combo)
        return None

    def _bar(self, bar: list[Table], size: int) -> list[str] | None:
        for i, t in enumerate(bar):
            if t.state != "available":
                continue
            if size == 1:
                return [t.id]
            if i + 1 < len(bar) and bar[i + 1].state == "available":
                return [t.id, bar[i + 1].id]
        return None

    def seat(self, party: Party, table_ids: list[str], now: float) -> None:
        party.table_ids = list(table_ids)
        party.state = "seated"
        party.seated_at = now
        for tid in table_ids:
            t = self.tables[tid]
            t.state, t.party_id, t.since = "seated", party.id, now
        if party.id in self.waitlist:
            self.waitlist.remove(party.id)

    def set_state(self, party: Party, state: str) -> list[Table]:
        party.state = state
        out = []
        for tid in party.table_ids:
            t = self.tables[tid]
            t.state = state if state != "left" else "bussing"
            if state == "available":
                t.party_id = None
            out.append(t)
        return out

    def release(self, tid: str) -> Table:
        t = self.tables[tid]
        t.state, t.party_id = "available", None
        return t

    def party_of_table(self, tid: str) -> Party | None:
        pid = self.tables[tid].party_id
        return self.parties.get(pid) if pid else None

    def table_view(self, tid: str, now: float) -> dict:
        t = self.tables[tid]
        d = t.to_dict()
        p = self.parties.get(t.party_id) if t.party_id else None
        d["party_size"] = p.size if p else 0
        d["party_kind"] = p.kind if p else None
        d["elapsed_min"] = round((now - t.since) / 60, 0) if p else 0
        d["joined"] = [x for x in p.table_ids if x != tid] if p else []
        return d

    def occupancy(self) -> dict:
        occ = sum(t.seats for t in self.tables.values() if t.state not in ("available", "bussing"))
        guests = sum(p.size for p in self.parties.values() if p.state not in ("waiting", "left"))
        return {"seats": self.seats, "occupied_seats": occ, "guests_in_house": guests,
                "waiting_parties": len(self.waitlist), "tables_free": sum(1 for t in self.tables.values() if t.state == "available")}
