"""Unit tests for the deterministic real-time pieces (no seeded DB needed)."""
from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from restaurant_ops.core.constraints import blocking, check_action
from restaurant_ops.data import catalog as cat
from restaurant_ops.data.catalog import load_catalog
from restaurant_ops.db.connection import get_conn, init_db
from restaurant_ops.live import events as E
from restaurant_ops.live.bus import InProcessBus, RedisStreamsBus, redis_client
from restaurant_ops.live.floor import Floor, Party
from restaurant_ops.live.kitchen import Kitchen, cook_minutes, is_quick
from restaurant_ops.live.tools import allergen_resolution, parse_mods, safe_alternatives

STAFF = {"grill": 2, "saute": 4, "cold": 2, "pastry": 1}


# --------------------------------------------------------------------------- catalog / world


def test_four_stations_and_80_seats():
    assert cat.STATIONS == ["grill", "saute", "cold", "pastry"]
    assert {d.station for d in cat.DISHES} <= set(cat.STATIONS)
    assert len(cat.DISHES) == 20 and len(cat.INGREDIENTS) == 60 and len(cat.SUPPLIERS) == 4
    f = Floor()
    assert f.seats == 80
    assert sum(1 for t in f.tables.values() if t.shape != "bar") == 20
    assert sum(1 for t in f.tables.values() if t.shape == "bar") == 8


# --------------------------------------------------------------------------- kitchen-load model


def test_quote_is_wait_plus_prep_plus_buffer():
    k = Kitchen(STAFF, seed=1)
    q = k.quote(["ribeye_steak"], now=0.0, buffer_min=3.0)
    assert q["p50_min"] == pytest.approx(cook_minutes("ribeye_steak") + 3.0, abs=0.05)
    assert q["p90_min"] > q["p50_min"]
    assert q["slowest_station"] == "grill"


def test_queue_raises_wait_and_quote():
    k = Kitchen(STAFF, seed=1)
    base = k.quote(["ribeye_steak"], 0.0)["p50_min"]
    for i in range(12):
        k.add_item(f"i{i}", "o1", "ribeye_steak", 0.0)
    k.advance(0.0)
    st = k.stations["grill"]
    assert len(st.cooking) == st.slots and len(st.queue) == 12 - st.slots
    w = k.wait_minutes("grill", 0.0)
    assert w == pytest.approx(12 * cook_minutes("ribeye_steak") / st.slots, rel=1e-6)
    assert k.quote(["ribeye_steak"], 0.0)["p50_min"] == pytest.approx(base + w, abs=0.05)
    # other stations are unaffected
    assert k.quote(["caesar_salad"], 0.0)["p50_min"] == pytest.approx(cook_minutes("caesar_salad") + 3.0, abs=0.05)


def test_items_finish_and_recalibrate():
    k = Kitchen(STAFF, seed=3)
    for i in range(20):
        k.add_item(f"i{i}", "o", "carbonara", i * 30.0)
    done = []
    t = 0.0
    while t < 4 * 3600 and len(done) < 20:
        t += 15
        done += k.advance(t)["done"]
    assert len(done) == 20
    assert all(i.done_at >= i.start_at >= i.fire_at for i in done)
    actual = sum(i.actual_min for i in done) / 20 / cook_minutes("carbonara")
    assert k.cal["saute"] != 1.0
    assert abs(k.cal["saute"] - actual) < abs(1.0 - actual) + 0.05  # moved toward the observed ratio


def test_load_busyness_levels():
    k = Kitchen(STAFF, seed=1)
    assert k.load(0.0)["busyness"] == "calm"
    for i in range(30):
        k.add_item(f"i{i}", "o", "ribeye_steak", 0.0)
    k.advance(0.0)
    assert k.load(0.0)["busyness"] == "slammed"


def test_quick_items():
    assert is_quick("caesar_salad")
    assert not is_quick("ribeye_steak")


# --------------------------------------------------------------------------- order parsing / allergens


def test_parse_mods():
    m = parse_mods("salmon, no dairy, nut allergy")
    assert set(m["allergens"]) == {"milk", "nuts"}
    assert parse_mods("no onion please")["remove"] == ["onion"]
    assert "gluten" in parse_mods("coeliac, no gluten")["allergens"]
    assert set(parse_mods("shellfish allergy")["allergens"]) == {"crustaceans", "molluscs"}
    assert parse_mods("")["allergens"] == []


def test_allergen_resolution():
    salmon = allergen_resolution("seared_salmon", ["milk", "nuts"])
    assert salmon["conflict"] == ["milk"] and salmon["resolvable"]
    assert set(salmon["leave_out"]) == {"butter", "cream"}
    carb = allergen_resolution("carbonara", ["eggs"])
    assert carb["conflict"] == ["eggs"] and not carb["resolvable"]
    assert allergen_resolution("ribeye_steak", ["nuts"])["conflict"] == []


def test_safe_alternatives_never_contain_the_allergen():
    avail = {d.id: 10 for d in cat.DISHES}
    alts = safe_alternatives("carbonara", ["eggs"], avail)
    assert alts
    for a in alts:
        assert "eggs" not in cat.dish_allergens(a) or allergen_resolution(a, ["eggs"])["resolvable"]


# --------------------------------------------------------------------------- floor seating


def test_seating_picks_fitting_tables_and_combos():
    f = Floor()
    two = f.find_seating(2)
    assert len(two) == 1 and f.tables[two[0]].seats == 2
    six = f.find_seating(6)
    assert f.tables[six[0]].seats == 6
    twelve = f.find_seating(12)
    assert len(twelve) == 2 and sum(f.tables[t].seats for t in twelve) >= 12
    bar = f.find_seating(2, prefer_bar=True)
    assert len(bar) == 2 and all(f.tables[t].shape == "bar" for t in bar)
    p = Party("P1", 12, "large", 0.0)
    f.parties[p.id] = p
    f.seat(p, twelve, 0.0)
    assert all(f.tables[t].state == "seated" and f.tables[t].party_id == "P1" for t in twelve)


# --------------------------------------------------------------------------- event bus


def _bus_conn(tmp_path) -> sqlite3.Connection:
    c = get_conn(tmp_path / "bus.db")
    init_db(c)
    return c


def _ordering(bus):
    seen = []

    def a(ev):
        seen.append(("a", ev.type, ev.seq))
        if ev.type == "x":
            bus.publish("y", {"from": ev.seq})  # nested publish is queued, not recursive

    bus.subscribe(["x", "y"], a)
    bus.subscribe("*", lambda ev: seen.append(("*", ev.type, ev.seq)))
    bus.publish("x", {})
    bus.publish("x", {})
    return seen


def test_inprocess_bus_orders_and_logs(tmp_path):
    c = _bus_conn(tmp_path)
    bus = InProcessBus("svc-t", c, clock=lambda: "2026-11-02T18:00:00")
    seen = _ordering(bus)
    assert [s for s in seen if s[0] == "a"] == [("a", "x", 1), ("a", "y", 2), ("a", "x", 3), ("a", "y", 4)]
    assert [s[2] for s in seen if s[0] == "*"] == [1, 2, 3, 4]
    rows = c.execute("SELECT seq, type, sim_ts FROM event_log WHERE service_id='svc-t' ORDER BY seq").fetchall()
    assert [(r[0], r[1]) for r in rows] == [(1, "x"), (2, "y"), (3, "x"), (4, "y")]
    assert rows[0][2] == "2026-11-02T18:00:00"


def test_bus_isolates_failing_subscriber(tmp_path):
    bus = InProcessBus("svc-t", None)
    got = []
    bus.subscribe("x", lambda ev: 1 / 0, "bad")
    bus.subscribe("x", lambda ev: got.append(ev.seq), "good")
    bus.publish("x")
    assert got == [1] and bus.errors and "bad" in bus.errors[0]


def test_redis_streams_bus(tmp_path):
    r = redis_client()
    if r is None:
        pytest.skip("no Redis server reachable at REDIS_URL")
    bus = RedisStreamsBus(f"svc-test-{tmp_path.name}", r, _bus_conn(tmp_path))
    try:
        seen = _ordering(bus)
        assert [s for s in seen if s[0] == "a"] == [("a", "x", 1), ("a", "y", 2), ("a", "x", 3), ("a", "y", 4)]
        assert r.xlen(bus.stream) == 4
    finally:
        r.delete(bus.stream)


def test_event_types_are_unique():
    assert len(E.ALL_TYPES) == len(set(E.ALL_TYPES))
    assert E.ORDER_CREATED in E.ALL_TYPES and E.DECISION_RESOLVED in E.ALL_TYPES


# --------------------------------------------------------------------------- constraint checker: live action types


@pytest.fixture()
def conn(tmp_path):
    c = get_conn(tmp_path / "c.db")
    init_db(c)
    load_catalog(c)
    c.executemany("INSERT INTO lots VALUES (?,?,?,?,?,?,?,?)", [
        ("L-COD", "cod", 5.0, "2026-11-01", "2026-11-04", "sup_freshcatch", 19.0, "active"),
        ("L-TOM", "cherry_tomato", 5.0, "2026-11-01", "2026-11-08", "sup_metro", 6.0, "active"),
        ("L-CAP", "capers", 1.0, "2026-10-01", "2027-10-01", "sup_metro", 14.0, "active"),
        ("L-FEN", "fennel", 5.0, "2026-11-01", "2026-11-09", "sup_metro", 3.5, "active"),
        ("L-OIL", "olive_oil", 5.0, "2026-10-01", "2027-10-01", "sup_metro", 8.0, "active"),
        ("L-WIN", "white_wine", 5.0, "2026-10-01", "2027-01-01", "sup_metro", 6.0, "active"),
        ("L-CRM", "cream", 3.0, "2026-11-01", "2026-11-11", "sup_metro", 5.5, "active"),
    ])
    c.commit()
    yield c
    c.close()


AS_OF = date(2026, 11, 2)


def act(t, **p):
    return {"action_type": t, "payload": p, "agent": "test"}


def codes(vs, sev="block"):
    return {v.code for v in vs if v.severity == sev}


def test_throttle_online_rules(conn):
    assert not blocking(check_action(act("throttle_online", mode="quick_only", reason="grill 14 min behind"), conn, AS_OF))
    assert "invalid_mode" in codes(check_action(act("throttle_online", mode="closed", reason="x"), conn, AS_OF))
    assert "missing_reason" in codes(check_action(act("throttle_online", mode="paused"), conn, AS_OF))


def test_allergen_swap_guard(conn):
    ok = act("allergen_swap", order_id="o", item_id="i", from_dish="carbonara", to_dish="baked_cod", guest_allergens=["eggs"])
    assert not blocking(check_action(ok, conn, AS_OF))
    bad = act("allergen_swap", order_id="o", item_id="i", from_dish="carbonara", to_dish="chicken_milanese", guest_allergens=["eggs"])
    assert "allergen_conflict" in codes(check_action(bad, conn, AS_OF))
    drop = act("allergen_swap", order_id="o", item_id="i", from_dish="carbonara", to_dish=None, guest_allergens=["eggs"])
    assert not blocking(check_action(drop, conn, AS_OF))


def test_comp_and_discard_lots(conn):
    assert not blocking(check_action(act("comp", order_id="W1", amount=5.0, order_total=40.0), conn, AS_OF))
    assert "comp_too_large" in codes(check_action(act("comp", order_id="W1", amount=30.0, order_total=40.0), conn, AS_OF))
    ok = act("discard_lots", lots=[{"lot_id": "L-CRM", "reason": "spoilage"}])
    assert not blocking(check_action(ok, conn, AS_OF))
    assert "lot_not_found" in codes(check_action(act("discard_lots", lots=[{"lot_id": "NOPE", "reason": "spoilage"}]), conn, AS_OF))
