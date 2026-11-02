"""Operability: structured (JSON) logging, a Prometheus /metrics exposition, retention of live service files, and
backups with restore and a restore drill (`python -m restaurant_ops.live.ops backup|list|verify|restore|drill`)."""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from restaurant_ops.config import settings

if TYPE_CHECKING:  # pragma: no cover
    from restaurant_ops.live.service import Service


class JsonFormatter(logging.Formatter):
    """One JSON object per line: ts, level, logger, msg, plus any `extra=` fields and the exception."""

    RESERVED = set(vars(logging.LogRecord("", 0, "", 0, "", (), None)))

    def format(self, record: logging.LogRecord) -> str:
        out = {"ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
               "level": record.levelname, "logger": record.name, "msg": record.getMessage()}
        for k, v in record.__dict__.items():
            if k not in self.RESERVED and not k.startswith("_"):
                out[k] = v
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


def configure_logging(json_logs: bool | None = None, level: str | None = None) -> None:
    json_logs = settings.LOG_JSON if json_logs is None else json_logs
    root = logging.getLogger()
    root.setLevel((level or settings.LOG_LEVEL).upper())
    h = logging.StreamHandler()
    h.setFormatter(JsonFormatter() if json_logs else logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.handlers = [h]


# =========================================================================== Prometheus exposition


def _line(name: str, value, help_: str, kind: str = "gauge", labels: dict | None = None) -> list[str]:
    if value is None:
        return []
    lab = "{" + ",".join(f'{k}="{v}"' for k, v in labels.items()) + "}" if labels else ""
    return [f"# HELP {name} {help_}", f"# TYPE {name} {kind}", f"{name}{lab} {float(value)}"]


def prometheus(svc: "Service | None", ws_clients: int = 0, started: float | None = None) -> str:
    out: list[str] = []
    out += _line("restaurant_ops_up", 1 if svc else 0, "1 when a live service is loaded")
    out += _line("restaurant_ops_ws_clients", ws_clients, "connected WebSocket clients")
    if started:
        out += _line("restaurant_ops_process_uptime_seconds", round(time.time() - started, 1), "backend uptime")
    if svc is None:
        return "\n".join(out) + "\n"
    k = svc.metrics.kpis(svc.forecast)
    pay = svc.payments.summary()
    gauges = {
        "covers": ("covers seated tonight", k["covers"]), "orders": ("orders created", k["orders"]),
        "online_orders": ("online orders", k["online_orders"]), "revenue_dollars": ("sales at menu price", k["revenue"]),
        "collected_dollars": ("payments collected", pay["collected"]), "tips_dollars": ("tips", pay["tips"]),
        "food_cost_pct": ("food cost % of sales", k["food_cost_pct"]), "waste_dollars": ("waste value", k["waste_value"]),
        "stockouts": ("guest ordered with no stock left", k["stockouts"]), "eighty_six": ("dishes 86'd", k["eighty_six"]),
        "late_tickets": ("tickets over 25 min", k["late_tickets"]), "quote_accuracy_pct": ("online ready within quote", k["quote_accuracy_pct"]),
        "order_latency_p95_ms": ("order -> station p95", k["order_latency_p95_ms"]),
        "depletion_p95_ms": ("stock depletion p95", k["depletion_p95_ms"]),
        "decisions_pending": ("decisions awaiting the manager", len(svc.coordinator.pending())),
        "decisions_auto_total": ("decisions executed without approval", k["decisions_auto"]),
        "decisions_approval_total": ("decisions sent for approval", k["decisions_approval"]),
        "bus_events_total": ("events published", svc.bus.seq), "bus_errors_total": ("subscriber errors", len(svc.bus.errors)),
        "service_minutes": ("service clock minutes since start", round(svc.clock.t / 60, 2)),
        "waitlist_parties": ("parties waiting for a table", len(svc.floor.waitlist)),
        "surge_labor_dollars": ("extra labour called in", svc.prep.surge_spent),
    }
    for name, (help_, v) in gauges.items():
        kind = "counter" if name.endswith("_total") else "gauge"
        out += _line(f"restaurant_ops_{name}", v, help_, kind)
    for r in svc.kitchen_load()["stations"]:
        out += _line("restaurant_ops_station_wait_minutes", r["wait_min"], "current wait per station", labels={"station": r["station"]})
        out += _line("restaurant_ops_station_cooks", r["cooks"], "cooks per station", labels={"station": r["station"]})
    return "\n".join(out) + "\n"


# =========================================================================== retention


def cleanup(retention_days: int | None = None, keep_last: int | None = None, keep: set[str] | None = None,
            dry_run: bool = False) -> dict:
    """Delete live service files (db, wal/shm, ckpt, state) older than retention_days, always keeping the newest
    `keep_last` services and any id in `keep`; also batch-sim run DBs in data/runs older than the window."""
    days = settings.LIVE_RETENTION_DAYS if retention_days is None else retention_days
    last = settings.LIVE_KEEP_LAST if keep_last is None else keep_last
    keep = set(keep or ())
    cutoff = time.time() - days * 86400
    removed, kept = [], []
    dbs = sorted(settings.LIVE_DIR.glob("svc-*.db"), key=lambda p: p.stat().st_mtime, reverse=True) if settings.LIVE_DIR.exists() else []
    for i, db in enumerate(dbs):
        sid = db.stem
        if i < last or sid in keep or db.stat().st_mtime >= cutoff:
            kept.append(sid)
            continue
        for f in settings.LIVE_DIR.glob(f"{sid}*"):
            removed.append(f.name)
            if not dry_run:
                f.unlink(missing_ok=True)
    runs = settings.DATA_DIR / "runs"
    if runs.exists():
        for f in runs.glob("*.db*"):
            if f.stat().st_mtime < cutoff:
                removed.append(f"runs/{f.name}")
                if not dry_run:
                    f.unlink(missing_ok=True)
    return {"removed": removed, "kept_services": kept, "retention_days": days, "keep_last": last, "dry_run": dry_run}


# =========================================================================== backups, restore, drill


def _sha(path) -> str:
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def backup(label: str = "") -> dict:
    """Consistent copies (SQLite online backup) of the master DB and every live service DB, plus service snapshots and
    approval checkpoints, with a SHA-256 manifest. Keeps the newest BACKUP_KEEP backups."""
    import shutil
    from datetime import datetime as _dt

    from restaurant_ops.db.connection import copy_db

    bid = _dt.now().strftime("%Y%m%d-%H%M%S") + (f"-{label}" if label else "")
    root = settings.BACKUP_DIR / bid
    (root / "live").mkdir(parents=True, exist_ok=True)
    files = {}
    if settings.DB_PATH.exists():
        copy_db(settings.DB_PATH, root / "restaurant.db")
        files["restaurant.db"] = _sha(root / "restaurant.db")
    if settings.LIVE_DIR.exists():
        for f in sorted(settings.LIVE_DIR.glob("svc-*")):
            if f.name.endswith((".db-wal", ".db-shm", ".tmp")):
                continue
            dst = root / "live" / f.name
            if f.suffix in (".db", ".sqlite"):
                copy_db(f, dst)
            else:
                shutil.copy2(f, dst)
            files[f"live/{f.name}"] = _sha(dst)
    manifest = {"backup_id": bid, "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "files": files}
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2))
    olds = sorted([d for d in settings.BACKUP_DIR.iterdir() if d.is_dir()], key=lambda d: d.name)
    for d in olds[: max(0, len(olds) - settings.BACKUP_KEEP)]:
        shutil.rmtree(d, ignore_errors=True)
    return {"backup_id": bid, "path": str(root), "files": len(files)}


def list_backups() -> list[dict]:
    if not settings.BACKUP_DIR.exists():
        return []
    out = []
    for d in sorted(settings.BACKUP_DIR.iterdir(), reverse=True):
        m = d / "manifest.json"
        if m.exists():
            man = json.loads(m.read_text())
            out.append({"backup_id": man["backup_id"], "created": man["created"], "files": len(man["files"])})
    return out


def verify_backup(backup_id: str) -> dict:
    root = settings.BACKUP_DIR / backup_id
    man = json.loads((root / "manifest.json").read_text())
    bad = [name for name, digest in man["files"].items() if not (root / name).exists() or _sha(root / name) != digest]
    return {"backup_id": backup_id, "files": len(man["files"]), "corrupt_or_missing": bad, "ok": not bad}


def restore(backup_id: str, data_dir=None, live_dir=None) -> dict:
    """Copy a verified backup into data_dir / live_dir (defaults: the live locations; stop the service first)."""
    import shutil
    from pathlib import Path as _P

    v = verify_backup(backup_id)
    if not v["ok"]:
        raise RuntimeError(f"backup {backup_id} failed verification: {v['corrupt_or_missing']}")
    root = settings.BACKUP_DIR / backup_id
    data_dir = _P(data_dir) if data_dir else settings.DB_PATH.parent
    live_dir = _P(live_dir) if live_dir else settings.LIVE_DIR
    data_dir.mkdir(parents=True, exist_ok=True)
    live_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    if (root / "restaurant.db").exists():
        for suffix in ("-wal", "-shm"):
            (data_dir / f"restaurant.db{suffix}").unlink(missing_ok=True)
        shutil.copy2(root / "restaurant.db", data_dir / "restaurant.db")
        n += 1
    for f in (root / "live").glob("*"):
        shutil.copy2(f, live_dir / f.name)
        n += 1
    return {"backup_id": backup_id, "restored_files": n, "data_dir": str(data_dir), "live_dir": str(live_dir)}


def drill() -> dict:
    """Restore drill: take a backup, restore it into a scratch directory, and prove it is usable: checksums, SQLite
    integrity_check on every database, key tables present, and the newest service snapshot loads."""
    import pickle
    import sqlite3
    import tempfile
    from pathlib import Path as _P

    b = backup("drill")
    checks = []
    with tempfile.TemporaryDirectory() as tmp:
        r = restore(b["backup_id"], _P(tmp) / "data", _P(tmp) / "live")
        dbs = [_P(tmp) / "data" / "restaurant.db", *sorted((_P(tmp) / "live").glob("svc-*.db"))]
        for db in dbs:
            if not db.exists():
                continue
            c = sqlite3.connect(db)
            try:
                integ = c.execute("PRAGMA integrity_check").fetchone()[0]
                tables = {t[0] for t in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                need = {"dishes", "ingredients", "lots"} | ({"event_log"} if db.parent.name == "live" else {"users"})
                checks.append({"db": db.name, "integrity": integ, "missing_tables": sorted(need - tables)})
            finally:
                c.close()
        snaps = sorted((_P(tmp) / "live").glob("svc-*.state.pkl"), key=lambda x: x.stat().st_mtime, reverse=True)
        snap_ok = None
        if snaps:
            try:
                pickle.loads(snaps[0].read_bytes())
                snap_ok = snaps[0].name
            except Exception as e:  # noqa: BLE001
                snap_ok = f"FAILED: {type(e).__name__}"
    ok = all(c["integrity"] == "ok" and not c["missing_tables"] for c in checks) and not str(snap_ok).startswith("FAILED")
    return {"ok": ok, "backup_id": b["backup_id"], "restored_files": r["restored_files"], "databases": checks, "snapshot": snap_ok}


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Operations: cleanup (default), backup, verify, restore, drill")
    ap.add_argument("command", nargs="?", default="cleanup", choices=["cleanup", "backup", "list", "verify", "restore", "drill"])
    ap.add_argument("backup_id", nargs="?")
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--keep-last", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if a.command == "backup":
        out = backup()
    elif a.command == "list":
        out = list_backups()
    elif a.command == "verify":
        out = verify_backup(a.backup_id)
    elif a.command == "restore":
        out = restore(a.backup_id)
    elif a.command == "drill":
        out = drill()
    else:
        out = cleanup(a.days, a.keep_last, dry_run=a.dry_run)
    print(json.dumps(out, indent=2))
    return 0 if not isinstance(out, dict) or out.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
