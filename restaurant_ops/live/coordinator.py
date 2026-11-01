"""Coordinator: every agent proposal passes through here, and nothing executes without it.

    submit(proposal) -> dedupe / merge -> LangGraph run:  check -> [approval interrupt] -> verify -> execute

- check:    the deterministic constraint checker (core/constraints.py) plus cross-agent conflict rules (e.g. Menu
            wants a salmon special while salmon is short and Procurement has nothing arriving before Friday).
- approval: LangGraph `interrupt()`; the run pauses on the request and resumes with the manager's answer
            (approve, reject with a reason, or edit-and-approve). A timeout resumes it with the proposal's safe
            default (no PO sent, no special published, ...).
- verify:   constraints again on the (possibly edited) action, because the world moved while it waited.
- execute:  the service's handler for the action type; the result goes on the bus as decision.resolved and the
            proposing agent hears about it (a rejection reason lets it re-plan).
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from restaurant_ops.agents import decision_log as DL
from restaurant_ops.config import settings
from restaurant_ops.core import constraints as C
from restaurant_ops.data import catalog as cat
from restaurant_ops.live import events as E
from restaurant_ops.tools import registry as R

if TYPE_CHECKING:  # pragma: no cover
    from restaurant_ops.live.service import Service

SUPPRESS_MIN = 20.0  # a resolved proposal with the same key is not re-raised within this many sim minutes
DB_STATUS = {"pending": "pending_approval", "executed": "executed", "approved": "approved", "rejected": "rejected",
             "blocked": "blocked", "timeout": "rejected", "failed": "rejected", "checking": "proposed"}


class DState(TypedDict, total=False):
    decision: dict
    violations: list
    blocked: bool
    approved: bool
    note: str
    result: dict


class Coordinator:
    def __init__(self, svc: "Service"):
        self.svc = svc
        self.decisions: dict[int, dict] = {}
        self.open_by_key: dict[str, int] = {}
        self.resolved_by_key: dict[str, dict] = {}
        self.handlers: dict[str, Any] = {}
        self.graph = self._build()

    # ------------------------------------------------------------------ approval policy (live service)
    def needs_approval(self, a: dict) -> bool:
        t, p = a["action_type"], a["payload"]
        if t in ("add_special", "86_dish", "allergen_swap", "comp", "substitute_ingredient"):
            return True
        if t in ("place_po", "substitute_supplier"):
            return C.total_of(a) > settings.LIVE_PO_APPROVAL_THRESHOLD
        if t in ("discard_lots", "discard_lot"):
            return float(p.get("value") or a.get("est_cost") or 0) > settings.LIVE_DISCARD_APPROVAL_VALUE
        if t == "throttle_online":
            return p.get("mode") != "open"
        return False

    # ------------------------------------------------------------------ conflicts between agents
    def conflicts(self, a: dict) -> list[dict]:
        svc, t, p = self.svc, a["action_type"], a["payload"]
        out = []
        if t == "add_special":
            d = p.get("dish_id")
            if d in svc.menu.eighty_sixed or d in svc.menu.limits:
                out.append({"code": "conflict_86", "severity": "block",
                            "message": f"{cat.DISHES_BY_ID[d].name} is being 86'd tonight; it cannot also be a special"})
            pend86 = self.open_by_key.get(f"86:{d}")
            if pend86:
                out.append({"code": "conflict_86", "severity": "block", "message": f"an 86 of this dish is awaiting approval (#{pend86})"})
            recipe = set(cat.DISHES_BY_ID[d].recipe) if d in cat.DISHES_BY_ID else set()
            for ing in sorted(recipe & set(svc.shortages)):
                s = svc.shortages[ing]
                nxt = svc.next_delivery(ing)
                out.append({"code": "conflict_supply", "severity": "block",
                            "message": f"Menu wants a {cat.DISHES_BY_ID[d].name} special but {cat.INGREDIENTS_BY_ID[ing].name.lower()} "
                                       f"is short ({s['supplier_id']} at {s['fill_rate']:.0%}) and Procurement has no full delivery "
                                       f"until {nxt or 'after ' + s['until']}"})
            for ing in sorted(recipe & set(svc.quarantined_ingredients())):
                out.append({"code": "conflict_quarantine", "severity": "block",
                            "message": f"{cat.INGREDIENTS_BY_ID[ing].name} is quarantined"})
        if t == "throttle_online" and p.get("mode") == svc.online.mode:
            out.append({"code": "no_change", "severity": "block", "message": f"online ordering is already '{svc.online.mode}'"})
        if t == "86_dish" and p.get("dish_id") in svc.menu.specials:
            out.append({"code": "special_removed", "severity": "warn", "message": "this dish is tonight's special; the special ends"})
        return out

    def _check(self, a: dict) -> list[dict]:
        self.svc.flush()  # the checker reads through its own connection: make current stock visible
        viol = R.check_action(action={"action_type": a["action_type"], "payload": a["payload"], "agent": a["agent"],
                                      "est_cost": a.get("est_cost", 0)})
        if a["action_type"] == "86_dish":  # a late-service 86 of a dish with stock is fine; drop the batch-planning warning
            viol = [v for v in viol if v["code"] != "unnecessary_86"]
        return viol + self.conflicts(a)

    # ------------------------------------------------------------------ graph
    def _build(self):
        def check(state: DState) -> dict:
            viol = self._check(state["decision"])
            return {"violations": viol, "blocked": any(v["severity"] == "block" for v in viol)}

        def route_check(state: DState) -> str:
            if state["blocked"]:
                return END
            return "approval" if state["decision"]["needs_approval"] else "execute"

        def approval(state: DState) -> dict:
            ans = interrupt({"decision_id": state["decision"]["id"]})  # no side effects before this line
            d = dict(state["decision"])
            if ans.get("payload"):
                d["payload"] = {**d["payload"], **ans["payload"]}
                d["edited"] = True
            return {"approved": bool(ans.get("approved")), "note": ans.get("note", ""), "decision": d}

        def route_approval(state: DState) -> str:
            return "verify" if state.get("approved") else END

        def verify(state: DState) -> dict:
            viol = self._check(state["decision"])
            return {"violations": viol, "blocked": any(v["severity"] == "block" for v in viol)}

        def route_verify(state: DState) -> str:
            return END if state["blocked"] else "execute"

        def execute(state: DState) -> dict:
            d = state["decision"]
            fn = self.handlers.get(d["action_type"])
            try:
                res = fn(d) if fn else {"recorded": True}
            except Exception as e:  # noqa: BLE001
                res = {"error": f"{type(e).__name__}: {e}"}
            return {"result": res}

        g = StateGraph(DState)
        g.add_node("check", check)
        g.add_node("approval", approval)
        g.add_node("verify", verify)
        g.add_node("execute", execute)
        g.add_edge(START, "check")
        g.add_conditional_edges("check", route_check, ["approval", "execute", END])
        g.add_conditional_edges("approval", route_approval, ["verify", END])
        g.add_conditional_edges("verify", route_verify, ["execute", END])
        g.add_edge("execute", END)
        return g.compile(checkpointer=InMemorySaver())

    def _cfg(self, did: int) -> dict:
        return {"configurable": {"thread_id": f"{self.svc.service_id}:{did}"}}

    # ------------------------------------------------------------------ public API
    def submit(self, prop: dict) -> dict:
        svc = self.svc
        key = prop.get("key") or f"{prop['action_type']}:{prop['title']}"
        if key in self.open_by_key:
            d = self.decisions[self.open_by_key[key]]
            d["merged"] = d.get("merged", 0) + 1
            svc.bus.publish(E.AGENT_LOG, {"agent": "coordinator", "message": f"merged duplicate proposal into #{d['id']}: {prop['title']}"},
                            source="coordinator")
            return d
        prev = self.resolved_by_key.get(key)
        if prev and not prop.get("force") and svc.clock.t / 60 - prev["t_min"] < SUPPRESS_MIN:
            return prev["decision"]
        d = {k: prop.get(k) for k in ("agent", "action_type", "payload", "title", "reason", "trace", "tools", "confidence",
                                      "timeout_default", "est_cost", "context")}
        d.update(key=key, mode=prop.get("mode", "rule"), needs_approval=self.needs_approval(prop), status="checking",
                 violations=[], created_sim=svc.clock.iso(), created_t=svc.clock.t, resolved_sim=None, note="", result=None,
                 merged=0, edited=False, timeout_s=settings.APPROVAL_TIMEOUT_S if self.needs_approval(prop) else 0)
        svc.flush()
        d["id"] = DL.log_decision(svc.service_id, svc.date.isoformat(), d["agent"], d["action_type"], d["payload"],
                                  d["reason"], status="proposed")
        self.decisions[d["id"]] = d
        out = self.graph.invoke({"decision": d}, self._cfg(d["id"]))
        d["violations"] = out.get("violations", [])
        snap = self.graph.get_state(self._cfg(d["id"]))
        if snap.next:  # paused at the approval interrupt
            d["status"] = "pending"
            d["remaining_s"] = d["timeout_s"]
            self.open_by_key[key] = d["id"]
            self._db(d)
            svc.metrics.decisions_approval += 1
            svc.bus.publish(E.DECISION_PROPOSED, self.view(d), source="coordinator")
            return d
        svc.bus.publish(E.DECISION_PROPOSED, self.view({**d, "status": "auto"}), source="coordinator")
        if out.get("blocked"):
            self._finish(d, "blocked", out, note="")
        else:
            svc.metrics.decisions_auto += 1
            self._finish(d, "executed" if "error" not in (out.get("result") or {}) else "failed", out, note="auto-executed")
        return d

    def resolve(self, decision_id: int, approved: bool, note: str = "", payload: dict | None = None, by: str = "manager") -> dict:
        d = self.decisions.get(int(decision_id))
        if d is None or d["status"] != "pending":
            raise KeyError(f"decision {decision_id} is not awaiting approval")
        out = self.graph.invoke(Command(resume={"approved": approved, "note": note, "payload": payload or {}}), self._cfg(d["id"]))
        d.update(out.get("decision") or {})
        if by == "timeout":
            status = "timeout"
        elif not approved:
            status = "rejected"
        elif out.get("blocked"):
            status = "blocked"
        else:
            status = "executed" if "error" not in (out.get("result") or {}) else "failed"
        self._finish(d, status, out, note=note or ("approved" if approved else "rejected"), approved=approved, by=by)
        return d

    def tick(self, wall_dt: float) -> None:
        """Count down approval timeouts (wall seconds while the clock runs); expired -> the safe default."""
        for d in list(self.decisions.values()):
            if d["status"] != "pending":
                continue
            d["remaining_s"] = round(d.get("remaining_s", d["timeout_s"]) - wall_dt, 2)
            if d["remaining_s"] <= 0:
                ok = d.get("timeout_default") == "approve"
                self.resolve(d["id"], ok, f"timeout after {d['timeout_s']:.0f}s: safe default ({'approve' if ok else 'no action'})",
                             by="timeout")
            policy = self.svc.auto_policy
            if policy and d["status"] == "pending":
                ans = policy(self.view(d))
                if ans is not None:
                    self.resolve(d["id"], bool(ans[0]), ans[1] if len(ans) > 1 else "", ans[2] if len(ans) > 2 else None,
                                 by="policy")

    # ------------------------------------------------------------------ internals
    def _finish(self, d: dict, status: str, out: dict, note: str, approved: bool | None = None, by: str = "") -> None:
        svc = self.svc
        d["status"] = status
        d["violations"] = out.get("violations", d.get("violations", []))
        d["result"] = out.get("result")
        d["note"] = note
        d["resolved_by"] = by or ("coordinator" if status in ("blocked", "executed") and not d["needs_approval"] else by)
        d["resolved_sim"] = svc.clock.iso()
        self.open_by_key.pop(d["key"], None)
        self.resolved_by_key[d["key"]] = {"t_min": svc.clock.t / 60, "decision": d}
        self._db(d)
        if status == "blocked":
            svc.metrics.decisions_blocked += 1
        svc.bus.publish(E.DECISION_RESOLVED, self.view(d), source="coordinator")
        agent = svc.agents.get(d["agent"])
        if agent is not None:
            agent.on_resolved(d)

    def _db(self, d: dict) -> None:
        self.svc.flush()
        DL.update_status(d["id"], DB_STATUS.get(d["status"], "proposed"), note=d.get("note") or None, violations=d.get("violations"))
        if d.get("edited"):
            with R.connection() as c:
                c.execute("UPDATE decisions SET payload=? WHERE id=?", (json.dumps(d["payload"], default=str), d["id"]))
                c.commit()

    def view(self, d: dict) -> dict:
        keys = ("id", "agent", "action_type", "title", "payload", "reason", "tools", "trace", "confidence", "needs_approval",
                "status", "violations", "timeout_s", "remaining_s", "timeout_default", "created_sim", "resolved_sim", "note",
                "mode", "result", "merged", "edited", "est_cost", "resolved_by", "key")
        return {k: d.get(k) for k in keys}

    def pending(self) -> list[dict]:
        return [self.view(d) for d in self.decisions.values() if d["status"] == "pending"]
