"""Forecast Analyst node: reviews the model forecast, adjusts it for unseen events, publishes final numbers.

Valid `adjust_forecast` actions are applied immediately (checked with `check_action`, logged as `executed`
decisions) so every downstream agent's forecast tools already reflect them; blocked ones stay in `proposed`
and go through the normal constraint_check / retry path. `state["forecast"]` then holds the numbers re-read
from the forecast tools.
"""
from __future__ import annotations

from restaurant_ops.agents import base, mock_policies
from restaurant_ops.agents import decision_log as DL
from restaurant_ops.agents.state import AgentReport, replace_agent
from restaurant_ops.tools import registry as R

NAME = "forecast_analyst"
TASK = ("Review the 3-day demand forecast for anything the model cannot see (events, private bookings, weather alerts). "
        "Propose adjust_forecast actions only for effects not yet reflected, then report.")


def final_forecast(sim_date: str, applied: list[dict]) -> dict:
    """Forecast numbers straight from the tools (approved adjustments already included)."""
    covers = R.forecast_covers(start_date=sim_date, days=3)
    need: dict[str, float] = {}
    for r in R.forecast_dish_mix(start_date=sim_date, days=1):
        need[r["dish_id"]] = round(need.get(r["dish_id"], 0.0) + r["p50"], 1)
    return {"start_date": sim_date, "days": 3, "covers": covers, "dish_need_today": need, "adjustments": applied}


def apply_adjustments(state: dict, rep: AgentReport) -> tuple[list[dict], list[dict], list[dict]]:
    """Returns (remaining action dicts, executed entries, applied adjustment summaries)."""
    remaining, executed, applied = [], [], []
    for a in rep.actions:
        ad = a.model_dump()
        if a.action_type != "adjust_forecast":
            remaining.append(ad)
            continue
        viols = R.check_action(action=ad)
        if any(v["severity"] == "block" for v in viols):
            remaining.append(ad)
            continue
        did = DL.log_decision(state["run_id"], state["sim_date"], NAME, a.action_type, a.payload, a.rationale,
                              status="executed", violations=viols)
        executed.append({"id": did, "action_type": a.action_type, "agent": NAME, "status": "executed",
                         "result": "applied before downstream agents ran"})
        applied.append({"decision_id": did, **{k: a.payload.get(k) for k in ("date", "meal_period", "factor", "reason")}})
    if executed:
        R.clear_cache()
    return remaining, executed, applied


def node(state: dict) -> dict:
    base.bind(state)
    rep = base.run_specialist(NAME, state, TASK, mock_policies.forecast)
    remaining, executed, applied = apply_adjustments(state, rep)
    with DL.node_context(state["run_id"], NAME, state["sim_date"]):
        fc = final_forecast(state["sim_date"], applied)
    return {"reports": {NAME: rep.model_dump()}, "proposed": replace_agent(NAME, remaining), "executed": executed,
            "forecast": fc, "step": int(state.get("step", 0)) + 1}
