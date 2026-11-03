"""Restoring a service after a restart: from its newest readable snapshot (`resume`), or, when no snapshot can be read,
from its database and event log (`recover_from_log`). `Service.resume` / `Service.recover_from_log` call these."""
from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

from restaurant_ops import admin as ADMIN
from restaurant_ops.config import settings
from restaurant_ops.db.connection import get_conn, init_db
from restaurant_ops.live import events as E
from restaurant_ops.live import persistence as PERSIST
from restaurant_ops.live.clock import ServiceClock
from restaurant_ops.live.coordinator import normalize_decision
from restaurant_ops.live.world import Policy, ServiceError
from restaurant_ops.tools import registry as R

if TYPE_CHECKING:  # pragma: no cover
    from restaurant_ops.live.service import Service


def resume(cls: "type[Service]", service_id: str, auto_policy: Policy | None = None, bus_kind: str | None = None) -> "Service":
    """Continue a service after a backend restart from its saved state, DB and approval checkpoints."""
    blob = PERSIST.load(service_id)
    svc = cls.__new__(cls)
    for k, v in blob["world"].items():
        setattr(svc, k, v)
    svc.db_path = settings.LIVE_DIR / f"{service_id}.db"
    svc.conn = get_conn(svc.db_path)
    svc.conn.execute("PRAGMA synchronous = NORMAL")
    init_db(svc.conn)
    R.set_db_path(svc.db_path)
    R.set_sim_date(svc.date)
    svc._init_runtime(bus_kind, auto_policy)
    svc.bus.seq = int(svc.conn.execute("SELECT COALESCE(MAX(seq),0) FROM event_log WHERE service_id=?", (service_id,)).fetchone()[0])
    for name, state in blob["agents"].items():
        if name in svc.agents:
            svc.agents[name].__dict__.update(state)
    svc.prep.attach()
    c = blob["coordinator"]
    svc.coordinator.decisions, svc.coordinator.open_by_key, svc.coordinator.resolved_by_key = (
        c["decisions"], c["open_by_key"], c["resolved_by_key"])
    svc.bus.publish(E.AGENT_LOG, {"agent": "service", "message": f"service resumed at {svc.clock.hhmm()} with "
                                  f"{len(svc.coordinator.pending())} approval(s) still pending"})
    svc.bus.publish(E.SERVICE_STARTED, {"service": svc.info(), "snapshot": svc.snapshot(), "resumed": True})
    svc.flush()
    return svc



def recover_from_log(cls: "type[Service]", service_id: str, auto_policy: Policy | None = None, bus_kind: str | None = None) -> "Service":
    """Last-resort restore when no snapshot is readable: continue the service on its database (stock, menu state,
    bookings, payments already recorded) and rebuild the decisions from the event log, so pending approvals still
    resume from their LangGraph checkpoints. Guests on the floor, kitchen queues and online orders in progress
    cannot be rebuilt from the log and are reported as lost."""
    import json as _json

    db_path = settings.LIVE_DIR / f"{service_id}.db"
    if not db_path.exists():
        raise ServiceError(f"no database for {service_id}")
    svc = cls.__new__(cls)
    svc.service_id, svc.db_path = service_id, db_path
    svc.conn = get_conn(db_path)
    svc.conn.execute("PRAGMA synchronous = NORMAL")
    init_db(svc.conn)
    run = svc.conn.execute("SELECT * FROM service_runs WHERE service_id=?", (service_id,)).fetchone()
    if run is None:
        raise ServiceError(f"{service_id}: no service_runs row")
    svc.seed, svc.date, svc.period = int(run["seed"]), date.fromisoformat(run["service_date"]), run["meal_period"]
    svc.master_path = settings.DB_PATH
    R.set_db_path(db_path)
    R.set_sim_date(svc.date)
    hours = ADMIN.hours_for(svc.conn, svc.date, svc.period)
    svc.clock = ServiceClock(svc.date, svc.period, 10, hours=hours[:2] if hours else None)
    svc._build_world()
    last = svc.conn.execute("SELECT seq, sim_ts FROM event_log WHERE service_id=? ORDER BY seq DESC LIMIT 1", (service_id,)).fetchone()
    if last and last["sim_ts"]:
        svc.clock.now = datetime.fromisoformat(last["sim_ts"])
    svc._init_runtime(bus_kind, auto_policy)
    svc.bus.seq = int(last["seq"]) if last else 0
    for dish, status, note in svc.conn.execute("SELECT dish_id, status, note FROM menu_state WHERE date=?", (svc.date.isoformat(),)):
        if status == "86":
            svc.menu.eighty_sixed[dish] = note or "86"
        elif status == "special":
            svc.menu.specials[dish] = {"portions": 0, "note": note or "special"}
    views: dict[int, dict] = {}
    kpi = None
    for t, payload in svc.conn.execute("SELECT type, payload FROM event_log WHERE service_id=? AND type IN "
                                       "('decision.proposed','decision.resolved','kpi.updated') ORDER BY seq", (service_id,)):
        p = _json.loads(payload)
        if t == E.KPI_UPDATED:
            kpi = p
        else:
            views[int(p["id"])] = {**views.get(int(p["id"]), {}), **p}
    for did, v in views.items():
        normalize_decision(v)  # events of any era -> the current decision shape (timeouts, key, defaults)
        if v["status"] == "pending" and v.get("created_sim"):  # service minutes already spent waiting still count
            waited = (svc.clock.now - datetime.fromisoformat(v["created_sim"])).total_seconds() / 60
            v["remaining_min"] = round(max(1.0, v["timeout_min"] - max(0.0, waited)), 3)
        svc.coordinator.decisions[did] = v
        if v["status"] == "pending":
            svc.coordinator.open_by_key[v["key"]] = did
    if kpi:
        for k in ("covers", "covers_arrived", "orders", "online_orders", "revenue", "food_cost", "waste_value", "eighty_six"):
            attr = {"covers": "covers_seated"}.get(k, k)
            if kpi.get(k) is not None and hasattr(svc.metrics, attr):
                setattr(svc.metrics, attr, kpi[k])
    for b in svc.conn.execute("SELECT * FROM bookings WHERE service_id=?", (service_id,)):
        from restaurant_ops.live.host import Booking

        svc.host.bookings[b["booking_id"]] = Booking(b["booking_id"], b["name"], int(b["party_size"]), svc.host.t_of(b["time"]),
                                                    status=b["status"], phone=b["phone"] or "", guest_id=b["guest_id"],
                                                    notes=b["notes"] or "", source=b["source"] or "phone")
    svc.host._n = len(svc.host.bookings)
    svc.prep.attach()
    svc.status = "running"
    svc.recovered = {"from": "event log", "at": svc.clock.hhmm(),
                     "lost": "guests on the floor, kitchen queues and online orders in progress",
                     "kept": f"stock, menu state, {len(svc.host.bookings)} bookings, {len(views)} decisions "
                             f"({len(svc.coordinator.pending())} pending), KPIs"}
    svc.bus.publish(E.AGENT_LOG, {"agent": "service", "message": f"recovered from the event log at {svc.clock.hhmm()}: kept "
                                  f"{svc.recovered['kept']}; lost {svc.recovered['lost']}"})
    svc.bus.publish(E.SERVICE_STARTED, {"service": svc.info(), "snapshot": svc.snapshot(), "resumed": True, "recovered": True})
    svc.flush()
    return svc
