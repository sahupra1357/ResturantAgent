"""Shared runtime of the live agents: tool calls with a trace, proposals, and optional LLM deliberation.

Deterministic rules always produce the candidate proposals (numbers come from tools). In live LLM mode the
Menu, Procurement and Forecasting agents hand their candidates and the tool evidence to a fast model, which may
drop candidates and writes the rationale; it runs on a worker thread, so the floor never waits on it, and any
error or a timeout falls back to the rule-based choice. Mock mode (the default without an API key) skips the model.
"""
from __future__ import annotations

import time
from concurrent.futures import Future
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from restaurant_ops.agents import llm
from restaurant_ops.live import events as E

if TYPE_CHECKING:  # pragma: no cover
    from restaurant_ops.live.service import Service

LLM_TIMEOUT_S = 12.0


class Judgment(BaseModel):
    """Structured output of the LLM review of rule-based candidate actions."""

    keep: list[int] = Field(default_factory=list, description="indices of the candidate actions to submit")
    rationale: str = Field("", description="one or two sentences for the manager, citing the tool numbers")
    confidence: float = Field(0.7, ge=0, le=1)


JUDGE_PROMPT = """You are the {agent} agent of a restaurant operations system during a live dinner service.
Deterministic tools produced the numbers and candidate actions below. You may not change any number.
Decide which candidates to submit (by index) and explain the decision to the manager in at most two sentences,
citing the numbers. Prefer fewer, clearly justified actions. Every action is checked by a constraint checker after you.

Situation (tool outputs):
{situation}

Candidate actions:
{candidates}"""


class LiveAgent:
    name = "agent"
    llm_reviewed = False  # set True on agents whose proposals the LLM reviews in live mode

    def __init__(self, svc: "Service"):
        self.svc = svc

    # ------------------------------------------------------------------ helpers
    def tool(self, trace: list | None, name: str, **kw):
        return self.svc.tools.call(trace, name, **kw)

    def log(self, message: str, **extra) -> None:
        self.svc.bus.publish(E.AGENT_LOG, {"agent": self.name, "message": message, **extra}, source=self.name)

    def proposal(self, action_type: str, payload: dict, title: str, reason: str, trace: list, *, key: str | None = None,
                 confidence: float = 0.85, timeout_default: str = "reject", force: bool = False, est_cost: float = 0.0,
                 context: dict | None = None) -> dict:
        return {"agent": self.name, "action_type": action_type, "payload": payload, "title": title, "reason": reason,
                "trace": trace, "tools": sorted({t["tool"] for t in trace}), "key": key or f"{action_type}:{title}",
                "confidence": round(confidence, 2), "timeout_default": timeout_default, "force": force,
                "est_cost": round(est_cost, 2), "context": context or {}}

    # ------------------------------------------------------------------ submission
    def submit(self, proposals: list[dict], situation: dict | None = None) -> list[dict]:
        """Send proposals to the coordinator, after an async LLM review when live mode is on for this agent."""
        if not proposals:
            return []
        if self.llm_reviewed and not llm.is_mock():
            fut = self.svc.executor.submit(_review, self.name, situation or {}, proposals)
            self.svc.pending_llm.append({"future": fut, "deadline": time.monotonic() + LLM_TIMEOUT_S,
                                         "agent": self, "proposals": proposals})
            return []
        return [self.svc.coordinator.submit(p) for p in proposals]

    def finish_review(self, proposals: list[dict], fut: Future | None) -> list[dict]:
        """Called by the service loop when the LLM review is done (or timed out)."""
        j = None
        if fut is not None and fut.done():
            try:
                j = fut.result()
            except Exception as e:  # noqa: BLE001
                self.log(f"LLM review failed ({type(e).__name__}); using rule-based proposals")
        if j is None:
            for p in proposals:
                p["mode"] = "rule"
            return [self.svc.coordinator.submit(p) for p in proposals]
        keep = [i for i in j.keep if 0 <= i < len(proposals)] or ([] if j.keep == [] else list(range(len(proposals))))
        out = []
        for i, p in enumerate(proposals):
            if i not in keep:
                self.log(f"LLM dropped candidate '{p['title']}': {j.rationale}")
                continue
            p["mode"] = "llm"
            p["reason"] = f"{j.rationale} (rule basis: {p['reason']})"
            p["confidence"] = round(min(p["confidence"], j.confidence) if j.confidence else p["confidence"], 2)
            out.append(self.svc.coordinator.submit(p))
        return out

    # ------------------------------------------------------------------ hooks
    def on_resolved(self, decision: dict) -> None:
        """A decision this agent proposed was approved, rejected (with the manager's reason) or timed out."""

    def on_tick(self, t_min: float) -> None:
        """Called once per sim minute."""


def _review(agent: str, situation: dict, proposals: list[dict]) -> Judgment:
    import json

    from langchain_core.messages import HumanMessage

    cands = [{"i": i, "action_type": p["action_type"], "title": p["title"], "payload": p["payload"], "reason": p["reason"]}
             for i, p in enumerate(proposals)]
    msg = JUDGE_PROMPT.format(agent=agent, situation=json.dumps(situation, default=str)[:3000],
                              candidates=json.dumps(cands, default=str)[:3000])
    return llm.get_llm().with_structured_output(Judgment).invoke([HumanMessage(content=msg)])
