"""Guest simulator: arrivals (reservations + a forecast-driven walk-in rate), dish choices from the forecast dish mix
with random mods and allergies (~10% of orders), and a separate online-order stream peaking 6-8 pm.

All randomness comes from one seeded generator, so a service with the same seed and the same manager actions
replays identically.
"""
from __future__ import annotations

import math

import numpy as np

from restaurant_ops.data import catalog as cat
from restaurant_ops.live.intraday import SLOT_MIN

PARTY_SIZES = {1: 0.12, 2: 0.48, 3: 0.15, 4: 0.17, 5: 0.05, 6: 0.03}
MEAN_PARTY = sum(k * v for k, v in PARTY_SIZES.items())
NO_SHOW = 0.05
MOD_RATE = 0.10
# (guest note, kind) used for the ~10% of orders with mods
MOD_TEXTS = ["nut allergy", "no dairy", "gluten free", "shellfish allergy", "egg allergy", "no onion please",
             "dairy free", "allergic to fish", "no garlic", "coeliac, no gluten"]
ONLINE_CURVE = [0.05, 0.09, 0.15, 0.18, 0.18, 0.14, 0.09, 0.06, 0.04, 0.02]
ONLINE_PER_92_COVERS = 26.0
SPECIAL_BOOST = 1.6


def _density(curve: list[float], t_min: float) -> float:
    """Arrivals per minute (share) at t."""
    k = int(t_min // SLOT_MIN)
    return curve[k] / SLOT_MIN if 0 <= k < len(curve) else 0.0


class GuestSim:
    def __init__(self, seed: int, forecast, reservations: list[dict], curve: list[float]):
        self.rng = np.random.default_rng([seed, 11])
        self.fc = forecast
        self.curve = curve
        self.walkin_mult = 1.0
        self.online_mult = 1.0
        booked = 0
        self.bookings: list[dict] = []
        for r in reservations:
            if self.rng.random() < NO_SHOW:
                continue
            slot = int(self.rng.choice(len(curve), p=np.asarray(curve) / sum(curve)))
            t = (slot * SLOT_MIN + float(self.rng.uniform(0, SLOT_MIN))) * 60
            self.bookings.append({"t": t, "size": int(r["party_size"]), "kind": r.get("kind", "standard")})
            booked += int(r["party_size"])
        self.bookings.sort(key=lambda b: b["t"])
        self.booked_covers = booked
        self._next_booking = 0

    # ------------------------------------------------------------------ arrivals
    def walkin_total(self) -> float:
        return max(0.0, float(self.fc.base.get("p50", 0)) - self.booked_covers)

    def arrivals(self, t0: float, t1: float) -> list[dict]:
        """Parties arriving in (t0, t1] sim seconds: [{t, size, kind}]."""
        out = []
        while self._next_booking < len(self.bookings) and self.bookings[self._next_booking]["t"] <= t1:
            b = self.bookings[self._next_booking]
            out.append({"t": b["t"], "size": b["size"], "kind": "reservation" if b["kind"] == "standard" else "private"})
            self._next_booking += 1
        dt_min = (t1 - t0) / 60
        if dt_min > 0:
            rate_covers = self.walkin_total() * self.walkin_mult * _density(self.curve, t0 / 60)
            n = int(self.rng.poisson(rate_covers / MEAN_PARTY * dt_min))
            for _ in range(n):
                size = int(self.rng.choice(list(PARTY_SIZES), p=list(PARTY_SIZES.values())))
                out.append({"t": float(self.rng.uniform(t0, t1)), "size": size, "kind": "walk_in"})
        return sorted(out, key=lambda a: a["t"])

    def online_arrivals(self, t0: float, t1: float) -> int:
        dt_min = (t1 - t0) / 60
        if dt_min <= 0:
            return 0
        total = ONLINE_PER_92_COVERS * float(self.fc.base.get("p50", 92)) / 92.0 * self.online_mult
        return int(self.rng.poisson(total * _density(ONLINE_CURVE, t0 / 60) * dt_min))

    # ------------------------------------------------------------------ choices
    def _probs(self) -> dict[str, float]:
        covers = max(float(self.fc.base.get("p50", 1)), 1.0)
        by_cat: dict[str, float] = {}
        for d, w in self.fc.dish_weights().items():
            by_cat[cat.DISHES_BY_ID[d].category] = by_cat.get(cat.DISHES_BY_ID[d].category, 0.0) + w
        return {c: min(0.98, v / covers) for c, v in by_cat.items()}

    def pick(self, category: str, available: dict[str, int], specials: set[str], exclude: set[str] = frozenset()) -> str | None:
        w = {d: v * (SPECIAL_BOOST if d in specials else 1.0) for d, v in self.fc.dish_weights().items()
             if cat.DISHES_BY_ID[d].category == category and available.get(d, 0) > 0 and d not in exclude}
        if not w:
            return None
        ids, vals = list(w), np.asarray(list(w.values()), dtype=float)
        if vals.sum() <= 0:
            return None
        return str(self.rng.choice(ids, p=vals / vals.sum()))

    def party_order(self, size: int, available: dict[str, int], specials: set[str], large: bool = False) -> dict:
        """Items for a party's food order (starters, mains, sides). Desserts come later as their own order."""
        p = self._probs()
        items: list[dict] = []
        for g in range(size):
            if large:  # big walk-in parties go heavy on steaks and burgers
                pool = {d: n for d, n in available.items() if cat.DISHES_BY_ID[d].station == "grill" and cat.DISHES_BY_ID[d].category == "main"}
                main = self.pick("main", pool, specials) if self.rng.random() < 0.8 else None
                main = main or self.pick("main", available, specials)
            else:
                main = self.pick("main", available, specials) if self.rng.random() < 0.95 else None
            if main:
                items.append({"dish_id": main, "course": "main", "seat": g + 1})
            if self.rng.random() < p.get("starter", 0.4):
                s = self.pick("starter", available, specials)
                if s:
                    items.append({"dish_id": s, "course": "starter", "seat": g + 1})
            if self.rng.random() < p.get("side", 0.3) * 0.8:
                s = self.pick("side", available, specials)
                if s:
                    items.append({"dish_id": s, "course": "side", "seat": g + 1})
        notes = ""
        if items and self.rng.random() < MOD_RATE:
            notes = str(self.rng.choice(MOD_TEXTS))
            k = int(self.rng.integers(0, len(items)))
            items[k]["mods"] = notes
            notes = ""
        return {"items": items, "notes": notes}

    def dessert_order(self, size: int, available: dict[str, int], specials: set[str]) -> list[dict]:
        p = min(0.9, self._probs().get("dessert", 0.3) * 1.1)
        out = []
        for g in range(size):
            if self.rng.random() < p:
                d = self.pick("dessert", available, specials)
                if d:
                    out.append({"dish_id": d, "course": "dessert", "seat": g + 1})
        return out

    def online_cart(self, allowed: dict[str, int], specials: set[str]) -> list[dict]:
        items = []
        m = self.pick("main", allowed, specials)
        if m:
            items.append({"dish_id": m, "course": "main"})
            if self.rng.random() < 0.35:
                m2 = self.pick("main", allowed, specials)
                if m2:
                    items.append({"dish_id": m2, "course": "main"})
        if self.rng.random() < 0.5:
            s = self.pick("side", allowed, specials) or self.pick("starter", allowed, specials)
            if s:
                items.append({"dish_id": s, "course": "side"})
        if not items:
            s = self.pick("starter", allowed, specials) or self.pick("dessert", allowed, specials)
            if s:
                items.append({"dish_id": s, "course": "starter"})
        if items and self.rng.random() < MOD_RATE / 2:
            items[0]["mods"] = str(self.rng.choice(MOD_TEXTS))
        return items

    # ------------------------------------------------------------------ timings (minutes)
    def minutes(self, what: str) -> float:
        lo, hi = {"to_order": (4, 8), "eat": (28, 40), "dessert_after": (15, 22), "pay": (6, 10), "bus": (3, 5),
                  "patience": (18, 28)}[what]
        return float(self.rng.uniform(lo, hi))

    def binomial(self, n: int, p: float) -> int:
        return int(self.rng.binomial(n, p))

    def chance(self, p: float) -> bool:
        return bool(self.rng.random() < p)


def round5(minutes: float, up: bool) -> float:
    return (math.ceil if up else math.floor)(minutes / 5) * 5
