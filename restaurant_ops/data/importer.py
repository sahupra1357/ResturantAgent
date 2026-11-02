"""Import real POS history: map a POS export (CSV, one row per item sold) onto `covers` and `pos_sales`.

    python -m restaurant_ops.data.importer sales.csv --db data/restaurant.db [--items-map map.csv]
           [--columns '{"opened_at": "Time", "item": "Menu Item"}'] [--replace] [--set-sim-start] [--dry-run]

Expected columns (rename with --columns): check_id, opened_at (ISO date-time or 'YYYY-MM-DD HH:MM'), item, qty,
guests (covers on the check; optional, repeated on every row of the check). Item names map to dish ids by: an
explicit mapping file (pos_item,dish_id), the dish id itself, the dish name (case-insensitive), then a fuzzy match
(difflib ratio >= FUZZY_CUTOFF). Unmapped items are reported, not guessed. The meal period comes from the check's
opening time (lunch < 15:00 <= dinner < 22:00 <= late). Covers per period = sum of guests per check (or, without a
guests column, checks x AVG_GUESTS_PER_CHECK). The forecast model pickle is removed so the next run retrains on the
imported history.
"""
from __future__ import annotations

import argparse
import csv
import difflib
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from restaurant_ops.config import settings
from restaurant_ops.data import catalog as cat
from restaurant_ops.db.connection import get_conn, init_db

DEFAULT_COLUMNS = {"check_id": "check_id", "opened_at": "opened_at", "item": "item", "qty": "qty", "guests": "guests"}
FUZZY_CUTOFF = 0.8
AVG_GUESTS_PER_CHECK = 2.4
PERIOD_BOUNDS = (("lunch", 0, 15), ("dinner", 15, 22), ("late", 22, 24))


class ImportError_(ValueError):
    pass


def period_of(ts: datetime) -> str:
    h = ts.hour
    for name, lo, hi in PERIOD_BOUNDS:
        if lo <= h < hi:
            return name
    return "late"


def parse_ts(v: str) -> datetime:
    v = (v or "").strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%m/%d/%Y %H:%M", "%d/%m/%Y %H:%M"):
        try:
            return datetime.strptime(v[:19] if fmt.endswith("%S") else v[:16], fmt)
        except ValueError:
            continue
    raise ImportError_(f"unreadable timestamp {v!r}")


def item_matcher(mapping: dict[str, str] | None = None):
    names = {d.name.lower(): d.id for d in cat.DISHES}
    ids = {d.id for d in cat.DISHES}
    explicit = {k.strip().lower(): v for k, v in (mapping or {}).items()}

    def match(name: str) -> tuple[str | None, str]:
        n = (name or "").strip().lower()
        if n in explicit:
            return (explicit[n] if explicit[n] in ids else None), "map"
        if n in ids:
            return n, "id"
        if n in names:
            return names[n], "name"
        best = difflib.get_close_matches(n, list(names), n=1, cutoff=FUZZY_CUTOFF)
        if best:
            return names[best[0]], "fuzzy"
        return None, "unmapped"

    return match


def read_rows(path: Path, columns: dict[str, str]) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        rd = csv.DictReader(f)
        missing = [c for k, c in columns.items() if k != "guests" and c not in (rd.fieldnames or [])]
        if missing:
            raise ImportError_(f"missing column(s) {missing}; found {rd.fieldnames}")
        return list(rd)


def transform(rows: list[dict], columns: dict[str, str], mapping: dict[str, str] | None = None) -> dict:
    match = item_matcher(mapping)
    sales: dict[tuple, int] = defaultdict(int)
    checks: dict[str, dict] = {}
    unmapped: dict[str, int] = defaultdict(int)
    how: dict[str, int] = defaultdict(int)
    bad_rows = 0
    has_guests = columns.get("guests") in (rows[0] if rows else {})
    for r in rows:
        try:
            ts = parse_ts(r[columns["opened_at"]])
            qty = int(float(r.get(columns["qty"]) or 1))
        except (ImportError_, ValueError):
            bad_rows += 1
            continue
        d, kind = match(r[columns["item"]])
        how[kind] += 1
        key = (ts.date().isoformat(), period_of(ts))
        cid = r.get(columns["check_id"]) or f"{ts.isoformat()}"
        chk = checks.setdefault(cid, {"key": key, "guests": 0})
        if has_guests:
            try:
                chk["guests"] = max(chk["guests"], int(float(r.get(columns["guests"]) or 0)))
            except ValueError:
                pass
        if d is None:
            unmapped[r[columns["item"]]] += qty
            continue
        sales[(*key, d)] += qty
    covers: dict[tuple, float] = defaultdict(float)
    for c in checks.values():
        covers[c["key"]] += c["guests"] if has_guests and c["guests"] > 0 else AVG_GUESTS_PER_CHECK
    dates = sorted({k[0] for k in covers})
    return {"sales": dict(sales), "covers": {k: int(round(v)) for k, v in covers.items()}, "unmapped": dict(unmapped),
            "matched_by": dict(how), "bad_rows": bad_rows, "checks": len(checks),
            "date_range": [dates[0], dates[-1]] if dates else None}


def write(conn: sqlite3.Connection, data: dict, replace: bool = True) -> dict:
    if not data["date_range"]:
        raise ImportError_("nothing to import")
    d0, d1 = data["date_range"]
    if replace:
        conn.execute("DELETE FROM pos_sales WHERE date BETWEEN ? AND ?", (d0, d1))
        conn.execute("DELETE FROM covers WHERE date BETWEEN ? AND ?", (d0, d1))
    conn.executemany("INSERT OR REPLACE INTO covers (date, meal_period, covers) VALUES (?,?,?)",
                     [(d, p, n) for (d, p), n in sorted(data["covers"].items())])
    conn.executemany("INSERT OR REPLACE INTO pos_sales (date, meal_period, dish_id, qty) VALUES (?,?,?,?)",
                     [(d, p, dish, q) for (d, p, dish), q in sorted(data["sales"].items())])
    conn.commit()
    return {"covers_rows": len(data["covers"]), "sales_rows": len(data["sales"])}


def import_pos(path: "str | Path", db: "str | Path | None" = None, columns: dict | None = None, mapping_file: "str | Path | None" = None,
               replace: bool = True, set_sim_start: bool = False, dry_run: bool = False) -> dict:
    cols = {**DEFAULT_COLUMNS, **(columns or {})}
    mapping = None
    if mapping_file:
        with open(mapping_file, newline="", encoding="utf-8-sig") as f:
            mapping = {r["pos_item"]: r["dish_id"] for r in csv.DictReader(f)}
    data = transform(read_rows(Path(path), cols), cols, mapping)
    summary = {k: v for k, v in data.items() if k not in ("sales", "covers")}
    summary.update(total_items=sum(data["sales"].values()), total_covers=sum(data["covers"].values()))
    if dry_run:
        return {**summary, "written": None}
    conn = get_conn(db or settings.DB_PATH)
    try:
        init_db(conn)
        if not conn.execute("SELECT 1 FROM dishes LIMIT 1").fetchone():
            cat.load_catalog(conn)
        summary["written"] = write(conn, data, replace)
        if set_sim_start:
            nxt = (date.fromisoformat(data["date_range"][1]) + timedelta(days=1)).isoformat()
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('sim_start', ?)", (nxt,))
            conn.commit()
            summary["sim_start"] = nxt
    finally:
        conn.close()
    from restaurant_ops.core.forecasting import MODEL_FILE

    (settings.MODELS_DIR / MODEL_FILE).unlink(missing_ok=True)
    summary["models"] = "removed: the next forecast retrains on the imported history"
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Import POS history (CSV) into covers / pos_sales")
    ap.add_argument("csv")
    ap.add_argument("--db", default=str(settings.DB_PATH))
    ap.add_argument("--items-map", default=None, help="CSV with columns pos_item,dish_id")
    ap.add_argument("--columns", default=None, help="JSON mapping of our column names to the export's")
    ap.add_argument("--no-replace", action="store_true", help="keep existing rows in the imported date range")
    ap.add_argument("--set-sim-start", action="store_true", help="make the day after the last imported day the first live day")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    try:
        out = import_pos(a.csv, a.db, json.loads(a.columns) if a.columns else None, a.items_map, not a.no_replace,
                         a.set_sim_start, a.dry_run)
    except (ImportError_, FileNotFoundError) as e:
        print(f"import failed: {e}", file=sys.stderr)
        return 1
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
