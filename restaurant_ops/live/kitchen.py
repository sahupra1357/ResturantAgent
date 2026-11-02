"""Kitchen-load model: one queue per station, deterministic ready-time quotes, recalibration from actuals.

Each station has `cooks` (from the staff roster for the service) and each cook works `PARALLEL[station]` items at
once (pans, grill space). An item fired at time t waits in its station's FIFO queue until a slot frees, then cooks
for its actual time (cook minutes x seeded noise). Quotes never look at actual times, only at estimates:

    W_s      = current wait at station s: queued minutes / cook slots, projected FIFO onto the slots' estimated
               free times (0 while a slot is free)
    P_i      = estimated cook minutes of item i (catalog cook time x station calibration)
    T_ready  = t_now + max_i (W_s(i) + P_i) + B                                      (B = packing buffer)

The P50 quote uses the calibrated estimate; P90 inflates waits and cook times by the observed 90th percentile of
actual/estimated ratios. Every finished item updates the station calibration (EWMA) and the ratio history.
Pure Python, no LLM, sub-millisecond per call.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

import numpy as np

from restaurant_ops.data import catalog as cat

# elapsed cook minutes per labour minute (catalog prep_minutes is hands-on labour); + plating
COOK_FACTOR = {"grill": 2.6, "saute": 2.4, "cold": 1.8, "pastry": 2.5}
PLATING_MIN = 1.5
PARALLEL = {"grill": 3, "saute": 3, "cold": 2, "pastry": 3}  # items one cook works at once
NOISE_SIGMA = 0.22          # lognormal spread of actual vs nominal cook time
PACK_BUFFER_MIN = 3.0       # B for online orders
WAIT_HORIZON_MIN = 15.0     # a station with this much wait is fully loaded (utilisation 1.0)
EWMA_ALPHA = 0.08
PRIOR_K90 = 1.30            # P90 / P50 ratio prior for cook times
PRIOR_W90 = 1.25            # P90 inflation of waits
QUICK_MAX_MIN = 8.0         # 'quick items' for the online throttle mode
BUSY_LEVELS = ((0.55, "calm"), (0.9, "busy"), (math.inf, "slammed"))


def station_factor(station: str) -> float:
    """Cook minutes per labour minute: admin setting (stations table) if present, else the built-in default."""
    return float(cat.STATION_META.get(station, {}).get("cook_factor", COOK_FACTOR.get(station, cat.COOK_FACTOR_DEFAULT)))


def station_parallel(station: str) -> int:
    return int(cat.STATION_META.get(station, {}).get("parallel", PARALLEL.get(station, cat.PARALLEL_DEFAULT)))


def cook_minutes(dish_id: str) -> float:
    d = cat.DISHES_BY_ID[dish_id]
    return round(d.prep_minutes * station_factor(d.station) + PLATING_MIN, 2)


@dataclass
class Item:
    item_id: str
    order_id: str
    dish_id: str
    station: str
    fire_at: float              # sim seconds since service start
    est_min: float              # estimate at fire time (calibrated)
    actual_min: float           # hidden truth (seeded noise); never used by quotes
    status: str = "scheduled"   # scheduled -> queued -> cooking -> done
    start_at: float | None = None
    done_at: float | None = None
    mods: list[str] = field(default_factory=list)
    large_party: bool = False
    scratch: bool = False        # nothing prepped: cooked from scratch (slower)


@dataclass
class Station:
    name: str
    cooks: int
    parallel: int
    queue: deque = field(default_factory=deque)        # queued Items (fired, waiting for a slot)
    cooking: list = field(default_factory=list)        # Items in progress
    slots_free_at: list = field(default_factory=list)  # sim seconds when each slot frees

    @property
    def slots(self) -> int:
        return max(1, self.cooks * self.parallel)


class Kitchen:
    def __init__(self, staff: dict[str, int], seed: int = 42):
        self.stations: dict[str, Station] = {}
        for s in cat.STATIONS:
            st = Station(s, int(staff.get(s, 1)), station_parallel(s))
            st.slots_free_at = [0.0] * st.slots
            self.stations[s] = st
        self.rng = np.random.default_rng([seed, 7])
        self.cal = {s: 1.0 for s in cat.STATIONS}           # actual / nominal cook time, EWMA per station
        self.ratios: dict[str, deque] = {s: deque(maxlen=200) for s in cat.STATIONS}
        self.scheduled: list[Item] = []                      # course timing: fired later
        self.items: dict[str, Item] = {}
        self.done_log: list[Item] = []
        self.fire_hook = None   # fn(Item) -> cook-time factor at fire (prep burn-down: 1.0 prepped, >1 from scratch)
        self.scratch_check = None  # fn(dish_id) -> factor used by estimates for items not yet fired

    def __getstate__(self):
        return {k: v for k, v in self.__dict__.items() if k not in ("fire_hook", "scratch_check")}

    def __setstate__(self, state):
        self.__dict__.update(state)
        self.fire_hook = self.scratch_check = None

    def set_cooks(self, station: str, cooks: int, now: float) -> dict:
        """Change a station's cooks mid-service (call-in / release). Slots are added free now, or idle ones removed."""
        st = self.stations[station]
        cooks = max(1, int(cooks))
        new_slots = cooks * st.parallel
        diff = new_slots - len(st.slots_free_at)
        if diff > 0:
            st.slots_free_at += [now] * diff
        elif diff < 0:
            st.slots_free_at = sorted(st.slots_free_at)[-diff:]  # the cook who leaves finishes nothing new
        st.cooks = cooks
        return {"station": station, "cooks": cooks, "slots": st.slots}

    # ------------------------------------------------------------------ estimates
    def est_minutes(self, dish_id: str) -> float:
        f = self.scratch_check(dish_id) if self.scratch_check else 1.0
        return cook_minutes(dish_id) * self.cal[cat.DISHES_BY_ID[dish_id].station] * f

    def k90(self, station: str) -> float:
        r = self.ratios[station]
        if len(r) < 12:
            return PRIOR_K90
        arr = np.asarray(r)
        return float(max(1.05, np.quantile(arr, 0.9) / max(np.median(arr), 1e-6)))

    def _projection(self, station: str, now: float) -> tuple[list[float], dict[str, float]]:
        """FIFO on estimates only: minutes-from-now when each slot frees after the queued items take theirs, and the
        projected start (minutes from now) of every queued item. With a long queue the wait tends to
        queued minutes / slots; with a free slot it is 0."""
        st = self.stations[station]
        free = sorted([max(0.0, (i.start_at + i.est_min * 60 - now) / 60) for i in st.cooking]
                      + [0.0] * max(0, st.slots - len(st.cooking)))
        starts: dict[str, float] = {}
        due = [i for i in self.scheduled if i.station == station and i.fire_at <= now + 60]
        for it in [*st.queue, *sorted(due, key=lambda i: i.fire_at)]:
            k = min(range(len(free)), key=free.__getitem__)
            starts[it.item_id] = free[k]
            free[k] += it.est_min
        return sorted(free), starts

    def wait_minutes(self, station: str, now: float) -> float:
        """W_s: minutes until a new item fired now would get a slot at this station."""
        free, _ = self._projection(station, now)
        return free[0] if free else 0.0

    def backlog_minutes(self, station: str, now: float) -> float:
        """Queued + in-progress estimated minutes / slots (the load measure behind utilisation)."""
        st = self.stations[station]
        rem = sum(max(0.0, (i.start_at + i.est_min * 60 - now) / 60) for i in st.cooking) + sum(i.est_min for i in st.queue)
        return rem / st.slots

    def quote(self, dish_ids: list[str], now: float, buffer_min: float = PACK_BUFFER_MIN) -> dict:
        """Ready-time quote for a new order of these dishes, fired now: {p50_min, p90_min, slowest_station}."""
        if not dish_ids:
            return {"p50_min": 0.0, "p90_min": 0.0, "slowest_station": None, "per_item": []}
        per, best50, best90, slow = [], 0.0, 0.0, None
        waits = {s: self.wait_minutes(s, now) for s in self.stations}
        for d in dish_ids:
            s = cat.DISHES_BY_ID[d].station
            p = self.est_minutes(d)
            t50 = waits[s] + p
            t90 = waits[s] * PRIOR_W90 + p * self.k90(s)
            per.append({"dish_id": d, "station": s, "wait_min": round(waits[s], 1), "cook_min": round(p, 1)})
            if t50 > best50:
                best50, slow = t50, s
            best90 = max(best90, t90)
        return {"p50_min": round(best50 + buffer_min, 1), "p90_min": round(max(best90, best50) + buffer_min, 1),
                "slowest_station": slow, "per_item": per}

    def estimate_items(self, items: list[Item], now: float, buffer_min: float = 0.0) -> dict:
        """Current P50/P90 minutes-from-now until all of `items` are done (no oracle: estimates only)."""
        lo = hi = 0.0
        proj: dict[str, tuple] = {}
        for it in items:
            if it.status == "done":
                continue
            k = self.k90(it.station)
            if it.status == "cooking":
                left = max(0.5, (it.start_at + it.est_min * 60 - now) / 60)
                a, b = left, max(left * k, left + 1.0)
            else:
                if it.station not in proj:
                    proj[it.station] = self._projection(it.station, now)
                free, starts = proj[it.station]
                if it.item_id in starts:
                    wait = starts[it.item_id]
                else:  # a later course: fires at fire_at, then waits for the first free slot
                    wait = max((it.fire_at - now) / 60, free[0] if free else 0.0)
                a = wait + it.est_min
                b = wait * PRIOR_W90 + it.est_min * k
            lo, hi = max(lo, a), max(hi, b)
        return {"p50_min": round(lo + buffer_min, 1), "p90_min": round(max(hi, lo) + buffer_min, 1)}

    # ------------------------------------------------------------------ flow
    def add_item(self, item_id: str, order_id: str, dish_id: str, fire_at: float, mods=None, large_party: bool = False) -> Item:
        d = cat.DISHES_BY_ID[dish_id]
        nominal = cook_minutes(dish_id)
        actual = nominal * float(self.rng.lognormal(0.0, NOISE_SIGMA))
        it = Item(item_id, order_id, dish_id, d.station, fire_at, round(self.est_minutes(dish_id), 2), round(actual, 2),
                  mods=list(mods or []), large_party=large_party)
        self.items[item_id] = it
        self.scheduled.append(it)
        return it

    def cancel(self, item_id: str) -> None:
        it = self.items.get(item_id)
        if it and it.status == "scheduled":
            self.scheduled.remove(it)
            it.status = "cancelled"

    def advance(self, now: float) -> dict[str, list[Item]]:
        """Move the kitchen to sim time `now`. Returns {'fired': [...], 'started': [...], 'done': [...]} in time order."""
        fired, started, done = [], [], []
        due = sorted([i for i in self.scheduled if i.fire_at <= now], key=lambda i: (i.fire_at, i.item_id))
        for it in due:
            self.scheduled.remove(it)
            it.status = "queued"
            f = self.fire_hook(it) if self.fire_hook else 1.0
            it.actual_min = round(it.actual_min * f, 2)
            it.est_min = round(cook_minutes(it.dish_id) * self.cal[it.station] * f, 2)
            it.scratch = f > 1.0
            self.stations[it.station].queue.append(it)
            fired.append(it)
        for st in self.stations.values():
            while True:
                # finish what is done before the next slot can be reused
                progressed = False
                for it in sorted(st.cooking, key=lambda i: i.done_at):
                    if it.done_at <= now:
                        st.cooking.remove(it)
                        it.status = "done"
                        done.append(it)
                        self._learn(it)
                        progressed = True
                if st.queue:
                    k = int(np.argmin(st.slots_free_at))
                    if st.slots_free_at[k] <= now:
                        it = st.queue.popleft()
                        it.start_at = max(st.slots_free_at[k], it.fire_at)
                        it.done_at = it.start_at + it.actual_min * 60
                        st.slots_free_at[k] = it.done_at
                        it.status = "cooking"
                        st.cooking.append(it)
                        started.append(it)
                        progressed = True
                if not progressed:
                    break
        done.sort(key=lambda i: i.done_at)
        return {"fired": fired, "started": started, "done": done}

    def _learn(self, it: Item) -> None:
        if it.scratch:
            self.done_log.append(it)
            return  # scratch cooking is explained by the missing prep, not by the station's pace
        nominal = cook_minutes(it.dish_id)
        ratio = it.actual_min / nominal
        self.ratios[it.station].append(ratio)
        self.cal[it.station] = (1 - EWMA_ALPHA) * self.cal[it.station] + EWMA_ALPHA * ratio
        self.done_log.append(it)

    # ------------------------------------------------------------------ load
    def load(self, now: float, expected_next_30: float = 0.0, capacity_30: float = 40.0) -> dict:
        """Snapshot for the dashboard / quotes: per station queue depth, wait, utilisation; busyness level."""
        rows = []
        for s, st in self.stations.items():
            w = self.wait_minutes(s, now)
            backlog = self.backlog_minutes(s, now)
            rows.append({"station": s, "cooks": st.cooks, "slots": st.slots, "queued": len(st.queue),
                         "cooking": len(st.cooking), "queue_depth": len(st.queue) + len(st.cooking),
                         "wait_min": round(w, 1), "backlog_min": round(backlog, 1),
                         "utilisation": round(min(max(w, backlog * 0.5) / WAIT_HORIZON_MIN, 2.0), 2),
                         "calibration": round(self.cal[s], 3)})
        max_util = max(r["utilisation"] for r in rows)
        demand = min(expected_next_30 / max(capacity_30, 1.0), 1.5)
        score = round(max(max_util, 0.6 * max_util + 0.4 * demand), 2)
        level = next(name for cut, name in BUSY_LEVELS if score < cut)
        return {"stations": rows, "busyness": level, "score": score, "max_utilisation": max_util,
                "expected_next_30": round(expected_next_30, 1), "worst_station": max(rows, key=lambda r: r["wait_min"])["station"]}


def is_quick(dish_id: str) -> bool:
    """Items the online page still offers when the kitchen is throttled to 'quick_only'."""
    return cook_minutes(dish_id) <= QUICK_MAX_MIN and cat.DISHES_BY_ID[dish_id].station != "grill"
