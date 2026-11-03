"""Gap fixes: the live LLM path through the real provider clients (HTTP mocked), executors for every action type (no
silent no-ops), decisions of any era (replay / recovery / snapshot compatibility), docs vs config."""
from __future__ import annotations

import json
import pickle
import re
from pathlib import Path

import httpx
import pytest

from restaurant_ops.config import PROJECT_ROOT, settings
from restaurant_ops.core import actions as ACT
from restaurant_ops.core import constraints as C
from restaurant_ops.live import events as E
from restaurant_ops.live.coordinator import normalize_decision
from restaurant_ops.live.run import POLICIES
from restaurant_ops.live.service import Service

pytestmark = pytest.mark.usefixtures("seeded", "live_dir")


def make(policy="approve", **kw) -> Service:
    return Service(seed=42, speed=60, bus_kind="memory", auto_policy=POLICIES[policy]() if policy else None, **kw)


def submit(svc, agent, action_type, payload, title="test", **kw):
    """Propose through the coordinator; a manager approves when it asks."""
    a = svc.agents[agent]
    d = svc.coordinator.submit(a.proposal(action_type, payload, title, "test reason", [], **kw))
    if d["status"] == "pending":
        d = svc.coordinator.resolve(d["id"], True, "ok", by="manager")
    return d


# =========================================================================== 2. no silent no-ops


def test_every_action_type_has_an_executor():
    from restaurant_ops.agents import supervisor

    assert set(ACT.ACTION_TYPES) == set(C._CHECKS), "core/actions.ACTION_TYPES must list every checked action type"
    svc = make()
    try:
        missing = set(ACT.ACTION_TYPES) - set(ACT.DIRECT_ONLY) - set(svc.coordinator.handlers)
        assert not missing, f"no live executor for {sorted(missing)}"
    finally:
        svc.close()
    batch_missing = set(ACT.ACTION_TYPES) - set(ACT.DIRECT_ONLY) - set(ACT.LIVE_ONLY) - set(supervisor.HANDLERS)
    assert not batch_missing, f"no batch executor for {sorted(batch_missing)}"


def test_an_action_without_executor_is_blocked_not_recorded():
    svc = make()
    try:
        svc.start()
        svc.coordinator.handlers.pop("flag_variance")
        d = submit(svc, "inventory", "flag_variance", {"ingredient_id": "salmon", "variance_pct": 12, "flag": "high"})
        assert d["status"] == "blocked" and d["violations"][0]["code"] == "no_executor"
        assert d["result"] is None
    finally:
        svc.close()


def test_reschedule_po_and_substitute_supplier_cancel_live(monkeypatch):
    from restaurant_ops.tools import registry as R

    svc = make()
    try:
        svc.start()
        svc.tools.bind()
        po = R.send_po(supplier_id="sup_metro", delivery_date="2026-11-04", lines=[{"ingredient_id": "salmon", "qty": 4, "unit_price": 26.88}])
        d = submit(svc, "procurement", "reschedule_po", {"po_id": po["po_id"], "new_delivery_date": "2026-11-06"})
        assert d["needs_approval"] and d["status"] == "executed", d
        row = svc.conn.execute("SELECT expected_delivery FROM purchase_orders WHERE po_id=?", (po["po_id"],)).fetchone()
        assert row[0] == "2026-11-06"
        sub = submit(svc, "procurement", "substitute_supplier",
                     {"po_id": po["po_id"], "cancel_original": True, "supplier_id": "sup_freshcatch", "delivery_date": "2026-11-03",
                      "lines": [{"ingredient_id": "salmon", "qty": 6, "unit_price": 24.0}], "total": 144.0}, title="switch salmon")
        assert sub["status"] == "executed", sub["violations"]
        assert sub["result"]["cancelled"] == po["po_id"]
        assert svc.conn.execute("SELECT status FROM purchase_orders WHERE po_id=?", (po["po_id"],)).fetchone()[0] == "cancelled"
    finally:
        svc.close()


def test_substitute_ingredient_changes_tonight_and_allergen_checks():
    svc = make()
    try:
        svc.start()
        before = svc.stock.portions("caesar_salad")
        d = submit(svc, "menu", "substitute_ingredient",
                   {"dish_id": "caesar_salad", "from_ingredient": "romaine", "to_ingredient": "shrimp", "allergens_declared": ["crustaceans"]},
                   title="Shrimp caesar")
        assert d["needs_approval"] and d["status"] == "executed", d
        assert "shrimp" in svc.stock.bom["caesar_salad"] and "romaine" not in svc.stock.bom["caesar_salad"]
        assert svc.stock.portions("caesar_salad") != before or svc.stock.on_hand("shrimp") > 0
        g = svc.crm.upsert("Allergic guest", phone="555-0999", allergies=["crustaceans"])
        t = next(tid for tid, tb in svc.floor.tables.items() if tb.shape != "bar" and tb.seats >= 2
                 and svc.floor.table_view(tid, svc.clock.t)["state"] == "available")
        r = svc.manual_order(t, [{"dish_id": "caesar_salad"}], party_size=2, guest_id=g["guest_id"])
        assert not r["items"] and (r["held"] or r["rejected"]), "the substituted shrimp must trip the allergy check"
        # an undeclared allergen is still blocked by the checker
        d2 = submit(svc, "menu", "substitute_ingredient",
                    {"dish_id": "caesar_salad", "from_ingredient": "parmesan", "to_ingredient": "cod", "allergens_declared": []},
                    title="Cod caesar")
        assert d2["status"] == "blocked"
    finally:
        svc.close()


def test_prep_list_tonight_and_for_a_later_service():
    svc = make()
    try:
        svc.start()
        p0 = svc.prep.prep.get("baked_cod", {}).get("prepped", 0)
        d = submit(svc, "prep", "prep_list", {"date": svc.date.isoformat(), "meal_period": svc.period,
                                              "items": [{"station": "saute", "dish_id": "baked_cod", "portions": p0 + 6}]})
        assert d["status"] == "executed", d
        assert d["result"]["applied"] == "tonight" and d["result"]["batches"][0]["portions"] == 6
        assert svc.prep.prep["baked_cod"]["par"] == p0 + 6
        from datetime import timedelta

        tmr = (svc.date + timedelta(days=1)).isoformat()
        d2 = submit(svc, "prep", "prep_list", {"date": tmr, "meal_period": "dinner",
                                               "items": [{"station": "saute", "dish_id": "baked_cod", "portions": 8}]}, title="tomorrow")
        assert d2["status"] == "executed" and d2["result"]["applied"] == "saved for that service"
    finally:
        svc.close()
    nxt = make(service_date=tmr)
    try:
        nxt.start()
        assert nxt.prep.prep["baked_cod"]["par"] == 8
    finally:
        nxt.close()


def test_adjust_forecast_reaches_tomorrows_service_without_compounding():
    import sqlite3

    svc = make()
    try:
        svc.start()
        from datetime import timedelta

        tmr = (svc.date + timedelta(days=1)).isoformat()
        for f in (0.8, 0.9):
            d = submit(svc, "forecasting", "adjust_forecast", {"date": tmr, "meal_period": "dinner", "factor": f, "reason": "test"},
                       key=f"adjust:{f}", force=True)
            assert d["status"] == "executed", d
    finally:
        svc.close()
    m = sqlite3.connect(settings.DB_PATH)
    rows = m.execute("SELECT payload FROM decisions WHERE action_type='adjust_forecast' AND run_id LIKE 'svc-%'").fetchall()
    m.close()
    assert [json.loads(r[0])["factor"] for r in rows] == [0.9], "the latest live adjustment replaces the earlier one"


# =========================================================================== 3. decisions of any era


LEGACY = {"id": 7, "agent": "procurement", "action_type": "place_po", "title": "Order salmon", "payload": {"supplier_id": "sup_metro"},
          "status": "pending", "timeout_s": 120, "remaining_s": 30}


def test_normalize_legacy_decision():
    d = normalize_decision(dict(LEGACY))
    assert d["timeout_min"] == settings.APPROVAL_TIMEOUT_SIM_MIN
    assert d["remaining_min"] == pytest.approx(settings.APPROVAL_TIMEOUT_SIM_MIN * 0.25)   # a quarter of the time was left
    assert d["key"] == "place_po:Order salmon" and d["needs_approval"] is True and d["context"] == {}
    assert "timeout_s" not in d and "remaining_s" not in d
    bare = normalize_decision({"id": 1, "action_type": "flag_variance", "status": "auto"})
    assert bare["status"] == "executed" and bare["needs_approval"] is False and bare["timeout_min"] == 0


def test_recover_from_an_old_format_event_log():
    """A log written before service-minute timeouts (timeout_s / remaining_s, no key) still recovers and ticks."""
    svc = Service(seed=42, speed=60, bus_kind="memory")
    sid = svc.service_id
    try:
        svc.run_headless(until_end=False, minutes=60)
        svc.flush()
        rows = svc.conn.execute("SELECT seq, payload FROM event_log WHERE service_id=? AND type IN ('decision.proposed','decision.resolved')",
                                (sid,)).fetchall()
        assert rows
        for seq, payload in rows:  # rewrite the log into the old shape
            p = json.loads(payload)
            for k in ("timeout_min", "remaining_min", "key", "context"):
                p.pop(k, None)
            p["timeout_s"], p["remaining_s"] = 120, 60
            svc.conn.execute("UPDATE event_log SET payload=? WHERE seq=? AND service_id=?", (json.dumps(p), seq, sid))
        svc.conn.commit()
        pending = {d["id"] for d in svc.coordinator.decisions.values() if d["status"] == "pending"}
    finally:
        svc.close()
    for p in Path(settings.LIVE_DIR).glob(f"{sid}.state*"):
        p.unlink()
    r = Service.recover_from_log(sid, bus_kind="memory")
    try:
        for d in r.coordinator.decisions.values():
            assert d["timeout_min"] is not None and d["key"]
        assert {d["id"] for d in r.coordinator.decisions.values() if d["status"] == "pending"} == pending
        r.run_headless(until_end=False, minutes=settings.APPROVAL_TIMEOUT_SIM_MIN + 2)   # ticks through the timeouts
        assert all(r.coordinator.decisions[i]["status"] != "pending" for i in pending)
    finally:
        r.close()


def test_old_snapshot_decisions_are_normalized():
    from restaurant_ops.live import persistence as PERSIST

    blob = {"version": 1, "world": {}, "coordinator": {"decisions": {7: dict(LEGACY)}}}
    out = PERSIST.migrate(blob)
    d = out["coordinator"]["decisions"][7]
    assert d["timeout_min"] and d["remaining_min"] and d["key"]


# =========================================================================== 1. live LLM path through the real clients


def _openai_response(content: str | None = None, tool_args: dict | None = None, prompt_tokens=180, completion_tokens=40) -> dict:
    msg = {"role": "assistant", "content": content}
    if tool_args is not None:
        msg = {"role": "assistant", "content": None,
               "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "Judgment", "arguments": json.dumps(tool_args)}}]}
    return {"id": "chatcmpl-1", "object": "chat.completion", "created": 0, "model": "test-model",
            "choices": [{"index": 0, "message": msg, "finish_reason": "stop" if tool_args is None else "tool_calls"}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens, "total_tokens": prompt_tokens + completion_tokens}}


def _client(handler, seen):
    def h(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content))
        return handler(req)

    return httpx.Client(transport=httpx.MockTransport(h))


@pytest.mark.parametrize("provider", ["meta", "openai"])
def test_live_review_through_the_provider_client(monkeypatch, provider):
    """The review call goes through the same ChatOpenAI subclasses as production (Meta: json_schema response_format;
    OpenAI: function calling); only the HTTP transport is mocked. Exercises request shape, parsing and token usage."""
    from restaurant_ops.agents import llm
    from restaurant_ops.live.agents.base import _review

    judgment = {"keep": [1], "rationale": "Salmon covers only 6 of 14; the cod special uses stock on hand.", "confidence": 0.82}
    seen: list = []
    if provider == "meta":
        handler = lambda req: httpx.Response(200, json=_openai_response(content=json.dumps(judgment)))  # noqa: E731
        model = llm._MetaChatOpenAI(model="muse-spark-1.3", api_key="test", base_url="https://model.test/v1",
                                    http_client=_client(handler, seen))
    else:
        handler = lambda req: httpx.Response(200, json=_openai_response(tool_args=judgment))  # noqa: E731
        model = llm._ToolCallingChatOpenAI(model="gpt-4.1", api_key="test", base_url="https://model.test/v1",
                                           http_client=_client(handler, seen))
    monkeypatch.setattr(llm, "get_review_llm", lambda: model)
    from restaurant_ops.live.llm_eval import SMOKE_CANDIDATES, SMOKE_SITUATION

    r = _review("menu", SMOKE_SITUATION, SMOKE_CANDIDATES)
    assert r["judgment"].keep == [1] and "cod" in r["judgment"].rationale
    assert (r["tokens_in"], r["tokens_out"]) == (180, 40)
    body = seen[0]
    assert "Candidate actions" in body["messages"][0]["content"]
    if provider == "meta":
        rf = body["response_format"]
        assert rf["type"] == "json_schema" and rf["json_schema"]["strict"] is False
        assert set(rf["json_schema"]["schema"]["required"]) == {"keep", "rationale", "confidence"}
        assert "tool_choice" not in body or body["tool_choice"] == "auto"
    else:
        assert body["tools"][0]["function"]["name"] == "Judgment"


def test_live_service_with_a_provider_records_calls_and_falls_back(monkeypatch):
    """A whole live segment with the review on: calls, tokens and cost are logged; an HTTP error falls back to the
    rule-based proposals and counts as a failure."""
    import time

    from restaurant_ops.agents import llm
    from restaurant_ops.live.run import parse_schedule

    state = {"fail": False}
    seen: list = []

    def handler(req):
        if state["fail"]:
            return httpx.Response(500, json={"error": {"message": "upstream down"}})
        body = json.loads(req.content)
        n = len(json.loads(body["messages"][0]["content"].split("Candidate actions:")[1]))
        return httpx.Response(200, json=_openai_response(content=json.dumps({"keep": list(range(n)), "rationale": "LIVE: as proposed",
                                                                             "confidence": 0.7})))

    model = llm._MetaChatOpenAI(model="muse-spark-1.3", api_key="test", base_url="https://model.test/v1", max_retries=0,
                                http_client=_client(handler, seen))
    monkeypatch.setattr(llm, "get_review_llm", lambda: model)
    monkeypatch.setattr(settings, "LLM_MODE", "live")

    def drain(svc):
        deadline = time.time() + 10
        while svc.pending_llm and time.time() < deadline:
            time.sleep(0.05)
            svc.advance(0.01)

    svc = make("approve")
    svc.schedule = parse_schedule(["salmon_short@18:00"], svc)
    try:
        svc.run_headless(until_end=False, minutes=80)
        drain(svc)
        g = svc.llm_guard
        assert g.calls >= 1 and g.failures == 0 and g.cost > 0
        reviewed = [d for d in svc.coordinator.decisions.values() if d["mode"] == "llm"]
        assert reviewed and reviewed[0]["reason"].startswith("LIVE")
        state["fail"] = True
        menu = svc.agents["menu"]
        prop = menu.proposal("add_special", {"dish_id": "baked_cod", "portions": 6}, "Cod special (provider down)", "test", [])
        assert menu.submit([prop], {"t": 1}) == []          # handed to the reviewer
        drain(svc)
        assert g.failures >= 1
        fell_back = [d for d in svc.coordinator.decisions.values() if d["title"] == "Cod special (provider down)"]
        assert fell_back and fell_back[0]["mode"] == "rule"
        svc.flush()
        rows = svc.conn.execute("SELECT ok, tokens_in, error FROM llm_calls WHERE service_id=?", (svc.service_id,)).fetchall()
        assert any(r[0] == 1 and r[1] == 180 for r in rows) and any(r[0] == 0 and r[2] for r in rows)
    finally:
        svc.close()


def test_smoke_reports_mock_mode_clearly():
    from restaurant_ops.live import llm_eval

    r = llm_eval.smoke()
    assert r["ok"] is False and "mock mode" in r["error"]


# =========================================================================== 5. docs and config agree


def _settings_fields() -> set[str]:
    return set(type(settings).model_fields)


def test_documented_settings_exist():
    """Every UPPER_CASE setting named in the README and .env.example is a real field (no dead or renamed knobs)."""
    fields = _settings_fields()
    allowed_env_only = {"LLAMA_API_KEY", "MODEL_API_KEY", "LANGSMITH_TRACING", "LANGSMITH_API_KEY", "LANGSMITH_PROJECT",
                        "LANGSMITH_ENDPOINT", "PRINTER_HOST_GRILL"}
    env_keys = {l.split("=", 1)[0].strip() for l in (PROJECT_ROOT / ".env.example").read_text().splitlines()
                if re.match(r"^[A-Z][A-Z0-9_]+=", l)}
    unknown = env_keys - fields - allowed_env_only
    assert not unknown, f".env.example names unknown settings: {sorted(unknown)}"
    readme = (PROJECT_ROOT / "README.md").read_text()
    named = set(re.findall(r"`([A-Z][A-Z0-9_]{3,})(?:=[^`]*)?`", readme))
    not_settings = {"POST", "GET", "PUT", "DELETE", "PATCH", "LLM_MODEL", "STATION"}
    unknown = {n for n in named - fields - not_settings - allowed_env_only if not n.startswith("PRINTER_HOST_")}
    assert not unknown, f"README names unknown settings: {sorted(unknown)}"


def test_obsolete_env_keys_are_reported(monkeypatch):
    from restaurant_ops import config

    monkeypatch.setenv("MANAGER_TOKEN", "x")
    monkeypatch.setenv("APPROVAL_TIMEOUT_S", "120")
    found = config.obsolete_settings()
    assert {"MANAGER_TOKEN", "APPROVAL_TIMEOUT_S"} <= {k for k, _ in found}


# =========================================================================== restored approvals without a checkpoint


def test_pending_decision_without_its_checkpoint_still_resolves():
    """A pending approval restored from a snapshot / the event log whose LangGraph checkpoint is missing used to crash
    every tick on resume (KeyError: 'decision' in the check node) and stop the service clock."""
    svc = make(policy=None)
    try:
        co = svc.coordinator

        def pending(title, empty_checkpoint=False):
            p = svc.agents["menu"].proposal("86_dish", {"dish_id": "seared_salmon", "date": svc.date.isoformat(),
                                                        "meal_period": svc.period, "after_portions": 0}, title, "test", [])
            d = co.submit(p)
            assert d["status"] == "pending"
            co.saver.delete_thread(co._cfg(d["id"])["configurable"]["thread_id"])
            if empty_checkpoint:  # the state seen in the field: a checkpoint paused before `check` with no decision in it
                co.graph.invoke({}, co._cfg(d["id"]), interrupt_before=["check"])
                assert co.graph.get_state(co._cfg(d["id"])).next == ("check",)
            assert not co._paused_at_approval(d["id"])
            return d

        a = co.resolve(pending("approve me")["id"], True, "ok", by="manager")
        assert a["status"] == "executed" and "seared_salmon" in svc.menu.eighty_sixed

        b = pending("let me time out", empty_checkpoint=True)
        b["remaining_min"] = 0.01
        co.tick(60)  # the approval timeout path that crashed in the server loop
        assert b["status"] == "timeout"
    finally:
        svc.close()


def test_legacy_station_ids_are_migrated(tmp_path):
    """Databases seeded by the first catalog cook dishes on 'fry' / 'garde_manger', which the kitchen no longer knows
    (KeyError: 'fry' on every order). seed_defaults moves them onto saute / cold and merges the shift cook counts."""
    from restaurant_ops import admin as ADMIN
    from restaurant_ops.db.connection import get_conn, init_db

    c = get_conn(tmp_path / "old.db")
    try:
        init_db(c)
        ADMIN.seed_defaults(c)
        c.execute("INSERT OR REPLACE INTO dishes (id, name, category, price, station, is_cold, seasonal_months, prep_minutes, active) "
                  "VALUES ('old_fries', 'Fries', 'side', 9, 'fry', 0, '', 1, 1), ('old_salad', 'Salad', 'starter', 12, 'garde_manger', 1, '', 2, 1)")
        c.executemany("INSERT OR REPLACE INTO staff_shifts (date, meal_period, station, staff) VALUES ('2026-10-01', 'dinner', ?, ?)",
                      [("saute", 2), ("fry", 1), ("garde_manger", 1)])
        c.commit()
        out = ADMIN.seed_defaults(c)
        assert out.get("legacy_stations") == {"fry": "saute", "garde_manger": "cold"}
        st = dict(c.execute("SELECT id, station FROM dishes WHERE id IN ('old_fries', 'old_salad')").fetchall())
        assert st == {"old_fries": "saute", "old_salad": "cold"}
        shifts = dict(c.execute("SELECT station, staff FROM staff_shifts WHERE date='2026-10-01'").fetchall())
        assert shifts == {"saute": 3, "cold": 1}
        assert "legacy_stations" not in ADMIN.seed_defaults(c)  # idempotent
    finally:
        c.close()
