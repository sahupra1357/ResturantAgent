"""CLI: build the seeded database.  `python -m restaurant_ops.data.seed [--db PATH] [--force] [--sim-start YYYY-MM-DD]`

The simulation starts on --sim-start, else SIM_START from the env / .env, else today, so the 365 days of history
end on yesterday's date. The chosen date is stored in the DB (meta.sim_start) and every later process reads it there.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from restaurant_ops.config import settings
from restaurant_ops.data.catalog import load_catalog
from restaurant_ops.data.generator import generate_all
from restaurant_ops.db.connection import get_conn, init_db

TABLES = ["ingredients", "dishes", "recipe_lines", "suppliers", "supplier_items", "lots", "covers",
          "pos_sales", "reservations", "events", "weather", "staff_shifts", "purchase_orders",
          "po_lines", "stock_counts"]


def main(argv: list[str] | None = None) -> int:
    """Create schema, load catalog, generate history, print a summary."""
    ap = argparse.ArgumentParser(description="Seed the restaurant database")
    ap.add_argument("--db", type=Path, default=settings.DB_PATH, help="database path")
    ap.add_argument("--force", action="store_true", help="overwrite an existing database")
    ap.add_argument("--sim-start", type=date.fromisoformat, default=None,
                    help="first simulated day (default: SIM_START from .env, else today; history ends the day before)")
    args = ap.parse_args(argv)

    if args.sim_start is not None:
        settings.SIM_START, settings.SIM_START_SOURCE = args.sim_start, "cli"
    elif settings.SIM_START_SOURCE != "env":
        settings.SIM_START, settings.SIM_START_SOURCE = date.today(), "today"

    if args.db.exists():
        if not args.force:
            print(f"{args.db} exists; use --force to rebuild", file=sys.stderr)
            return 1
        for suffix in ("", "-wal", "-shm"):
            Path(str(args.db) + suffix).unlink(missing_ok=True)

    conn = get_conn(args.db)
    init_db(conn)
    cat_counts = load_catalog(conn)
    gen = generate_all(conn)
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('sim_start', ?)", (settings.SIM_START.isoformat(),))
    conn.commit()
    # the forecast models were trained on the previous data; drop them so the next run retrains on this seed
    from restaurant_ops.core.forecasting import MODEL_FILE
    (settings.MODELS_DIR / MODEL_FILE).unlink(missing_ok=True)

    print(f"Database: {args.db}  (seed={settings.SEED}, sim_start={settings.SIM_START} {settings.SIM_START:%A}, "
          f"source={settings.SIM_START_SOURCE})")
    print(f"Catalog : {cat_counts}")
    print(f"Generated: {gen}")
    for t in TABLES:
        print(f"  {t:<16}{conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]:>8}")
    r = conn.execute("SELECT MIN(date), MAX(date) FROM covers").fetchone()
    print(f"History : {r[0]} .. {r[1]}")
    for row in conn.execute(
        "SELECT meal_period, ROUND(AVG(covers),1) FROM covers GROUP BY meal_period"
    ):
        print(f"  avg covers {row[0]:<7}{row[1]}")
    row = conn.execute("SELECT ROUND(SUM(p.qty*d.price),0) FROM pos_sales p JOIN dishes d ON d.id=p.dish_id").fetchone()
    print(f"Total revenue (history): ${row[0]:,.0f}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
