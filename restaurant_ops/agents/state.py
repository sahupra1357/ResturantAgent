"""Graph state and the Pydantic contracts agents produce.

State holds plain JSON-like values (dicts / lists), never Pydantic objects, so the SQLite checkpointer
can serialise it: an `AgentReport` is stored as `report.model_dump()` under `reports[agent]`.

Re-planning: `proposed` uses the `merge_proposed` reducer. A plain list appends; a `replace_agent(name, actions)`
update drops every earlier action of that agent first. A specialist that runs a second time (retry after
constraint violations, or an event re-run) therefore replaces its own proposals instead of duplicating them.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal, TypedDict

from pydantic import BaseModel, Field

ActionType = Literal["adjust_forecast", "place_po", "reschedule_po", "substitute_supplier", "add_special",
                     "86_dish", "substitute_ingredient", "prep_list", "discard_lot", "flag_variance"]

AGENT_ORDER = ["forecast_analyst", "inventory", "purchasing", "menu", "prep"]
REPLACE_KEY = "__replace_agent__"


class Action(BaseModel):
    """One proposed change to the world. Payload shapes are documented in `core/constraints.py`."""

    action_type: ActionType
    payload: dict = Field(default_factory=dict)
    rationale: str = ""
    agent: str = ""
    est_cost: float = 0.0


class AgentReport(BaseModel):
    """What a specialist hands back: a summary, findings (facts from tools) and proposed actions."""

    agent: str = ""
    summary: str = ""
    actions: list[Action] = Field(default_factory=list)
    findings: list[str] = Field(default_factory=list)


def merge_reports(left: dict | None, right: dict | None) -> dict:
    """Reducer: dict merge (later agents / re-runs overwrite their own key). A `{"__reset__": True}` key in the
    update drops everything merged so far (used by `sense` at the start of a cycle)."""
    right = dict(right or {})
    if right.pop("__reset__", False):
        return right
    return {**(left or {}), **right}


def replace_agent(agent: str, actions: list[dict]) -> dict:
    """Update value for `proposed` that replaces all earlier actions of `agent`."""
    return {REPLACE_KEY: agent, "actions": list(actions)}


def merge_proposed(left: list | None, right: Any) -> list:
    """Reducer for `proposed`: list -> append; `replace_agent(...)` dict -> replace that agent's actions
    (agent '*' clears everything first). Also used for `executed`."""
    left = list(left or [])
    if right is None:
        return left
    if isinstance(right, dict) and REPLACE_KEY in right:
        who = right[REPLACE_KEY]
        keep = [] if who == "*" else [a for a in left if a.get("agent") != who]
        return keep + list(right.get("actions", []))
    return left + list(right)


class OpsState(TypedDict, total=False):
    run_id: str
    sim_date: str
    db_path: str                        # "" = registry default database
    trigger: dict                       # {"kind": "morning"} | {"kind": "event", "event": {"kind", "params"}}
    context: dict                       # sense output: reservations, events, weather, stock summary
    forecast: dict                      # final forecast numbers from tools (adjustments applied)
    reports: Annotated[dict, merge_reports]       # agent -> AgentReport.model_dump()
    proposed: Annotated[list, merge_proposed]     # Action dicts (agent field set)
    validated: list[dict]               # {idx, action, violations, blocked, needs_approval, decision_id}
    approvals: list[dict]               # chef decisions {id, approved, note}
    executed: Annotated[list, merge_proposed]  # {id, action_type, agent, status, result}
    agents_to_run: list[str]            # supervisor plan (subset of AGENT_ORDER, in order)
    step: int                           # index into agents_to_run of the next specialist
    sup_steps: int                      # supervisor passes used (bounded by MAX_SUPERVISOR_STEPS)
    retries: int                        # constraint-violation retries used (max 1)
    retry_agents: list[str]             # set by constraint_check, consumed by supervisor
    violation_feedback: dict            # agent -> [violation messages] for the retry prompt
    plan_reason: str
