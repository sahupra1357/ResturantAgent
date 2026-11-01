"""The LangGraph: sense -> supervisor -> specialists -> constraint_check -> approval -> execute.

    sense -> supervisor -> [forecast_analyst -> inventory -> purchasing -> menu -> prep]  (the planned subset)
          -> constraint_check -> (blocked? back to supervisor once) -> approval (interrupt) -> execute -> END

Public API (Phase 4 / 5): `run_cycle`, `resume_cycle`, `get_pending`, `build_graph`.
Checkpoints live in a SqliteSaver at `settings.CHECKPOINT_DB`; thread_id = "{run_id}:{sim_date}:{trigger kind}".
Trigger dicts:
  {"kind": "morning"}
  {"kind": "event", "event": {"kind": <supplier_shortfall|private_booking|cooler_failure|heatwave>, "params": {...}}}
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Literal

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field

from restaurant_ops.agents import base, llm, prompts
from restaurant_ops.agents import decision_log as DL
from restaurant_ops.agents import forecast_analyst, inventory_agent, menu_agent, prep_planner, purchasing_agent
from restaurant_ops.agents.state import AGENT_ORDER, OpsState, replace_agent
from restaurant_ops.config import settings
from restaurant_ops.tools import registry as R

# event kind -> agents to re-run when the LLM supervisor is unavailable (mock mode or model error)
EVENT_RULES: dict[str, list[str]] = {
    "supplier_shortfall": ["purchasing", "menu", "prep"],
    "private_booking": ["forecast_analyst", "inventory", "purchasing", "prep"],
    "cooler_failure": ["inventory", "purchasing", "menu", "prep"],
    "heatwave": ["forecast_analyst", "menu", "prep"],
}
SPECIALISTS = {"forecast_analyst": forecast_analyst.node, "inventory": inventory_agent.node,
               "purchasing": purchasing_agent.node, "menu": menu_agent.node, "prep": prep_planner.node}


# =========================================================================== nodes


def sense(state: OpsState) -> dict:
    """Set the sim date and pull reservations / events / weather / stock summary into state."""
    base.bind(state)
    d, run = state["sim_date"], state["run_id"]
    with DL.node_context(run, "sense", d):
        lots = R.stock_by_lot()
        ctx = {
            "reservations": R.reservations_summary(start_date=d, days=3),
            "events": R.event_calendar(start_date=d, days=3),
            "weather": R.weather(start_date=d, days=3),
            "stock": {"active_lots": len(lots), "expiring_within_2d": sorted({l["ingredient_id"] for l in lots if l["days_left"] <= 2})},
        }
        baseline = forecast_analyst.final_forecast(d, [])  # replaced by the Forecast Analyst's adjusted numbers if it runs
    DL.write_trace(run, d, "sense", "event", {"trigger": state.get("trigger"), "reservations": len(ctx["reservations"]),
                                              "events": len(ctx["events"]), "stock": ctx["stock"]})
    return {"context": ctx, "step": 0, "sup_steps": 0, "retries": 0, "retry_agents": [], "violation_feedback": {},
            "validated": [], "approvals": [], "agents_to_run": [], "forecast": baseline,
            "reports": {"__reset__": True}, "proposed": replace_agent("*", []), "executed": replace_agent("*", [])}


class Plan(BaseModel):
    """Structured output of the LLM supervisor."""

    agents: list[Literal["forecast_analyst", "inventory", "purchasing", "menu", "prep"]] = Field(
        description="Specialists to re-run for this event")
    reason: str = ""


def _canonical(names: list[str]) -> list[str]:
    return [a for a in AGENT_ORDER if a in set(names)]


def plan_agents(state: OpsState) -> tuple[list[str], str]:
    """Which specialists run: everything on a morning trigger; for events the LLM picks the minimal set
    (rule table fallback in mock mode or on model error)."""
    kind, _ = base.event_of(state.get("trigger") or {})
    if kind is None:
        return list(AGENT_ORDER), "morning run: full order"
    fallback = _canonical(EVENT_RULES.get(kind, AGENT_ORDER))
    if llm.is_mock():
        return fallback, f"rule table for {kind}"
    try:
        system = prompts.render("supervisor", sim_date=state["sim_date"], trigger=base.trigger_text(state["trigger"]),
                                upstream=json.dumps(state.get("context", {}), default=str)[:1800],
                                feedback=DL.format_feedback(DL.recent_feedback(6)))
        plan = llm.get_llm().with_structured_output(Plan).invoke(
            [SystemMessage(content=system), HumanMessage(content="Which specialists must re-run?")])
        agents = _canonical(plan.agents)
        if agents:
            return agents, plan.reason or "LLM supervisor plan"
    except Exception as e:  # noqa: BLE001
        return fallback, f"rule table for {kind} (LLM planning failed: {type(e).__name__})"
    return fallback, f"rule table for {kind} (empty LLM plan)"


def supervisor(state: OpsState) -> dict:
    """Plan the run order (or, after blocked proposals, schedule the retry of the affected agents)."""
    base.bind(state)
    retry = list(state.get("retry_agents") or [])
    if retry:
        plan, reason = _canonical(retry), "retry after blocking constraint violations"
    else:
        plan, reason = plan_agents(state)
    n = int(state.get("sup_steps", 0)) + 1
    DL.write_trace(state["run_id"], state["sim_date"], "supervisor", "supervisor",
                   {"pass": n, "max_passes": settings.MAX_SUPERVISOR_STEPS, "agents": plan, "reason": reason})
    return {"agents_to_run": plan, "step": 0, "sup_steps": n, "plan_reason": reason, "retry_agents": []}


def route_next(state: OpsState) -> str:
    """Conditional edge: next planned specialist, else constraint_check."""
    plan, i = state.get("agents_to_run") or [], int(state.get("step", 0))
    return plan[i] if i < len(plan) else "constraint_check"


def _blocking_msgs(v: dict) -> list[str]:
    a = v["action"]
    return [f"{a['action_type']} {json.dumps(a['payload'], default=str)[:120]}: {x['code']}: {x['message']}"
            for x in v["violations"] if x["severity"] == "block"]


def constraint_check(state: OpsState) -> dict:
    """Run check_action on every proposal. Blocked proposals are sent back once for a retry of their agent;
    on the final pass every proposal is written to `decisions` (blocked / pending_approval / proposed)."""
    base.bind(state)
    run, d = state["run_id"], state["sim_date"]
    validated = []
    with DL.node_context(run, "constraint_check", d):
        for i, a in enumerate(state.get("proposed") or []):
            viols = R.check_action(action=a)
            validated.append({"idx": i, "action": a, "violations": viols,
                              "blocked": any(x["severity"] == "block" for x in viols),
                              "needs_approval": bool(R.needs_approval(action=a)), "decision_id": None})
    blocked = [v for v in validated if v["blocked"]]
    can_retry = blocked and int(state.get("retries", 0)) < 1 and int(state.get("sup_steps", 0)) < settings.MAX_SUPERVISOR_STEPS
    if can_retry:
        feedback: dict[str, list[str]] = {}
        for v in blocked:
            feedback.setdefault(v["action"]["agent"], []).extend(_blocking_msgs(v))
        DL.write_trace(run, d, "constraint_check", "constraint",
                       {"result": "retry", "blocked": len(blocked), "agents": sorted(feedback), "feedback": feedback})
        return {"validated": validated, "retries": int(state.get("retries", 0)) + 1, "retry_agents": sorted(feedback),
                "violation_feedback": feedback}
    for v in validated:
        a = v["action"]
        status = "blocked" if v["blocked"] else ("pending_approval" if v["needs_approval"] else "proposed")
        v["decision_id"] = DL.log_decision(run, d, a["agent"], a["action_type"], a["payload"], a.get("rationale", ""),
                                           status=status, violations=v["violations"])
    DL.write_trace(run, d, "constraint_check", "constraint",
                   {"result": "final", "proposed": len(validated), "blocked": len(blocked),
                    "needs_approval": sum(1 for v in validated if v["needs_approval"] and not v["blocked"]),
                    "blocked_detail": [x for v in blocked for x in _blocking_msgs(v)][:10]})
    return {"validated": validated, "retry_agents": []}


def route_after_check(state: OpsState) -> str:
    return "supervisor" if state.get("retry_agents") else "approval"


def _pending_items(state: OpsState) -> list[dict]:
    out = []
    for v in state.get("validated") or []:
        if v["blocked"] or not v["needs_approval"]:
            continue
        a = v["action"]
        out.append({"id": v["decision_id"], "agent": a["agent"], "action_type": a["action_type"], "payload": a["payload"],
                    "rationale": a.get("rationale", ""), "est_cost": a.get("est_cost", 0.0),
                    "warnings": [x for x in v["violations"] if x["severity"] != "block"]})
    return out


def approval(state: OpsState) -> dict:
    """Chef approval via interrupt(). The node re-runs from the top on resume, so nothing before interrupt() has side effects."""
    base.bind(state)
    pending = _pending_items(state)
    if not pending:
        return {"approvals": []}
    resp = interrupt({"pending": pending})
    decisions = resp.get("decisions", []) if isinstance(resp, dict) else list(resp or [])
    by_id = {int(x["id"]): x for x in decisions}
    out = []
    for p in pending:
        x = by_id.get(int(p["id"]))
        approved = bool(x and x.get("approved"))
        note = (x or {}).get("note") or ("approved" if approved else "rejected" if x else "no decision given")
        DL.update_status(p["id"], "approved" if approved else "rejected", note)
        out.append({"id": p["id"], "approved": approved, "note": note})
    DL.write_trace(state["run_id"], state["sim_date"], "approval", "approval",
                   {"approved": sum(o["approved"] for o in out), "rejected": sum(not o["approved"] for o in out), "decisions": out})
    return {"approvals": out}


# =========================================================================== execute


def _exec_place_po(p: dict, a: dict, did: int, today: str) -> dict:
    return R.send_po(supplier_id=p["supplier_id"], delivery_date=p["delivery_date"], lines=p["lines"], decision_id=did)


def _exec_substitute_supplier(p: dict, a: dict, did: int, today: str) -> dict:
    res = _exec_place_po(p, a, did, today)
    if p.get("po_id") and p.get("cancel_original"):
        with R.connection() as c:
            c.execute("UPDATE purchase_orders SET status='cancelled' WHERE po_id=? AND status IN ('sent','pending_approval','draft')",
                      (p["po_id"],))
            c.commit()
        res["cancelled"] = p["po_id"]
    return res


def _exec_reschedule_po(p: dict, a: dict, did: int, today: str) -> dict:
    with R.connection() as c:
        c.execute("UPDATE purchase_orders SET expected_delivery=? WHERE po_id=?", (p["new_delivery_date"], p["po_id"]))
        c.commit()
    return {"po_id": p["po_id"], "expected_delivery": p["new_delivery_date"]}


def _set_menu(status: str, p: dict, a: dict, note: str, today: str) -> dict:
    day = p.get("date") or today
    with R.connection() as c:
        c.execute("INSERT OR REPLACE INTO menu_state (date, dish_id, status, note) VALUES (?,?,?,?)",
                  (day, p["dish_id"], status, note[:300]))
        c.commit()
    return {"date": day, "dish_id": p["dish_id"], "status": status}


def _exec_add_special(p: dict, a: dict, did: int, today: str) -> dict:
    per = f" ({p['meal_period']})" if p.get("meal_period") else ""
    return _set_menu("special", p, a, f"{p.get('portions')} portions{per}: {a.get('rationale', '')}", today)


def _exec_86(p: dict, a: dict, did: int, today: str) -> dict:
    per = f" ({p['meal_period']})" if p.get("meal_period") else ""
    return _set_menu("86", p, a, f"86'd{per}: {a.get('rationale', '')}", today)


def _exec_discard_lot(p: dict, a: dict, did: int, today: str) -> dict:
    with R.connection() as c:
        lot = c.execute("SELECT * FROM lots WHERE lot_id=?", (p["lot_id"],)).fetchone()
        qty = float(p.get("qty") or lot["qty"])
        full = qty >= float(lot["qty"]) - 1e-9
        c.execute("INSERT INTO waste_log (date, lot_id, ingredient_id, qty, value, reason) VALUES (?,?,?,?,?,?)",
                  (today, lot["lot_id"], lot["ingredient_id"], qty, round(qty * float(lot["unit_cost"]), 2), p.get("reason", "discarded")))
        if full:
            c.execute("UPDATE lots SET status='discarded' WHERE lot_id=?", (lot["lot_id"],))
        else:
            c.execute("UPDATE lots SET qty=qty-? WHERE lot_id=?", (qty, lot["lot_id"]))
        c.commit()
    return {"lot_id": p["lot_id"], "discarded_qty": qty}


def _exec_noop(p: dict, a: dict, did: int, today: str) -> dict:
    return {"recorded": True}  # prep_list / adjust_forecast / flag_variance / substitute_ingredient live in `decisions`


HANDLERS = {"place_po": _exec_place_po, "substitute_supplier": _exec_substitute_supplier, "reschedule_po": _exec_reschedule_po,
            "add_special": _exec_add_special, "86_dish": _exec_86, "discard_lot": _exec_discard_lot,
            "prep_list": _exec_noop, "adjust_forecast": _exec_noop, "flag_variance": _exec_noop,
            "substitute_ingredient": _exec_noop}


def execute(state: OpsState) -> dict:
    """Apply approved / approval-free actions (re-checked first) and record the outcome in `decisions`."""
    base.bind(state)
    run, today = state["run_id"], state["sim_date"]
    ok = {x["id"]: x for x in state.get("approvals") or []}
    done: list[dict] = []
    with DL.node_context(run, "execute", today):
        for v in state.get("validated") or []:
            a, did = v["action"], v["decision_id"]
            if v["blocked"]:
                continue
            if v["needs_approval"] and not (did in ok and ok[did]["approved"]):
                continue
            viols = R.check_action(action=a)  # world may have changed since the first check
            if any(x["severity"] == "block" for x in viols):
                DL.update_status(did, "blocked", violations=viols)
                done.append({"id": did, "action_type": a["action_type"], "agent": a["agent"], "status": "blocked",
                             "result": {"violations": viols}})
                continue
            try:
                res = HANDLERS[a["action_type"]](a["payload"], a, did, today)
                DL.update_status(did, "executed")
                status = "executed"
            except Exception as e:  # noqa: BLE001  one failing action must not stop the others
                res, status = {"error": f"{type(e).__name__}: {e}"}, "failed"
            done.append({"id": did, "action_type": a["action_type"], "agent": a["agent"], "status": status, "result": res})
            DL.write_trace(run, today, "execute", "execute", {"decision_id": did, "action_type": a["action_type"],
                                                              "status": status, "result": res})
    R.clear_cache()
    return {"executed": done}


# =========================================================================== graph


_SAVER = None


def default_checkpointer():
    """Process-wide SqliteSaver at settings.CHECKPOINT_DB."""
    global _SAVER
    if _SAVER is None:
        from langgraph.checkpoint.sqlite import SqliteSaver

        settings.CHECKPOINT_DB.parent.mkdir(parents=True, exist_ok=True)
        _SAVER = SqliteSaver(sqlite3.connect(str(settings.CHECKPOINT_DB), check_same_thread=False))
    return _SAVER


def build_graph(checkpointer: Any = None):
    """Compile the ops graph. `checkpointer=None` uses the SqliteSaver at settings.CHECKPOINT_DB."""
    DL.install_trace_hook()
    g = StateGraph(OpsState)
    g.add_node("sense", sense)
    g.add_node("supervisor", supervisor)
    for name, fn in SPECIALISTS.items():
        g.add_node(name, fn)
    g.add_node("constraint_check", constraint_check)
    g.add_node("approval", approval)
    g.add_node("execute", execute)
    g.add_edge(START, "sense")
    g.add_edge("sense", "supervisor")
    targets = [*AGENT_ORDER, "constraint_check"]
    for src in ("supervisor", *AGENT_ORDER):
        g.add_conditional_edges(src, route_next, targets)
    g.add_conditional_edges("constraint_check", route_after_check, ["supervisor", "approval"])
    g.add_edge("approval", "execute")
    g.add_edge("execute", END)
    return g.compile(checkpointer=checkpointer if checkpointer is not None else default_checkpointer())


# =========================================================================== public API


def make_thread_id(run_id: str, sim_date: str, trigger: dict) -> str:
    kind, _ = base.event_of(trigger)
    return f"{run_id}:{sim_date}:{kind or 'morning'}"


def _cfg(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}, "recursion_limit": 60}


def _pending_of(snap) -> list[dict] | None:
    """Pending approval items of a paused snapshot (None when the graph is not waiting for the chef)."""
    ints = list(getattr(snap, "interrupts", ()) or ())
    for t in getattr(snap, "tasks", ()) or ():
        ints += list(getattr(t, "interrupts", ()) or ())
    for i in ints:
        val = getattr(i, "value", None)
        if isinstance(val, dict) and "pending" in val:
            return val["pending"]
    return None


def _finish(graph, thread_id: str, auto_approve: bool) -> dict:
    cfg = _cfg(thread_id)
    for _ in range(3):
        snap = graph.get_state(cfg)
        pending = _pending_of(snap) if snap.next else None
        if pending is None:
            out = dict(snap.values)
            out.update(status="completed", thread_id=thread_id)
            return out
        if not auto_approve:
            v = snap.values
            return {"status": "awaiting_approval", "pending": pending, "thread_id": thread_id,
                    "run_id": v.get("run_id"), "sim_date": v.get("sim_date")}
        graph.invoke(Command(resume={"decisions": [{"id": p["id"], "approved": True, "note": "auto-approved"} for p in pending]}), cfg)
    raise RuntimeError("approval loop did not converge")


def run_cycle(run_id: str, sim_date: str, trigger: dict, db_path: str | None = None, auto_approve: bool = False,
              thread_id: str | None = None, checkpointer: Any = None) -> dict:
    """Run one planning cycle. Returns the final state (+ status 'completed', thread_id), or
    {"status": "awaiting_approval", "pending": [...], "thread_id": ...} when the chef must decide."""
    if db_path:
        R.set_db_path(db_path)
    sim_date = R.set_sim_date(sim_date)
    thread_id = thread_id or make_thread_id(run_id, sim_date, trigger)
    graph = build_graph(checkpointer)
    graph.invoke({"run_id": run_id, "sim_date": sim_date, "db_path": str(db_path or ""), "trigger": trigger or {"kind": "morning"}},
                 _cfg(thread_id))
    return _finish(graph, thread_id, auto_approve)


def resume_cycle(thread_id: str, decisions: list[dict], db_path: str | None = None, checkpointer: Any = None) -> dict:
    """Resume a paused cycle with chef decisions [{"id", "approved", "note"}] (ids from `pending`)."""
    if db_path:
        R.set_db_path(db_path)
    graph = build_graph(checkpointer)
    graph.invoke(Command(resume={"decisions": decisions}), _cfg(thread_id))
    return _finish(graph, thread_id, auto_approve=False)


def get_pending(thread_id: str, db_path: str | None = None, checkpointer: Any = None) -> list[dict]:
    """Approval items the cycle is waiting on ([] when none / not paused)."""
    if db_path:
        R.set_db_path(db_path)
    snap = build_graph(checkpointer).get_state(_cfg(thread_id))
    return (_pending_of(snap) if snap.next else None) or []
