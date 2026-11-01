"""Agent runtime shared by the five specialists.

- `make_agent(name, tools, prompt)` builds a LangGraph ReAct agent (`create_react_agent`) that finishes with a
  structured `AgentReport` (a separate `with_structured_output` call made by the prebuilt after the tool loop).
  The step cap is `settings.MAX_AGENT_STEPS` tool-loop iterations, enforced through `recursion_limit`.
- `run_specialist(...)` is what the graph nodes call: it traces agent_start / agent_end, runs the live agent or
  the mock policy, and never raises (errors become an AgentReport with the error in `findings`).
- Context helpers turn state into prompt text (trigger, upstream reports, forecast, feedback).
"""
from __future__ import annotations

import json
from typing import Callable

from langchain_core.messages import HumanMessage, SystemMessage

from restaurant_ops.agents import decision_log as DL
from restaurant_ops.agents import llm, prompts
from restaurant_ops.agents.state import AGENT_ORDER, Action, AgentReport
from restaurant_ops.config import settings


# =========================================================================== prompt context


def trigger_text(trigger: dict) -> str:
    """One-line description of what started this cycle."""
    if (trigger or {}).get("kind") == "event":
        ev = trigger.get("event", {})
        return f"EVENT {ev.get('kind', 'unknown')}: {json.dumps(ev.get('params', {}), default=str)}"
    return "Morning planning run (no disruption)."


def event_of(trigger: dict) -> tuple[str | None, dict]:
    """(event kind, params) of an event trigger, else (None, {}). Tolerates flat event dicts."""
    if (trigger or {}).get("kind") != "event":
        return None, {}
    ev = trigger.get("event") or {}
    params = ev.get("params")
    if params is None:
        params = {k: v for k, v in ev.items() if k not in ("kind", "day_offset")}
    return ev.get("kind"), dict(params or {})


def summarise_reports(reports: dict, upto: str | None = None) -> str:
    """Compact text of the reports of agents that run before `upto` (all when None)."""
    names = AGENT_ORDER if upto is None else AGENT_ORDER[:AGENT_ORDER.index(upto)]
    lines = []
    for n in names:
        r = (reports or {}).get(n)
        if not r:
            continue
        lines.append(f"[{n}] {r.get('summary', '')}")
        for f in r.get("findings", [])[:8]:
            lines.append(f"  - {f}")
        for a in r.get("actions", [])[:10]:
            lines.append(f"  * proposes {a['action_type']} {json.dumps(a['payload'], default=str)[:220]}")
    return "\n".join(lines) or "None yet."


def forecast_text(forecast: dict | None) -> str:
    return json.dumps(forecast, default=str)[:2500] if forecast else "Not available."


def build_context(state: dict, agent: str) -> dict:
    """The values every specialist needs: sim date, trigger text, upstream summary, forecast, feedback, violations."""
    viol = (state.get("violation_feedback") or {}).get(agent) or []
    return {
        "sim_date": state["sim_date"], "trigger": state.get("trigger") or {"kind": "morning"},
        "reports": state.get("reports") or {}, "forecast": state.get("forecast") or {},
        "feedback": DL.recent_feedback(8), "violations": list(viol),
    }


def human_message(ctx: dict, task: str) -> str:
    """The human turn: date, trigger and the task. Upstream/feedback are in the system prompt."""
    from restaurant_ops.core.util import day_label

    msg = f"Date: {day_label(ctx['sim_date'])}. Trigger: {trigger_text(ctx['trigger'])}\n{task}"
    if ctx.get("violations"):
        msg += "\nYour previous proposal was blocked: " + " | ".join(ctx["violations"]) + "\nCorrect it or drop it."
    return msg


# =========================================================================== live agent


class Agent:
    """A ReAct agent for one specialist. `invoke(ctx, task)` returns an AgentReport."""

    def __init__(self, name: str, tools: list, prompt: str):
        from langgraph.prebuilt import create_react_agent  # deprecated alias of langchain.agents.create_agent, still shipped

        self.name, self.prompt = name, prompt
        self.graph = create_react_agent(llm.get_llm(), tools, response_format=AgentReport, name=name)

    def invoke(self, ctx: dict, task: str) -> AgentReport:
        system = prompts.fill(self.prompt, sim_date=ctx["sim_date"], trigger=trigger_text(ctx["trigger"]),
                              upstream=summarise_reports(ctx["reports"], upto=self.name if self.name in AGENT_ORDER else None),
                              forecast=forecast_text(ctx["forecast"]),
                              feedback=prompts_feedback(ctx, self.name), violations="\n".join(ctx.get("violations", [])))
        # each ReAct iteration = model node + tool node (2 graph steps); +3 for entry and the structured-output call
        limit = settings.MAX_AGENT_STEPS * 2 + 3
        out = self.graph.invoke({"messages": [SystemMessage(content=system), HumanMessage(content=human_message(ctx, task))]},
                                {"recursion_limit": limit})
        rep = out.get("structured_response")
        if isinstance(rep, dict):
            rep = AgentReport(**rep)
        if (rep is None or is_empty(rep)) and out.get("messages"):
            rep = self._reask(out["messages"]) or rep  # the model sometimes returns a blank report after its tools
        if rep is None:
            last = out["messages"][-1].content if out.get("messages") else ""
            rep = AgentReport(summary=str(last)[:500], findings=["agent returned no structured report"])
        return rep

    def _reask(self, messages: list) -> AgentReport | None:
        """One more structured-output call over the same conversation, asking for the report explicitly."""
        try:
            rep = llm.get_llm().with_structured_output(AgentReport).invoke(
                list(messages) + [HumanMessage(content=REASK)])
        except Exception:  # noqa: BLE001  keep the original (empty) report
            return None
        rep = AgentReport(**rep) if isinstance(rep, dict) else rep
        return None if rep is None or is_empty(rep) else rep


REASK = ("Your final report came back empty. Using the tool results above, return the complete report now: "
         "agent (your name), a one-paragraph summary, findings (the numbers you relied on) and actions (every action "
         "you propose, with its full payload; an empty list only if nothing should change).")
EMPTY_NOTE = "empty report (re-asked once); earlier proposals kept"


def is_empty(rep: AgentReport) -> bool:
    """A report with no summary, findings or actions: the model answered with blanks."""
    return not (rep.summary or "").strip() and not rep.actions and not rep.findings


def prompts_feedback(ctx: dict, agent: str) -> str:
    return DL.format_feedback(ctx.get("feedback") or [], agent)


def make_agent(name: str, tools: list, prompt: str) -> Agent:
    """Build a specialist agent: `create_react_agent` with its toolset, step cap and AgentReport output."""
    return Agent(name, tools, prompt)


# =========================================================================== node runner


def _normalise(name: str, rep: AgentReport) -> AgentReport:
    rep.agent = name
    for a in rep.actions:
        a.agent = name
    return rep


def error_report(name: str, err: Exception) -> AgentReport:
    return AgentReport(agent=name, summary=f"{name} could not complete: {type(err).__name__}",
                       findings=[f"error: {type(err).__name__}: {err}"], actions=[])


def run_specialist(name: str, state: dict, task: str, mock_policy: Callable[[dict], AgentReport],
                   tools_key: str | None = None) -> AgentReport:
    """Run one specialist (live agent or mock policy) with tracing; never raises."""
    ctx = build_context(state, name)
    run_id, sim = state["run_id"], state["sim_date"]
    mode = "mock" if llm.is_mock() else "live"
    DL.write_trace(run_id, sim, name, "agent_start", {"mode": mode, "trigger": ctx["trigger"], "retry": bool(ctx["violations"])})
    with DL.node_context(run_id, name, sim):
        try:
            if llm.is_mock():
                rep = mock_policy(ctx)
            else:
                from restaurant_ops.tools.registry import TOOLSETS

                rep = make_agent(name, TOOLSETS[tools_key or name], prompts.PROMPTS[name]).invoke(ctx, task)
            rep = _normalise(name, rep)
        except Exception as e:  # noqa: BLE001  model / tool failures must not kill the cycle
            rep = _normalise(name, error_report(name, e))
    if is_empty(rep):  # still blank after the re-ask: say so in the trace; report_update keeps prior proposals
        rep.summary, rep.findings = f"{name} returned an empty report", [EMPTY_NOTE]
    DL.write_trace(run_id, sim, name, "agent_end", {"summary": rep.summary, "n_actions": len(rep.actions),
                                                    "findings": rep.findings[:6],
                                                    "actions": [{"type": a.action_type, "rationale": a.rationale[:160]} for a in rep.actions]})
    return rep


def actions_as_dicts(rep: AgentReport) -> list[dict]:
    return [a.model_dump() for a in rep.actions]


# =========================================================================== node helpers


def bind(state: dict) -> None:
    """Point the tools at the run's database / sim date and attribute tool calls to this run.
    Called at the start of every graph node (also after a resume in a fresh process)."""
    from restaurant_ops.tools import registry as R

    db = state.get("db_path") or None
    if db and str(R._DB_PATH or "") != str(db):
        R.set_db_path(db)
    R.set_sim_date(state["sim_date"])
    DL.install_trace_hook()
    DL.set_run_context(state["run_id"], "graph", state["sim_date"])


def report_update(state: dict, name: str, rep: AgentReport) -> dict:
    """Partial state for a finished specialist: its report, its proposals (replacing earlier ones), cursor + 1."""
    from restaurant_ops.agents.state import replace_agent

    step = int(state.get("step", 0)) + 1
    prev = (state.get("reports") or {}).get(name)
    if rep.findings == [EMPTY_NOTE] and prev:
        # an empty re-run (e.g. a constraint retry) must not wipe what this agent proposed before
        return {"reports": {name: {**prev, "findings": [*prev.get("findings", []), EMPTY_NOTE]}}, "step": step}
    return {"reports": {name: rep.model_dump()}, "proposed": replace_agent(name, actions_as_dicts(rep)), "step": step}
