"""Small shared helpers for the deterministic core (dates, connections, rounding). No LLM imports."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Iterator

from restaurant_ops.config import settings
from restaurant_ops.db.connection import get_conn


def to_date(x: "str | date | datetime | None") -> date:
    """Parse an ISO string / date / datetime into a date (None -> SIM_START)."""
    if x is None:
        return settings.SIM_START
    if isinstance(x, datetime):
        return x.date()
    if isinstance(x, date):
        return x
    return date.fromisoformat(str(x)[:10])


def day_label(x: "str | date | datetime | None") -> str:
    """'2026-11-02 (Monday)'; the input unchanged if it is not a date."""
    try:
        d = to_date(x)
    except (TypeError, ValueError):
        return str(x)
    return f"{d.isoformat()} ({d:%A})"


def week_outlook(x: "str | date | datetime | None", days: int = 7) -> str:
    """'Tue 2026-11-03, Wed 2026-11-04, ... Sat 2026-11-07 (weekend)': the next `days` days after x."""
    try:
        d = to_date(x)
    except (TypeError, ValueError):
        return ""
    out = []
    for i in range(1, days + 1):
        n = d + timedelta(days=i)
        out.append(f"{n:%a} {n.isoformat()}" + (" (weekend)" if n.weekday() >= 5 else ""))
    return ", ".join(out)


def with_weekday(rows: list[dict], key: str = "date") -> list[dict]:
    """Add a 'weekday' ('Mon'..'Sun') to each row that has an ISO date under `key`."""
    for r in rows:
        try:
            r.setdefault("weekday", to_date(r[key]).strftime("%a"))
        except (KeyError, TypeError, ValueError):
            pass
    return rows


def iso(x: "str | date | datetime | None") -> str:
    """ISO date string for a date-like value."""
    return to_date(x).isoformat()


def daterange(start: "str | date", days: int) -> list[date]:
    """`days` consecutive dates starting at `start`."""
    s = to_date(start)
    return [s + timedelta(days=i) for i in range(int(days))]


def r2(x: float, nd: int = 2) -> float:
    """Round a number to `nd` places and return a plain float."""
    return round(float(x), nd)


def rq(x: float) -> float:
    """Round a quantity to 3 places (ingredient units)."""
    return round(float(x), 3)


@contextmanager
def conn_ctx(conn: sqlite3.Connection | None = None) -> Iterator[sqlite3.Connection]:
    """Yield `conn`, or open (and close afterwards) a default connection."""
    if conn is not None:
        yield conn
        return
    c = get_conn()
    try:
        yield c
    finally:
        c.close()
