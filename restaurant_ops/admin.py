"""Restaurant administration and onboarding: profile, taxes, opening hours, floor tables, stations, delivery zones,
ingredients, dishes and recipes, suppliers and their prices, staff and rosters.

Everything is validated here and written to the master database (settings.DB_PATH). A live service copies the master
database when it starts, so edits take effect from the next service; the running dinner keeps the configuration
it started with (no mid-service surprises, no conflicting writes). `seed_defaults` fills the admin tables from the
built-in world the first time, so an existing database upgrades without manual steps.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import date

from restaurant_ops.data import catalog as cat

SLUG = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
TABLE_ID = re.compile(r"^[A-Z][A-Z0-9]{0,5}$")
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
CATEGORIES = ("starter", "main", "dessert", "side")
UNITS = ("kg", "l", "ea")
STORAGE = ("dry", "cooler", "freezer")
SHAPES = ("round", "rect", "bar")
ROLES_STAFF = ("cook", "prep", "server", "host", "manager", "driver")
TAX_APPLIES = ("food", "alcohol", "delivery_fee", "all")
TAX_CHANNELS = ("all", "dine_in", "online")
PERIODS = ("lunch", "dinner", "late")

DEFAULT_PROFILE = {
    "name": "Bistro 80", "address": "", "phone": "", "email": "", "timezone": "UTC", "currency": "USD",
    "loyalty": {"points_per_dollar": 1.0, "tiers": {"Silver": 500, "Gold": 1500}, "redeem_value_per_point": 0.01},
    "discount_max_pct": {"manager": 30.0, "server": 10.0},
    "overbook_factor": 1.1,
}
DEFAULT_HOURS = {"lunch": ("11:30", "14:30"), "dinner": ("17:00", "22:30"), "late": ("22:00", "23:59")}
DEFAULT_TAXES = [("State sales tax", "state", 0.0625, "all", "all"), ("City sales tax", "city", 0.02625, "food", "all"),
                 ("City alcohol tax", "city", 0.02625, "alcohol", "all")]
DEFAULT_STAFF = [("Marco", "cook", "grill", 26), ("Aisha", "cook", "grill", 26), ("Tom", "cook", "saute", 25), ("Lina", "cook", "saute", 25),
                 ("Ravi", "cook", "saute", 25), ("Jo", "cook", "saute", 24), ("Kemi", "cook", "cold", 24), ("Pat", "cook", "cold", 23),
                 ("Elle", "cook", "pastry", 25), ("Sam", "prep", None, 21), ("Ana", "server", None, 15), ("Ben", "server", None, 15),
                 ("Chloe", "server", None, 15), ("Dev", "server", None, 15), ("Rosa", "host", None, 17), ("Nico", "driver", None, 18),
                 ("Ivy", "driver", None, 18), ("Omar", "driver", None, 18), ("Grace", "manager", None, 34)]


class AdminError(ValueError):
    pass


def _rows(conn, sql, args=()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, args)]


def _need(cond: bool, msg: str) -> None:
    if not cond:
        raise AdminError(msg)


def touch(conn: sqlite3.Connection) -> None:
    from restaurant_ops.live.events import wall_now

    conn.execute("INSERT OR REPLACE INTO config_kv (key, value) VALUES ('last_admin_change', ?)", (json.dumps(wall_now()),))
    conn.commit()


# =========================================================================== defaults


# The first catalog had five stations; the fryer was folded into sauté and garde manger renamed "cold". Databases seeded
# then still cook dishes (and count shift cooks) on the old ids, which no longer exist: the kitchen raised KeyError: 'fry'.
LEGACY_STATIONS = {"fry": "saute", "garde_manger": "cold"}


def migrate_legacy_stations(conn: sqlite3.Connection) -> dict[str, str]:
    """Move dishes, staff and shift counts off a legacy station id that is not a configured station onto its successor
    (when that one is). Idempotent; a restaurant that really configured a 'fry' station keeps it."""
    have = {r[0] for r in conn.execute("SELECT id FROM stations")}
    moved = {}
    for old, new in LEGACY_STATIONS.items():
        if old in have or new not in have:
            continue
        n = conn.execute("UPDATE dishes SET station=? WHERE station=?", (new, old)).rowcount
        n += conn.execute("UPDATE staff SET station=? WHERE station=?", (new, old)).rowcount
        n += conn.execute("UPDATE roster SET station=? WHERE station=?", (new, old)).rowcount
        n += conn.execute("INSERT INTO staff_shifts (date, meal_period, station, staff) SELECT date, meal_period, ?, staff "
                          "FROM staff_shifts WHERE station=? ON CONFLICT (date, meal_period, station) "
                          "DO UPDATE SET staff = staff + excluded.staff", (new, old)).rowcount
        conn.execute("DELETE FROM staff_shifts WHERE station=?", (old,))
        if n:
            moved[old] = new
    return moved


def seed_defaults(conn: sqlite3.Connection) -> dict:
    """Fill empty admin tables from the built-in world (idempotent)."""
    from restaurant_ops.config import settings
    from restaurant_ops.live.floor import build_layout
    from restaurant_ops.live.kitchen import COOK_FACTOR, PARALLEL

    out = {}
    if not conn.execute("SELECT 1 FROM config_kv WHERE key='profile'").fetchone():
        conn.execute("INSERT INTO config_kv (key, value) VALUES ('profile', ?)", (json.dumps(DEFAULT_PROFILE),))
        out["profile"] = 1
    if not conn.execute("SELECT 1 FROM tax_rules").fetchone():
        conn.executemany("INSERT INTO tax_rules (name, jurisdiction, rate, applies_to, channel) VALUES (?,?,?,?,?)", DEFAULT_TAXES)
        out["tax_rules"] = len(DEFAULT_TAXES)
    if not conn.execute("SELECT 1 FROM opening_hours").fetchone():
        conn.executemany("INSERT INTO opening_hours (weekday, meal_period, open, close, closed) VALUES (?,?,?,?,0)",
                         [(wd, p, a, b) for wd in range(7) for p, (a, b) in DEFAULT_HOURS.items()])
        out["opening_hours"] = 21
    if not conn.execute("SELECT 1 FROM floor_tables").fetchone():
        conn.executemany("INSERT INTO floor_tables (id, seats, x, y, w, h, shape, section) VALUES (?,?,?,?,?,?,?,?)",
                         [(t.id, t.seats, t.x, t.y, t.w, t.h, t.shape, t.section) for t in build_layout()])
        out["floor_tables"] = 28
    if not conn.execute("SELECT 1 FROM stations").fetchone():
        names = {"grill": "Grill", "saute": "Sauté (incl. fryer)", "cold": "Cold (garde manger)", "pastry": "Pastry"}
        conn.executemany("INSERT INTO stations (id, name, parallel, cook_factor, lunch_cooks, dinner_cooks, late_cooks) VALUES (?,?,?,?,?,?,?)",
                         [(s, names.get(s, s), PARALLEL.get(s, 3), COOK_FACTOR.get(s, 2.4), cat.DEFAULT_STAFFING[s]["lunch"],
                           cat.DEFAULT_STAFFING[s]["dinner"], cat.DEFAULT_STAFFING[s]["late"]) for s in cat.STATIONS])
        out["stations"] = len(cat.STATIONS)
    moved = migrate_legacy_stations(conn)
    if moved:
        out["legacy_stations"] = moved
    if not conn.execute("SELECT 1 FROM delivery_zones").fetchone():
        conn.executemany("INSERT INTO delivery_zones (id, label, fee, drive_min, min_order) VALUES (?,?,?,?,?)",
                         [(z, v["label"], v["fee"], v["drive_min"], 0.0) for z, v in settings.DELIVERY_ZONES.items()])
        out["delivery_zones"] = len(settings.DELIVERY_ZONES)
    if not conn.execute("SELECT 1 FROM staff").fetchone():
        conn.executemany("INSERT INTO staff (name, role, station, hourly_rate) VALUES (?,?,?,?)", DEFAULT_STAFF)
        out["staff"] = len(DEFAULT_STAFF)
    conn.commit()
    return out


# =========================================================================== profile, taxes, hours


def get_profile(conn) -> dict:
    r = conn.execute("SELECT value FROM config_kv WHERE key='profile'").fetchone()
    prof = {**DEFAULT_PROFILE, **(json.loads(r[0]) if r else {})}
    return prof


def set_profile(conn, patch: dict) -> dict:
    prof = get_profile(conn)
    for k, v in patch.items():
        if k not in DEFAULT_PROFILE:
            raise AdminError(f"unknown profile field {k}")
        if k == "loyalty":
            v = {**prof["loyalty"], **v}
            _need(float(v["points_per_dollar"]) >= 0, "points_per_dollar must be >= 0")
            _need(0 <= float(v["redeem_value_per_point"]) <= 1, "redeem_value_per_point must be 0..1")
            _need(all(int(x) >= 0 for x in v["tiers"].values()), "tier thresholds must be >= 0")
        if k == "discount_max_pct":
            v = {**prof["discount_max_pct"], **v}
            _need(all(0 <= float(x) <= 100 for x in v.values()), "discount caps must be 0..100")
        if k == "overbook_factor":
            _need(1.0 <= float(v) <= 1.5, "overbook_factor must be 1.0..1.5")
        prof[k] = v
    conn.execute("INSERT OR REPLACE INTO config_kv (key, value) VALUES ('profile', ?)", (json.dumps(prof),))
    touch(conn)
    return prof


def list_taxes(conn) -> list[dict]:
    return _rows(conn, "SELECT * FROM tax_rules ORDER BY id")


def save_tax(conn, rule: dict, tax_id: int | None = None) -> dict:
    _need(str(rule.get("name", "")).strip() != "", "tax needs a name")
    rate = float(rule.get("rate", -1))
    _need(0 <= rate <= 0.5, "rate is a fraction 0..0.5 (e.g. 0.0625)")
    _need(rule.get("applies_to", "all") in TAX_APPLIES, f"applies_to must be one of {TAX_APPLIES}")
    _need(rule.get("channel", "all") in TAX_CHANNELS, f"channel must be one of {TAX_CHANNELS}")
    vals = (rule["name"], rule.get("jurisdiction", ""), rate, rule.get("applies_to", "all"), rule.get("channel", "all"),
            int(rule.get("active", 1)))
    if tax_id is None:
        tax_id = conn.execute("INSERT INTO tax_rules (name, jurisdiction, rate, applies_to, channel, active) VALUES (?,?,?,?,?,?)",
                              vals).lastrowid
    else:
        _need(conn.execute("SELECT 1 FROM tax_rules WHERE id=?", (tax_id,)).fetchone() is not None, f"unknown tax {tax_id}")
        conn.execute("UPDATE tax_rules SET name=?, jurisdiction=?, rate=?, applies_to=?, channel=?, active=? WHERE id=?", (*vals, tax_id))
    touch(conn)
    return _rows(conn, "SELECT * FROM tax_rules WHERE id=?", (tax_id,))[0]


def delete_tax(conn, tax_id: int) -> None:
    conn.execute("DELETE FROM tax_rules WHERE id=?", (tax_id,))
    touch(conn)


def list_hours(conn) -> list[dict]:
    return _rows(conn, "SELECT * FROM opening_hours ORDER BY weekday, meal_period")


def set_hours(conn, weekday: int, meal_period: str, open_: str, close: str, closed: bool = False) -> dict:
    _need(0 <= int(weekday) <= 6, "weekday 0 (Mon) .. 6 (Sun)")
    _need(meal_period in PERIODS, f"meal_period must be one of {PERIODS}")
    _need(bool(HHMM.match(open_)) and bool(HHMM.match(close)), "times are HH:MM")
    _need(open_ < close, "open must be before close")
    conn.execute("INSERT OR REPLACE INTO opening_hours (weekday, meal_period, open, close, closed) VALUES (?,?,?,?,?)",
                 (int(weekday), meal_period, open_, close, int(closed)))
    touch(conn)
    return {"weekday": int(weekday), "meal_period": meal_period, "open": open_, "close": close, "closed": int(closed)}


def hours_for(conn, day: date, meal_period: str) -> tuple[str, str, bool] | None:
    try:
        r = conn.execute("SELECT open, close, closed FROM opening_hours WHERE weekday=? AND meal_period=?",
                         (day.weekday(), meal_period)).fetchone()
    except sqlite3.Error:
        return None
    return (r[0], r[1], bool(r[2])) if r else None


# =========================================================================== floor, stations, zones


def list_tables(conn) -> list[dict]:
    return _rows(conn, "SELECT * FROM floor_tables ORDER BY shape='bar', CAST(SUBSTR(id, 2) AS INTEGER), id")


def save_table(conn, t: dict) -> dict:
    tid = str(t.get("id", "")).upper()
    _need(bool(TABLE_ID.match(tid)), "table id like T21 or B9")
    seats = int(t.get("seats", 0))
    _need(1 <= seats <= 20, "seats 1..20")
    shape = t.get("shape", "rect")
    _need(shape in SHAPES, f"shape must be one of {SHAPES}")
    x, y, w, h = (float(t.get(k, d)) for k, d in (("x", 100), ("y", 100), ("w", 80), ("h", 60)))
    _need(0 <= x <= 980 and 0 <= y <= 680 and 20 <= w <= 300 and 20 <= h <= 300, "position within the 1000 x 700 plan")
    conn.execute("INSERT OR REPLACE INTO floor_tables (id, seats, x, y, w, h, shape, section, active) VALUES (?,?,?,?,?,?,?,?,?)",
                 (tid, seats, x, y, w, h, shape, t.get("section", ""), int(t.get("active", 1))))
    touch(conn)
    return _rows(conn, "SELECT * FROM floor_tables WHERE id=?", (tid,))[0]


def delete_table(conn, tid: str) -> None:
    conn.execute("DELETE FROM floor_tables WHERE id=?", (tid,))
    touch(conn)


def list_stations(conn) -> list[dict]:
    return _rows(conn, "SELECT * FROM stations ORDER BY rowid")


def save_station(conn, s: dict) -> dict:
    sid = str(s.get("id", ""))
    _need(bool(SLUG.match(sid)), "station id: lowercase letters, digits, underscore")
    _need(str(s.get("name", "")).strip() != "", "station needs a name")
    par, fac = int(s.get("parallel", 3)), float(s.get("cook_factor", 2.4))
    _need(1 <= par <= 8 and 0.5 <= fac <= 6, "parallel 1..8 items per cook; cook_factor 0.5..6")
    cooks = [int(s.get(f"{p}_cooks", 1)) for p in PERIODS]
    _need(all(0 <= c <= 12 for c in cooks), "cooks per period 0..12")
    conn.execute("INSERT OR REPLACE INTO stations (id, name, parallel, cook_factor, lunch_cooks, dinner_cooks, late_cooks, active) "
                 "VALUES (?,?,?,?,?,?,?,?)", (sid, s["name"], par, fac, *cooks, int(s.get("active", 1))))
    touch(conn)
    return _rows(conn, "SELECT * FROM stations WHERE id=?", (sid,))[0]


def delete_station(conn, sid: str) -> None:
    used = conn.execute("SELECT COUNT(*) FROM dishes WHERE station=? AND active=1", (sid,)).fetchone()[0]
    _need(used == 0, f"{used} active dish(es) are cooked on {sid}; move them first")
    conn.execute("DELETE FROM stations WHERE id=?", (sid,))
    touch(conn)


def list_zones(conn) -> list[dict]:
    return _rows(conn, "SELECT * FROM delivery_zones ORDER BY id")


def save_zone(conn, z: dict) -> dict:
    zid = str(z.get("id", "")).strip()
    _need(re.match(r"^[A-Za-z0-9_-]{1,12}$", zid) is not None, "zone id: letters/digits, up to 12")
    fee, drive, mino = float(z.get("fee", -1)), float(z.get("drive_min", 0)), float(z.get("min_order", 0))
    _need(0 <= fee <= 50 and 1 <= drive <= 120 and mino >= 0, "fee 0..50, drive_min 1..120, min_order >= 0")
    conn.execute("INSERT OR REPLACE INTO delivery_zones (id, label, fee, drive_min, min_order, active) VALUES (?,?,?,?,?,?)",
                 (zid, z.get("label", zid), fee, drive, mino, int(z.get("active", 1))))
    touch(conn)
    return _rows(conn, "SELECT * FROM delivery_zones WHERE id=?", (zid,))[0]


def delete_zone(conn, zid: str) -> None:
    conn.execute("DELETE FROM delivery_zones WHERE id=?", (zid,))
    touch(conn)


def zones_dict(conn) -> dict[str, dict]:
    try:
        rows = _rows(conn, "SELECT * FROM delivery_zones WHERE active=1 ORDER BY id")
    except sqlite3.Error:
        rows = []
    return {r["id"]: {"label": r["label"], "fee": r["fee"], "drive_min": r["drive_min"], "min_order": r["min_order"]} for r in rows}


# =========================================================================== ingredients, dishes, recipes


def list_ingredients(conn) -> list[dict]:
    return _rows(conn, "SELECT * FROM ingredients ORDER BY category, name")


def save_ingredient(conn, i: dict) -> dict:
    iid = str(i.get("id", ""))
    _need(bool(SLUG.match(iid)), "ingredient id: lowercase letters, digits, underscore")
    _need(str(i.get("name", "")).strip() != "", "ingredient needs a name")
    _need(i.get("unit") in UNITS, f"unit must be one of {UNITS}")
    _need(i.get("storage") in STORAGE, f"storage must be one of {STORAGE}")
    shelf, cost = int(i.get("shelf_life_days", 0)), float(i.get("ref_cost", -1))
    _need(1 <= shelf <= 3650 and cost > 0, "shelf_life_days 1..3650, ref_cost > 0")
    allergens = ",".join(sorted({a.strip().lower() for a in _list(i.get("allergens")) if a.strip()}))
    months = ",".join(str(int(m)) for m in _list(i.get("seasonal_months")) if str(m).strip())
    _need(all(1 <= int(m) <= 12 for m in months.split(",") if m), "seasonal months 1..12")
    conn.execute("INSERT OR REPLACE INTO ingredients (id, name, unit, category, storage, shelf_life_days, allergens, ref_cost, seasonal_months) "
                 "VALUES (?,?,?,?,?,?,?,?,?)", (iid, i["name"], i["unit"], i.get("category", "dry"), i["storage"], shelf, allergens,
                                                 cost, months))
    touch(conn)
    return _rows(conn, "SELECT * FROM ingredients WHERE id=?", (iid,))[0]


def delete_ingredient(conn, iid: str) -> None:
    used = [r[0] for r in conn.execute("SELECT DISTINCT dish_id FROM recipe_lines WHERE ingredient_id=?", (iid,))]
    _need(not used, f"used by {', '.join(used)}; change those recipes first")
    conn.execute("DELETE FROM supplier_items WHERE ingredient_id=?", (iid,))
    conn.execute("DELETE FROM ingredients WHERE id=?", (iid,))
    touch(conn)


def _list(v) -> list:
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return list(v)
    return [x for x in str(v).split(",")]


def list_dishes(conn, include_retired: bool = True) -> list[dict]:
    rows = _rows(conn, "SELECT * FROM dishes" + ("" if include_retired else " WHERE active=1") + " ORDER BY category, name")
    lines: dict[str, list] = {}
    for r in conn.execute("SELECT dish_id, ingredient_id, qty_per_portion FROM recipe_lines"):
        lines.setdefault(r[0], []).append({"ingredient_id": r[1], "qty_per_portion": r[2]})
    for r in rows:
        r["recipe"] = lines.get(r["id"], [])
    return rows


def save_dish(conn, d: dict) -> dict:
    did = str(d.get("id", ""))
    _need(bool(SLUG.match(did)), "dish id: lowercase letters, digits, underscore")
    _need(str(d.get("name", "")).strip() != "", "dish needs a name")
    _need(d.get("category") in CATEGORIES, f"category must be one of {CATEGORIES}")
    price, prep = float(d.get("price", 0)), float(d.get("prep_minutes", 0))
    _need(0 < price <= 500 and 0 < prep <= 60, "price 0..500, prep_minutes 0..60")
    st = d.get("station")
    _need(conn.execute("SELECT 1 FROM stations WHERE id=? AND active=1", (st,)).fetchone() is not None, f"unknown station {st}")
    recipe = d.get("recipe") or []
    _need(len(recipe) >= 1, "a dish needs at least one recipe line")
    for line in recipe:
        _need(conn.execute("SELECT 1 FROM ingredients WHERE id=?", (line.get("ingredient_id"),)).fetchone() is not None,
              f"unknown ingredient {line.get('ingredient_id')}")
        _need(0 < float(line.get("qty_per_portion", 0)) <= 50, "qty_per_portion must be > 0")
    tc = d.get("tax_category", "food")
    _need(tc in ("food", "alcohol"), "tax_category food | alcohol")
    months = ",".join(str(int(m)) for m in _list(d.get("seasonal_months")) if str(m).strip())
    cost = sum(float(l["qty_per_portion"]) * conn.execute("SELECT ref_cost FROM ingredients WHERE id=?", (l["ingredient_id"],)).fetchone()[0]
               for l in recipe)
    _need(price > cost, f"price {price:.2f} is below the recipe's food cost {cost:.2f}")
    conn.execute("INSERT OR REPLACE INTO dishes (id, name, category, price, station, is_cold, seasonal_months, prep_minutes, active, tax_category) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?)", (did, d["name"], d["category"], price, st, int(bool(d.get("is_cold"))), months, prep,
                                                   int(d.get("active", 1)), tc))
    conn.execute("DELETE FROM recipe_lines WHERE dish_id=?", (did,))
    conn.executemany("INSERT INTO recipe_lines (dish_id, ingredient_id, qty_per_portion) VALUES (?,?,?)",
                     [(did, l["ingredient_id"], float(l["qty_per_portion"])) for l in recipe])
    touch(conn)
    return next(x for x in list_dishes(conn) if x["id"] == did)


def retire_dish(conn, did: str, active: bool = False) -> dict:
    _need(conn.execute("SELECT 1 FROM dishes WHERE id=?", (did,)).fetchone() is not None, f"unknown dish {did}")
    conn.execute("UPDATE dishes SET active=? WHERE id=?", (int(active), did))
    touch(conn)
    return next(x for x in list_dishes(conn) if x["id"] == did)


# =========================================================================== suppliers


def list_suppliers(conn) -> list[dict]:
    rows = _rows(conn, "SELECT * FROM suppliers ORDER BY name")
    items: dict[str, list] = {}
    for r in conn.execute("SELECT * FROM supplier_items ORDER BY ingredient_id"):
        items.setdefault(r["supplier_id"], []).append(dict(r))
    for r in rows:
        r["items"] = items.get(r["id"], [])
    return rows


def save_supplier(conn, s: dict) -> dict:
    sid = str(s.get("id", ""))
    _need(bool(SLUG.match(sid)), "supplier id: lowercase letters, digits, underscore")
    _need(str(s.get("name", "")).strip() != "", "supplier needs a name")
    lead, mino = int(s.get("lead_time_days", 1)), float(s.get("min_order_value", 0))
    fill, delay = float(s.get("fill_rate", 0.95)), float(s.get("delay_prob", 0.05))
    _need(0 <= lead <= 14 and mino >= 0 and 0 < fill <= 1 and 0 <= delay < 1, "lead 0..14 days, fill 0..1, delay 0..1")
    email = str(s.get("email", "")).strip()
    _need(email == "" or re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email) is not None, "invalid email")
    conn.execute("INSERT OR REPLACE INTO suppliers (id, name, lead_time_days, min_order_value, fill_rate, delay_prob, order_cutoff_hour, email) "
                 "VALUES (?,?,?,?,?,?,?,?)", (sid, s["name"], lead, mino, fill, delay, int(s.get("order_cutoff_hour", 14)), email))
    touch(conn)
    return next(x for x in list_suppliers(conn) if x["id"] == sid)


def save_supplier_item(conn, supplier_id: str, ingredient_id: str, price: float, pack_size: float) -> dict:
    _need(conn.execute("SELECT 1 FROM suppliers WHERE id=?", (supplier_id,)).fetchone() is not None, f"unknown supplier {supplier_id}")
    _need(conn.execute("SELECT 1 FROM ingredients WHERE id=?", (ingredient_id,)).fetchone() is not None, f"unknown ingredient {ingredient_id}")
    _need(float(price) > 0 and float(pack_size) > 0, "price and pack_size must be > 0")
    conn.execute("INSERT OR REPLACE INTO supplier_items (supplier_id, ingredient_id, price, pack_size) VALUES (?,?,?,?)",
                 (supplier_id, ingredient_id, float(price), float(pack_size)))
    touch(conn)
    return {"supplier_id": supplier_id, "ingredient_id": ingredient_id, "price": float(price), "pack_size": float(pack_size)}


def delete_supplier_item(conn, supplier_id: str, ingredient_id: str) -> None:
    conn.execute("DELETE FROM supplier_items WHERE supplier_id=? AND ingredient_id=?", (supplier_id, ingredient_id))
    touch(conn)


# =========================================================================== staff and roster


def list_staff(conn) -> list[dict]:
    return _rows(conn, "SELECT * FROM staff ORDER BY active DESC, role, name")


def save_staff(conn, s: dict, staff_id: int | None = None) -> dict:
    _need(str(s.get("name", "")).strip() != "", "staff member needs a name")
    _need(s.get("role") in ROLES_STAFF, f"role must be one of {ROLES_STAFF}")
    st = s.get("station") or None
    if st:
        _need(conn.execute("SELECT 1 FROM stations WHERE id=?", (st,)).fetchone() is not None, f"unknown station {st}")
    rate = float(s.get("hourly_rate", 0))
    _need(0 <= rate <= 200, "hourly_rate 0..200")
    vals = (s["name"], s["role"], st, rate, int(s.get("active", 1)))
    if staff_id is None:
        staff_id = conn.execute("INSERT INTO staff (name, role, station, hourly_rate, active) VALUES (?,?,?,?,?)", vals).lastrowid
    else:
        _need(conn.execute("SELECT 1 FROM staff WHERE id=?", (staff_id,)).fetchone() is not None, f"unknown staff {staff_id}")
        conn.execute("UPDATE staff SET name=?, role=?, station=?, hourly_rate=?, active=? WHERE id=?", (*vals, staff_id))
    touch(conn)
    return _rows(conn, "SELECT * FROM staff WHERE id=?", (staff_id,))[0]


def get_roster(conn, day: str, meal_period: str) -> list[dict]:
    return _rows(conn, "SELECT r.staff_id, r.station, s.name, s.role, s.hourly_rate FROM roster r JOIN staff s ON s.id=r.staff_id "
                       "WHERE r.date=? AND r.meal_period=? ORDER BY r.station, s.name", (day, meal_period))


def set_roster(conn, day: str, meal_period: str, entries: list[dict]) -> dict:
    """Who works a shift. Cooks with a station become that station's cook count in `staff_shifts` (the kitchen model
    reads it), so the roster drives the kitchen's capacity for that service."""
    date.fromisoformat(day)
    _need(meal_period in PERIODS, f"meal_period must be one of {PERIODS}")
    staff = {r["id"]: r for r in list_staff(conn)}
    counts: dict[str, int] = {}
    for e in entries:
        sid = int(e["staff_id"])
        _need(sid in staff and staff[sid]["active"], f"unknown or inactive staff {sid}")
        st = e.get("station") or staff[sid]["station"]
        if staff[sid]["role"] in ("cook",):
            _need(st is not None, f"{staff[sid]['name']} needs a station")
            counts[st] = counts.get(st, 0) + 1
    conn.execute("DELETE FROM roster WHERE date=? AND meal_period=?", (day, meal_period))
    conn.executemany("INSERT INTO roster (date, meal_period, staff_id, station) VALUES (?,?,?,?)",
                     [(day, meal_period, int(e["staff_id"]), e.get("station") or staff[int(e["staff_id"])]["station"]) for e in entries])
    for st in [r["id"] for r in list_stations(conn)]:
        if st in counts:
            conn.execute("INSERT OR REPLACE INTO staff_shifts (date, meal_period, station, staff) VALUES (?,?,?,?)",
                         (day, meal_period, st, counts[st]))
        else:
            conn.execute("DELETE FROM staff_shifts WHERE date=? AND meal_period=? AND station=?", (day, meal_period, st))
    touch(conn)
    labor = sum(staff[int(e["staff_id"])]["hourly_rate"] for e in entries)
    return {"date": day, "meal_period": meal_period, "cooks_per_station": counts, "people": len(entries),
            "labor_per_hour": round(labor, 2)}


# =========================================================================== everything for the setup screen


def snapshot(conn) -> dict:
    last = conn.execute("SELECT value FROM config_kv WHERE key='last_admin_change'").fetchone()
    return {"profile": get_profile(conn), "taxes": list_taxes(conn), "hours": list_hours(conn), "tables": list_tables(conn),
            "stations": list_stations(conn), "zones": list_zones(conn), "ingredients": list_ingredients(conn),
            "dishes": list_dishes(conn), "suppliers": list_suppliers(conn), "staff": list_staff(conn),
            "last_change": json.loads(last[0]) if last else None,
            "choices": {"categories": CATEGORIES, "units": UNITS, "storage": STORAGE, "shapes": SHAPES, "roles": ROLES_STAFF,
                        "tax_applies": TAX_APPLIES, "tax_channels": TAX_CHANNELS, "periods": PERIODS}}
