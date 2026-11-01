"""Streamlit UI for Restaurant Ops Agents.   Run: `uv run streamlit run ui/app.py`

All SimSession calls go through one dedicated worker thread (SQLite connections and the LangGraph checkpointer are
thread-bound, and Streamlit may run each rerun on a different thread). The UI itself only reads the run DBs through
short-lived connections.
"""
from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT), str(ROOT / "ui")):
    if p not in sys.path:
        sys.path.insert(0, p)

import pandas as pd  # noqa: E402
import plotly.express as px  # noqa: E402
import plotly.graph_objects as go  # noqa: E402
import streamlit as st  # noqa: E402

import components as C  # noqa: E402
from restaurant_ops.config import settings, sim_start_from_db  # noqa: E402
from restaurant_ops.db.connection import get_conn  # noqa: E402
from restaurant_ops.sim import metrics  # noqa: E402
from restaurant_ops.sim.disruptions import SCENARIO_NAMES  # noqa: E402

st.set_page_config(page_title="Restaurant Ops Agents", page_icon="🍽️", layout="wide")


# =========================================================================== session plumbing


@st.cache_resource
def _executor() -> ThreadPoolExecutor:
    return ThreadPoolExecutor(max_workers=1, thread_name_prefix="sim")


def on_worker(fn, *args, **kw):
    """Run fn on the single sim worker thread and return its result (exceptions propagate)."""
    return _executor().submit(fn, *args, **kw).result()


def _init_state() -> None:
    ss = st.session_state
    ss.setdefault("sessions", {})      # policy -> SimSession
    ss.setdefault("cfg", None)         # config used for the current run
    ss.setdefault("awaiting", False)   # agent is paused for chef approval
    ss.setdefault("log", [])           # status messages
    ss.setdefault("run_id", f"ui{datetime.now():%m%d%H%M%S}")
    ss.setdefault("results", {})       # policy -> list of day summaries
    ss.setdefault("job", None)         # running background job: {label, future, out, since, t0}
    ss.setdefault("job_error", None)   # last background job failure, shown once
    ss.setdefault("to_end", False)     # a Run to end is in progress (continues after chef decisions)
    ss.setdefault("viewing", None)     # run id of a past run opened read-only from the sidebar


def log(msg: str) -> None:
    st.session_state.log.append(f"{datetime.now():%H:%M:%S} {msg}")


def agent_s():
    return st.session_state.sessions.get("agent")


def base_s():
    return st.session_state.sessions.get("baseline")


def any_s():
    return agent_s() or base_s()


def all_finished() -> bool:
    ss = st.session_state.sessions
    return bool(ss) and all(s.finished for s in ss.values())


def reset_run(new_id: bool = True) -> None:
    for s in list(st.session_state.sessions.values()):
        try:
            on_worker(s.close)
        except Exception:  # noqa: BLE001
            pass
    st.session_state.sessions = {}
    st.session_state.cfg = None
    st.session_state.awaiting = False
    st.session_state.results = {}
    st.session_state.log = []
    st.session_state.to_end = False
    if new_id:
        st.session_state.run_id = f"ui{datetime.now():%m%d%H%M%S}"


def start_run(policy: str, scenario: str, days: int, seed: int, run_id: str, auto: bool) -> None:
    from restaurant_ops.sim.run import SimSession

    reset_run(new_id=False)
    pols = ["agent", "baseline"] if policy == "both" else [policy]
    for pol in pols:
        s = SimSession(pol, scenario, run_id, seed, days, auto_approve=auto)
        on_worker(s.start)
        st.session_state.sessions[pol] = s
        st.session_state.results[pol] = []
    st.session_state.cfg = dict(policy=policy, scenario=scenario, days=days, seed=seed, run_id=run_id, auto=auto)
    log(f"started run {run_id} ({policy}, {scenario}, {days} days, seed {seed})")


# ---------------------------------------------------------------- background jobs
# Step day / Run to end / chef decisions run on the worker thread while the page keeps rendering. The job functions
# below never touch st.*: they append (policy, result) pairs to `out`, which the live panel drains on the script
# thread every few seconds, so finished days show up while a long run is still going.


def _job_days(agent, base, max_days: int, out: list) -> None:
    """Step up to max_days days: the agent (or the only session) first, then the baseline through the same day."""
    lead = agent or base
    for _ in range(max_days):
        if lead.finished:
            return
        r = lead.step_day()
        out.append(("agent" if agent else "baseline", r))
        if r["status"] != "day_complete":
            return  # paused for the chef (or finished)
        if agent and base is not None and not base.finished:
            out.append(("baseline", base.step_day()))


def _job_resume(agent, base, decisions: list[dict], more_days: int, out: list) -> None:
    """Resume the paused day with the chef's decisions; a paused Run to end then carries on for more_days."""
    r = agent.resume(decisions)
    out.append(("agent", r))
    if r["status"] != "day_complete":
        return
    if base is not None and not base.finished:
        out.append(("baseline", base.step_day()))
    if more_days:
        _job_days(agent, base, more_days, out)


def busy() -> bool:
    return st.session_state.get("job") is not None


def _trace_max_id() -> int:
    db = db_paths().get("agent")
    df = C.safe_df(db, "SELECT COALESCE(MAX(id), 0) AS m FROM trace") if db else None
    return int(df["m"].iloc[0]) if df is not None and not df.empty else 0


def launch(label: str, fn, *args) -> None:
    """Start fn(*args, out) on the worker thread and remember it in session_state.job."""
    out: list = []
    st.session_state.job = {"label": label, "out": out, "since": _trace_max_id(), "t0": datetime.now(),
                            "future": _executor().submit(fn, *args, out)}
    log(f"{label} started")


def _apply_result(pol: str, r: dict) -> None:
    """Fold one step_day()/resume() result into session_state (script thread only)."""
    ss = st.session_state
    lead = "agent" if agent_s() else "baseline"
    if pol == lead:
        ss.awaiting = r["status"] == "awaiting_approval"
    if r["status"] == "awaiting_approval":
        log(f"{r['date']}: {len(r['pending'])} item(s) awaiting chef approval")
    elif r["status"] == "day_complete":
        ss.results[pol].append(r["summary"])
        log(f"{r['date']}: {pol} day complete")


def _drain_job() -> bool:
    """Apply finished results; when the job is done, clear it. Returns True if anything changed."""
    job = st.session_state.job
    changed = False
    while job["out"]:
        _apply_result(*job["out"].pop(0))
        changed = True
    if job["future"].done():
        err = job["future"].exception()
        while job["out"]:
            _apply_result(*job["out"].pop(0))
        if err is not None:
            st.session_state.job_error = f"{job['label']} failed: {type(err).__name__}: {err}"
            log(f"ERROR {type(err).__name__}: {err}")
        else:
            log(f"{job['label']} finished in {(datetime.now() - job['t0']).seconds}s")
        st.session_state.job = None
        if err is not None or not st.session_state.awaiting:
            st.session_state.to_end = False  # Run to end is over unless it is only paused for the chef
        changed = True
    return changed


def step_day() -> None:
    ss = st.session_state
    if busy() or not ss.sessions or ss.awaiting or all_finished():
        return
    ss.to_end = False
    launch("Step day", _job_days, agent_s(), base_s(), 1)


def run_to_end() -> None:
    ss = st.session_state
    if busy() or not ss.sessions or ss.awaiting or all_finished():
        return
    ss.to_end = True
    launch("Run to end", _job_days, agent_s(), base_s(), 60)


def submit_decisions(decisions: list[dict]) -> None:
    log(f"chef decisions submitted ({sum(d['approved'] for d in decisions)}/{len(decisions)} approved)")
    st.session_state.awaiting = False  # hides the approval form while the resume runs
    more = 60 if st.session_state.to_end else 0
    launch("Resume after chef decisions" + (" (continuing Run to end)" if more else ""),
           _job_resume, agent_s(), base_s(), decisions, more)


@st.fragment(run_every=1.5)
def live_panel() -> None:
    """Progress of the running job: newest trace rows, refreshed every 1.5 s without blocking the page."""
    ss = st.session_state
    if ss.get("job") is None:
        return
    if _drain_job():
        st.rerun()  # full-page rerun: new day summaries, approvals, or the job just ended
    job = ss.job
    secs = (datetime.now() - job["t0"]).seconds
    db = db_paths().get("agent")
    rows = [r for r in C.trace_rows(db, ss.run_id) if r["id"] > job["since"]] if db else []
    with st.status(f"{job['label']} running… {secs}s · {len(rows)} new trace rows", expanded=True):
        if not rows:
            st.caption("Waiting for the first agent step (baseline-only runs write no trace)…")
        last_agent = next((r["node"] for r in reversed(rows) if r["node"] not in ("sim",)), None)
        if last_agent:
            st.markdown(f"Now in: **{last_agent}**")
        for r in rows[-12:]:
            st.markdown(C.trace_line(r))


def commit_all() -> None:
    for s in st.session_state.sessions.values():
        if s.world is not None:
            try:
                on_worker(s.world.conn.commit)
            except Exception:  # noqa: BLE001
                pass


def db_paths() -> dict[str, Path]:
    """policy -> DB file of this run (existing sessions first, else files of the configured run id)."""
    from restaurant_ops.sim.run import run_db_path

    ss = st.session_state
    out = {p: s.db_path for p, s in ss.sessions.items()}
    for pol in ("agent", "baseline"):
        if pol not in out:
            f = run_db_path(ss.run_id, pol)
            if f.exists():
                out[pol] = f
    return out


NEW_RUN = "— new run —"


@st.cache_data(show_spinner=False)
def _run_info(run_id: str, files: tuple[tuple[str, float], ...]) -> dict:
    """{policies, first, last, days} for a saved run (cached on the files' mtimes)."""
    first = last = None
    days = 0
    for f, _ in files:
        d = C.safe_df(f, "SELECT MIN(date) AS a, MAX(date) AS b, COUNT(DISTINCT date) AS n FROM kpi_daily WHERE run_id=?",
                      (run_id,))
        if d is not None and not d.empty and d.iloc[0]["a"]:
            a, b, n = d.iloc[0]["a"], d.iloc[0]["b"], int(d.iloc[0]["n"])
            first = min(first or a, a)
            last = max(last or b, b)
            days = max(days, n)
    return {"policies": [Path(f).stem.rsplit("_", 1)[1] for f, _ in files], "first": first, "last": last, "days": days}


def past_runs() -> dict[str, str]:
    """run_id -> label for every run saved in data/runs, newest first."""
    from restaurant_ops.sim.run import RUNS_DIR

    files: dict[str, list[Path]] = {}
    for f in RUNS_DIR.glob("*.db"):
        rid, _, pol = f.stem.rpartition("_")
        if rid and pol in ("agent", "baseline"):
            files.setdefault(rid, []).append(f)
    out = {}
    for rid, fs in sorted(files.items(), key=lambda kv: -max(f.stat().st_mtime for f in kv[1])):
        info = _run_info(rid, tuple(sorted((str(f), f.stat().st_mtime) for f in fs)))
        span = f"{info['first']} → {info['last']} ({info['days']}d)" if info["first"] else "no days run"
        out[rid] = f"{rid} · {'+'.join(sorted(info['policies']))} · {span}"
    return out


def _pick_past_run() -> None:
    ss = st.session_state
    choice = ss.get("past_run", NEW_RUN)
    if choice == NEW_RUN:
        ss.viewing = None
        ss.run_id = f"ui{datetime.now():%m%d%H%M%S}"
    else:
        ss.viewing = choice
        ss.run_id = choice


def run_start(db: Path | None) -> str:
    """First simulated day of the run in `db` (falls back to the configured SIM_START)."""
    return ((sim_start_from_db(db) if db else None) or settings.SIM_START).isoformat()


# =========================================================================== sidebar


def sidebar() -> None:
    ss = st.session_state
    sb = st.sidebar
    sb.title("🍽️ Restaurant Ops")
    running = bool(ss.sessions)
    if ss.pop("clear_past_run", False):
        ss.past_run = NEW_RUN
    runs = past_runs()
    if ss.get("past_run") not in (NEW_RUN, *runs):
        ss.past_run = NEW_RUN
    sb.selectbox("Load past run", [NEW_RUN, *runs], key="past_run", disabled=running or busy(),
                 format_func=lambda r: runs.get(r, r), on_change=_pick_past_run,
                 help="Open a saved run from data/runs read-only (Run, Agent trace, Approvals history, Forecast, KPIs).")
    if ss.viewing and not running:
        sb.info(f"👀 Viewing past run **{ss.viewing}** (read-only). Pick “{NEW_RUN}” or press Start for a new run.")
    policy = sb.selectbox("Policy", ["both", "agent", "baseline"], disabled=running)
    scenario = sb.selectbox("Scenario", list(SCENARIO_NAMES), index=list(SCENARIO_NAMES).index("salmon_shortfall"),
                            disabled=running)
    days = sb.slider("Days", 1, 7, settings.DEMO_DAYS, disabled=running)
    seed = sb.number_input("Seed", value=int(settings.SEED), step=1, disabled=running)
    run_id = sb.text_input("Run id", value=ss.run_id, disabled=running)
    auto = sb.toggle("Auto-approve chef items", value=False, disabled=running,
                     help="Off: awaiting items stop the run and appear in the Approvals tab.")
    mock = settings.LLM_MODE != "live"
    if mock:
        sb.markdown("**LLM mode:** 🟡 mock (rule-based policies)")
    else:
        sb.markdown(f"**LLM mode:** 🟢 live  \n`{settings.LLM_PROVIDER}:{settings.model_name}`")

    c1, c2 = sb.columns(2)
    if c1.button("Start", disabled=running or not C.seeded(), use_container_width=True, type="primary"):
        ss.run_id = run_id.strip() or ss.run_id
        from restaurant_ops.sim.run import run_db_path
        if any(run_db_path(ss.run_id, p).exists() for p in ("agent", "baseline")):
            ss.run_id = f"ui{datetime.now():%m%d%H%M%S}"  # never overwrite a saved run
        ss.viewing = None
        ss.clear_past_run = True
        try:
            with st.spinner("Copying DB, training models if needed..."):
                start_run(policy, scenario, int(days), int(seed), ss.run_id, auto)
        except Exception as e:  # noqa: BLE001
            reset_run(new_id=False)
            sb.error(f"Start failed: {type(e).__name__}: {e}")
        st.rerun()
    step_disabled = not running or ss.awaiting or all_finished() or busy()
    if c2.button("Step day", disabled=step_disabled, use_container_width=True):
        step_day()
        st.rerun()
    c3, c4 = sb.columns(2)
    if c3.button("Run to end", disabled=step_disabled, use_container_width=True):
        run_to_end()
        st.rerun()
    if c4.button("Reset", disabled=not running or busy(), use_container_width=True):
        reset_run()
        st.rerun()

    if not C.seeded():
        sb.warning("Database not seeded: `uv run python -m restaurant_ops.data.seed`")
    if busy():
        sb.info(f"⏳ {ss.job['label']} running: watch the progress panel or the **Agent trace** tab")
    elif ss.awaiting:
        sb.error(f"⏸ {len(agent_s().pending_approvals())} item(s) need the chef: open the **Approvals** tab")
    elif running and all_finished():
        sb.success("Run finished")
    s = any_s()
    if s is not None:
        sb.caption(f"Sim date: **{s.today}** · day {s.day_index}/{s.days}")
    if ss.log:
        with sb.expander("Log", expanded=False):
            st.code("\n".join(ss.log[-30:]))


# =========================================================================== tabs


def tab_run() -> None:
    ss = st.session_state
    s = any_s()
    if s is None and ss.viewing and db_paths():
        _tab_run_past()
        return
    if s is None:
        if not C.seeded():
            C.empty_state()
        else:
            st.info("Pick a policy and scenario in the sidebar, then press **Start**, or open a saved run with "
                    "**Load past run**.")
        return
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Sim date", s.today)
    c2.metric("Day", f"{s.day_index} / {s.days}")
    c3.metric("Scenario", ss.cfg["scenario"])
    c4.metric("Status", "running" if busy() else "awaiting chef" if ss.awaiting else ("finished" if all_finished() else "ready"))

    st.subheader("Day summaries")
    for pol, res in ss.results.items():
        st.markdown(f"**{pol}**")
        if not res:
            st.caption("No completed days yet.")
            continue
        df = pd.DataFrame(res)
        cols = ["date", "weekday", "revenue", "covers_served", "covers_demand", "waste_value", "stockouts",
                "dishes_86d", "approvals"]
        df = df[[c for c in cols if c in df.columns]].rename(columns={"waste_value": "waste $", "dishes_86d": "86s"})
        st.dataframe(df, hide_index=True, use_container_width=True)
    _run_details(db_paths(), s.today)


def _tab_run_past() -> None:
    """Run tab for a saved run opened read-only: day summaries from kpi_daily, then the usual details."""
    ss = st.session_state
    paths = db_paths()
    frames = []
    for pol, db in paths.items():
        d = C.safe_df(db, "SELECT * FROM kpi_daily WHERE run_id=? AND policy=? ORDER BY date", (ss.run_id, pol))
        if d is not None and not d.empty:
            frames.append((pol, d))
    dates = sorted({x for _, d in frames for x in d["date"]})
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Run", ss.run_id)
    c2.metric("Days run", len(dates))
    c3.metric("Dates", f"{dates[0]} → {dates[-1]}" if dates else "-")
    c4.metric("Policies", " + ".join(sorted(paths)))
    st.subheader("Day summaries")
    if not frames:
        st.caption("This run has no completed days.")
    for pol, d in frames:
        st.markdown(f"**{pol}**")
        d = d.copy()
        d.insert(1, "weekday", pd.to_datetime(d["date"]).dt.strftime("%a"))
        cols = ["date", "weekday", "revenue", "covers_served", "covers_demand", "waste_value", "stockouts", "dishes_86d"]
        d = d[[c for c in cols if c in d.columns]].rename(columns={"waste_value": "waste $", "dishes_86d": "86s"})
        st.dataframe(d, hide_index=True, use_container_width=True)
    _run_details(paths, dates[-1] if dates else run_start(next(iter(paths.values()))))


def _run_details(paths: dict[str, Path], today: str) -> None:
    """Disruptions, menu state and purchase orders of the run in `paths` (live or saved)."""
    ss = st.session_state
    main_db = paths.get("agent") or paths.get("baseline")
    start = run_start(main_db)
    left, right = st.columns(2)
    with left:
        st.subheader("Disruptions that fired")
        tr = C.trace_rows(main_db, ss.run_id)
        ev = [r for r in tr if r["node"] == "sim" and isinstance(r["content"], dict) and r["content"].get("event") == "disruption"]
        if ev:
            st.dataframe(pd.DataFrame([{"date": r["sim_date"], "kind": r["content"].get("kind"),
                                        "params": str(r["content"].get("params"))[:120],
                                        "result": str(r["content"].get("result"))[:160]} for r in ev]),
                         hide_index=True, use_container_width=True)
        else:
            st.caption("None yet.")
        st.subheader("Today's menu state")
        ms = C.safe_df(main_db, "SELECT date, dish_id, status, note FROM menu_state WHERE date=? AND status<>'active'",
                       (today,))
        if ms is None or ms.empty:
            ms = C.safe_df(main_db, "SELECT date, dish_id, status, note FROM menu_state WHERE status<>'active' "
                                    "AND date>=? ORDER BY date DESC LIMIT 20", (start,))
            st.caption("No specials / 86s for today; latest entries:")
        if ms is None or ms.empty:
            st.caption("No specials or 86s yet.")
        else:
            st.dataframe(ms, hide_index=True, use_container_width=True)
    with right:
        st.subheader("Purchase orders")
        for pol, db in paths.items():
            po = C.safe_df(db, "SELECT po_id, supplier_id, created_date, expected_delivery, status, total "
                               "FROM purchase_orders WHERE created_date>=? ORDER BY created_date DESC, po_id",
                           (run_start(db),))
            st.markdown(f"**{pol}**")
            if po is None or po.empty:
                st.caption("No purchase orders yet.")
            else:
                st.dataframe(po, hide_index=True, use_container_width=True, height=260)


def tab_trace() -> None:
    ss = st.session_state
    paths = db_paths()
    db = paths.get("agent")
    if db is None:
        C.empty_state("The agent trace appears here once an **agent** (or **both**) run has started.")
        return
    rows = C.trace_rows(db, ss.run_id)
    if not rows:
        C.empty_state("No trace rows yet. Step a day.")
        return
    nodes = sorted({r["node"] for r in rows})
    kinds = [k for k in C.KIND_STYLE]
    f1, f2, f3 = st.columns([2, 2, 1])
    sel_nodes = f1.multiselect("Agent / node", nodes, default=nodes)
    sel_kinds = f2.multiselect("Kind", kinds, default=[k for k in kinds if k not in ("tool_result",)])
    hide_sim = f3.checkbox("Hide simulator", value=False)
    rows = [r for r in rows if r["node"] in sel_nodes and r["kind"] in sel_kinds and not (hide_sim and r["node"] == "sim")]
    st.caption(f"{len(rows)} trace rows · " + " ".join(f"{i} {k}" for k, (i, _) in C.KIND_STYLE.items()))
    for d, groups in C.group_trace(rows).items():
        with st.expander(f"📅 {d}", expanded=(d == (agent_s().today if agent_s() else d))):
            for label, grp in groups:
                if not grp:
                    continue
                st.markdown(f"**{label}** ({len(grp)} rows)")
                C.render_trace_group(grp)
    st.subheader("Decisions")
    dec = C.safe_df(db, "SELECT id, sim_date, agent, action_type, status, rationale, violations, chef_note, payload "
                        "FROM decisions WHERE run_id=? ORDER BY id DESC", (ss.run_id,))
    if dec is None or dec.empty:
        st.caption("No decisions yet.")
    else:
        sa, ss_ = st.columns(2)
        agents = sa.multiselect("Decision agent", sorted(dec["agent"].unique()), default=sorted(dec["agent"].unique()))
        stats = ss_.multiselect("Status", sorted(dec["status"].unique()), default=sorted(dec["status"].unique()))
        dec = dec[dec["agent"].isin(agents) & dec["status"].isin(stats)]
        st.dataframe(dec, hide_index=True, use_container_width=True)


@st.fragment(run_every=2)
def live_trace() -> None:
    tab_trace()


def tab_approvals() -> None:
    ss = st.session_state
    s = agent_s()
    if s is None and ss.viewing and "agent" in db_paths():
        st.caption(f"Past run **{ss.viewing}** (read-only): chef decisions made during that run.")
        _chef_history(db_paths()["agent"])
        return
    if s is None:
        C.empty_state("Approvals apply to the **agent** policy. Start a run with policy agent or both.")
        return
    pending = s.pending_approvals() if ss.awaiting else []
    st.subheader(f"Pending ({len(pending)})")
    if not pending:
        st.success("Nothing waiting for the chef." + (" Auto-approve is on." if ss.cfg and ss.cfg["auto"] else ""))
    else:
        with st.form("approvals_form"):
            for p in pending:
                with st.container(border=True):
                    h1, h2, h3 = st.columns([2, 2, 1])
                    h1.markdown(f"**{p['agent']}** → `{p['action_type']}`")
                    h2.markdown(f"decision #{p['id']}")
                    h3.metric("Est. cost", C.money(p.get("est_cost", 0)))
                    st.markdown(f"_{p.get('rationale', '')}_")
                    for w in p.get("warnings") or []:
                        st.warning(w if isinstance(w, str) else str(w))
                    with st.expander("Payload"):
                        st.json(p["payload"])
                    d1, d2 = st.columns([1, 3])
                    d1.radio("Decision", ["Approve", "Reject"], key=f"dec_{p['id']}", horizontal=True)
                    d2.text_input("Note to agents (optional)", key=f"note_{p['id']}")
            submitted = st.form_submit_button("Submit decisions", type="primary")
        if submitted:
            decisions = [{"id": p["id"], "approved": ss.get(f"dec_{p['id']}", "Approve") == "Approve",
                          "note": ss.get(f"note_{p['id']}", "").strip() or
                          ("approved" if ss.get(f"dec_{p['id']}", "Approve") == "Approve" else "rejected")}
                         for p in pending]
            if not busy():
                submit_decisions(decisions)
            st.rerun()
    _chef_history(s.db_path)


def _chef_history(db: Path) -> None:
    st.subheader("Past chef decisions")
    hist = C.safe_df(db, "SELECT id, sim_date, agent, action_type, status, chef_note, rationale FROM decisions "
                                "WHERE status IN ('approved','rejected') OR (status='executed' AND chef_note IS NOT NULL "
                                "AND chef_note<>'') ORDER BY id DESC LIMIT 50")
    if hist is None or hist.empty:
        st.caption("No chef decisions yet.")
    else:
        st.dataframe(hist, hide_index=True, use_container_width=True)


def _forecast_db_and_date() -> tuple[Path | None, str]:
    paths = db_paths()
    s = any_s()
    db = paths.get("agent") or paths.get("baseline") or (settings.DB_PATH if C.seeded() else None)
    return db, (s.today if s else run_start(db))


def tab_forecast() -> None:
    from restaurant_ops.core import forecasting as F

    db, today = _forecast_db_and_date()
    if db is None:
        C.empty_state()
        return
    try:
        conn = get_conn(db)
        try:
            with st.spinner("Forecasting (trains models on first use)..."):
                fc = F.forecast_covers(today, 3, conn=conn)
                signals = F.signal_report(conn)
                ev = F.evaluate(conn)
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        C.empty_state(f"Forecast unavailable: {type(e).__name__}: {e}")
        return
    st.subheader(f"Covers forecast, next 3 days from {today} (P10 / P50 / P90)")
    df = pd.DataFrame(fc)
    cols = st.columns(len(settings.MEAL_PERIODS))
    for col, per in zip(cols, settings.MEAL_PERIODS):
        d = df[df["meal_period"] == per]
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=d["date"], y=d["p90"], mode="lines", line=dict(width=0), name="P90", showlegend=False))
        fig.add_trace(go.Scatter(x=d["date"], y=d["p10"], mode="lines", line=dict(width=0), fill="tonexty",
                                 fillcolor="rgba(37,99,235,0.2)", name="P10-P90"))
        fig.add_trace(go.Scatter(x=d["date"], y=d["p50"], mode="lines+markers", line=dict(color="#2563eb"), name="P50"))
        if len(d):
            fig.add_hline(y=float(d["capacity_cap"].iloc[0]), line_dash="dot", line_color="#dc2626",
                          annotation_text="capacity")
        fig.update_layout(title=per, height=300, margin=dict(l=10, r=10, t=40, b=10), legend=dict(orientation="h"))
        col.plotly_chart(fig, use_container_width=True)

    st.subheader("Forecast vs actual (completed days)")
    paths = db_paths()
    fl = None
    for pol in ("agent", "baseline"):
        if pol in paths:
            fl = C.safe_df(paths[pol], "SELECT date, meal_period, p10, p50, p90, actual FROM forecast_log "
                                       "WHERE run_id=? AND policy=? AND actual IS NOT NULL ORDER BY date",
                           (st.session_state.run_id, pol))
            if fl is not None and not fl.empty:
                break
    if fl is None or fl.empty:
        st.caption("Complete a simulated day to compare the logged forecast with realised covers.")
    else:
        fig = px.line(fl, x="date", y=["p50", "actual"], facet_col="meal_period", markers=True)
        fig.update_layout(height=320, margin=dict(l=10, r=10, t=30, b=10))
        st.plotly_chart(fig, use_container_width=True)
        st.dataframe(fl, hide_index=True, use_container_width=True)

    st.subheader("Signal report: which signals matter here?")
    if signals:
        sdf = pd.DataFrame(signals)
        st.dataframe(sdf, hide_index=True, use_container_width=True)
    st.subheader("Held-out accuracy (last month)")
    m1, m2 = st.columns(2)
    m1.metric("MAPE", f"{ev.get('mape', float('nan')):.1%}" if ev.get("mape") is not None else "-")
    m2.metric("P90 coverage", f"{ev.get('p90_coverage', float('nan')):.1%}" if ev.get("p90_coverage") is not None else "-")
    with st.expander("All evaluation metrics"):
        st.json(ev)


_METRIC_CARDS = [  # key, label, fmt, lower_is_better
    ("waste_value", "Waste $", "${:,.0f}", True), ("waste_pct", "Waste % of purchases", "{:.2f}%", True),
    ("stockouts", "Stockouts", "{:d}", True), ("dishes_86d", "86s (dish-days)", "{:d}", True),
    ("food_cost_pct", "Food cost %", "{:.2f}%", True), ("revenue", "Revenue", "${:,.0f}", False),
    ("mape", "Covers MAPE", "{:.1%}", True), ("p90_coverage", "P90 coverage", "{:.1%}", False),
    ("approvals_needed", "Chef approvals", "{:d}", True),
]


def tab_kpis() -> None:
    ss = st.session_state
    paths = db_paths()
    if not paths:
        C.empty_state("KPIs appear after a run has started.")
        return
    commit_all()
    rid = ss.run_id
    try:
        if "agent" in paths and "baseline" in paths:
            cmp = metrics.compare(paths["agent"], paths["baseline"], rid)
        else:
            pol, db = next(iter(paths.items()))
            cmp = {"agent": {}, "baseline": {}, "delta": {}, "winner": {}}
            cmp[pol] = metrics.kpis(db, rid, pol)
    except Exception as e:  # noqa: BLE001
        C.empty_state(f"KPIs unavailable: {type(e).__name__}: {e}")
        return
    a, b = cmp["agent"], cmp["baseline"]
    both = bool(a) and bool(b)
    st.subheader("Agent vs baseline" if both else "KPIs (single policy)")
    cols = st.columns(3)
    for i, (k, label, fmt, lower) in enumerate(_METRIC_CARDS):
        va, vb = a.get(k), b.get(k)
        shown = va if va is not None else vb
        with cols[i % 3]:
            if shown is None:
                st.metric(label, "-")
                continue
            val = fmt.format(int(shown) if "{:d}" in fmt else shown)
            if both and va is not None and vb is not None:
                d = va - vb
                dtxt = (f"{d:+.1%}" if k in ("mape", "p90_coverage") else f"{d:+,.2f}") + " vs baseline"
                st.metric(label, val, dtxt, delta_color="inverse" if lower else "normal")
                st.caption(f"baseline: {fmt.format(int(vb) if '{:d}' in fmt else vb)} · winner: {cmp['winner'].get(k, '-')}")
            else:
                st.metric(label, val)
    if both:
        with st.expander("Text comparison"):
            st.code(metrics.format_comparison(cmp))

    st.subheader("Daily trends")
    frames = []
    for pol, db in paths.items():
        d = C.safe_df(db, "SELECT * FROM kpi_daily WHERE run_id=? AND policy=? ORDER BY date", (rid, pol))
        if d is not None and not d.empty:
            frames.append(d)
    if not frames:
        st.caption("No completed days yet.")
        return
    daily = pd.concat(frames)
    daily["food_cost_pct"] = (daily["food_cost"] / daily["revenue"].replace(0, pd.NA) * 100).astype(float)
    grid = st.columns(2)
    for i, (k, t) in enumerate([("revenue", "Revenue $"), ("waste_value", "Waste $"), ("stockouts", "Stockouts"),
                                ("food_cost_pct", "Food cost %"), ("dishes_86d", "86s"), ("covers_served", "Covers served")]):
        fig = px.line(daily, x="date", y=k, color="policy", markers=True, title=t,
                      color_discrete_map={"agent": "#2563eb", "baseline": "#d97706"})
        fig.update_layout(height=280, margin=dict(l=10, r=10, t=40, b=10))
        grid[i % 2].plotly_chart(fig, use_container_width=True)


# =========================================================================== main


def main() -> None:
    _init_state()
    if busy():
        _drain_job()  # pick up anything that finished since the last refresh before drawing the controls
    sidebar()
    if st.session_state.job_error:
        st.error(st.session_state.job_error)
        st.session_state.job_error = None
    live_panel()
    n_pend = len(agent_s().pending_approvals()) if agent_s() and st.session_state.awaiting and not busy() else 0
    if n_pend:
        st.error(f"⏸ The agents are waiting for the chef: {n_pend} item(s). Open the **Approvals** tab.")
        st.toast("Approval needed", icon="👨‍🍳")
    labels = ["Run", "Agent trace", f"Approvals 🔔 {n_pend}" if n_pend else "Approvals", "Forecast", "KPIs"]
    t_run, t_trace, t_appr, t_fc, t_kpi = st.tabs(labels)
    with t_run:
        tab_run()
    with t_trace:
        # refresh the trace every 2 s while a job runs, so agent steps appear as they happen
        (live_trace if busy() else tab_trace)()
    with t_appr:
        tab_approvals()
    with t_fc:
        tab_forecast()
    with t_kpi:
        tab_kpis()


main()
