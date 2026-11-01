"""Replay a logged service from its append-only event log.

`fold(events)` is the Python twin of the UI's reducer (frontend/src/store.js): starting from the snapshot carried
by service.started it applies every event in sequence order. Folding the log of a finished service gives back its
tables, decisions, menu, online orders and KPIs, which the tests compare with the live service.

    python -m restaurant_ops.live.replay                 # list logged services
    python -m restaurant_ops.live.replay SERVICE_ID      # decision timeline + final KPIs from the log alone
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from restaurant_ops.config import settings
from restaurant_ops.live import events as E


def db_for(service_id: str) -> Path:
    p = settings.LIVE_DIR / f"{service_id}.db"
    if not p.exists():
        raise FileNotFoundError(f"no logged service {service_id} in {settings.LIVE_DIR}")
    return p


def load_events(service_id: str, after_seq: int = 0) -> list[dict]:
    c = sqlite3.connect(f"file:{db_for(service_id)}?mode=ro", uri=True)
    try:
        rows = c.execute("SELECT seq, wall_ts, sim_ts, type, source, payload FROM event_log WHERE service_id=? AND seq>? ORDER BY seq",
                         (service_id, after_seq)).fetchall()
    finally:
        c.close()
    return [{"seq": s, "wall_ts": w, "sim_ts": t, "type": ty, "source": so, "payload": json.loads(p) if p else {}}
            for s, w, t, ty, so, p in rows]


def list_services() -> list[dict]:
    out = []
    for p in sorted(settings.LIVE_DIR.glob("svc-*.db"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
            r = c.execute("SELECT service_id, service_date, meal_period, seed, started_ts, bus, llm_mode, status FROM service_runs").fetchone()
            n = c.execute("SELECT COUNT(*) FROM event_log").fetchone()[0]
            c.close()
        except sqlite3.Error:
            continue
        if r:
            out.append({"service_id": r[0], "date": r[1], "period": r[2], "seed": r[3], "started": r[4], "bus": r[5],
                        "llm_mode": r[6], "status": r[7], "events": n})
    return out


def fold(events: list[dict], state: dict | None = None) -> dict:
    s = json.loads(json.dumps(state)) if state else {}
    for ev in events:
        t, p = ev["type"], ev["payload"]
        if t == E.SERVICE_STARTED:
            s = json.loads(json.dumps(p["snapshot"]))
            s.setdefault("feed", [])
        elif not s:
            continue
        elif t == E.TABLE_UPDATED:
            for tb in p["tables"]:
                s["tables"][tb["id"]] = tb
        elif t == E.KITCHEN_LOAD:
            s["kitchen"] = p
        elif t == E.CLOCK_TICK:
            s["clock"] = p
        elif t == E.INVENTORY_SNAPSHOT:
            s["inventory"] = p
        elif t == E.FORECAST_UPDATED:
            s["forecast"] = {k: v for k, v in p.items() if k not in ("reason", "recheck")}
        elif t == E.MENU_UPDATED:
            s["menu"] = p
        elif t == E.ONLINE_MODE:
            s["online"]["mode"], s["online"]["reason"] = p["mode"], p.get("reason", "")
        elif t in (E.ONLINE_ORDER, E.QUOTE_UPDATED):
            s["online"]["orders"][p["order_id"]] = {k: v for k, v in p.items() if k not in ("notify",)}
        elif t in (E.DECISION_PROPOSED, E.DECISION_RESOLVED):
            s["decisions"][str(p["id"])] = p
        elif t == E.KPI_UPDATED:
            s["kpis"] = p
        elif t == E.SERVICE_ENDED:
            s["kpis"] = p["kpis"]
            s["service"]["status"] = "ended"
    return s


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Replay a logged live service")
    ap.add_argument("service_id", nargs="?")
    a = ap.parse_args(argv)
    if not a.service_id:
        for r in list_services():
            print(f"{r['service_id']}  {r['date']} {r['period']}  seed={r['seed']}  events={r['events']}  {r['status']}")
        return 0
    evs = load_events(a.service_id)
    st = fold(evs)
    print(f"{a.service_id}: {len(evs)} events")
    for d in sorted(st.get("decisions", {}).values(), key=lambda d: d["id"]):
        print(f"  {d['created_sim'][11:16]} [{d['agent']}] {d['title']} -> {d['status']}")
    for k, v in (st.get("kpis") or {}).items():
        print(f"  {k:<26}{v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
