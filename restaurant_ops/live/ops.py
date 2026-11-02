"""Operability: structured (JSON) logging, a Prometheus /metrics exposition, and retention of live service files."""
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


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Delete old live-service and batch-run databases")
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--keep-last", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    print(json.dumps(cleanup(a.days, a.keep_last, dry_run=a.dry_run), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
