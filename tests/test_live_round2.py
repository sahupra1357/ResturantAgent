"""Round 2: prep & staffing agent, negotiation and supplier learning, auth and roles, persistence / resume, host stand,
payments and delivery, POS import, guest profiles, food safety, menu engineering, Redis failure, stubbed LLM review,
API contract, WebSocket fan-out, metrics, retention, JSON logs."""
from __future__ import annotations

import csv
import json
import logging
import os
import sqlite3
import time

import pytest

from restaurant_ops.config import settings
from tests.conftest import ANON, bearer, token  # noqa: F401
from restaurant_ops.live import events as E
from restaurant_ops.live.replay import load_events
from restaurant_ops.live.run import POLICIES, parse_schedule
from restaurant_ops.live.service import Service

pytestmark = pytest.mark.usefixtures("seeded", "live_dir")


def make(policy="approve", scenarios=(), seed=42, bus="memory") -> Service:
    svc = Service(seed=seed, speed=60, bus_kind=bus, auto_policy=POLICIES[policy]())
    svc.schedule = parse_schedule(list(scenarios), svc)
    return svc


def decisions(svc, action_type=None, agent=None):
    return [d for d in svc.coordinator.decisions.values()
            if (action_type is None or d["action_type"] == action_type) and (agent is None or d["agent"] == agent)]


def events_of(svc, t):
    svc.flush()
    rows = svc.conn.execute("SELECT seq, sim_ts, type, payload FROM event_log WHERE service_id=? AND type=? ORDER BY seq",
                            (svc.service_id, t)).fetchall()
    return [{"seq": r[0], "sim_ts": r[1], "type": r[2], "payload": json.loads(r[3])} for r in rows]


@pytest.fixture(scope="module")
def full_night(tmp_path_factory, seeded):
    """One complete seeded dinner with salmon short, a party of 12 and the cooler failure (demo manager policy)."""
    old = settings.LIVE_DIR
    settings.LIVE_DIR = tmp_path_factory.mktemp("live_full")

    svc = Service(seed=42, speed=60, bus_kind="memory", auto_policy=POLICIES["demo"]())
    svc.schedule = parse_schedule(["salmon_short@18:45", "party_of_12@19:15", "cooler_failure@20:30"], svc)
    svc.run_headless()
    yield svc
    svc.close()
    settings.LIVE_DIR = old


# =========================================================================== 1. prep & staffing agent


def test_prep_list_burns_down_and_scratch_is_slower(full_night):
    svc = full_night
    p = svc.prep.prep
    assert p and sum(v["par"] for v in p.values()) > 50
    assert sum(v["used"] for v in p.values()) > 30, "fired items use prepped portions"
    assert any(d["status"] == "executed" for d in decisions(svc, "prep_batch", "prep")), "batches are scheduled during service"
    scratch = [i for i in svc.kitchen.items.values() if getattr(i, "scratch", False)]
    for it in scratch[:5]:
        assert it.est_min > 0 and it.actual_min > 0


def test_surge_staffing_call_in_and_release(full_night):
    svc = full_night
    calls = decisions(svc, "add_staff", "prep")
    assert calls and calls[0]["needs_approval"] and calls[0]["status"] == "executed"
    st = calls[0]["payload"]["station"]
    ups = [e for e in events_of(svc, E.STAFF_UPDATED) if e["payload"]["station"] == st and e["payload"]["change"] == "+1"]
    assert ups, "the called-in cook arrives and the station gains slots"
    assert svc.prep.surge_spent == pytest.approx(settings.COOK_HOURLY_RATE * settings.SURGE_SHIFT_HOURS)
    assert svc.prep.surge_spent <= settings.SURGE_LABOR_BUDGET


def test_labor_budget_and_set_cooks():
    from restaurant_ops.core.constraints import blocking, check_action
    from restaurant_ops.db.connection import get_conn
    from restaurant_ops.live.kitchen import Kitchen

    c = get_conn(settings.DB_PATH)
    try:
        ok = {"action_type": "add_staff", "payload": {"station": "grill", "add": 1, "hours": 3, "hourly_rate": 28, "surge_spent": 0}}
        assert not blocking(check_action(ok, c, settings.SIM_START))
        over = {"action_type": "add_staff", "payload": {"station": "grill", "add": 1, "hours": 3, "hourly_rate": 28,
                                                       "surge_spent": settings.SURGE_LABOR_BUDGET - 10}}
        assert "labor_budget" in {v.code for v in blocking(check_action(over, c, settings.SIM_START))}
    finally:
        c.close()
    k = Kitchen({"grill": 2, "saute": 4, "cold": 2, "pastry": 1})
    assert k.stations["grill"].slots == 6
    k.set_cooks("grill", 3, 0.0)
    assert k.stations["grill"].slots == 9 and len(k.stations["grill"].slots_free_at) == 9
    k.set_cooks("grill", 2, 0.0)
    assert len(k.stations["grill"].slots_free_at) == 6


# =========================================================================== 2. negotiation + supplier learning


def test_negotiation_and_supplier_learning(full_night):
    svc = full_night
    pos = [d for d in decisions(svc, "place_po", "procurement") if (d.get("context") or {}).get("ingredient_id") == "salmon"]
    assert pos
    ctx = pos[0]["context"]
    assert "negotiate" in pos[0]["tools"]
    assert ctx["negotiated_price"] <= ctx["list_price"]
    learned = events_of(svc, E.SUPPLIER_LEARNED)
    assert learned and learned[0]["payload"]["supplier_id"] == "sup_freshcatch"
    assert learned[0]["payload"]["fill_rate"] < learned[0]["payload"]["fill_rate_before"]
    row = svc.conn.execute("SELECT fill_rate FROM suppliers WHERE id='sup_freshcatch'").fetchone()
    assert row[0] == pytest.approx(learned[-1]["payload"]["fill_rate"])
    assert svc.conn.execute("SELECT COUNT(*) FROM supplier_perf").fetchone()[0] >= 1


def test_scorecard_blends_history():
    from restaurant_ops.core import suppliers as S
    from restaurant_ops.db.connection import get_conn

    c = get_conn(settings.DB_PATH)
    try:
        rows = {r["supplier_id"]: r for r in S.scorecard(c, write=False)}
    finally:
        c.close()
    b = rows["sup_budget"]
    assert b["observed_pos"] > 0 and b["observed_fill"] is not None
    assert min(b["prior_fill"], b["observed_fill"]) <= b["fill_rate"] <= max(b["prior_fill"], b["observed_fill"])


# =========================================================================== 3. auth & roles


def _client(user: str | None = "manager"):
    """A TestClient that sends `user`'s session token by default (None: anonymous)."""
    from fastapi.testclient import TestClient

    from restaurant_ops.live import server

    return TestClient(server.app, headers=bearer(user) if user else {})


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


def test_roles_guard_mutations():
    with _client(None) as c:
        assert c.get("/api/state").status_code == 401                       # reads need a signed-in user
        assert c.get("/api/public/state").status_code == 200                # ... except the public online view
        assert c.post("/api/clock", json={"speed": 0}).status_code == 401    # no session
        assert c.post("/api/clock", json={"speed": 0}, headers=H("wrong")).status_code == 401
        srv, mgr, kit = bearer("server"), bearer("manager"), bearer("kitchen")
        assert c.post("/api/clock", json={"speed": 0}, headers=srv).status_code == 403
        assert c.post("/api/clock", json={"speed": 0}, headers=mgr).status_code == 200
        assert c.post("/api/scenarios/heatwave", headers=kit).status_code == 403
        assert c.post("/api/decisions/1", json={"approved": True}, headers=srv).status_code == 403
        assert c.post("/api/online/mode", json={"mode": "paused"}, headers=srv).status_code == 403
        assert c.get("/api/admin/config", headers=mgr).status_code == 403   # setup is admin-only
        st = c.get("/api/state", headers=srv).json()
        free = next(t for t, v in st["tables"].items() if v["state"] == "available" and v["seats"] >= 2)
        body = {"table_id": free, "items": [{"dish_id": "caesar_salad"}], "party_size": 2}
        assert c.post("/api/orders", json=body, headers=kit).status_code == 403
        assert c.post("/api/orders", json=body, headers=srv).status_code == 200
        assert c.get("/api/auth/me", headers=kit).json()["role"] == "kitchen"
        assert c.get(f"/api/report.html?token={token('manager')}").status_code == 401   # no tokens in URLs
        assert c.get("/api/report.html", headers=mgr).status_code == 200
        # the public online page needs no session
        assert c.post("/api/online/quote", json={"dish_ids": ["caesar_salad"]}).status_code == 200


def test_auth_can_be_disabled(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_ENABLED", False)
    with _client() as c:
        assert c.post("/api/clock", json={"speed": 0}).status_code == 200
        assert c.get("/api/auth/me", headers=ANON).json()["role"] == "admin"   # auth off: local admin


# =========================================================================== 4. persistence / resume


def test_pending_approval_survives_restart():
    svc = make("timeout")
    svc.start()
    svc.advance(30)
    free = next(t for t, v in svc.floor.tables.items() if v.state == "available" and v.seats >= 2)
    res = svc.manual_order(free, [{"dish_id": "carbonara", "mods": "egg allergy"}], party_size=1)
    swap = next(d for d in decisions(svc, "allergen_swap") if d["status"] == "pending")
    sid, t_before, covers = svc.service_id, svc.clock.t, svc.metrics.covers_seated
    svc.save_state()
    svc.close()                                                 # backend goes down
    back = Service.resume(sid, auto_policy=None)                # ... and comes back
    try:
        assert back.clock.t == t_before and back.metrics.covers_seated == covers
        pend = [d for d in back.coordinator.pending() if d["id"] == swap["id"]]
        assert pend, "the paused approval is still there"
        out = back.coordinator.resolve(swap["id"], True, "ok after restart")
        assert out["status"] == "executed"
        assert any(i["dish_id"] == swap["payload"]["to_dish"] for i in back.orders[res["order_id"]]["items"])
        back.advance(20)                                        # the service keeps running
        assert back.clock.t > t_before
        assert any(e["type"] == E.SERVICE_STARTED and e["payload"].get("resumed") for e in load_events(sid))
    finally:
        back.close()


def test_latest_resumable(tmp_path):
    from restaurant_ops.live import persistence as P

    svc = make()
    svc.start()
    svc.save_state()
    sid = svc.service_id
    svc.close()
    assert P.latest_resumable() == sid


# =========================================================================== 5. reservations, waitlist, tables


def test_bookings_overbooking_and_status():
    from restaurant_ops.live.host import HostError

    svc = make()
    try:
        svc.start()
        b = svc.host.create("Kim", 4, "19:30", phone="555-0199")
        assert b.status == "booked" and b.guest_id
        assert svc.crm.by_phone("555-0199")["name"] == "Kim"
        with pytest.raises(HostError, match="overbooked"):
            for i in range(20):  # 20-person bookings around 19:30 until the window is full
                svc.host.create(f"Group {i}", 20, "19:30")
        ok = svc.host.create("Allowed", 20, "19:30", allow_overbook=True)
        assert ok.overbooked
        svc.host.set_status(ok.booking_id, "cancelled")
        assert svc.host.bookings[ok.booking_id].status == "cancelled"
        r = svc.host.arrive(b.booking_id)
        assert r["status"] in ("seated", "arrived") and svc.floor.parties[r["party_id"]].guest_id == b.guest_id
        with pytest.raises(HostError):
            svc.host.arrive(b.booking_id)
    finally:
        svc.close()


def test_sim_bookings_arrive_and_no_shows(full_night):
    svc = full_night
    st = {}
    for b in svc.host.bookings.values():
        st[b.status] = st.get(b.status, 0) + 1
    assert st.get("seated", 0) > 5
    assert st.get("booked", 0) == 0, "every simulated booking ends seated, no-show or cancelled"


def test_waitlist_quotes_and_table_ops(monkeypatch):
    svc = make()
    try:
        svc.start()
        orig = svc.floor.find_seating
        monkeypatch.setattr(svc.floor, "find_seating", lambda size, prefer_bar=False: None)
        w = svc.host.add_to_waitlist("Rivera", 4, phone="555-0177")
        assert not w["seated"] and w["quoted_min"] >= 0
        view = svc.host.waitlist_view()
        assert view[0]["name"] == "Rivera" and view[0]["position"] == 1
        monkeypatch.setattr(svc.floor, "find_seating", orig)
        free4 = next(t for t, v in svc.floor.tables.items() if v.state == "available" and v.seats == 4)
        svc.host.seat_from_waitlist(w["party_id"], [free4])
        p = svc.floor.parties[w["party_id"]]
        assert p.table_ids == [free4] and svc.floor.tables[free4].party_id == p.id
        other = next(t for t, v in svc.floor.tables.items() if v.state == "available" and v.seats == 4 and t != free4)
        svc.host.move(p.id, [other])
        assert svc.floor.tables[free4].state == "available" and svc.floor.tables[other].party_id == p.id
        extra = next(t for t, v in svc.floor.tables.items() if v.state == "available" and v.seats == 2)
        svc.host.combine(p.id, extra)
        assert set(p.table_ids) == {other, extra}
        svc.host.split(p.id, extra)
        assert p.table_ids == [other] and svc.floor.tables[extra].state == "available"
    finally:
        svc.close()


# =========================================================================== 6. payments & delivery


def test_checks_paid_with_tips_and_splits(full_night):
    svc = full_night
    paid = [c for c in svc.payments.checks.values() if c.status == "paid" and c.channel == "dine_in"]
    assert len(paid) > 10
    s = svc.payments.summary()
    assert s["tips"] > 0 and s["collected"] > 0
    for c in paid:
        t = svc.payments.totals(c)
        assert t["balance"] == 0 and t["paid"] == pytest.approx(t["due"], abs=0.02)
    for c in paid:  # a simulated split (20% of parties of 4+) pays every share
        assert all(sp["paid"] for sp in c.splits)
    assert svc.conn.execute("SELECT COUNT(*) FROM payments WHERE kind='payment'").fetchone()[0] >= len(paid)


def test_simulated_party_splits_the_bill_evenly():
    svc = make()
    try:
        svc.start()
        st = svc.snapshot()
        free = next(t for t, v in st["tables"].items() if v["state"] == "available" and v["seats"] >= 4)
        svc.manual_order(free, [{"dish_id": "ribeye_steak"}, {"dish_id": "caesar_salad"}], party_size=4)
        c = svc.payments.open_for_party(svc.floor.parties[svc.floor.tables[free].party_id])
        svc.payments.settle_sim(c, tip_pct=0.18, split_ways=2)
        assert len(c.splits) == 2 and all(sp["paid"] for sp in c.splits) and svc.payments.totals(c)["balance"] == 0
        assert len([p for p in c.payments if p["amount"] > 0]) == 2
    finally:
        svc.close()


def test_discount_refund_guards():
    from restaurant_ops.live.payments import PaymentError

    svc = make()
    try:
        svc.start()
        svc.advance(60)
        p = next(p for p in svc.floor.parties.values() if p.food_order and p.table_ids)
        c = svc.payments.open_for_party(p)
        t = svc.payments.totals(c)
        with pytest.raises(PaymentError, match="max"):
            svc.payments.discount(c.check_id, "too generous", pct=60)
        with pytest.raises(PaymentError, match="reason"):
            svc.payments.discount(c.check_id, "", pct=10)
        svc.payments.discount(c.check_id, "birthday", pct=10)
        split = svc.payments.split(c.check_id, "even", 3)
        assert sum(x["amount"] for x in split["splits"]) == pytest.approx(split["balance"], abs=0.01)
        for i, sp in enumerate(split["splits"]):
            svc.payments.pay(c.check_id, sp["amount"], tip=2.0, split_index=i)
        assert svc.payments.checks[c.check_id].status == "paid"
        paid = svc.payments.totals(c)["paid"]
        with pytest.raises(PaymentError, match="exceeds"):
            svc.payments.refund(c.check_id, paid + 5, "overcharge")
        out = svc.payments.refund(c.check_id, 5.0, "cold fries")
        assert out["refunded"] == 5.0
        assert t["subtotal"] > 0
    finally:
        svc.close()


def test_delivery_orders_dispatch_and_arrive(full_night):
    svc = full_night
    dl = [o for o in svc.online.orders.values() if o.fulfilment == "delivery"]
    assert dl, "some online orders are deliveries"
    done = [o for o in dl if o.status == "delivered"]
    assert done and all(o.driver_id and o.delivered_t > o.ready_t for o in done)
    assert all(o.fee > 0 for o in dl)
    assert svc.kpis()["delivery_accuracy_pct"] is not None
    online_checks = [c for c in svc.payments.checks.values() if c.channel == "online"]
    assert len(online_checks) == len(svc.online.orders) and all(c.status == "paid" for c in online_checks)


def test_delivery_quote_includes_drive():
    svc = make()
    try:
        svc.start()
        svc.advance(30)
        pick = svc.quote_preview(["ribeye_steak"])
        far = svc.quote_preview(["ribeye_steak"], "delivery", "C")
        assert far["p50_min"] >= pick["p50_min"] + settings.DELIVERY_ZONES["C"]["drive_min"] - 0.1
        assert far["fee"] == settings.DELIVERY_ZONES["C"]["fee"]
        r = svc.place_online([{"dish_id": "ribeye_steak"}], "Test", "delivery", "B", phone="555-0123")
        assert r["online"]["fulfilment"] == "delivery" and r["online"]["zone"] == "B"
        assert r["online"]["guest_id"] == svc.crm.by_phone("555-0123")["guest_id"]
    finally:
        svc.close()


# =========================================================================== 7. POS import


def test_pos_import(tmp_path):
    from restaurant_ops.data import importer as IMP
    from restaurant_ops.db.connection import get_conn, init_db

    path = tmp_path / "pos.csv"
    rows = [
        ["CHK1", "2026-09-01 12:05", "Caesar Salad", "2", "2"],
        ["CHK1", "2026-09-01 12:05", "grilled ribeye", "1", "2"],
        ["CHK2", "2026-09-01 19:40", "Pan Seared Salmon", "3", "3"],   # fuzzy match
        ["CHK2", "2026-09-01 19:40", "House Wine", "2", "3"],          # not a dish: unmapped
        ["CHK3", "2026-09-01 22:30", "BBQ Chicken Wings", "1", "1"],
        ["CHK4", "not a date", "Caesar Salad", "1", "1"],
    ]
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["check_id", "opened_at", "item", "qty", "guests"])
        w.writerows(rows)
    db = tmp_path / "pilot.db"
    dry = IMP.import_pos(path, db, dry_run=True)
    assert dry["written"] is None and dry["unmapped"] == {"House Wine": 2} and dry["bad_rows"] == 1
    assert dry["matched_by"]["fuzzy"] >= 1
    out = IMP.import_pos(path, db, set_sim_start=True)
    c = get_conn(db)
    try:
        init_db(c)
        cov = {(r[0], r[1]): r[2] for r in c.execute("SELECT date, meal_period, covers FROM covers")}
        sales = {(r[0], r[1], r[2]): r[3] for r in c.execute("SELECT date, meal_period, dish_id, qty FROM pos_sales")}
        meta = c.execute("SELECT value FROM meta WHERE key='sim_start'").fetchone()[0]
    finally:
        c.close()
    assert cov == {("2026-09-01", "lunch"): 2, ("2026-09-01", "dinner"): 3, ("2026-09-01", "late"): 1}
    assert sales[("2026-09-01", "dinner", "seared_salmon")] == 3 and sales[("2026-09-01", "lunch", "ribeye_steak")] == 1
    assert meta == "2026-09-02" and out["written"]["sales_rows"] == 4


# =========================================================================== 8. guest profiles & loyalty


def test_profile_allergies_apply_and_loyalty():
    from restaurant_ops.live.crm import tier_for

    svc = make("approve")
    try:
        svc.start()
        svc.advance(20)
        g = svc.crm.upsert("Ana Pereira", phone="555-0142", allergies=["dairy"])
        assert g["allergies"] == ["milk"]
        free = next(t for t, v in svc.floor.tables.items() if v.state == "available" and v.seats >= 2)
        res = svc.manual_order(free, [{"dish_id": "seared_salmon"}], party_size=1, guest_id=g["guest_id"])  # nothing written down
        item = res["items"][0]
        assert item["allergy_resolved"] and any("milk" in m.lower() for m in item["mods"])
        assert res["profile_allergens"] == ["milk"]
        after = svc.crm.record_visit(g["guest_id"], svc.service_id, svc.date.isoformat(), 600.0, 2, ["seared_salmon"])
        assert after["visits"] == 1 and after["points"] == 600 and after["tier"] == tier_for(600) == "Silver"
        assert svc.crm.visits(g["guest_id"])[0]["spend"] == 600.0
    finally:
        svc.close()


# =========================================================================== 9. food safety & waste


def test_temperature_log_haccp_and_cooler_alert(full_night):
    svc = full_night
    n = svc.conn.execute("SELECT COUNT(*) FROM temp_log WHERE service_id=?", (svc.service_id,)).fetchone()[0]
    assert n >= 3 * 10, "three units logged every 30 service minutes"
    alerts = events_of(svc, E.TEMP_ALERT)
    assert alerts and alerts[0]["payload"]["unit"] == "walk_in" and not alerts[0]["payload"]["ok"]
    disc = decisions(svc, "discard_lots", "inventory")
    assert disc and disc[0]["status"] == "executed" and "°C" in disc[0]["reason"]
    chk = svc.safety.checklist()
    assert any(r["done_by"] == "sensor" for r in chk)
    manual = next(r for r in chk if r["done_ts"] is None)
    done = svc.safety.complete(manual["id"], "kitchen", True, "checked")
    assert done["done_by"] == "kitchen"
    w = svc.waste(1)
    assert w["total_value"] > 0 and any(r["reason"] == "spoilage" for r in w["by_reason"])


def test_donation_flow(monkeypatch):
    svc = make("approve")
    try:
        svc.start()
        lot = svc.conn.execute("SELECT lot_id, ingredient_id FROM lots WHERE status='active' AND qty*unit_cost > 30 "
                               "AND ingredient_id='cream' LIMIT 1").fetchone() or \
            svc.conn.execute("SELECT lot_id, ingredient_id FROM lots WHERE status='active' AND qty*unit_cost > 30 LIMIT 1").fetchone()
        svc.conn.execute("UPDATE lots SET expiry_date=? WHERE lot_id=?", (svc.date.isoformat(), lot[0]))
        svc.stock.reload()
        monkeypatch.setattr(svc.forecast, "dish_remaining", lambda t_min: {})
        svc.inventory.propose_donations()
        d = next(d for d in decisions(svc, "donate_lots") if lot[0] in [x["lot_id"] for x in d["payload"]["lots"]])
        assert d["needs_approval"]
        svc.advance(1)  # the approve policy answers on the next tick
        assert d["status"] == "executed"
        row = svc.conn.execute("SELECT partner, value FROM donations WHERE lot_id=?", (lot[0],)).fetchone()
        assert row and row[1] > 0
        assert svc.conn.execute("SELECT status FROM lots WHERE lot_id=?", (lot[0],)).fetchone()[0] == "depleted"
        assert svc.waste(1)["donated_value"] > 0
    finally:
        svc.close()


# =========================================================================== 10. menu engineering


def test_menu_engineering_and_price_change():
    from restaurant_ops.core import menu_engineering as ME
    from restaurant_ops.core.constraints import blocking, check_action

    svc = make("approve")
    try:
        svc.start()
        eng = svc.menu_engineering()
        classes = {d["class"] for d in eng["dishes"]}
        assert classes <= {"star", "plowhorse", "puzzle", "dog"} and len(classes) >= 2
        assert abs(sum(d["mix_pct"] for d in eng["dishes"]) - 100) < 1.0
        assert isinstance(eng["rotation"], list)
        svc.advance(1)
        for d in decisions(svc, "price_change", "menu"):
            assert d["needs_approval"] and d["status"] == "executed"
            new = svc.conn.execute("SELECT price FROM dishes WHERE id=?", (d["payload"]["dish_id"],)).fetchone()[0]
            assert new == d["payload"]["new_price"]
        svc.flush()
        bad = {"action_type": "price_change", "payload": {"dish_id": "smash_burger", "new_price": 40.0}}
        assert "price_step" in {v.code for v in blocking(check_action(bad, svc.conn, svc.date))}
        rot = ME.rotation("2026-05-20")
        assert any(r["change"] == "entering" for r in rot) or any(r["change"] == "leaving" for r in rot)
    finally:
        svc.close()


# =========================================================================== 11. robustness: Redis failure, stubbed LLM


class _DeadRedis:
    def xadd(self, *a, **k):
        raise ConnectionError("redis down")

    def xread(self, *a, **k):
        raise ConnectionError("redis down")

    def close(self):
        pass


def test_redis_failure_degrades_to_memory(tmp_path):
    from restaurant_ops.db.connection import get_conn, init_db
    from restaurant_ops.live.bus import RedisStreamsBus, make_bus

    c = get_conn(tmp_path / "b.db")
    init_db(c)
    bus = RedisStreamsBus("svc-x", _DeadRedis(), c)
    got = []
    bus.subscribe("x", lambda e: got.append(e.seq))
    bus.publish("x")
    bus.publish("x")
    assert got == [1, 2] and bus.kind == "redis-degraded" and bus.errors
    assert c.execute("SELECT COUNT(*) FROM event_log").fetchone()[0] == 2  # the log is unaffected
    old = settings.REDIS_URL
    try:
        settings.REDIS_URL = "redis://127.0.0.1:1/0"
        assert make_bus("svc-y", None, kind="auto").kind == "memory"
        with pytest.raises(RuntimeError):
            make_bus("svc-z", None, kind="redis")
    finally:
        settings.REDIS_URL = old


def test_redis_dies_mid_service():
    from restaurant_ops.live.bus import redis_client

    if redis_client() is None:
        pytest.skip("no Redis server reachable at REDIS_URL")
    svc = make("approve", bus="redis")
    try:
        svc.run_headless(minutes=30)
        assert svc.bus.kind == "redis"
        real = svc.bus.r
        svc.bus.r = _DeadRedis()
        svc.run_headless(minutes=30)
        assert svc.bus.kind == "redis-degraded" and svc.clock.t >= 60 * 60 - 1
        assert svc.metrics.orders > 0
        real.delete(f"ro:events:{svc.service_id}")
    finally:
        svc.close()


class _FakeStructured:
    def __init__(self, judgment, fail=False, include_raw=False):
        self.j, self.fail, self.raw = judgment, fail, include_raw

    def invoke(self, messages):
        if self.fail:
            raise RuntimeError("model unavailable")
        assert "Candidate actions" in messages[0].content
        return {"parsed": self.j, "raw": None, "parsing_error": None} if self.raw else self.j


class _FakeLLM:
    def __init__(self, judgment=None, fail=False):
        self.j, self.fail = judgment, fail

    def with_structured_output(self, schema, include_raw=False, **kw):
        return _FakeStructured(self.j, self.fail, include_raw)


@pytest.mark.parametrize("fail", [False, True])
def test_llm_review_path_with_stub(monkeypatch, fail):
    from restaurant_ops.agents import llm
    from restaurant_ops.live.agents.base import Judgment

    monkeypatch.setattr(llm, "is_mock", lambda: False)
    monkeypatch.setattr(llm, "get_llm", lambda: _FakeLLM(Judgment(keep=[0], rationale="STUB: salmon is short, 86 it", confidence=0.9), fail))
    svc = make("approve", ["salmon_short@18:00"])
    try:
        svc.run_headless(minutes=80)
        deadline = time.time() + 5
        while svc.pending_llm and time.time() < deadline:
            time.sleep(0.05)
            svc.advance(0.01)
        menu = decisions(svc, agent="menu")
        assert menu
        if fail:
            assert all(d["mode"] == "rule" for d in menu), "a failing model falls back to the rule-based proposals"
        else:
            reviewed = [d for d in menu if d["mode"] == "llm"]
            assert reviewed and reviewed[0]["reason"].startswith("STUB")
            assert all(d["mode"] in ("llm", "rule") for d in decisions(svc))
    finally:
        svc.close()


# =========================================================================== 12. API contract, WS fan-out, metrics, retention, logs


def test_api_contract():
    mgr = bearer('manager')
    srv = bearer('server')
    with _client() as c:
        st = c.get("/api/state").json()
        for key in ("service", "clock", "tables", "kitchen", "inventory", "forecast", "menu", "online", "decisions", "kpis",
                    "host", "checks", "prep", "safety", "drivers", "zones", "dishes", "scenarios"):
            assert key in st, key
        assert c.post("/api/reservations", json={"name": "X", "party_size": 2, "time": "7pm"}, headers=srv).status_code == 422
        b = c.post("/api/reservations", json={"name": "Lee", "party_size": 2, "time": "20:00", "phone": "555-0111"}, headers=srv).json()
        assert b["booking_id"].startswith("B") and b["time"] == "20:00"
        assert c.post(f"/api/reservations/{b['booking_id']}/cancel", headers=srv).json()["status"] == "cancelled"
        assert c.post(f"/api/reservations/{b['booking_id']}/explode", headers=srv).status_code == 404
        assert c.post("/api/waitlist", json={"name": "Wu", "party_size": 2}, headers=srv).status_code == 200
        assert "quote_for" in c.get("/api/waitlist").json()
        assert c.post("/api/online/orders", json={"items": [{"dish_id": "caesar_salad"}], "fulfilment": "delivery"}).status_code == 400
        assert c.get("/api/menu/engineering").json()["dishes"]
        assert "by_reason" in c.get("/api/waste?days=7").json()
        assert len(c.get("/api/suppliers").json()) == 4
        assert c.get("/api/safety").json()["checklist"]
        assert c.get("/api/prep").json()["dishes"]
        g = c.post("/api/guests", json={"name": "Mo", "phone": "555-0155", "allergies": ["nut"]}, headers=srv).json()
        assert g["allergies"] == ["nuts"]
        assert c.patch(f"/api/guests/{g['guest_id']}", json={"allergies": "gluten"}, headers=srv).json()["allergies"] == ["gluten"]
        rep = c.get("/api/report", headers=mgr).json()
        assert {"kpis", "payments", "decisions", "sales_by_dish", "waste"} <= set(rep)
        csv_ = c.get("/api/report.csv", headers=mgr)
        assert csv_.status_code == 200 and csv_.text.startswith("section,key,value")
        m = c.get("/metrics").text
        assert "restaurant_ops_up 1.0" in m and 'restaurant_ops_station_wait_minutes{station="grill"}' in m
        assert c.post("/api/ops/cleanup?dry_run=true", headers=mgr).json()["dry_run"] is True


def test_websocket_fan_out_many_clients():
    from restaurant_ops.live.server import Hub

    import asyncio

    hub = Hub()
    svc = make()
    hub._attach(svc)
    queues = [asyncio.Queue() for _ in range(200)]
    hub.clients.update(queues)
    try:
        svc.start()
        t0 = time.perf_counter()
        svc.run_headless(minutes=90)
        elapsed = time.perf_counter() - t0
        n = svc.bus.seq
        assert all(q.qsize() >= n - 1 for q in queues), "every client got every event"
        assert hub.dropped == 0
        assert elapsed < 60, f"90 service minutes with 200 clients took {elapsed:.1f}s"
    finally:
        svc.close()


def test_websocket_clients_receive_snapshot_and_batches():
    mgr = bearer('manager')
    with _client() as c:
        socks = [c.websocket_connect("/ws") for _ in range(10)]
        conns = [s.__enter__() for s in socks]
        try:
            assert all(w.receive_json()["type"] == "snapshot" for w in conns)
            c.post("/api/scenarios/heatwave", headers=mgr)
            for w in conns:
                msg = w.receive_json()
                assert msg["type"] == "batch" and msg["events"]
        finally:
            for s in socks:
                s.__exit__(None, None, None)


def test_retention_cleanup(tmp_path):
    from restaurant_ops.live import ops

    d = settings.LIVE_DIR
    d.mkdir(parents=True, exist_ok=True)
    old = time.time() - 40 * 86400
    for i in range(5):
        for suffix in (".db", ".state.pkl", ".ckpt.sqlite"):
            f = d / f"svc-old-{i}{suffix}"
            f.write_text("x")
            os.utime(f, (old + i, old + i))
    res = ops.cleanup(retention_days=14, keep_last=2, keep={"svc-old-0"})
    left = sorted(p.name for p in d.glob("svc-old-*.db"))
    assert left == ["svc-old-0.db", "svc-old-3.db", "svc-old-4.db"]
    assert not (d / "svc-old-1.state.pkl").exists() and (d / "svc-old-0.ckpt.sqlite").exists()
    assert len(res["removed"]) == 6


def test_json_logs():
    from restaurant_ops.live.ops import JsonFormatter

    rec = logging.LogRecord("restaurant_ops.live", logging.INFO, __file__, 1, "service started", (), None)
    rec.service_id = "svc-1"
    out = json.loads(JsonFormatter().format(rec))
    assert out["msg"] == "service started" and out["service_id"] == "svc-1" and out["level"] == "INFO"


def test_new_constraint_types():
    from restaurant_ops.core.constraints import blocking, check_action
    from restaurant_ops.db.connection import get_conn

    c = get_conn(settings.DB_PATH)
    try:
        d = settings.SIM_START
        codes = lambda a: {v.code for v in blocking(check_action(a, c, d))}  # noqa: E731
        assert "discount_too_large" in codes({"action_type": "discount", "payload": {"check_id": "C", "subtotal": 100, "pct": 50, "reason": "x"}})
        assert not codes({"action_type": "discount", "payload": {"check_id": "C", "subtotal": 100, "pct": 20, "reason": "x"}})
        assert "refund_exceeds_paid" in codes({"action_type": "refund", "payload": {"check_id": "C", "amount": 50, "paid": 20, "reason": "x"}})
        assert "missing_field" in codes({"action_type": "donate_lots", "payload": {"lots": []}})
        assert "stock_available" in codes({"action_type": "prep_batch", "payload": {"dish_id": "seared_salmon", "portions": 100000}})
    finally:
        c.close()
