"""Eval set for the LLM review path, built from real (logged) services.

    python -m restaurant_ops.live.llm_eval build            # data/evals/llm_review.jsonl from data/live/*.db
    python -m restaurant_ops.live.llm_eval run [--judge live|mock] [--limit 50]

A case is one Menu / Procurement / Forecasting proposal that a person (or a scripted manager policy, marked as such)
decided on: label "keep" when it was approved, "drop" when it was rejected. `run` asks the reviewer about each case
with the same prompt the live agents use and reports agreement with the manager, how many rejections it would have
caught, latency and cost. The mock judge (keep everything, i.e. the rule-based behaviour) is the baseline a model has
to beat before the pilot widens. Live runs obey the same per-call timeout as the service and stop at the cost cap.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutTimeout
from pathlib import Path

from restaurant_ops.config import settings

REVIEWED = ("menu", "procurement", "forecasting")
EVAL_PATH = settings.DATA_DIR / "evals" / "llm_review.jsonl"


def build(out: Path = EVAL_PATH, live_dir: Path | None = None) -> dict:
    live_dir = live_dir or settings.LIVE_DIR
    cases, seen = [], set()
    for db in sorted(live_dir.glob("svc-*.db")):
        try:
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            rows = c.execute("SELECT payload FROM event_log WHERE type='decision.resolved'").fetchall()
            c.close()
        except sqlite3.Error:
            continue
        for (payload,) in rows:
            d = json.loads(payload)
            if d.get("agent") not in REVIEWED or not d.get("needs_approval"):
                continue
            if d.get("status") not in ("executed", "rejected", "blocked") or d.get("resolved_by") in ("timeout", None):
                continue
            label = "drop" if d["status"] == "rejected" else "keep"
            key = (d["agent"], d["title"], label)
            if key in seen:
                continue
            seen.add(key)
            cases.append({"case_id": f"{db.stem}:{d['id']}", "agent": d["agent"], "action_type": d["action_type"], "title": d["title"],
                          "reason": d.get("reason", ""), "payload": d.get("payload", {}), "label": label, "manager_note": d.get("note", ""),
                          "decided_by": d.get("resolved_by")})
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w") as f:
        for case in cases:
            f.write(json.dumps(case, default=str) + "\n")
    return {"cases": len(cases), "keep": sum(c["label"] == "keep" for c in cases), "drop": sum(c["label"] == "drop" for c in cases),
            "path": str(out)}


def load(path: Path = EVAL_PATH) -> list[dict]:
    with open(path) as f:
        return [json.loads(l) for l in f if l.strip()]


def run(path: Path = EVAL_PATH, judge: str = "mock", limit: int | None = None, reviewer=None) -> dict:
    """`reviewer(agent, situation, proposals)` -> {"judgment", "latency_ms", "tokens_in", "tokens_out"}; default the
    live agents' `_review`. judge='mock' keeps every candidate (the rule-based baseline)."""
    from restaurant_ops.live.agents.base import _review
    from restaurant_ops.live.llm_guard import cost_of

    reviewer = reviewer or _review
    cases = load(path)[: limit or None]
    results, cost, lat, errors = [], 0.0, [], 0
    pool = ThreadPoolExecutor(max_workers=1)
    for case in cases:
        cand = [{"action_type": case["action_type"], "title": case["title"], "payload": case["payload"], "reason": case["reason"]}]
        if judge == "mock":
            keep, why = True, "rule-based baseline keeps every candidate"
        else:
            if cost >= settings.LLM_MAX_COST_PER_SERVICE:
                break
            fut = pool.submit(reviewer, case["agent"], {"eval": True, "note": "historical decision"}, cand)
            t0 = time.perf_counter()
            try:
                r = fut.result(timeout=settings.LLM_TIMEOUT_S)
                keep, why = 0 in (r["judgment"].keep or []), r["judgment"].rationale
                lat.append(r["latency_ms"])
                cost += cost_of(r["tokens_in"], r["tokens_out"])
            except (FutTimeout, Exception) as e:  # noqa: BLE001  a failed call counts as "fall back to rules" (keep)
                errors += 1
                keep, why = True, f"error: {type(e).__name__} -> rule fallback"
                lat.append((time.perf_counter() - t0) * 1000)
        results.append({"case_id": case["case_id"], "label": case["label"], "keep": keep, "agree": (case["label"] == "keep") == keep,
                        "why": why})
    pool.shutdown(wait=False, cancel_futures=True)
    n = len(results)
    drops = [r for r in results if r["label"] == "drop"]
    caught = sum(1 for r in drops if not r["keep"])
    false_drops = sum(1 for r in results if r["label"] == "keep" and not r["keep"])
    lat.sort()
    return {"judge": judge, "cases": n, "agreement_pct": round(100 * sum(r["agree"] for r in results) / n, 1) if n else None,
            "rejections_caught": f"{caught}/{len(drops)}", "false_drops": false_drops, "errors": errors,
            "p50_ms": lat[len(lat) // 2] if lat else None, "p95_ms": lat[int(len(lat) * 0.95)] if lat else None,
            "cost": round(cost, 4), "results": results}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="LLM review eval from logged services")
    ap.add_argument("command", choices=["build", "run"])
    ap.add_argument("--judge", choices=["mock", "live"], default="mock")
    ap.add_argument("--limit", type=int, default=None)
    a = ap.parse_args(argv)
    if a.command == "build":
        print(json.dumps(build(), indent=2))
    else:
        r = run(judge=a.judge, limit=a.limit)
        print(json.dumps({k: v for k, v in r.items() if k != "results"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
