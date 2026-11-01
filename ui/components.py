"""Streamlit UI helpers: DB reads with friendly failures, trace grouping, small renderers."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

from restaurant_ops.config import settings
from restaurant_ops.db.connection import get_conn, query_df

SEED_HINT = "No data yet. Run `uv run python -m restaurant_ops.data.seed` first, then press **Start** in the sidebar."

# kind -> (icon, colour)
KIND_STYLE = {
    "supervisor": ("🧭", "#6d28d9"),
    "agent_start": ("▶️", "#2563eb"),
    "tool_call": ("🔧", "#d97706"),
    "tool_result": ("📊", "#059669"),
    "agent_end": ("🏁", "#2563eb"),
    "constraint": ("🛡️", "#dc2626"),
    "approval": ("👨‍🍳", "#db2777"),
    "execute": ("⚙️", "#0f766e"),
    "event": ("📣", "#6b7280"),
}


def db_ok(path: str | Path | None) -> bool:
    return bool(path) and Path(path).exists()


def safe_df(db_path: str | Path | None, sql: str, params: tuple = ()) -> pd.DataFrame | None:
    """Run a SELECT on a DB file; returns None (not an exception) if the DB or table is missing."""
    if not db_ok(db_path):
        return None
    try:
        conn = get_conn(db_path)
        try:
            return query_df(sql, params, conn=conn)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return None


def empty_state(msg: str | None = None) -> None:
    st.info(msg or SEED_HINT)


def seeded() -> bool:
    return Path(settings.DB_PATH).exists()


def parse_json(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def trace_rows(db_path, run_id: str) -> list[dict]:
    df = safe_df(db_path, "SELECT * FROM trace WHERE run_id=? ORDER BY id", (run_id,))
    if df is None or df.empty:
        return []
    rows = df.to_dict("records")
    for r in rows:
        r["content"] = parse_json(r["content"])
    return rows


def group_trace(rows: list[dict]) -> dict[str, list[tuple[str, list[dict]]]]:
    """{sim_date: [(cycle_label, rows)]}. A new cycle starts at each `sense` event; sim events form their own group."""
    out: dict[str, list[tuple[str, list[dict]]]] = {}
    cur: dict[str, list[dict]] = {}
    for r in rows:
        d = r.get("sim_date") or "n/a"
        groups = out.setdefault(d, [])
        if r["node"] == "sim":
            key = "sim"
            if not groups or groups[-1][0] != "simulator":
                groups.append(("simulator", []))
            groups[-1][1].append(r)
            continue
        if r["node"] == "sense" and r["kind"] == "event":
            trig = (r["content"] or {}).get("trigger") if isinstance(r["content"], dict) else None
            kind = (trig or {}).get("kind", "morning")
            if kind == "event":
                kind = ((trig or {}).get("event") or {}).get("kind", "event")
            groups.append((f"cycle #{len(groups) + 1}: {kind}", []))
        if not groups or groups[-1][0] == "simulator":
            groups.append((f"cycle #{len(groups) + 1}", []))
        groups[-1][1].append(r)
    return out


def trace_line(r: dict) -> str:
    icon, _ = KIND_STYLE.get(r["kind"], ("•", "#6b7280"))
    c = r["content"]
    brief = ""
    if isinstance(c, dict):
        for k in ("summary", "tool", "event", "reason", "action_type", "decision", "message"):
            if c.get(k):
                brief = str(c[k])
                break
        if r["kind"] == "tool_call" and c.get("args"):
            brief += " " + json.dumps(c["args"], default=str)[:80]
    elif c:
        brief = str(c)
    return f"{icon} **{r['node']}** · `{r['kind']}` · {brief[:120]}"


def render_trace_group(rows: list[dict]) -> None:
    for r in rows:
        icon, color = KIND_STYLE.get(r["kind"], ("•", "#6b7280"))
        with st.container(border=False):
            st.markdown(f"<span style='border-left:4px solid {color};padding-left:8px'>{trace_line(r)}</span>",
                        unsafe_allow_html=True)
            if r["content"]:
                with st.expander(f"#{r['id']} {r['ts']}", expanded=False):
                    st.json(r["content"] if isinstance(r["content"], (dict, list)) else {"content": r["content"]})


def money(v) -> str:
    try:
        return f"${float(v):,.0f}"
    except (TypeError, ValueError):
        return "-"


def delta_fmt(v, fmt: str = "{:+,.2f}") -> str | None:
    return None if v is None else fmt.format(v)
