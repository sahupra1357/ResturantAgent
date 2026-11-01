"""Simulation driver and CLI.

    python -m restaurant_ops.sim.run --days 7 --policy agent|baseline|both \\
        --scenario salmon_shortfall|private_booking|cooler_failure|heatwave|all|none \\
        [--auto-approve] [--run-id X] [--seed N]

Each policy runs on its own copy of the seeded database (`data/runs/{run_id}_{policy}.db`), so both face the identical
world. Per day: morning cycle (agent: supervisor graph; baseline: par-level orders) -> disruptions due that day are injected
-> event cycle for the agent (chef prompted in the terminal for items needing approval unless --auto-approve) -> the world
advances one day. Public API: `run_simulation(...)` (whole run) and `SimSession` (step-wise, for the Streamlit UI).
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Callable

from restaurant_ops.config import settings
from restaurant_ops.db.connection import copy_db
from restaurant_ops.sim import disruptions as D
from restaurant_ops.sim import metrics
from restaurant_ops.sim.baseline import BaselinePolicy
from restaurant_ops.sim.simulator import World

RUNS_DIR = settings.DATA_DIR / "runs"
PromptFn = Callable[[list[dict]], list[dict]]  # pending items -> [{"id", "approved", "note"}]


def run_db_path(run_id: str, policy: str) -> Path:
    return RUNS_DIR / f"{run_id}_{policy}.db"


def ensure_models(source_db: str | Path | None = None) -> dict:
    """Train (and pickle) the forecasting models on the seeded DB if they are not there yet."""
    from restaurant_ops.core import forecasting
    from restaurant_ops.db.connection import get_conn

    conn = get_conn(source_db)
    try:
        return forecasting.load_models(conn)["metrics"]
    finally:
        conn.close()


def cli_prompt(pending: list[dict]) -> list[dict]:
    """Ask the chef in the terminal: y/n and a note for each pending item."""
    out = []
    print("\n  --- chef approval needed ---")
    for p in pending:
        payload = json.dumps(p["payload"], default=str)
        print(f"  [{p['id']}] {p['agent']} -> {p['action_type']} (est ${p.get('est_cost', 0):,.0f})")
        print(f"       {payload[:300]}")
        print(f"       why: {p.get('rationale', '')[:300]}")
        try:
            ok = input("       approve? [y/n] ").strip().lower().startswith("y")
            note = input("       note (optional): ").strip()
        except EOFError:
            ok, note = False, "no input"
        out.append({"id": p["id"], "approved": ok, "note": note or ("approved" if ok else "rejected")})
    return out


class SimSession:
    """One policy's run, advanced step by step.

        s = SimSession("agent", "all", run_id="demo")
        s.start()
        while not s.finished:
            r = s.step_day()                      # runs cycles until the day is done or the chef must decide
            while r["status"] == "awaiting_approval":
                r = s.resume([{"id": p["id"], "approved": True, "note": ""} for p in s.pending_approvals()])
        s.kpis()

    `step_day()` returns {"status": "day_complete"|"awaiting_approval"|"finished", "date", "cycles": [...],
    "pending": [...], "summary": <World.advance_day summary, when complete>}. Several sessions can coexist (every call
    re-points the tools at this session's DB and date).
    """

    def __init__(self, policy: str, scenario: str = "none", run_id: str | None = None, seed: int | None = None,
                 days: int = settings.DEMO_DAYS, auto_approve: bool = False, source_db: str | Path | None = None):
        if policy not in ("agent", "baseline"):
            raise ValueError("policy must be 'agent' or 'baseline'")
        self.policy, self.scenario, self.days = policy, scenario, int(days)
        self.run_id = run_id or f"run{datetime.now():%m%d%H%M%S}"
        self.seed = settings.SEED if seed is None else int(seed)
        self.auto_approve = auto_approve
        self.source_db = Path(source_db) if source_db else settings.DB_PATH
        self.db_path = run_db_path(self.run_id, policy)
        self.world: World | None = None
        self.baseline = BaselinePolicy()
        self.daily: list[dict] = []
        self.day_index = 0
        self._queue: list[dict] | None = None
        self._cycles: list[dict] = []
        self._awaiting: dict | None = None

    # ------------------------------------------------------------------ lifecycle

    @property
    def finished(self) -> bool:
        return self.day_index >= self.days

    @property
    def today(self) -> str:
        return self.world.today.isoformat() if self.world else settings.SIM_START.isoformat()

    def start(self) -> "SimSession":
        """Copy the seeded DB, make sure the models exist, build the World and queue the scenario."""
        if not self.source_db.exists():
            raise FileNotFoundError(f"{self.source_db} not found; run `python -m restaurant_ops.data.seed` first")
        ensure_models(self.source_db)
        copy_db(self.source_db, self.db_path)
        self.world = World(self.db_path, self.run_id, self.policy, self.seed)
        self.world.load_scenario(self.scenario)
        self._activate()
        return self

    def _activate(self) -> None:
        """Point the tools registry at this session's DB and date."""
        from restaurant_ops.tools import registry as R

        R.set_db_path(self.db_path)
        R.set_sim_date(self.world.today)

    def close(self) -> None:
        if self.world:
            self.world.close()
            self.world = None

    # ------------------------------------------------------------------ stepping

    def pending_approvals(self) -> list[dict]:
        """Items waiting for the chef: [{id, agent, action_type, payload, rationale, est_cost, warnings}]."""
        return list(self._awaiting["pending"]) if self._awaiting else []

    def step_day(self) -> dict:
        """Run the current day's remaining phases. Pauses (status 'awaiting_approval') when the chef must decide."""
        if self.world is None:
            self.start()
        if self.finished:
            return {"status": "finished", "date": self.today, "cycles": [], "pending": []}
        if self._awaiting:
            return self._status("awaiting_approval")
        if self._queue is None:
            self._queue, self._cycles = [{"type": "morning"}], []
        return self._run_queue()

    def resume(self, decisions: list[dict]) -> dict:
        """Give the chef's decisions [{"id","approved","note"}] for the pending items and carry on with the day."""
        if not self._awaiting:
            return self.step_day()
        from restaurant_ops.agents.supervisor import resume_cycle

        self._activate()
        aw, self._awaiting = self._awaiting, None
        res = resume_cycle(aw["thread_id"], decisions, db_path=str(self.db_path))
        if res.get("status") == "awaiting_approval":
            self._awaiting = {"thread_id": res["thread_id"], "pending": res["pending"], "item": aw["item"]}
            return self._status("awaiting_approval")
        self._record_cycle(aw["item"], res)
        self._queue.pop(0)
        if aw["item"]["type"] == "morning":
            self._after_morning()
        return self._run_queue()

    def run_day(self, prompt: PromptFn | None = None) -> dict:
        """Step one whole day, answering approval requests with `prompt` (default: auto-approve everything)."""
        r = self.step_day()
        while r["status"] == "awaiting_approval":
            pend = self.pending_approvals()
            r = self.resume(prompt(pend) if prompt else [{"id": p["id"], "approved": True, "note": "auto-approved"} for p in pend])
        return r

    def run_all(self, prompt: PromptFn | None = None, on_day: Callable[[dict], None] | None = None) -> list[dict]:
        """Run every remaining day."""
        while not self.finished:
            r = self.run_day(prompt)
            if on_day:
                on_day(r)
        return self.daily

    def _run_queue(self) -> dict:
        self._activate()
        w = self.world
        while self._queue:
            item = self._queue[0]
            if item["type"] == "morning":
                if self.policy == "agent":
                    res = self._cycle({"kind": "morning"})
                    if res.get("status") == "awaiting_approval":
                        self._awaiting = {"thread_id": res["thread_id"], "pending": res["pending"], "item": item}
                        return self._status("awaiting_approval")
                    self._record_cycle(item, res)
                else:
                    plan = self.baseline.plan(w)
                    self._cycles.append({"cycle": "morning", "status": "completed", "orders": len(plan["orders"]),
                                         "proposed": len(plan["orders"]), "executed": len(plan["orders"])})
                self._queue.pop(0)
                self._after_morning()
            elif item["type"] == "event":
                res = self._cycle(item["trigger"])
                if res.get("status") == "awaiting_approval":
                    self._awaiting = {"thread_id": res["thread_id"], "pending": res["pending"], "item": item}
                    return self._status("awaiting_approval")
                self._record_cycle(item, res)
                self._queue.pop(0)
        date_str = w.today.isoformat()
        summary = w.advance_day()
        summary["cycles"] = self._cycles
        self.daily.append(summary)
        self.day_index += 1
        self._queue, cycles, self._cycles = None, self._cycles, []
        if not self.finished:
            self._activate()
        return {"status": "day_complete", "date": date_str, "cycles": cycles, "pending": [], "summary": summary}

    def _after_morning(self) -> None:
        """Disruptions land after the morning plan: apply them to the world and queue the agent's event cycles."""
        for dis in self.world.due_events():
            out = self.world.inject(dis)
            self._cycles.append({"cycle": f"inject:{dis['kind']}", "status": "injected", "result": out["result"]})
            if self.policy == "agent":
                self._queue.append({"type": "event", "kind": dis["kind"], "trigger": out["trigger"]})
        self._activate()

    def _cycle(self, trigger: dict) -> dict:
        from restaurant_ops.agents.supervisor import run_cycle

        self._activate()
        return run_cycle(self.run_id, self.world.today.isoformat(), trigger, db_path=str(self.db_path),
                         auto_approve=self.auto_approve)

    def _record_cycle(self, item: dict, res: dict) -> None:
        executed = [e for e in res.get("executed") or [] if e.get("status") == "executed"]
        self._cycles.append({"cycle": item.get("kind", item["type"]), "status": res.get("status"),
                             "proposed": len(res.get("proposed") or []), "executed": len(executed),
                             "approvals": len(res.get("approvals") or []),
                             "rejected": sum(1 for a in res.get("approvals") or [] if not a.get("approved")),
                             "agents": list(res.get("agents_to_run") or [])})

    def _status(self, status: str) -> dict:
        return {"status": status, "date": self.today, "cycles": list(self._cycles), "pending": self.pending_approvals()}

    # ------------------------------------------------------------------ results

    def kpis(self) -> dict:
        self.world.conn.commit()
        return metrics.kpis(self.db_path, self.run_id, self.policy)


# =========================================================================== whole run


def format_day(policy: str, s: dict) -> str:
    cyc = ", ".join(f"{c['cycle']}({c.get('executed', c.get('orders', ''))})" for c in s.get("cycles", []) if c["status"] != "injected")
    inj = ", ".join(c["cycle"].split(":")[1] for c in s.get("cycles", []) if c["status"] == "injected")
    return (f"{s['weekday']} {s['date']} [{policy:<8}] covers {s['covers_served']:>3}/{s['covers_demand']:<3} "
            f"rev ${s['revenue']:>8,.0f}  food ${s['food_cost']:>7,.0f}  waste ${s['waste_value']:>6,.0f}  "
            f"stockouts {s['stockouts']:>2}  86s {s['dishes_86d']}  appr {s['approvals']}"
            + (f"  | disruptions: {inj}" if inj else "") + (f"  | actions: {cyc}" if cyc else ""))


def run_simulation(days: int = settings.DEMO_DAYS, policy: str = "both", scenario: str = "none", auto_approve: bool = False,
                   run_id: str | None = None, seed: int | None = None, prompt: PromptFn | None = None,
                   verbose: bool = True, source_db: str | Path | None = None) -> dict:
    """Run one or both policies for `days` days.

    Returns {"run_id", "scenario", "days", "results": {policy: {"db_path", "kpis", "daily"}}, "comparison": compare(...) | None}.
    `prompt` answers approval requests (default: terminal prompt, or auto-approve when auto_approve=True).
    """
    run_id = run_id or f"run{datetime.now():%m%d%H%M%S}"
    policies = ["agent", "baseline"] if policy == "both" else [policy]
    prompt = None if auto_approve else (prompt or cli_prompt)
    results: dict[str, dict] = {}
    for pol in policies:
        if verbose:
            print(f"\n=== {pol} | scenario={scenario} | run_id={run_id} | seed={settings.SEED if seed is None else seed} ===")
        s = SimSession(pol, scenario, run_id, seed, days, auto_approve=auto_approve, source_db=source_db).start()
        try:
            s.run_all(prompt, on_day=(lambda r, p=pol: print(format_day(p, r["summary"]))) if verbose else None)
            results[pol] = {"db_path": str(s.db_path), "kpis": s.kpis(), "daily": s.daily}
        finally:
            s.close()
    comparison = None
    if len(policies) == 2:
        comparison = metrics.compare(results["agent"]["db_path"], results["baseline"]["db_path"], run_id)
        if verbose:
            print("\n=== KPI comparison (agent vs baseline) ===")
            print(metrics.format_comparison(comparison))
    elif verbose:
        k = results[policies[0]]["kpis"]
        print("\n=== KPIs ===")
        print(json.dumps({x: k[x] for x in metrics.KEYS if x in k}, indent=2))
    return {"run_id": run_id, "scenario": scenario, "days": days, "results": results, "comparison": comparison}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run the restaurant ops simulation")
    ap.add_argument("--days", type=int, default=settings.DEMO_DAYS)
    ap.add_argument("--policy", choices=["agent", "baseline", "both"], default="both")
    ap.add_argument("--scenario", choices=list(D.SCENARIO_NAMES), default="none")
    ap.add_argument("--auto-approve", action="store_true", help="approve every chef-approval item automatically")
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--db", type=Path, default=None, help="seeded database to copy (default: settings.DB_PATH)")
    a = ap.parse_args(argv)
    run_simulation(a.days, a.policy, a.scenario, a.auto_approve, a.run_id, a.seed, source_db=a.db)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
