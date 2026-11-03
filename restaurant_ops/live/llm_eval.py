"""Eval set for the LLM review path, built from real (logged) services.

    python -m restaurant_ops.live.llm_eval build            # data/evals/llm_review.jsonl from data/live/*.db
    python -m restaurant_ops.live.llm_eval run [--judge live|mock] [--limit 50]
    python -m restaurant_ops.live.llm_eval smoke            # one real review call with the configured provider

A case is one Menu / Procurement / Forecasting proposal that a person (or a scripted manager policy, marked as such)
decided on: label "keep" when it was approved, "drop" when it was rejected. Each case carries the tool outputs the agent
saw (its logged trace) and the rule's own reason (an earlier LLM rationale is stripped, so no verdict leaks). `run` asks
the reviewer about each case with the same prompt the live agents use, with that trace as the situation, and reports
agreement with the manager (over all cases, errors counted as the rule fallback, and over answered calls only), how many
rejections it would have caught (all, and only those a person made: a scripted policy's reasons are not in the data),
latency and cost. `--out` writes the per-case results. The mock judge (keep everything, i.e. the rule-based behaviour) is the baseline a model has
to beat before the pilot widens. Live runs obey the same per-call timeout as the service and stop at the cost cap.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutTimeout
from pathlib import Path

from restaurant_ops.config import settings

REVIEWED = ("menu", "procurement", "forecasting")
SCRIPTED = ("policy",)  # resolved_by values that are a scripted manager, not a person
_RULE_BASIS = re.compile(r"\(rule basis: (.*)\)\s*$", re.S)


def rule_reason(reason: str) -> str:
    """The rule-based reason without an earlier LLM review's rationale ("<rationale> (rule basis: <reason>)")."""
    m = _RULE_BASIS.search(reason or "")
    return m.group(1) if m else (reason or "")


def situation_of(trace: list | None, now: str | None = None) -> dict:
    """The logged tool calls as the reviewer's situation (what the agent saw when it proposed), with the service clock
    at proposal time so the model does not judge dates against today's."""
    return {**({"service_time": now} if now else {}), "tools": [{"tool": t.get("tool"), "args": t.get("args"), "result": t.get("result")} for t in (trace or [])
                      if isinstance(t, dict)]}
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
                          "reason": rule_reason(d.get("reason", "")), "situation": situation_of(d.get("trace"), d.get("created_sim")), "payload": d.get("payload", {}), "label": label, "manager_note": d.get("note", ""),
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


def run(path: Path = EVAL_PATH, judge: str = "mock", limit: int | None = None, reviewer=None, out: Path | None = None) -> dict:
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
        error = False
        if judge == "mock":
            keep, why = True, "rule-based baseline keeps every candidate"
        else:
            if cost >= settings.LLM_MAX_COST_PER_SERVICE:
                break
            situation = case.get("situation") or {"note": "historical decision, no logged tool outputs"}
            fut = pool.submit(reviewer, case["agent"], situation, cand)
            t0 = time.perf_counter()
            try:
                r = fut.result(timeout=settings.LLM_TIMEOUT_S)
                keep, why = 0 in (r["judgment"].keep or []), r["judgment"].rationale
                lat.append(r["latency_ms"])
                cost += cost_of(r["tokens_in"], r["tokens_out"])
            except (FutTimeout, Exception) as e:  # noqa: BLE001  a failed call counts as "fall back to rules" (keep)
                errors += 1
                error = True
                keep, why = True, f"error: {type(e).__name__} -> rule fallback"
                lat.append((time.perf_counter() - t0) * 1000)
        results.append({"case_id": case["case_id"], "label": case["label"], "keep": keep, "agree": (case["label"] == "keep") == keep,
                        "error": error, "decided_by": case.get("decided_by"), "agent": case["agent"], "title": case["title"],
                        "why": why})
    pool.shutdown(wait=False, cancel_futures=True)
    if out:
        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w") as f:
            for r in results:
                f.write(json.dumps(r, default=str) + "\n")
    n = len(results)
    drops = [r for r in results if r["label"] == "drop"]
    caught = sum(1 for r in drops if not r["keep"])
    human_drops = [r for r in drops if r["decided_by"] not in SCRIPTED]
    answered = [r for r in results if not r["error"]]
    false_drops = sum(1 for r in results if r["label"] == "keep" and not r["keep"])
    lat.sort()
    return {"judge": judge, "cases": n, "agreement_pct": round(100 * sum(r["agree"] for r in results) / n, 1) if n else None,
            "answered_agreement_pct": round(100 * sum(r["agree"] for r in answered) / len(answered), 1) if answered else None,
            "rejections_caught": f"{caught}/{len(drops)}",
            "human_rejections_caught": f"{sum(1 for r in human_drops if not r['keep'])}/{len(human_drops)}", "false_drops": false_drops, "errors": errors,
            "p50_ms": lat[len(lat) // 2] if lat else None, "p95_ms": lat[int(len(lat) * 0.95)] if lat else None,
            "cost": round(cost, 4), "results": results, **({"out": str(out)} if out else {})}


SMOKE_SITUATION = {"salmon_on_hand_kg": 1.2, "salmon_portions_left": 6, "forecast_salmon_orders_tonight": 14,
                   "next_salmon_delivery": "tomorrow 08:00"}
SMOKE_CANDIDATES = [
    {"action_type": "86_dish", "title": "86 Pan-Seared Salmon after 6 more", "payload": {"dish_id": "seared_salmon", "after_portions": 6},
     "reason": "6 portions left vs 14 still forecast tonight; no delivery before tomorrow"},
    {"action_type": "add_special", "title": "Special: Cod Provencal (12 portions)", "payload": {"dish_id": "baked_cod", "portions": 12},
     "reason": "cod has 4.1 kg on hand (22 portions), uses the same station"},
]


def _classify(e: BaseException) -> tuple[str, str]:
    """(kind, detail) of a provider error, from the exception class names (no SDK import needed)."""
    names = {c.__name__ for c in type(e).__mro__}
    detail = f"{type(e).__name__}: {e}"[:400]
    if names & {"APITimeoutError", "TimeoutException", "ReadTimeout", "ConnectTimeout", "TimeoutError"}:
        return "timeout", detail
    if "AuthenticationError" in names or "PermissionDeniedError" in names:
        return "auth", detail
    if "NotFoundError" in names:
        return "not_found", detail
    if "BadRequestError" in names or "UnprocessableEntityError" in names:
        return "rejected", detail
    if "RateLimitError" in names:
        return "rate_limit", detail
    if names & {"APIConnectionError", "ConnectError"}:
        return "connection", detail
    return "error", detail


def _timed(fn, timeout: float) -> dict:
    """Run fn on a worker thread with a hard wait limit (the HTTP client has the same timeout)."""
    pool = ThreadPoolExecutor(max_workers=1)
    t0 = time.perf_counter()
    fut = pool.submit(fn)
    try:
        val = fut.result(timeout=timeout + 2)
        return {"ok": True, "seconds": round(time.perf_counter() - t0, 2), "value": val}
    except FutTimeout:
        return {"ok": False, "seconds": round(time.perf_counter() - t0, 2), "kind": "timeout", "error": f"no answer in {timeout}s"}
    except Exception as e:  # noqa: BLE001
        kind, detail = _classify(e)
        return {"ok": False, "seconds": round(time.perf_counter() - t0, 2), "kind": kind, "error": detail}
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _hint(ping: dict, review: dict | None, timeout: float, effort: str) -> str:
    if ping.get("kind") == "auth":
        return "the API key was refused: check META_API_KEY (or the key of LLM_PROVIDER) in .env"
    if ping.get("kind") == "not_found":
        return "endpoint or model not found: check META_BASE_URL / META_MODEL (LLM_MODEL)"
    if ping.get("kind") == "connection":
        return "cannot reach the endpoint: check META_BASE_URL, network or proxy"
    if ping.get("kind") == "rejected":
        return ("the request was rejected; if it mentions reasoning_effort, try --effort '' (API default) or another "
                "value your model accepts")
    if not ping["ok"]:
        return (f"even a one-word reply took longer than {timeout}s: the model or endpoint is slow right now; measure it "
                f"with --timeout 60, then set LLM_TIMEOUT_S above that (and/or a lower --effort)")
    if review and not review["ok"] and review.get("kind") == "timeout":
        return (f"plain replies are fine ({ping['seconds']}s) but the structured review needs more than {timeout}s at "
                f"reasoning effort '{effort or 'API default'}': try --effort low (or minimal), or measure with --timeout 60 "
                f"and raise LLM_TIMEOUT_S")
    if review and not review["ok"] and review.get("kind") == "rejected":
        return "the structured-output request was rejected (response_format json_schema); the error says which part"
    if review and not review["ok"]:
        return "the review call failed; see review.error"
    return ""


def smoke(timeout: float | None = None, effort: str | None = None) -> dict:
    """One real call through the live review path (`agents/base._review`), preceded by a plain one-word request so a
    failure can be placed: endpoint / key / model speed vs the structured review itself. `timeout` and `effort`
    override LLM_TIMEOUT_S and META_REVIEW_REASONING_EFFORT for this run only."""
    from langchain_core.messages import HumanMessage

    from restaurant_ops.agents import llm
    from restaurant_ops.live.agents.base import _review
    from restaurant_ops.live.llm_guard import cost_of

    timeout = float(timeout or settings.LLM_TIMEOUT_S)
    effort = settings.META_REVIEW_REASONING_EFFORT if effort is None else effort
    meta = settings.LLM_PROVIDER == "meta"
    out = {"provider": llm.describe(), "mode": "mock" if llm.is_mock() else "live",
           "settings": {"base_url": settings.META_BASE_URL if meta else (settings.OPENAI_BASE_URL or None),
                        "timeout_s": timeout, "max_retries": settings.LLM_MAX_RETRIES,
                        "reasoning_effort": (effort or "API default") if meta else None}}
    if llm.is_mock():
        return {**out, "ok": False, "error": "mock mode: set LLM_MODE=live and the provider's API key"}
    try:
        model = llm._build(timeout=timeout, max_retries=settings.LLM_MAX_RETRIES, reasoning_effort=effort)
    except Exception as e:  # noqa: BLE001
        return {**out, "ok": False, "error": f"could not create the client: {type(e).__name__}: {e}"[:400]}

    ping = _timed(lambda: model.invoke([HumanMessage(content="Reply with the single word OK.")]), timeout)
    if ping["ok"]:
        ping["reply"] = str(getattr(ping.pop("value"), "content", ""))[:40]
    out["ping"] = ping
    if not ping["ok"]:
        return {**out, "ok": False, "error": f"ping: {ping['error']}", "hint": _hint(ping, None, timeout, effort)}

    review = _timed(lambda: _review("menu", SMOKE_SITUATION, SMOKE_CANDIDATES, model=model), timeout)
    if not review["ok"]:
        out["review"] = review
        return {**out, "ok": False, "error": f"review: {review['error']}", "hint": _hint(ping, review, timeout, effort)}
    r = review.pop("value")
    j = r["judgment"]
    valid = [i for i in (j.keep or []) if 0 <= i < len(SMOKE_CANDIDATES)]
    out["review"] = {**review, "keep": valid, "ignored_indices": sorted(set(j.keep or []) - set(valid)), "rationale": j.rationale,
                     "confidence": j.confidence, "tokens_in": r["tokens_in"], "tokens_out": r["tokens_out"],
                     "cost": round(cost_of(r["tokens_in"], r["tokens_out"]), 6), "usage_reported": bool(r["tokens_in"] or r["tokens_out"])}
    margin = timeout - review["seconds"]
    out["ok"] = True
    out["note"] = (f"review answered in {review['seconds']}s of the {timeout}s budget" +
                   ("; that is close to the limit, consider a higher LLM_TIMEOUT_S" if margin < timeout * 0.25 else ""))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="LLM review eval from logged services")
    ap.add_argument("command", choices=["build", "run", "smoke"])
    ap.add_argument("--judge", choices=["mock", "live"], default="mock")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--timeout", type=float, default=None, help="smoke: seconds to wait (default LLM_TIMEOUT_S)")
    ap.add_argument("--effort", default=None, help="smoke: Meta reasoning effort to try ('' = API default)")
    ap.add_argument("--out", type=Path, default=None, help="run: write per-case results (jsonl) here")
    a = ap.parse_args(argv)
    if a.command == "smoke":
        r = smoke(a.timeout, a.effort)
        print(json.dumps(r, indent=2))
        return 0 if r["ok"] else 1
    if a.command == "build":
        print(json.dumps(build(), indent=2))
    else:
        r = run(judge=a.judge, limit=a.limit, out=a.out)
        print(json.dumps({k: v for k, v in r.items() if k != "results"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
