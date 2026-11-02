"""Service snapshots: everything needed to continue a dinner after a backend restart.

The world (clock, kitchen queues, stock, floor, online orders, checks, bookings, metrics, RNG states), the agents'
private state and the coordinator's decisions are pickled to `{LIVE_DIR}/{service_id}.state.pkl` every
SNAPSHOT_EVERY_MIN service minutes and whenever a decision is proposed or resolved. Snapshots carry a version (older
ones are migrated on load), a SHA-256 sidecar (a corrupted file is detected) and are rotated (SNAPSHOT_KEEP kept), so
a bad newest snapshot falls back to the previous one; with none readable, `Service.recover_from_log` rebuilds from the
service database and its event log. Paused approvals are LangGraph
interrupts checkpointed in `{service_id}.ckpt.sqlite` (SqliteSaver), so after `Service.resume(service_id)` the manager
can still approve or reject them and the graph continues where it stopped. Writes are atomic (temp file + rename).
"""
from __future__ import annotations

import hashlib
import os
import pickle
from pathlib import Path
from typing import TYPE_CHECKING

from restaurant_ops.config import settings

if TYPE_CHECKING:  # pragma: no cover
    from restaurant_ops.live.service import Service

VERSION = 2
WORLD = ["seed", "date", "period", "service_id", "master_path", "profile", "zones", "tax_rules", "tax_category", "recovered", "llm_guard", "clock", "kitchen", "stock", "forecast", "menu", "floor", "online", "metrics",
         "guests", "host", "payments", "dispatcher", "safety", "committed", "item_skip", "item_meta", "orders", "order_results",
         "shortages", "inventory_dirty", "large_fires", "order_fire", "counted_items", "_n_party", "_n_order", "_n_online",
         "_n_cust", "status", "_last_quote_view", "schedule", "feed", "sim_since_snapshot"]


def state_path(service_id: str, n: int = 0) -> Path:
    """n = 0 is the newest snapshot, 1.. the older rotated ones."""
    return settings.LIVE_DIR / (f"{service_id}.state.pkl" if n == 0 else f"{service_id}.state.{n}.pkl")


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def save(svc: "Service") -> Path:
    world = {k: getattr(svc, k) for k in WORLD if hasattr(svc, k)}
    agents = {name: {k: v for k, v in a.__dict__.items() if k != "svc"} for name, a in svc.agents.items()}
    coord = {"decisions": svc.coordinator.decisions, "open_by_key": svc.coordinator.open_by_key,
             "resolved_by_key": svc.coordinator.resolved_by_key}
    blob = {"version": VERSION, "world": world, "agents": agents, "coordinator": coord, "bus_seq": svc.bus.seq}
    data = pickle.dumps(blob, protocol=pickle.HIGHEST_PROTOCOL)
    keep = max(1, settings.SNAPSHOT_KEEP)
    for n in range(keep - 1, 0, -1):  # rotate: state.pkl -> state.1.pkl -> state.2.pkl ...
        older, newer = state_path(svc.service_id, n), state_path(svc.service_id, n - 1)
        if newer.exists():
            os.replace(newer, older)
            sidecar = newer.with_suffix(".sha256")
            if sidecar.exists():
                os.replace(sidecar, older.with_suffix(".sha256"))
    path = state_path(svc.service_id)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    path.with_suffix(".sha256").write_text(_digest(data))
    return path


def migrate(blob: dict) -> dict:
    """Bring an older snapshot up to VERSION (one step per version)."""
    v = blob.get("version")
    if v == 1:  # v2 added the admin configuration and the master DB path to the world
        from restaurant_ops import admin as ADMIN

        w = blob["world"]
        w.setdefault("profile", dict(ADMIN.DEFAULT_PROFILE))
        w.setdefault("zones", dict(settings.DELIVERY_ZONES))
        w.setdefault("tax_rules", [])
        w.setdefault("tax_category", {})
        w.setdefault("master_path", settings.DB_PATH)
        w.setdefault("recovered", None)
        blob["version"] = v = 2
    if v != VERSION:
        raise ValueError(f"snapshot version {v} is newer than this code ({VERSION})")
    return blob


def _read(path: Path) -> dict:
    data = path.read_bytes()
    sidecar = path.with_suffix(".sha256")
    if sidecar.exists() and sidecar.read_text().strip() != _digest(data):
        raise ValueError(f"{path.name}: checksum mismatch (corrupted)")
    return migrate(pickle.loads(data))


def load(service_id: str) -> dict:
    """The newest snapshot that reads cleanly (checksum + version migration); older rotated ones are the fallback."""
    errors = []
    for n in range(max(1, settings.SNAPSHOT_KEEP)):
        path = state_path(service_id, n)
        if not path.exists():
            continue
        try:
            blob = _read(path)
            blob["source"] = path.name
            blob["skipped"] = errors
            return blob
        except Exception as e:  # noqa: BLE001  corrupted / unreadable: try the previous one
            errors.append(f"{path.name}: {type(e).__name__}: {e}")
    if errors:
        raise SnapshotError("; ".join(errors))
    raise FileNotFoundError(f"no saved state for {service_id} in {settings.LIVE_DIR}")


class SnapshotError(RuntimeError):
    pass


def latest_resumable() -> str | None:
    """The newest service whose run is still marked running and has a saved state."""
    import sqlite3

    for p in sorted(settings.LIVE_DIR.glob("svc-*.db"), key=lambda x: x.stat().st_mtime, reverse=True):
        sid = p.stem
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
