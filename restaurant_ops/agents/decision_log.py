"""Decision log and agent trace (both live in the tools' database, so sim DB copies get their own).

- `log_decision` / `update_status` write the `decisions` table; `recent_feedback` returns chef
  approvals/rejections (with notes) for agent prompts.
- `write_trace` / `read_trace` write/read the `trace` table (UI timeline).
- `install_trace_hook()` wires every registry tool call into `trace` as tool_call + tool_result rows. The
  current run / node come from a context var (with a process-wide fallback for worker threads).
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Iterator

from restaurant_ops.tools import registry as R

_RUN: ContextVar[dict | None] = ContextVar("agent_run_ctx", default=None)
_GLOBAL_RUN: dict | None = None
RESULT_CHARS = 1200  # compact tool results in the trace
ARGS_CHARS = 400


def set_run_context(run_id: str | None, node: str | None = None, sim_date: str | None = None) -> None:
    """Record which run / node is active so the trace hook can attribute tool calls."""
    global _GLOBAL_RUN
    ctx = {"run_id": run_id, "node": node or "", "sim_date": sim_date} if run_id else None
    _RUN.set(ctx)
    _GLOBAL_RUN = ctx


def get_run_context() -> dict | None:
    return _RUN.get() or _GLOBAL_RUN


@contextmanager
def node_context(run_id: str, node: str, sim_date: str | None = None) -> Iterator[None]:
    """Temporarily attribute tool calls to `node` (restores the previous context afterwards)."""
    prev = get_run_context()
    set_run_context(run_id, node, sim_date or (prev or {}).get("sim_date"))
    try:
        yield
    finally:
        if prev is None:
            set_run_context(None)
        else:
            set_run_context(prev["run_id"], prev["node"], prev.get("sim_date"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _compact(obj, limit: int):
    """Shrink a JSON value for the trace: long lists keep their head plus a count, long strings are cut."""
    if isinstance(obj, list) and len(obj) > 6:
        return {"n": len(obj), "head": [_compact(x, limit // 6) for x in obj[:5]]}
    s = json.dumps(obj, default=str)
    if len(s) <= limit:
        return obj
    if isinstance(obj, dict):
        return {k: _compact(v, max(80, limit // max(len(obj), 1))) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_compact(v, max(80, limit // max(len(obj), 1))) for v in obj]
    return s[:limit] + "..."


# =========================================================================== trace


def write_trace(run_id: str, sim_date: str | None, node: str, kind: str, content) -> int:
    """Insert one trace row; returns its id."""
    with R.connection() as c:
        cur = c.execute("INSERT INTO trace (run_id, sim_date, ts, node, kind, content) VALUES (?,?,?,?,?,?)",
                        (run_id, sim_date, _now(), node, kind, json.dumps(content, default=str)))
        c.commit()
        return int(cur.lastrowid)


def read_trace(run_id: str, since_id: int = 0, limit: int = 1000) -> list[dict]:
    """Trace rows of a run with id > since_id, oldest first, content decoded."""
    with R.connection() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM trace WHERE run_id=? AND id>? ORDER BY id LIMIT ?",
                                           (run_id, since_id, limit))]
    for r in rows:
        try:
            r["content"] = json.loads(r["content"]) if isinstance(r["content"], str) else r["content"]
        except ValueError:
            pass
    return rows


def _tool_hook(name: str, args: dict, result) -> None:
    ctx = get_run_context()
    if not ctx or not ctx.get("run_id"):
        return
    node = ctx.get("node") or "unknown"
    sim = ctx.get("sim_date") or R.get_sim_date()
    write_trace(ctx["run_id"], sim, node, "tool_call", {"tool": name, "args": _compact(args, ARGS_CHARS)})
    write_trace(ctx["run_id"], sim, node, "tool_result", {"tool": name, "result": _compact(result, RESULT_CHARS)})


def install_trace_hook() -> None:
    """Register the trace hook on the tools registry (idempotent)."""
    R.set_trace_hook(_tool_hook)


# =========================================================================== decisions


def log_decision(run_id: str, sim_date: str, agent: str, action_type: str, payload: dict, rationale: str = "",
                 status: str = "proposed", violations: list[dict] | None = None, chef_note: str | None = None) -> int:
    """Insert a decision row; returns its id."""
    with R.connection() as c:
        cur = c.execute("INSERT INTO decisions (run_id, sim_date, ts, agent, action_type, payload, rationale, status, "
                        "violations, chef_note) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (run_id, sim_date, _now(), agent, action_type, json.dumps(payload, default=str), rationale,
                         status, json.dumps(violations or [], default=str), chef_note))
        c.commit()
        return int(cur.lastrowid)


def update_status(decision_id: int, status: str, note: str | None = None, violations: list[dict] | None = None) -> None:
    """Change a decision's status (and optionally the chef note / violations)."""
    sets, params = ["status=?"], [status]
    if note is not None:
        sets.append("chef_note=?")
        params.append(note)
    if violations is not None:
        sets.append("violations=?")
        params.append(json.dumps(violations, default=str))
    with R.connection() as c:
        c.execute(f"UPDATE decisions SET {', '.join(sets)} WHERE id=?", (*params, decision_id))
        c.commit()
    R.clear_cache()  # adjust_forecast status changes alter forecast tool output


def get_decision(decision_id: int) -> dict | None:
    with R.connection() as c:
        r = c.execute("SELECT * FROM decisions WHERE id=?", (decision_id,)).fetchone()
    if r is None:
        return None
    d = dict(r)
    for k in ("payload", "violations"):
        try:
            d[k] = json.loads(d[k]) if isinstance(d[k], str) else d[k]
        except ValueError:
            pass
    return d


def recent_feedback(n: int = 8) -> list[dict]:
    """Latest chef approvals / rejections: [{agent, action_type, status, rationale, chef_note, payload, sim_date}]."""
    with R.connection() as c:
        rows = c.execute("SELECT id, sim_date, agent, action_type, payload, rationale, status, chef_note FROM decisions "
                         "WHERE status IN ('approved','rejected') OR (status='executed' AND chef_note IS NOT NULL AND chef_note<>'') "
                         "ORDER BY id DESC LIMIT ?", (n,)).fetchall()
    out = []
    for r in rows:
        try:
            payload = json.loads(r["payload"]) if r["payload"] else {}
        except ValueError:
            payload = {}
        out.append({"agent": r["agent"], "action_type": r["action_type"], "status": r["status"],
                    "rationale": r["rationale"], "chef_note": r["chef_note"] or "", "payload": _compact(payload, 200),
                    "sim_date": r["sim_date"]})
    return out


def format_feedback(items: list[dict], agent: str | None = None) -> str:
    """Prompt-ready text for recent chef feedback (own agent's items first)."""
    if not items:
        return "No chef feedback yet."
    items = sorted(items, key=lambda x: x["agent"] != agent) if agent else items
    lines = []
    for i in items:
        verdict = "REJECTED" if i["status"] == "rejected" else "approved"
        note = f' - chef note: "{i["chef_note"]}"' if i["chef_note"] else ""
        lines.append(f'- [{i["sim_date"]}] {i["agent"]} {i["action_type"]} {json.dumps(i["payload"], default=str)} '
                     f'was {verdict}{note}')
    return "\n".join(lines)
