"""Service snapshots: everything needed to continue a dinner after a backend restart.

The world (clock, kitchen queues, stock, floor, online orders, checks, bookings, metrics, RNG states), the agents'
private state and the coordinator's decisions are pickled to `{LIVE_DIR}/{service_id}.state.pkl` every
SNAPSHOT_EVERY_MIN service minutes and whenever a decision is proposed or resolved. Paused approvals are LangGraph
interrupts checkpointed in `{service_id}.ckpt.sqlite` (SqliteSaver), so after `Service.resume(service_id)` the manager
can still approve or reject them and the graph continues where it stopped. Writes are atomic (temp file + rename).
"""
from __future__ import annotations

import os
import pickle
from pathlib import Path
from typing import TYPE_CHECKING

from restaurant_ops.config import settings

if TYPE_CHECKING:  # pragma: no cover
    from restaurant_ops.live.service import Service

VERSION = 1
WORLD = ["seed", "date", "period", "service_id", "clock", "kitchen", "stock", "forecast", "menu", "floor", "online", "metrics",
         "guests", "host", "payments", "dispatcher", "safety", "committed", "item_skip", "item_meta", "orders", "order_results",
         "shortages", "inventory_dirty", "large_fires", "order_fire", "counted_items", "_n_party", "_n_order", "_n_online",
         "_n_cust", "status", "_last_quote_view", "schedule", "feed", "sim_since_snapshot"]


def state_path(service_id: str) -> Path:
    return settings.LIVE_DIR / f"{service_id}.state.pkl"


def save(svc: "Service") -> Path:
    world = {k: getattr(svc, k) for k in WORLD if hasattr(svc, k)}
    agents = {name: {k: v for k, v in a.__dict__.items() if k != "svc"} for name, a in svc.agents.items()}
    coord = {"decisions": svc.coordinator.decisions, "open_by_key": svc.coordinator.open_by_key,
             "resolved_by_key": svc.coordinator.resolved_by_key}
    blob = {"version": VERSION, "world": world, "agents": agents, "coordinator": coord, "bus_seq": svc.bus.seq}
    path = state_path(svc.service_id)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "wb") as f:
        pickle.dump(blob, f, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp, path)
    return path


def load(service_id: str) -> dict:
    path = state_path(service_id)
    if not path.exists():
        raise FileNotFoundError(f"no saved state for {service_id} in {settings.LIVE_DIR}")
    with open(path, "rb") as f:
        blob = pickle.load(f)
    if blob.get("version") != VERSION:
        raise ValueError(f"saved state version {blob.get('version')} != {VERSION}")
    return blob


def latest_resumable() -> str | None:
    """The newest service whose run is still marked running and has a saved state."""
    import sqlite3

    for p in sorted(settings.LIVE_DIR.glob("svc-*.state.pkl"), key=lambda x: x.stat().st_mtime, reverse=True):
        sid = p.name[: -len(".state.pkl")]
        db = settings.LIVE_DIR / f"{sid}.db"
        if not db.exists():
            continue
        try:
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            r = c.execute("SELECT status FROM service_runs WHERE service_id=?", (sid,)).fetchone()
            c.close()
        except sqlite3.Error:
            continue
        if r and r[0] == "running":
            return sid
    return None
