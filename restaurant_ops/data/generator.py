"""Seeded synthetic world: hidden demand truth, 365 days of history, and the demo week's inputs.

The hidden truth functions (`true_covers`, `true_dish_mix`, `realise_day`) bake in every signal from
DESIGN.md. The Phase 4 simulator reuses them to realise future demand, passing event / weather /
private-booking overrides. No LLM imports here.
"""
from __future__ import annotations

import math
import sqlite3
from datetime import date, timedelta
from typing import Iterable, Mapping, Sequence

import numpy as np

from restaurant_ops.config import settings
from restaurant_ops.data import catalog as cat

PERIODS: list[str] = list(settings.MEAL_PERIODS)

# =========================================================================== hidden truth model

BASE_COVERS = {"lunch": 52.0, "dinner": 92.0, "late": 22.0}  # Thursday, mid-year, no events
# Day-of-week multipliers, Monday..Sunday
DOW_FACTOR = {
    "lunch": [0.80, 0.90, 0.95, 1.00, 1.15, 1.25, 1.10],
    "dinner": [0.70, 0.75, 0.85, 1.00, 1.35, 1.45, 1.05],
    "late": [0.50, 0.50, 0.70, 0.90, 1.70, 1.80, 0.60],
}
PAYDAY_FACTOR = 1.09  # 15th and last day of month; start-of-month has NO effect
GAME_COVER_FACTOR = 1.25  # home game: dinner and late covers
HEAT_C = 30.0  # above this: cold dishes +40%, hot mains -20%
HEAT_COLD_FACTOR, HEAT_HOT_MAIN_FACTOR = 1.40, 0.80
GAME_FRIED_FACTOR, GAME_SHAREABLE_FACTOR = 1.60, 1.25
PORTIONS_PER_COVER = {"lunch": 1.25, "dinner": 1.75, "late": 1.40}
COVERS_DISPERSION = 50.0  # gamma shape for negative-binomial-like noise (higher = less noise)
SPORTS_SEASON_MONTHS = (9, 10, 11, 12, 1, 2, 3)

HOLIDAY_EFFECTS: dict[str, dict[str, float]] = {
    "New Year's Eve": {"lunch": 0.8, "dinner": 1.4, "late": 1.6},
    "New Year's Day": {"lunch": 0.8, "dinner": 0.8, "late": 0.6},
    "Valentine's Day": {"lunch": 1.0, "dinner": 1.35, "late": 0.9},
    "St. Patrick's Day": {"lunch": 1.05, "dinner": 1.1, "late": 1.4},
    "Mother's Day": {"lunch": 1.4, "dinner": 1.15, "late": 0.8},
    "Independence Day": {"lunch": 0.85, "dinner": 0.85, "late": 0.9},
    "Halloween": {"lunch": 1.0, "dinner": 1.05, "late": 1.25},
    "Thanksgiving": {"lunch": 0.45, "dinner": 0.4, "late": 0.3},
    "Christmas Eve": {"lunch": 1.1, "dinner": 0.85, "late": 0.7},
    "Christmas Day": {"lunch": 0.4, "dinner": 0.35, "late": 0.2},
}
# Local events: name -> per-period multiplier
LOCAL_EFFECTS: dict[str, dict[str, float]] = {
    "Farmers Market Festival": {"lunch": 1.20, "dinner": 1.0, "late": 1.0},
    "Street Food Fair": {"lunch": 1.10, "dinner": 0.95, "late": 1.0},
    "City Marathon": {"lunch": 1.25, "dinner": 1.05, "late": 1.0},
    "Jazz Night Downtown": {"lunch": 1.0, "dinner": 1.12, "late": 1.20},
    "Winter Lights Parade": {"lunch": 1.0, "dinner": 1.15, "late": 1.10},
}


def nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """n-th (1-based) given weekday (Mon=0) of a month."""
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def holiday_for(d: date) -> str | None:
    """Name of the holiday on date `d`, or None."""
    fixed = {(12, 31): "New Year's Eve", (1, 1): "New Year's Day", (2, 14): "Valentine's Day",
             (3, 17): "St. Patrick's Day", (7, 4): "Independence Day", (10, 31): "Halloween",
             (12, 24): "Christmas Eve", (12, 25): "Christmas Day"}
    if (d.month, d.day) in fixed:
        return fixed[(d.month, d.day)]
    if d == nth_weekday(d.year, 5, 6, 2):
        return "Mother's Day"
    if d == nth_weekday(d.year, 11, 3, 4):
        return "Thanksgiving"
    return None


def is_payday(d: date) -> bool:
    """Payday = 15th and last day of the month."""
    return d.day == 15 or (d + timedelta(days=1)).day == 1


def season_factor(d: date) -> float:
    """Smooth week-of-year seasonality: summer patio peak, December party bump."""
    doy = d.timetuple().tm_yday
    f = 1.0 + 0.12 * math.cos(2 * math.pi * (doy - 200) / 365)
    if d.month == 12 and d.day <= 23:
        f += 0.08
    return f


def event_multiplier(meal_period: str, events: Iterable[Mapping] = ()) -> float:
    """Cover multiplier from sports / local events (holidays come from the calendar)."""
    m = 1.0
    for e in events:
        etype, name, ep = e.get("type"), e.get("name", ""), e.get("meal_period")
        if etype == "sports" and meal_period in ("dinner", "late") and ep in (None, "dinner", "late"):
            m *= GAME_COVER_FACTOR
        elif etype == "local":
            m *= LOCAL_EFFECTS.get(name, {}).get(meal_period, 1.05 if ep in (None, meal_period) else 1.0)
    return m


def has_home_game(events: Iterable[Mapping]) -> bool:
    """True if any event is a sports home game."""
    return any(e.get("type") == "sports" for e in events)


def expected_covers(d: date, meal_period: str, *, temp_c: float | None = None,
                    events: Sequence[Mapping] = (), extra_covers: int = 0) -> float:
    """Noise-free expected covers (capped at seats x turns), plus any extra private covers."""
    mean = BASE_COVERS[meal_period] * DOW_FACTOR[meal_period][d.weekday()] * season_factor(d)
    if is_payday(d):
        mean *= PAYDAY_FACTOR
    hol = holiday_for(d)
    if hol:
        mean *= HOLIDAY_EFFECTS[hol][meal_period]
    mean *= event_multiplier(meal_period, events)
    return min(mean + extra_covers, float(settings.seat_cap(meal_period)))


def true_covers(d: date, meal_period: str, *, temp_c: float | None = None,
                events: Sequence[Mapping] = (), extra_covers: int = 0,
                rng: np.random.Generator | None = None) -> int:
    """Hidden-truth covers for one meal period.

    Args:
        d: service date.
        meal_period: 'lunch' | 'dinner' | 'late'.
        temp_c: temperature (accepted for a uniform signature; heat shifts the dish mix, not covers).
        events: event dicts ({'type','name','meal_period'}); sports/local ones move demand,
            holidays are looked up from the calendar so they are not double counted.
        extra_covers: private-booking covers added on top of organic demand (e.g. 40).
        rng: numpy Generator. None returns the rounded expectation (no noise).
    The result never exceeds seats x turns for the period.
    """
    cap = settings.seat_cap(meal_period)
    if rng is None:
        return int(round(expected_covers(d, meal_period, temp_c=temp_c, events=events,
                                         extra_covers=extra_covers)))
    mean = expected_covers(d, meal_period, temp_c=temp_c, events=events, extra_covers=0)
    lam = mean * rng.gamma(COVERS_DISPERSION, 1.0 / COVERS_DISPERSION)  # gamma-Poisson = neg. binomial
    return int(min(int(rng.poisson(lam)) + extra_covers, cap))


def _period_factor(dish: cat.Dish, meal_period: str) -> float:
    """How much a meal period favours a dish (lunch: lighter; late: shareables)."""
    f = 1.0
    shareable = "shareable" in dish.tags
    if meal_period == "lunch":
        if dish.category == "starter" and dish.is_cold:
            f *= 1.5
        if dish.id in ("smash_burger", "carbonara", "chicken_milanese"):
            f *= 1.3
        if dish.id == "ribeye_steak":
            f *= 0.6
        if dish.category == "dessert":
            f *= 0.6
    elif meal_period == "late":
        if shareable:
            f *= 1.6
        if dish.category == "main" and not shareable:
            f *= 0.45
        if dish.category == "dessert":
            f *= 1.3
        if dish.category == "starter" and dish.is_cold:
            f *= 0.6
    else:  # dinner
        if dish.category == "main":
            f *= 1.15
        if dish.id == "ribeye_steak":
            f *= 1.3
    return f


def dish_rates(d: date, meal_period: str, covers: float, *, temp_c: float | None = None,
               events: Sequence[Mapping] = ()) -> dict[str, float]:
    """Expected portions per dish. Seasonal dishes are 0 out of season; heat and games shift the mix."""
    base = {x.id: x.popularity * _period_factor(x, meal_period)
            for x in cat.DISHES if cat.in_season(x.seasonal_months, d.month)}
    norm = sum(base.values())
    total = covers * PORTIONS_PER_COVER[meal_period]
    hot = temp_c is not None and temp_c > HEAT_C
    game = has_home_game(events) and meal_period in ("dinner", "late")
    rates: dict[str, float] = {}
    for dish in cat.DISHES:
        if dish.id not in base:
            rates[dish.id] = 0.0
            continue
        r = total * base[dish.id] / norm
        if hot:
            if dish.is_cold:
                r *= HEAT_COLD_FACTOR
            elif dish.category == "main":
                r *= HEAT_HOT_MAIN_FACTOR
        if game:
            if "fried" in dish.tags:
                r *= GAME_FRIED_FACTOR
            elif "shareable" in dish.tags:
                r *= GAME_SHAREABLE_FACTOR
        rates[dish.id] = r
    return rates


def true_dish_mix(d: date, meal_period: str, covers: float, *, temp_c: float | None = None,
                  events: Sequence[Mapping] = (), rng: np.random.Generator | None = None) -> dict[str, int]:
    """Hidden-truth portions sold per dish (Poisson around `dish_rates`; rounded rates if rng is None)."""
    rates = dish_rates(d, meal_period, covers, temp_c=temp_c, events=events)
    if rng is None:
        return {k: int(round(v)) for k, v in rates.items()}
    return {k: int(rng.poisson(v)) for k, v in rates.items()}


def day_rng(d: date, seed: int | None = None) -> np.random.Generator:
    """Per-date generator so a date always realises identically regardless of call order."""
    return np.random.default_rng([settings.SEED if seed is None else seed, d.toordinal()])


def realise_day(d: date, *, temp_c: float | None = None, events: Sequence[Mapping] = (),
                extra_covers: Mapping[str, int] | None = None,
                seed: int | None = None) -> dict[str, dict]:
    """Realise one service day: {period: {'covers': int, 'sales': {dish_id: qty}}}.

    The simulator calls this with the day's weather/events/private bookings (the disruptions).
    """
    rng = day_rng(d, seed)
    out: dict[str, dict] = {}
    for p in PERIODS:
        covers = true_covers(d, p, temp_c=temp_c, events=events,
                             extra_covers=(extra_covers or {}).get(p, 0), rng=rng)
        out[p] = {"covers": covers,
                  "sales": true_dish_mix(d, p, covers, temp_c=temp_c, events=events, rng=rng)}
    return out


# =========================================================================== DB readers (for the simulator)


def events_for(conn: sqlite3.Connection, d: date) -> list[dict]:
    """Event rows for a date as dicts (feed straight into the truth functions)."""
    rows = conn.execute("SELECT date,name,type,meal_period,impact_hint FROM events WHERE date=?",
                        (d.isoformat(),)).fetchall()
    return [dict(r) for r in rows]


def weather_for(conn: sqlite3.Connection, d: date) -> float | None:
    """Temperature for a date, or None."""
    r = conn.execute("SELECT temp_c FROM weather WHERE date=?", (d.isoformat(),)).fetchone()
    return None if r is None else float(r[0])


# =========================================================================== history generation


def _daterange(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def _make_weather(days: list[date], rng: np.random.Generator) -> dict[date, tuple[float, str | None]]:
    """Seasonal temperature + AR(1) noise + a few summer heatwave episodes (>30 C)."""
    heat_boost: dict[date, float] = {}
    for year in sorted({d.year for d in days}):
        summer = [d for d in days if d.year == year and date(year, 6, 15) <= d <= date(year, 8, 15)]
        if len(summer) < 10:
            continue
        for _ in range(3):
            start = summer[int(rng.integers(0, len(summer) - 4))]
            for k in range(int(rng.integers(3, 5))):
                heat_boost[start + timedelta(days=k)] = 9.5
    out, noise = {}, 0.0
    for d in days:
        noise = 0.7 * noise + float(rng.normal(0, 2.4))
        doy = d.timetuple().tm_yday
        t = round(14 + 11 * math.cos(2 * math.pi * (doy - 200) / 365) + noise + heat_boost.get(d, 0.0), 1)
        out[d] = (t, "heatwave" if t >= 32 else None)
    return out


def _make_events(hist: list[date], demo: list[date], rng: np.random.Generator) -> list[dict]:
    """Holidays, ~2 home games per in-season month, a few local events, a few private parties."""
    events: list[dict] = []

    def add(d: date, name: str, etype: str, period: str | None = None, hint: str = "") -> None:
        events.append({"date": d.isoformat(), "name": name, "type": etype,
                       "meal_period": period, "impact_hint": hint})

    game_hint = "+25% dinner/late covers, more fried/shareables"
    for d in hist + demo:
        h = holiday_for(d)
        if h:
            add(d, h, "holiday", None, "holiday traffic pattern")
    # home games: 2 per in-season month in history; the demo window gets one on each Wednesday and Saturday
    demo_month = (demo[0].year, demo[0].month)
    for (y, m) in sorted({(d.year, d.month) for d in hist}):
        if m not in SPORTS_SEASON_MONTHS or (y, m) == demo_month:
            continue
        cands = [d for d in hist if d.year == y and d.month == m and d.weekday() in (2, 4, 5, 6)]
        if not cands:
            continue
        for i in sorted(int(p) for p in rng.choice(len(cands), size=min(2, len(cands)), replace=False)):
            add(cands[i], "Home game vs. Rivals", "sports", "dinner", game_hint)
    for d in [x for x in demo if x.weekday() in (2, 5)]:  # works for any start weekday
        add(d, "Home game vs. Rivals", "sports", "dinner", game_hint)
    # local events: a handful across the year (history only)
    names = list(LOCAL_EFFECTS)
    for i in rng.choice(len(hist), size=7, replace=False):
        add(hist[int(i)], names[int(rng.integers(0, len(names)))], "local", None, "local event")
    # private parties (history only): Thu-Sat dinner
    cands = [d for d in hist if d.weekday() in (3, 4, 5) and holiday_for(d) is None]
    for i in sorted(int(x) for x in rng.choice(len(cands), size=5, replace=False)):
        n = int(rng.integers(25, 46))
        add(cands[i], f"Private party ({n} covers)", "private", "dinner", f"{n} extra covers at dinner")
    events.sort(key=lambda e: (e["date"], e["name"]))
    return events


RES_SHARE = {"lunch": 0.35, "dinner": 0.60, "late": 0.20}  # share of covers that book ahead


def _party_sizes(target: int, rng: np.random.Generator) -> list[int]:
    """Draw party sizes until they sum to about `target` covers."""
    sizes, tot = [], 0
    while tot < target:
        s = int(rng.choice([2, 3, 4, 5, 6, 8, 10], p=[0.38, 0.16, 0.20, 0.08, 0.10, 0.05, 0.03]))
        sizes.append(s)
        tot += s
    return sizes


def _reservation_rows(d: date, period: str, organic_covers: float, rng: np.random.Generator,
                      as_of: date | None) -> list[tuple]:
    """Standard reservation rows for one slot; bookings created after `as_of` are not visible yet."""
    rows = []
    for size in _party_sizes(int(round(organic_covers * RES_SHARE[period])), rng):
        created = d - timedelta(days=int(min(21, rng.exponential(5.0))))
        if as_of is not None and created > as_of:
            continue
        rows.append((d.isoformat(), period, size, "standard", created.isoformat()))
    return rows


def _staffing_rows(d: date, events: Sequence[Mapping]) -> list[tuple]:
    """Staff per station: default roster, +1 on grill/saute when busy, +1 saute (fryer) on game nights."""
    rows = []
    for p in PERIODS:
        busy = expected_covers(d, p, events=events) > 1.15 * BASE_COVERS[p]
        for s in cat.STATIONS:
            n = cat.DEFAULT_STAFFING[s][p]
            if busy and s in ("grill", "saute") and p != "late":
                n += 1
            if has_home_game(events) and s == "saute" and p in ("dinner", "late"):
                n += 1
            rows.append((d.isoformat(), p, s, n))
    return rows


def generate_all(conn: sqlite3.Connection, seed: int | None = None) -> dict:
    """Populate history, reservations, events, weather, staff, stock, lots and POs. Returns a summary."""
    seed = settings.SEED if seed is None else seed
    r_weather, r_events, r_res, r_stock, r_lots, r_po = (
        np.random.default_rng(s) for s in np.random.SeedSequence(seed).spawn(6))

    hist = _daterange(settings.history_start, settings.SIM_START - timedelta(days=1))
    demo = _daterange(settings.SIM_START, settings.SIM_START + timedelta(days=settings.DEMO_DAYS - 1))
    weather = _make_weather(hist + demo, r_weather)
    events = _make_events(hist, demo, r_events)
    ev_by_date: dict[str, list[dict]] = {}
    private_by_date: dict[str, dict[str, int]] = {}
    for e in events:
        ev_by_date.setdefault(e["date"], []).append(e)
        if e["type"] == "private":
            private_by_date.setdefault(e["date"], {})[e["meal_period"]] = int(e["name"].split("(")[1].split()[0])

    cur = conn.cursor()
    cur.executemany("INSERT OR REPLACE INTO weather VALUES (?,?,?)",
                    [(d.isoformat(), t, a) for d, (t, a) in weather.items()])
    cur.executemany("INSERT INTO events (date,name,type,meal_period,impact_hint) VALUES (?,?,?,?,?)",
                    [(e["date"], e["name"], e["type"], e["meal_period"], e["impact_hint"]) for e in events])

    # ---- covers + POS sales from the hidden truth
    dish_ids = [x.id for x in cat.DISHES]
    ing_idx = {x.id: i for i, x in enumerate(cat.INGREDIENTS)}
    bom = np.zeros((len(dish_ids), len(cat.INGREDIENTS)))
    for r, did in enumerate(dish_ids):
        for ing, q in cat.DISHES_BY_ID[did].recipe.items():
            bom[r, ing_idx[ing]] = q
    usage = np.zeros((len(hist), len(cat.INGREDIENTS)))
    cover_rows, sales_rows, res_rows, shift_rows = [], [], [], []
    for t, d in enumerate(hist):
        iso = d.isoformat()
        evs, extra = ev_by_date.get(iso, []), private_by_date.get(iso, {})
        day = realise_day(d, temp_c=weather[d][0], events=evs, extra_covers=extra, seed=seed)
        qty_vec = np.zeros(len(dish_ids))
        for p in PERIODS:
            cover_rows.append((iso, p, day[p]["covers"]))
            for k, did in enumerate(dish_ids):
                q = day[p]["sales"][did]
                if q > 0:
                    sales_rows.append((iso, p, did, q))
                    qty_vec[k] += q
            res_rows += _reservation_rows(d, p, max(0, day[p]["covers"] - extra.get(p, 0)), r_res, None)
            if p in extra:
                created = d - timedelta(days=int(r_res.integers(14, 31)))
                res_rows.append((iso, p, extra[p], "private", created.isoformat()))
        usage[t] = qty_vec @ bom
        shift_rows += _staffing_rows(d, evs)

    # ---- demo week: the full booking stream with real created dates, plus staffing plan. Readers only see
    # bookings created on or before the sim date, so the book fills up day by day as the simulation runs.
    for d in demo:
        evs = ev_by_date.get(d.isoformat(), [])
        for p in PERIODS:
            exp = expected_covers(d, p, events=evs)
            res_rows += _reservation_rows(d, p, exp, r_res, None)
        shift_rows += _staffing_rows(d, evs)

    cur.executemany("INSERT OR REPLACE INTO covers VALUES (?,?,?)", cover_rows)
    cur.executemany("INSERT OR REPLACE INTO pos_sales VALUES (?,?,?,?)", sales_rows)
    cur.executemany("INSERT INTO reservations (date,meal_period,party_size,kind,created_date) VALUES (?,?,?,?,?)",
                    res_rows)
    cur.executemany("INSERT OR REPLACE INTO staff_shifts VALUES (?,?,?,?)", shift_rows)

    system_last = _stock_counts(cur, hist, usage, r_stock)
    avg_usage = {x.id: float(usage[-28:, i].mean()) for i, x in enumerate(cat.INGREDIENTS)}
    n_lots = _opening_lots(cur, system_last, avg_usage, r_lots)
    n_po = _historic_pos(cur, avg_usage, r_po)
    conn.commit()

    return {"history_days": len(hist), "demo_days": len(demo), "cover_rows": len(cover_rows),
            "pos_rows": len(sales_rows), "reservations": len(res_rows), "events": len(events),
            "lots": n_lots, "purchase_orders": n_po}


# --------------------------------------------------------------------------- stock, lots, POs

SHRINKAGE_INGREDIENT = "ribeye"  # persistent ~6.5% shrinkage -> variance_report should flag it
SHRINKAGE_PCT = 0.065
# ingredient -> (days_left at SIM_START, excess factor) so waste-risk / specials have something to find
NEAR_EXPIRY = {"cod": (1, 3.0), "cream": (2, 2.5), "mozzarella": (2, 2.5), "spinach": (1, 2.5),
               "mushroom": (1, 2.0)}


def _cycle_days(shelf: int) -> int:
    """Typical days between deliveries for an item, from its shelf life."""
    return 1 if shelf <= 4 else 2 if shelf <= 10 else 3 if shelf <= 30 else 7


def _stock_counts(cur, hist: list[date], usage: np.ndarray, rng: np.random.Generator) -> dict[str, float]:
    """Daily stock counts: theoretical sawtooth stock vs. counted (noise; one persistent shrinkage item)."""
    rows, last = [], {}
    for i, ing in enumerate(cat.INGREDIENTS):
        cyc = _cycle_days(ing.shelf_life_days)
        phase = int(rng.integers(0, cyc))
        level = float(usage[:7, i].mean() * (cyc + 1.5))
        nd = 0 if ing.unit == "ea" else 3
        for t, d in enumerate(hist):
            if (t + phase) % cyc == 0:
                level = float(usage[max(0, t - 7):t + 1, i].mean() * (cyc + 1.5))
            level = max(0.0, level - float(usage[t, i]))
            if ing.id == SHRINKAGE_INGREDIENT:
                counted = level * (1 - SHRINKAGE_PCT + float(rng.normal(0, 0.012)))
            else:
                counted = level * (1 + float(rng.normal(0, 0.012)))
            rows.append((d.isoformat(), ing.id, round(max(0.0, counted), nd), round(level, nd)))
            last[ing.id] = level
    cur.executemany("INSERT OR REPLACE INTO stock_counts VALUES (?,?,?,?)", rows)
    return last


def _primary_supplier(ingredient_id: str) -> str:
    """Cheapest supplier for an ingredient (used to label opening lots)."""
    return min(cat.suppliers_for(ingredient_id), key=lambda s: s.price).supplier_id


def _opening_lots(cur, system_last: dict[str, float], avg_usage: dict[str, float],
                  rng: np.random.Generator) -> int:
    """Active lots as of SIM_START: an older and a fresher lot per ingredient, some near expiry."""
    start = settings.SIM_START
    rows = []
    for ing in cat.INGREDIENTS:
        shelf = ing.shelf_life_days
        total = max(system_last.get(ing.id, 0.0), avg_usage[ing.id] * 1.5, 0.05)
        if ing.unit == "ea":
            total = max(total, 6.0)
        old_share = float(rng.uniform(0.35, 0.5))
        old_qty, new_qty = total * old_share, total * (1 - old_share)
        if shelf >= 31:
            k_old = int(min(shelf - 1, rng.integers(5, 26)))
        elif shelf >= 3:
            k_old = int(rng.integers(2, min(shelf - 1, 4) + 1))
        else:
            k_old = 1
        old_recv = start - timedelta(days=k_old)
        old_exp = old_recv + timedelta(days=shelf)
        if ing.id in NEAR_EXPIRY:
            days_left, factor = NEAR_EXPIRY[ing.id]
            old_exp = start + timedelta(days=days_left)
            old_recv = old_exp - timedelta(days=shelf)
            old_qty *= factor
        new_recv = start - timedelta(days=1)
        new_exp = new_recv + timedelta(days=shelf)
        sup = _primary_supplier(ing.id)
        nd = 0 if ing.unit == "ea" else 2
        for n, (recv, exp, qty) in enumerate(((old_recv, old_exp, old_qty), (new_recv, new_exp, new_qty))):
            cost = round(ing.ref_cost * float(rng.uniform(0.95, 1.05)), 3)
            rows.append((f"LOT-{recv:%y%m%d}-{ing.id}-{n + 1}", ing.id, round(qty, nd), recv.isoformat(),
                         exp.isoformat(), sup, cost, "active"))
    cur.executemany("INSERT OR REPLACE INTO lots VALUES (?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def _historic_pos(cur, avg_usage: dict[str, float], rng: np.random.Generator, weeks: int = 6) -> int:
    """A few delivered / partial POs from the last weeks (feeds supplier fill-rate history)."""
    start = settings.SIM_START
    by_sup: dict[str, list[cat.SupplierItem]] = {}
    for si in cat.SUPPLIER_ITEMS:
        by_sup.setdefault(si.supplier_id, []).append(si)
    po_rows, line_rows, n = [], [], 0
    for w in range(weeks, 0, -1):
        for sup in cat.SUPPLIERS:
            delivery = start - timedelta(days=7 * w - int(rng.integers(0, 5)))
            created = delivery - timedelta(days=sup.lead_time_days)
            items = by_sup[sup.id]
            lines, total = [], 0.0
            for j in rng.permutation(len(items)):
                si = items[int(j)]
                raw = max(avg_usage[si.ingredient_id] * float(rng.uniform(2.0, 5.0)), si.pack_size)
                qty = math.ceil(raw / si.pack_size) * si.pack_size
                price = round(si.price * float(rng.uniform(0.97, 1.03)), 3)
                lines.append((si.ingredient_id, qty, price))
                total += qty * price
                if len(lines) >= 5 and total >= sup.min_order_value * 1.1:
                    break
            n += 1
            po_id = f"PO-H{n:04d}"
            fills = [min(1.0, sup.fill_rate + float(rng.normal(0.03, 0.06))) for _ in lines]
            for (ing, qty, price), f in zip(lines, fills):
                line_rows.append((po_id, ing, qty, price, round(qty * (1.0 if f >= 0.98 else f), 3)))
            po_rows.append((po_id, sup.id, created.isoformat(), delivery.isoformat(),
                            "partial" if any(f < 0.98 for f in fills) else "delivered",
                            round(total, 2), None))
    cur.executemany("INSERT OR REPLACE INTO purchase_orders VALUES (?,?,?,?,?,?,?)", po_rows)
    cur.executemany("INSERT OR REPLACE INTO po_lines VALUES (?,?,?,?,?)", line_rows)
    return len(po_rows)
