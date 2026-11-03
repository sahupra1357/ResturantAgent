"""End-to-end tests of the live service on the seeded world: a full dinner, each disruption scenario, approvals with
interrupts and timeouts, the allergen guard, latency, determinism and replay from the event log."""
from __future__ import annotations

import pytest

from restaurant_ops.live import events as E
from restaurant_ops.live.replay import fold, load_events
from restaurant_ops.live.run import POLICIES, parse_schedule
from restaurant_ops.live.service import Service

pytestmark = pytest.mark.usefixtures("seeded", "live_dir")


def make(policy="demo", scenarios=(), seed=42, bus="memory") -> Service:
    pol = POLICIES[policy]()
    svc = Service(seed=seed, speed=60, bus_kind=bus, auto_policy=pol)
    svc.schedule = parse_schedule(list(scenarios), svc)
    return svc


def decisions(svc, action_type=None, agent=None):
    return [d for d in svc.coordinator.decisions.values()
            if (action_type is None or d["action_type"] == action_type) and (agent is None or d["agent"] == agent)]


def events_of(svc, type_):
    return [e for e in load_events(svc.service_id) if e["type"] == type_]


@pytest.fixture()
def quiet_night():
    svc = make("approve")
    svc.run_headless()
    yield svc
    svc.close()


def test_full_service_runs_and_meets_latency_targets(quiet_night):
    svc = quiet_night
    k = svc.metrics.kpis(svc.forecast)
    assert svc.status == "ended"
    assert not svc.bus.errors, svc.bus.errors[:3]
    assert k["covers"] > 40 and k["orders"] > 20 and k["online_orders"] > 5
    assert k["revenue"] > 1000
    assert k["order_latency_max_ms"] < 2000          # order -> stations: plan target < 2 s
    assert k["depletion_p95_ms"] < 500               # inventory hot path: plan target < 500 ms
    assert k["quotes_closed"] > 0 and k["quote_accuracy_pct"] is not None
    assert k["ticket_avg_min"] and 5 < k["ticket_avg_min"] < 40
    assert 15 < k["food_cost_pct"] < 50
    # every item that fired was depleted, and every table cycled through the state machine
    assert len(events_of(svc, E.ITEM_FIRED)) == len(svc.metrics.depletion_ms)
    states = {t["state"] for e in events_of(svc, E.TABLE_UPDATED) for t in e["payload"]["tables"]}
    assert {"seated", "ordered", "entrees_fired", "eating", "check", "bussing", "available"} <= states


def test_replay_rebuilds_the_final_state(quiet_night):
    svc = quiet_night
    st = fold(load_events(svc.service_id))
    live = svc.snapshot()
    assert st["service"]["status"] == "ended"
    assert {k: v["status"] for k, v in st["decisions"].items()} == {k: v["status"] for k, v in live["decisions"].items()}
    assert {k: v["state"] for k, v in st["tables"].items()} == {k: v["state"] for k, v in live["tables"].items()}
    assert st["menu"] == live["menu"]
    assert {k: v["status"] for k, v in st["online"]["orders"].items()} == {k: v["status"] for k, v in live["online"]["orders"].items()}
    for key in ("covers", "revenue", "orders", "eighty_six", "waste_value"):
        assert st["kpis"][key] == live["kpis"][key]


def test_seeded_runs_are_reproducible():
    a, b = make("approve"), make("approve")
    try:
        ka, kb = a.run_headless(minutes=150), b.run_headless(minutes=150)
        drop = lambda k: {x: v for x, v in k.items() if "latency" not in x and "_ms" not in x}  # noqa: E731
        assert drop(ka) == drop(kb)
        assert [d["title"] for d in decisions(a)] == [d["title"] for d in decisions(b)]
    finally:
        a.close()
        b.close()


def test_salmon_short_end_to_end():
    svc = make("demo", ["salmon_short@18:45"])
    try:
        svc.run_headless()
        assert not svc.bus.errors, svc.bus.errors[:3]
        low = [e for e in events_of(svc, E.STOCK_LOW) if e["payload"]["ingredient_id"] == "salmon"]
        assert low, "Inventory flags salmon low"
        e86 = [d for d in decisions(svc, "86_dish") if d["payload"]["dish_id"] == "seared_salmon"]
        assert e86 and e86[0]["needs_approval"] and e86[0]["status"] == "executed"
        assert e86[0]["payload"]["after_portions"] >= 0
        assert "seared_salmon" in svc.menu.eighty_sixed or "seared_salmon" in svc.menu.limits
        specials = decisions(svc, "add_special", "menu")
        assert specials, "Menu proposes a special to replace the salmon"
        short_at = events_of(svc, E.SUPPLIER_SHORTFALL)[0]["sim_ts"]
        assert all("salmon" not in d["payload"]["dish_id"] for d in specials
                   if d["status"] == "executed" and d["created_sim"] >= short_at), "no salmon special once salmon is short"
        assert "seared_salmon" not in svc.menu.specials, "the 86 ends any salmon special"
        pos = [d for d in decisions(svc, "place_po", "procurement") if (d.get("context") or {}).get("ingredient_id") == "salmon"]
        assert pos, "Procurement drafts a fallback PO"
        short_pos = [d for d in pos if "short-shipping" in d["reason"]]
        assert short_pos and all(any(t["tool"] == "observed_shortfall" and t["result"]["excluded_from_quotes"] for t in d["trace"])
                                 for d in short_pos), "the shortfall that rules out the usual supplier is in the PO's evidence"
        first = pos[0]
        assert first["payload"]["supplier_id"] != "sup_freshcatch"
        assert first["needs_approval"] and first["status"] == "rejected" and "budget" in first["note"].lower()
        replans = [d for d in pos[1:] if (d.get("context") or {}).get("replan")]
        logs = [e for e in events_of(svc, E.AGENT_LOG) if e["payload"].get("agent") == "procurement"
                and e["payload"].get("ingredient_id") == "salmon"]
        assert replans or logs, "Procurement re-plans after the rejection"
        if replans:
            assert replans[0]["payload"]["total"] <= first["payload"]["total"]
        assert svc.metrics.stockouts == 0, "the 86 lands before a guest orders salmon with no stock"
    finally:
        svc.close()


def test_party_of_12_throttles_online():
    svc = make("demo", ["party_of_12@19:00"])
    try:
        svc.run_headless(minutes=210)
        fires = [e for e in events_of(svc, E.ITEM_FIRED) if e["payload"].get("large_party")]
        assert len(fires) >= 10
        grill = max(r["wait_min"] for e in events_of(svc, E.KITCHEN_LOAD) for r in e["payload"]["stations"]
                    if r["station"] == "grill")
        assert grill >= 10, "grill wait jumps"
        thr = decisions(svc, "throttle_online", "order")
        asked = [d for d in thr if d["payload"]["mode"] != "open"]
        assert asked and asked[0]["needs_approval"] and asked[0]["status"] == "executed"
        assert "grill" in asked[0]["title"]
        modes = [e["payload"]["mode"] for e in events_of(svc, E.ONLINE_MODE)]
        assert modes and modes[0] in ("quick_only", "paused")
        slipped = [e for e in events_of(svc, E.QUOTE_UPDATED) if e["payload"].get("notify")]
        assert slipped, "online quotes stretch and customers are notified"
    finally:
        svc.close()


def test_cooler_failure_discards_and_86s():
    svc = make("demo", ["cooler_failure@18:30"])
    try:
        svc.run_headless(minutes=150)
        disc = decisions(svc, "discard_lots", "inventory")
        assert disc and disc[0]["needs_approval"] and disc[0]["status"] == "executed"
        assert svc.metrics.waste_value > 0
        assert not svc.stock.quarantine
        e86 = [d for d in decisions(svc, "86_dish", "menu") if d["status"] == "executed"]
        assert e86, "Menu removes dishes that lost their dairy"
    finally:
        svc.close()


def test_forecast_miss_revises_and_rechecks_tomorrow():
    svc = make("approve", ["forecast_miss@17:00"])
    try:
        svc.run_headless(minutes=200)
        miss = events_of(svc, E.FORECAST_MISS)
        assert miss and miss[0]["payload"]["miss_pct"] >= 15
        adj = decisions(svc, "adjust_forecast", "forecasting")
        assert adj and adj[0]["status"] == "executed" and adj[0]["payload"]["factor"] > 1
        rechecks = [d for d in decisions(svc, "place_po", "procurement") if d["key"].startswith("recheck:")]
        logs = [e for e in events_of(svc, E.AGENT_LOG) if e["payload"].get("agent") == "procurement" and "re-check" in e["payload"]["message"]]
        assert rechecks or logs, "Procurement re-checks tomorrow's order"
    finally:
        svc.close()


def test_heatwave_updates_forecast():
    svc = make("approve", ["heatwave@18:00"])
    try:
        svc.run_headless(minutes=120)
        ups = [e for e in events_of(svc, E.FORECAST_UPDATED) if "weather" in (e["payload"].get("reason") or "")]
        assert ups and ups[0]["payload"]["recheck"]
    finally:
        svc.close()


def test_timeouts_fall_back_to_safe_defaults():
    svc = make("timeout", ["salmon_short@18:45"])
    try:
        svc.run_headless(minutes=260)
        timed = [d for d in decisions(svc) if d["status"] == "timeout" or d.get("resolved_by") == "timeout"]
        assert timed
        for d in timed:
            if d["action_type"] in ("place_po", "add_special"):
                assert d["status"] == "timeout" and not d.get("result"), "no PO sent, no special published"
        with svc.conn:
            n = svc.conn.execute("SELECT COUNT(*) FROM purchase_orders WHERE created_date=? AND supplier_id<>'sup_freshcatch' "
                                 "AND decision_id IS NOT NULL", (svc.date.isoformat(),)).fetchone()[0]
        approved_pos = [d for d in decisions(svc, "place_po") if d["status"] == "executed"]
        assert n == len(approved_pos)
    finally:
        svc.close()


def test_allergen_conflict_goes_to_manager_and_guard_blocks_unsafe_swap():
    svc = make("timeout")
    try:
        svc.start()
        svc.advance(30)  # 30 sim minutes at 60x
        free = next(t for t, v in svc.floor.tables.items() if v.state == "available" and v.seats >= 2)
        res = svc.manual_order(free, [{"dish_id": "carbonara", "mods": "egg allergy"}, {"dish_id": "seared_salmon",
                                                                                       "mods": "no dairy, nut allergy"}], party_size=2)
        assert res["latency_ms"] < 2000
        salmon = next(i for i in res["items"] if i["dish_id"] == "seared_salmon")
        assert salmon["allergy_resolved"] and any("no" in m.lower() for m in salmon["mods"])
        assert res["held"] and res["held"][0]["dish_id"] == "carbonara"
        swap = next(d for d in decisions(svc, "allergen_swap") if d["status"] == "pending")
        assert swap["payload"]["to_dish"]
        # the manager edits the swap to a dish with eggs: the constraint checker blocks it
        out = svc.coordinator.resolve(swap["id"], True, "try milanese", {"to_dish": "chicken_milanese"})
        assert out["status"] == "blocked"
        assert any(v["code"] == "allergen_conflict" for v in out["violations"])
        order = svc.orders[res["order_id"]]
        assert not any(i["dish_id"] == "chicken_milanese" for i in order["items"])
        assert any(r["reason"].startswith("allergen") for r in order["rejected"])
    finally:
        svc.close()


def test_manager_approves_safe_swap():
    svc = make("timeout")
    try:
        svc.start()
        svc.advance(20)
        free = next(t for t, v in svc.floor.tables.items() if v.state == "available" and v.seats >= 2)
        res = svc.manual_order(free, [{"dish_id": "carbonara", "mods": "egg allergy"}], party_size=1)
        swap = next(d for d in decisions(svc, "allergen_swap") if d["status"] == "pending")
        out = svc.coordinator.resolve(swap["id"], True, "ok")
        assert out["status"] == "executed"
        order = svc.orders[res["order_id"]]
        assert any(i["dish_id"] == swap["payload"]["to_dish"] for i in order["items"])
    finally:
        svc.close()


def test_online_quote_and_order():
    svc = make("approve")
    try:
        svc.start()
        svc.advance(60)
        q = svc.quote_preview(["ribeye_steak", "parmesan_fries"])
        assert q["p90_min"] >= q["p50_min"] > 0 and len(q["range_hhmm"]) == 2
        r = svc.place_online([{"dish_id": "ribeye_steak"}], customer="Test")
        o = r["online"]
        assert o["status"] == "cooking" and o["quoted_hhmm"][0] <= o["quoted_hhmm"][1]
        svc.set_online_mode("paused")
        with pytest.raises(Exception):
            svc.place_online([{"dish_id": "ribeye_steak"}])
        v = svc.override_quote(r["order_id"], "21:15")
        assert v["override"] and v["current_hhmm"][0] == "21:15"
    finally:
        svc.close()


def test_redis_bus_service_if_available():
    from restaurant_ops.live.bus import redis_client

    if redis_client() is None:
        pytest.skip("no Redis server reachable at REDIS_URL")
    svc = make("approve", bus="redis")
    try:
        assert svc.bus.kind == "redis"
        svc.run_headless(minutes=45)
        assert not svc.bus.errors
        assert svc.bus.r.xlen(svc.bus.stream) == svc.bus.seq
    finally:
        svc.bus.r.delete(svc.bus.stream)
        svc.close()


def test_server_api_and_websocket():
    from fastapi.testclient import TestClient

    from restaurant_ops.live import server

    from restaurant_ops.config import settings

    from tests.conftest import bearer

    mgr = bearer("manager")
    with TestClient(server.app, headers=mgr) as c:
        st = c.get("/api/state").json()
        assert len(st["tables"]) == 28 and st["service"]["status"] == "running"
        assert c.post("/api/clock", json={"speed": 0}, headers=mgr).json()["speed"] == 0
        free = next(t for t, v in st["tables"].items() if v["state"] == "available" and v["seats"] >= 2)
        r = c.post("/api/orders", json={"table_id": free, "items": [{"dish_id": "ribeye_steak"}], "party_size": 2}, headers=mgr).json()
        assert r["items"] and r["items"][0]["station"] == "grill"
        assert c.post("/api/online/quote", json={"dish_ids": ["caesar_salad"]}).status_code == 200
        assert c.post("/api/scenarios/nope", headers=mgr).status_code == 400
        with c.websocket_connect("/ws") as ws:
            first = ws.receive_json()
            assert first["type"] == "snapshot" and first["payload"]["tables"]
            c.post("/api/scenarios/party_of_12", headers=mgr)
            msg = ws.receive_json()
            assert msg["type"] == "batch" and msg["events"]
