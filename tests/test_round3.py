"""Round 3: setup/admin, importers, taxes, gift cards, points, payment processor, PO e-mail/PDF, kitchen printer + KDS,
named users + audit + closed reads, idempotent orders, day close, backups + restore drill, snapshot rotation / migration
/ event-log recovery, the LLM guard + eval set, meal periods and opening hours, and guests waiting in the lobby."""
from __future__ import annotations

import json
import pickle
import sqlite3
from pathlib import Path

import httpx
import pytest

from restaurant_ops import admin as ADMIN
from restaurant_ops.config import settings
from restaurant_ops.db.connection import get_conn, init_db
from restaurant_ops.live import auth as AUTH
from restaurant_ops.live import dayclose as DC
from restaurant_ops.live import giftcards as GC
from restaurant_ops.live import ops as OPS
from restaurant_ops.live import outputs as OUT
from restaurant_ops.live import persistence as PERSIST
from restaurant_ops.live import po_channel as PO
from restaurant_ops.live.payments import PaymentError
from restaurant_ops.live.processor import ProcessorError, SimulatedProcessor, StripeProcessor
from restaurant_ops.live.service import Service, ServiceError
from restaurant_ops.live.run import POLICIES


@pytest.fixture()
def master(tmp_path, monkeypatch, seeded, live_dir):
    """A private copy of the master DB (admin edits, users, gift cards) and scratch output folders."""
    real_db = settings.DB_PATH
    dst = tmp_path / "restaurant.db"
    src = sqlite3.connect(settings.DB_PATH)
    out = sqlite3.connect(dst)
    src.backup(out)
    src.close()
    out.close()
    monkeypatch.setattr(settings, "DB_PATH", dst)
    for k in ("OUTBOX_DIR", "PRINTER_DIR", "BACKUP_DIR", "EXPORT_DIR"):
        monkeypatch.setattr(settings, k, tmp_path / k.lower())
    c = get_conn(dst)
    init_db(c)
    ADMIN.seed_defaults(c)
    yield c
    c.close()
    from restaurant_ops.data import catalog
    from restaurant_ops.live import service as S

    # put the module-level catalog back to the real master's contents for the following tests
    real = get_conn(real_db)
    try:
        S.apply_config(real)
    finally:
        real.close()
    assert catalog.DISHES


def svc_on(master_db, **kw) -> Service:
    return Service(seed=kw.pop("seed", 42), speed=60, bus_kind="memory", auto_policy=POLICIES[kw.pop("policy", "approve")](), **kw)


def seat_and_order(svc, dishes=("caesar_salad", "ribeye_steak"), size=2):
    t = next(tid for tid, tb in svc.floor.tables.items() if tb.shape != "bar" and tb.seats >= size and svc.floor.table_view(tid, svc.clock.t)["state"] == "available")
    r = svc.manual_order(t, [{"dish_id": d} for d in dishes], party_size=size)
    svc.flush()
    party = svc.floor.tables[t].party_id
    check = svc.payments.open_for_party(svc.floor.parties[party])
    return t, r, party, check


# =========================================================================== 1. admin + onboarding


def test_admin_validation_and_crud(master):
    with pytest.raises(ADMIN.AdminError):
        ADMIN.save_tax(master, {"name": "Bad", "rate": 0.9})
    t = ADMIN.save_tax(master, {"name": "Tourism levy", "jurisdiction": "county", "rate": 0.01, "applies_to": "food", "channel": "dine_in"})
    assert t["rate"] == 0.01 and any(x["name"] == "Tourism levy" for x in ADMIN.list_taxes(master))
    with pytest.raises(ADMIN.AdminError):
        ADMIN.save_table(master, {"id": "T99", "seats": 4, "x": 5000, "y": 10})
    ADMIN.save_table(master, {"id": "T29", "seats": 4, "shape": "rect", "x": 120, "y": 590, "w": 80, "h": 40, "section": "C"})
    with pytest.raises(ADMIN.AdminError, match="active dish"):
        ADMIN.delete_station(master, "grill")
    with pytest.raises(ADMIN.AdminError, match="used by"):
        ADMIN.delete_ingredient(master, "salmon")
    with pytest.raises(ADMIN.AdminError, match="below the recipe"):
        ADMIN.save_dish(master, {"id": "cheap_steak", "name": "Cheap steak", "category": "main", "price": 1, "station": "grill",
                                 "prep_minutes": 10, "recipe": [{"ingredient_id": "ribeye", "qty_per_portion": 0.3}]})
    with pytest.raises(ADMIN.AdminError, match="email"):
        ADMIN.save_supplier(master, {"id": "sup_x", "name": "X", "email": "not-an-email"})
    p = ADMIN.set_profile(master, {"name": "Chez Test", "discount_max_pct": {"manager": 25, "server": 5}})
    assert p["name"] == "Chez Test" and ADMIN.get_profile(master)["discount_max_pct"]["server"] == 5
    snap = ADMIN.snapshot(master)
    assert {"profile", "taxes", "hours", "tables", "stations", "zones", "ingredients", "dishes", "suppliers", "staff", "choices"} <= set(snap)
    assert snap["last_change"]


def test_new_dish_table_and_roster_apply_to_the_next_service(master):
    ADMIN.save_dish(master, {"id": "veg_burger", "name": "Veg burger", "category": "main", "price": 18, "station": "grill",
                             "prep_minutes": 9, "recipe": [{"ingredient_id": "brioche_bun", "qty_per_portion": 1}]}
                    ) if master.execute("SELECT 1 FROM ingredients WHERE id='brioche_bun'").fetchone() else None
    ing = master.execute("SELECT id FROM ingredients ORDER BY id LIMIT 1").fetchone()[0]
    ADMIN.save_dish(master, {"id": "chef_special", "name": "Chef special", "category": "main", "price": 99, "station": "saute",
                             "prep_minutes": 12, "recipe": [{"ingredient_id": ing, "qty_per_portion": 0.1}]})
    ADMIN.save_table(master, {"id": "T29", "seats": 4, "shape": "rect", "x": 120, "y": 590, "w": 80, "h": 40, "section": "C"})
    ADMIN.retire_dish(master, "caesar_salad")
    svc = svc_on(master)
    try:
        assert "chef_special" in svc.dishes_view() if hasattr(svc, "dishes_view") else "chef_special" in svc.snapshot()["dishes"]
        assert "caesar_salad" not in svc.snapshot()["dishes"]
        assert "T29" in svc.floor.tables and len(svc.floor.tables) == 29
        svc.start()
        r = svc.manual_order("T29", [{"dish_id": "chef_special"}], party_size=2)
        assert r["items"][0]["station"] == "saute"
        on_menu = {d["dish_id"] for d in svc.online_menu()["dishes"]}
        assert "chef_special" in on_menu and "caesar_salad" not in on_menu
    finally:
        svc.close()


def test_roster_drives_station_cooks(master):
    staff = ADMIN.list_staff(master)
    grill = [s for s in staff if s["role"] == "cook" and s["station"] == "grill"]
    assert grill
    r = ADMIN.set_roster(master, "2026-11-20", "dinner", [{"staff_id": grill[0]["id"]}])
    assert r["cooks_per_station"] == {"grill": 1}
    assert master.execute("SELECT staff FROM staff_shifts WHERE date='2026-11-20' AND meal_period='dinner' AND station='grill'").fetchone()[0] == 1
    assert [x["staff_id"] for x in ADMIN.get_roster(master, "2026-11-20", "dinner")] == [grill[0]["id"]]


def test_menu_and_inventory_import(master, tmp_path):
    from restaurant_ops.data import importer as IMP

    ing = master.execute("SELECT id, name FROM ingredients ORDER BY id LIMIT 2").fetchall()
    menu = tmp_path / "menu.csv"
    menu.write_text("id,name,category,price,station,prep_minutes,recipe,tax_category\n"
                    f"house_bowl,House bowl,main,21,cold,6,{ing[0][0]}:0.1;{ing[1][0]}:0.1,food\n"
                    "bad_row,Bad,main,20,nowhere,5,x:1,food\n")
    dry = IMP.import_menu(menu, settings.DB_PATH, dry_run=True)
    assert dry["imported"] == ["house_bowl"] and len(dry["errors"]) == 1 and dry["dry_run"]
    assert master.execute("SELECT 1 FROM dishes WHERE id='house_bowl'").fetchone() is None   # dry run wrote nothing
    real = IMP.import_menu(menu, settings.DB_PATH)
    assert real["imported"] == ["house_bowl"]
    master.commit()
    assert get_conn(settings.DB_PATH).execute("SELECT price FROM dishes WHERE id='house_bowl'").fetchone()[0] == 21
    inv = tmp_path / "inv.csv"
    inv.write_text(f"ingredient,qty,unit_cost\n{ing[0][1]},5,3.5\nunknown_thing,2,1\n")
    r = IMP.import_inventory(inv, settings.DB_PATH, as_of="2026-11-01")
    assert r["lots"] == 1 and len(r["errors"]) == 1


# =========================================================================== 2. money: taxes, gift cards, points, processor


def test_multi_jurisdiction_tax_and_role_discount_caps(master):
    ADMIN.set_profile(master, {"discount_max_pct": {"manager": 30, "server": 10}})
    svc = svc_on(master)
    try:
        svc.start()
        _, _, _, c = seat_and_order(svc)
        t = svc.payments.totals(c)
        names = {x["name"] for x in t["tax_lines"]}
        assert {"State sales tax", "City sales tax"} <= names
        assert abs(sum(x["amount"] for x in t["tax_lines"]) - t["tax"]) < 0.011
        with pytest.raises((PaymentError, ValueError, ServiceError)):
            svc.payments.discount(c.check_id, "loyal guest", pct=20, by="srv", role="server")
        svc.payments.discount(c.check_id, "loyal guest", pct=20, by="mgr", role="manager")
        assert svc.payments.totals(c)["discounts"] > 0
    finally:
        svc.close()


def test_gift_card_and_points_payments(master):
    g = GC.issue(master, 40, "test")
    assert GC.get(master, g["code"])["balance"] == 40
    with pytest.raises(GC.GiftCardError):
        GC.redeem(master, g["code"], 100)
    svc = svc_on(master)
    try:
        svc.start()
        _, _, _, c = seat_and_order(svc)
        due = svc.payments.totals(c)["due"]
        svc.payments.pay(c.check_id, 25, method="gift_card", gift_code=g["code"])
        with svc.master() as m:
            assert GC.get(m, g["code"])["balance"] == 15
            led = GC.ledger(m, g["code"])
            assert round(sum(x["amount"] for x in led), 2) == 15        # issue +40, redeem -25
        svc.payments.pay(c.check_id, round(due - 25, 2), method="card")
        assert c.status == "paid"
        assert any((p.get("ref") or "").startswith("sim_ch_") for p in c.payments)
        r = svc.payments.refund(c.check_id, 5, "cold food", by="mgr")
        assert r
    finally:
        svc.close()


def test_simulated_and_stripe_processors():
    sim = SimulatedProcessor()
    assert sim.charge(10)["status"] == "succeeded"
    with pytest.raises(ProcessorError):
        sim.charge(10, token="tok_decline")
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.url.path, req.headers.get("Idempotency-Key"), req.content.decode()))
        if req.url.path.endswith("/payment_intents"):
            return httpx.Response(200, json={"id": "pi_123", "status": "succeeded", "amount": 1234})
        return httpx.Response(200, json={"id": "re_1", "status": "succeeded"})

    sp = StripeProcessor("sk_test_x", client=httpx.Client(transport=httpx.MockTransport(handler)))
    ch = sp.charge(12.34, token="pm_card_visa")
    assert ch == {"id": "pi_123", "status": "succeeded", "amount": 12.34}
    assert "amount=1234" in seen[0][2] and seen[0][1]
    assert sp.refund("pi_123", 2)["status"] == "succeeded"

    def declined(req):
        return httpx.Response(402, json={"error": {"message": "Your card was declined."}})

    with pytest.raises(ProcessorError, match="declined"):
        StripeProcessor("sk", client=httpx.Client(transport=httpx.MockTransport(declined))).charge(5)


# =========================================================================== 2b. supplier channel, printer, KDS


def test_po_pdf_email_and_outbox(master, monkeypatch):
    svc = svc_on(master, policy="approve")
    try:
        svc.run_headless(until_end=False, minutes=120)
        svc.flush()
        po = svc.conn.execute("SELECT po_id, supplier_id FROM purchase_orders ORDER BY rowid DESC LIMIT 1").fetchone()
        if po is None:
            pytest.skip("no purchase order in the first two hours")
        pdf = PO.text_pdf(["Line one", "Line (two)"], "PO")
        assert pdf.startswith(b"%PDF-1.4") and pdf.rstrip().endswith(b"%%EOF") and b"Line \\(two\\)" in pdf
        row = PO.send_po(svc.conn, po[0], svc.profile)
        assert Path(row["pdf_path"]).exists() and Path(row["eml_path"]).exists() and row["status"] in ("queued", "sent")
        sent = []

        class FakeSMTP:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def starttls(self): pass
            def login(self, *a): pass
            def send_message(self, msg): sent.append(msg)

        svc.conn.execute("UPDATE suppliers SET email='orders@supplier.test' WHERE id=?", (po[1],))
        monkeypatch.setattr(settings, "SMTP_HOST", "smtp.test")
        row = PO.send_po(svc.conn, po[0], svc.profile, smtp_factory=FakeSMTP)
        assert row["status"] == "sent" and sent and sent[0]["To"] == "orders@supplier.test"
        assert any(p.get_content_type() == "application/pdf" for p in sent[0].iter_attachments())
    finally:
        svc.close()


def test_kitchen_chits_and_kds_bump(master):
    svc = svc_on(master)
    try:
        svc.start()
        t, r, _, _ = seat_and_order(svc, ("caesar_salad", "ribeye_steak"))
        chit_dir = Path(settings.PRINTER_DIR) / svc.service_id
        files = {p.stem: p.read_text() for p in chit_dir.glob("*.txt")}
        assert {"cold", "grill"} <= set(files) and r["order_id"] in files["grill"]
        tk = OUT.tickets(svc)
        mine = [x for x in tk if x["order_id"] == r["order_id"]]
        assert {x["station"] for x in mine} == {"cold", "grill"}
        assert OUT.bump(svc, r["order_id"], "grill", "chef") == 1
        assert all(x["station"] != "grill" for x in OUT.tickets(svc) if x["order_id"] == r["order_id"])
        assert any(x["station"] == "grill" for x in OUT.tickets(svc, include_bumped=True) if x["order_id"] == r["order_id"])
    finally:
        svc.close()


# =========================================================================== 3. identity, audit, closed reads, idempotency


def test_users_passwords_lockout_and_last_admin(master, monkeypatch):
    with pytest.raises(AUTH.AuthError):
        AUTH.create_user(master, "x", "short", "server")
    u = AUTH.create_user(master, "alice", "correct-horse", "server", "Alice")
    assert AUTH.verify_password("correct-horse", master.execute("SELECT pw_hash FROM users WHERE id=?", (u["id"],)).fetchone()[0])
    tok, user = AUTH.login(master, "alice", "correct-horse")
    assert AUTH.resolve(master, tok)["username"] == "alice"
    assert master.execute("SELECT COUNT(*) FROM sessions WHERE token_hash=?", (tok,)).fetchone()[0] == 0   # only the hash is stored
    AUTH.update_user(master, u["id"], password="another-pass")
    assert AUTH.resolve(master, tok) is None                                  # password change signs out
    monkeypatch.setattr(settings, "LOGIN_MAX_FAILURES", 3)
    for _ in range(3):
        with pytest.raises(AUTH.AuthError):
            AUTH.login(master, "alice", "wrong")
    with pytest.raises(AUTH.AuthError, match="locked"):
        AUTH.login(master, "alice", "another-pass")
    admins = [x for x in AUTH.list_users(master) if x["role"] == "admin" and x["active"]]
    if len(admins) == 1:
        with pytest.raises(AUTH.AuthError):
            AUTH.update_user(master, admins[0]["id"], role="server")
    assert AUTH.allowed("manager", ()) and not AUTH.allowed("server", ()) and AUTH.allowed("kitchen", ("kitchen",))
    assert AUTH.allowed("admin", ("admin",)) and not AUTH.allowed("manager", ("admin",))


def test_api_auth_csrf_audit_idempotency_and_public_ws(master, monkeypatch):
    from fastapi.testclient import TestClient

    from restaurant_ops.live import server

    monkeypatch.setattr(settings, "RESUME_ON_START", False)
    with TestClient(server.app) as c:
        assert c.get("/api/state").status_code == 401
        assert c.post("/api/auth/login", json={"username": "manager", "password": "nope"}).status_code == 401
        r = c.post("/api/auth/login", json={"username": "server", "password": "server-demo"})
        assert r.status_code == 200 and "ro_session" in r.cookies
        assert "httponly" in r.headers["set-cookie"].lower() and "samesite=strict" in r.headers["set-cookie"].lower()
        assert c.get("/api/state").status_code == 200                       # cookie session
        assert c.post("/api/waitlist", json={"name": "Ng", "party_size": 2}).status_code == 403   # cookie write without CSRF header
        hdr = {"X-Requested-With": "ro-ui"}
        assert c.post("/api/waitlist", json={"name": "Ng", "party_size": 2}, headers=hdr).status_code == 200
        st = c.get("/api/state").json()
        free = next(t for t, v in st["tables"].items() if v["state"] == "available" and v["seats"] >= 2)
        body = {"table_id": free, "items": [{"dish_id": "ribeye_steak"}], "party_size": 2}
        k = {**hdr, "Idempotency-Key": "offline-123"}
        a = c.post("/api/orders", json=body, headers=k).json()
        b = c.post("/api/orders", json=body, headers=k).json()
        assert a["order_id"] == b["order_id"] and b.get("replayed") is True
        with c.websocket_connect("/ws") as ws:
            assert ws.receive_json()["type"] == "snapshot"
        c.post("/api/auth/logout", headers=hdr)
        c.cookies.clear()
        assert c.get("/api/state").status_code == 401
        with c.websocket_connect("/ws") as ws:
            assert ws.receive_json()["type"] == "error"
        with c.websocket_connect("/ws/public") as ws:
            snap = ws.receive_json()["payload"]
            assert snap["public"] and snap["tables"] == {} and snap["decisions"] == {} and "menu" in snap
        o = c.post("/api/online/orders", json={"items": [{"dish_id": "caesar_salad"}], "customer": "Secret Name"}).json()
        assert "customer" not in o["online"]
        assert c.get(f"/api/report.html?token={c.cookies.get('ro_session') or 'x'}").status_code == 401
        mgr = {"Authorization": f"Bearer {c.post('/api/auth/login', json={'username': 'manager', 'password': 'manager-demo'}).json()['token']}"}
        c.cookies.clear()
        rows = c.get("/api/admin/audit", headers=mgr).json()
        paths = {(x["username"], x["path"], x["status"]) for x in rows}
        assert ("server", "/api/orders", 200) in paths and ("server", "/api/waitlist", 403) in paths
        assert any(x["path"] == "/api/auth/login" and x["status"] == 401 for x in rows)
        assert not any(secret in (x["detail"] or "") for x in rows for secret in ("server-demo", "manager-demo", "nope"))
        assert c.get("/api/admin/config", headers=mgr).status_code == 403


# =========================================================================== 4. operations: day close, backups, recovery, periods


def test_day_close_balanced_journal(master):
    svc = svc_on(master, policy="approve")
    try:
        svc.run_headless(until_end=False, minutes=150)
        with pytest.raises(DC.DayCloseError):
            DC.close_day(svc, 0, "mgr")                                      # still running
        rec = DC.reconcile(svc, counted_cash=None)
        rep = DC.close_day(svc, rec["expected_cash"] - 5, "mgr", force=True)
        assert rep["totals"]["balanced"] and rep["totals"]["debits"] > 0
        assert rep["reconciliation"]["cash_over_short"] == -5.0
        assert Path(rep["files"]["journal_csv"]).exists()
        with svc.master() as m:
            h = DC.history(m)
        assert h[0]["service_id"] == svc.service_id and h[0]["balanced"]
    finally:
        svc.close()


def test_backup_verify_restore_drill(master, tmp_path):
    svc = svc_on(master)
    try:
        svc.start()
        svc.run_headless(until_end=False, minutes=10)
        svc.save_state()
        b = OPS.backup("test")
        assert OPS.verify_backup(b["backup_id"])["ok"]
        r = OPS.restore(b["backup_id"], data_dir=tmp_path / "rd", live_dir=tmp_path / "rl")
        assert (tmp_path / "rd" / "restaurant.db").exists() and r["restored_files"] >= 2
        d = OPS.drill()
        assert d["ok"] and d["snapshot"]
        # a corrupted backup file fails verification
        bad = next(p for p in (Path(settings.BACKUP_DIR) / b["backup_id"]).rglob("*.db"))
        bad.write_bytes(b"garbage")
        assert not OPS.verify_backup(b["backup_id"])["ok"]
    finally:
        svc.close()


def test_snapshot_rotation_corruption_and_v1_migration(master):
    svc = svc_on(master)
    sid = svc.service_id
    try:
        svc.start()
        svc.run_headless(until_end=False, minutes=5)
        for _ in range(3):
            svc.save_state()
            svc.run_headless(until_end=False, minutes=2)
        t_saved = svc.clock.t
        svc.save_state()
    finally:
        svc.close()
    assert PERSIST.state_path(sid, 0).exists() and PERSIST.state_path(sid, 1).exists()
    assert not PERSIST.state_path(sid, settings.SNAPSHOT_KEEP).exists()
    PERSIST.state_path(sid, 0).write_bytes(b"corrupt")                         # newest is damaged -> older one loads
    blob = PERSIST.load(sid)
    assert blob["world"]["clock"].t < t_saved
    old = pickle.loads(PERSIST.state_path(sid, 1).read_bytes())
    old["version"] = 1
    for k in ("profile", "zones", "tax_rules", "tax_category", "master_path", "recovered"):
        old["world"].pop(k, None)
    m = PERSIST.migrate(old)
    assert m["version"] == PERSIST.VERSION and m["world"]["profile"]["discount_max_pct"]


def test_recover_from_event_log_keeps_pending_approvals(master):
    svc = Service(seed=42, speed=60, bus_kind="memory")                        # no auto policy: approvals stay pending
    sid = svc.service_id
    try:
        svc.run_headless(until_end=False, minutes=90)
        pending = {d["id"] for d in svc.coordinator.decisions.values() if d["status"] == "pending"}
        hhmm = svc.clock.hhmm()
        svc.flush()
    finally:
        svc.close()
    for p in Path(settings.LIVE_DIR).glob(f"{sid}.state*"):
        p.unlink()
    r = Service.recover_from_log(sid, bus_kind="memory")
    try:
        assert r.recovered and r.clock.hhmm() >= hhmm[:4]
        assert {d["id"] for d in r.coordinator.decisions.values() if d["status"] == "pending"} == pending
        r.run_headless(until_end=False, minutes=5)                              # and it keeps running
    finally:
        r.close()


def test_lunch_service_and_closed_hours(master):
    svc = svc_on(master, period="lunch")
    try:
        assert svc.period == "lunch" and svc.clock.hhmm() == "11:30"
    finally:
        svc.close()
    wd = svc.date.weekday()
    ADMIN.set_hours(master, wd, "dinner", "17:00", "22:30", closed=True)
    with pytest.raises(ServiceError, match="closed"):
        svc_on(master, period="dinner", service_date=svc.date.isoformat())
    with pytest.raises(ServiceError):
        svc_on(master, period="brunch")


def test_guests_wait_in_the_lobby_before_being_seated(master):
    svc = svc_on(master)
    try:
        seen_reasons, max_wait = set(), 0
        for _ in range(150):
            svc.run_headless(until_end=False, minutes=1)
            wl = svc.host.waitlist_view()
            max_wait = max(max_wait, len(wl))
            seen_reasons |= {w["reason"] for w in wl}
        assert max_wait >= 1 and "checking in" in seen_reasons
        assert svc.metrics.covers_seated > 0
        snap = svc.snapshot()
        assert snap["layout"]["height"] == 700 and snap["layout"]["fixtures"]["lobby"]["kind"] == "zone"
    finally:
        svc.close()


# =========================================================================== 5. LLM pilot: guard, fallback, eval


class _Fut:
    def __init__(self, value=None, exc=None):
        self.value, self.exc = value, exc

    def done(self):
        return True

    def result(self, timeout=None):
        if self.exc:
            raise self.exc
        return self.value


def test_llm_guard_caps_breaker_and_rule_fallback(master, monkeypatch):
    from restaurant_ops.live.agents.base import Judgment

    svc = svc_on(master)
    try:
        g = svc.llm_guard
        assert g.allow() == (False, "mock mode")
        monkeypatch.setattr(settings, "LLM_MODE", "live")
        assert g.allow()[0]
        agent = svc.agents["menu"] if hasattr(svc, "agents") and isinstance(svc.agents, dict) else next(
            a for a in getattr(svc, "agents", []) if getattr(a, "name", "") == "menu")
        props = [agent.proposal("price_change", {"dish_id": "caesar_salad", "new_price": 15}, "Price up", "margin", [])]
        out = agent.finish_review([dict(p) for p in props], _Fut({"judgment": Judgment(keep=[0], rationale="ok"), "latency_ms": 900,
                                                                  "tokens_in": 1000, "tokens_out": 200}), 0.0)
        assert out and g.calls == 1 and g.cost > 0
        for _ in range(settings.LLM_BREAKER_FAILURES):
            agent.finish_review([dict(p) for p in props], _Fut(exc=RuntimeError("boom")), 0.0)
        ok, why = g.allow()
        assert not ok and "circuit breaker" in why
        props2 = [agent.proposal("price_change", {"dish_id": "caesar_salad", "new_price": 16}, "Price up again", "margin", [])]
        res = agent.submit(props2, {})
        assert res and all("LLM review skipped" in d["reason"] for d in res)
        svc.flush()
        n = svc.conn.execute("SELECT COUNT(*), SUM(ok) FROM llm_calls WHERE service_id=?", (svc.service_id,)).fetchone()
        assert n[0] == 1 + settings.LLM_BREAKER_FAILURES and n[1] == 1
        g2 = type(g)(svc)
        monkeypatch.setattr(settings, "LLM_MAX_COST_PER_SERVICE", 0.0)
        assert "cost cap" in g2.allow()[1]
    finally:
        svc.close()


def test_llm_eval_set_from_logged_services(tmp_path, master):
    from restaurant_ops.live import llm_eval
    from restaurant_ops.live.agents.base import Judgment

    svc = svc_on(master, policy="demo")
    try:
        svc.run_headless()
    finally:
        svc.close()
    out = tmp_path / "eval.jsonl"
    b = llm_eval.build(out, settings.LIVE_DIR)
    if b["cases"] == 0:
        pytest.skip("this seed produced no manager-decided reviewed proposals")
    base = llm_eval.run(out, judge="mock")
    assert base["agreement_pct"] == round(100 * b["keep"] / b["cases"], 1)
    drop_all = llm_eval.run(out, judge="live", reviewer=lambda a, s, p: {"judgment": Judgment(keep=[], rationale="no"), "latency_ms": 5,
                                                                          "tokens_in": 10, "tokens_out": 5})
    assert drop_all["rejections_caught"] == f"{b['drop']}/{b['drop']}" and drop_all["false_drops"] == b["keep"]
    assert drop_all["p50_ms"] == 5 and drop_all["cost"] > 0
