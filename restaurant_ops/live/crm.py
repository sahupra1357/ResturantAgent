"""Guest profiles: persistent allergies and preferences, visit history and loyalty points / tiers.

Profiles live in the `guests` table (one row per guest, phone unique). The Order agent merges a profile's
allergies into every order linked to that guest, so a regular with a nut allergy is protected even when the
server forgets to write it down. Payments add a visit, spend and points (LOYALTY_POINTS_PER_DOLLAR); tiers come
from LOYALTY_TIERS.
"""
from __future__ import annotations

import sqlite3

import numpy as np

from restaurant_ops.config import settings
from restaurant_ops.live.events import wall_now
from restaurant_ops.live.tools import parse_mods

FIRST = ["Ava", "Liam", "Maya", "Noah", "Zoe", "Omar", "Iris", "Leo", "Nina", "Ravi", "Sofia", "Kenji", "Lena", "Mateo", "Priya",
         "Jonas", "Amara", "Theo", "Hana", "Diego"]
LAST = ["Park", "Okafor", "Silva", "Novak", "Haddad", "Ito", "Moreau", "Kowalski", "Reyes", "Lindqvist", "Mensah", "Costa"]
REGULAR_ALLERGIES = ["nuts", "milk", "gluten", "crustaceans,molluscs", "eggs", "fish"]
PROFILE_FIELDS = ("name", "phone", "email", "allergies", "preferences", "notes")


def _norm_allergies(v) -> str:
    if v is None:
        return ""
    items = v if isinstance(v, (list, tuple, set)) else [x for x in str(v).split(",")]
    out: set[str] = set()
    for x in items:
        x = str(x).strip().lower()
        if not x:
            continue
        parsed = parse_mods(f"{x} allergy")["allergens"]
        out.update(parsed or [x])
    return ",".join(sorted(out))


def tier_for(points: int, tiers: dict | None = None) -> str:
    t = "Member"
    for name, need in sorted((tiers or settings.LOYALTY_TIERS).items(), key=lambda kv: kv[1]):
        if points >= need:
            t = name
    return t


class CRM:
    def __init__(self, conn: sqlite3.Connection, loyalty: dict | None = None):
        """loyalty = the restaurant profile's {points_per_dollar, tiers, redeem_value_per_point} (admin-editable)."""
        self.conn = conn
        self.loyalty = loyalty or {"points_per_dollar": settings.LOYALTY_POINTS_PER_DOLLAR, "tiers": settings.LOYALTY_TIERS,
                                   "redeem_value_per_point": 0.01}

    def __getstate__(self):
        return {}

    @staticmethod
    def view(r) -> dict | None:
        if r is None:
            return None
        d = dict(r)
        d["allergies"] = [a for a in (d.get("allergies") or "").split(",") if a]
        return d

    def get(self, guest_id: int) -> dict | None:
        return self.view(self.conn.execute("SELECT * FROM guests WHERE guest_id=?", (int(guest_id),)).fetchone())

    def by_phone(self, phone: str) -> dict | None:
        return self.view(self.conn.execute("SELECT * FROM guests WHERE phone=?", (phone,)).fetchone()) if phone else None

    def search(self, q: str = "", limit: int = 25) -> list[dict]:
        like = f"%{q.strip()}%"
        rows = self.conn.execute("SELECT * FROM guests WHERE name LIKE ? OR phone LIKE ? OR email LIKE ? "
                                 "ORDER BY total_spend DESC, guest_id LIMIT ?", (like, like, like, limit)).fetchall()
        return [self.view(r) for r in rows]

    def upsert(self, name: str, phone: str | None = None, email: str | None = None, allergies=None,
               preferences: str | None = None, notes: str | None = None) -> dict:
        cur = self.by_phone(phone) if phone else None
        if cur:
            return self.update(cur["guest_id"], name=name, email=email, allergies=allergies, preferences=preferences, notes=notes)
        c = self.conn.execute("INSERT INTO guests (name, phone, email, allergies, preferences, notes, created) VALUES (?,?,?,?,?,?,?)",
                              (name, phone or None, email, _norm_allergies(allergies), preferences or "", notes or "", wall_now()))
        return self.get(c.lastrowid)

    def update(self, guest_id: int, **fields) -> dict:
        sets, args = [], []
        for k, v in fields.items():
            if k not in PROFILE_FIELDS or v is None:
                continue
            sets.append(f"{k}=?")
            args.append(_norm_allergies(v) if k == "allergies" else v)
        if sets:
            self.conn.execute(f"UPDATE guests SET {', '.join(sets)} WHERE guest_id=?", (*args, int(guest_id)))
        g = self.get(guest_id)
        if g is None:
            raise KeyError(f"unknown guest {guest_id}")
        return g

    def record_visit(self, guest_id: int, service_id: str, date: str, spend: float, party_size: int = 0,
                     dishes: list[str] | None = None) -> dict:
        g = self.get(guest_id)
        if g is None:
            raise KeyError(f"unknown guest {guest_id}")
        pts = int(g["points"] + round(spend * float(self.loyalty["points_per_dollar"])))
        self.conn.execute("UPDATE guests SET visits=visits+1, total_spend=total_spend+?, points=?, tier=?, last_visit=? WHERE guest_id=?",
                          (round(spend, 2), pts, tier_for(pts, self.loyalty["tiers"]), date, int(guest_id)))
        self.conn.execute("INSERT INTO guest_visits (guest_id, service_id, date, party_size, spend, dishes) VALUES (?,?,?,?,?,?)",
                          (int(guest_id), service_id, date, party_size, round(spend, 2), ",".join(dishes or [])))
        return self.get(guest_id)

    def spend_points(self, guest_id: int, points: int) -> dict:
        g = self.get(guest_id)
        if g is None or g["points"] < points:
            raise ValueError("not enough points")
        self.conn.execute("UPDATE guests SET points=points-? WHERE guest_id=?", (int(points), int(guest_id)))
        return self.get(guest_id)

    def visits(self, guest_id: int, limit: int = 20) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT date, party_size, spend, dishes FROM guest_visits WHERE guest_id=? "
                                                   "ORDER BY id DESC LIMIT ?", (int(guest_id), limit))]

    def seed_regulars(self, seed: int, n: int = 30) -> list[int]:
        """Synthetic regulars (once per database): some carry allergies, some have loyalty history."""
        have = [r[0] for r in self.conn.execute("SELECT guest_id FROM guests ORDER BY guest_id")]
        if have:
            return have
        rng = np.random.default_rng([seed, 23])
        ids = []
        for i in range(n):
            name = f"{FIRST[i % len(FIRST)]} {LAST[(i * 7) % len(LAST)]}"
            allergies = REGULAR_ALLERGIES[int(rng.integers(0, len(REGULAR_ALLERGIES)))] if rng.random() < 0.25 else ""
            g = self.upsert(name, phone=f"555-01{i:02d}", email=f"guest{i}@example.com", allergies=allergies,
                            preferences="window seat" if rng.random() < 0.2 else "")
            pts = int(rng.integers(0, 1800))
            self.conn.execute("UPDATE guests SET visits=?, total_spend=?, points=?, tier=? WHERE guest_id=?",
                              (int(pts // 80), float(pts), pts, tier_for(pts), g["guest_id"]))
            ids.append(g["guest_id"])
        return ids
