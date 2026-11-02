"""SQLite helpers: connections, schema init, pandas queries, DB copies."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable, Sequence

import pandas as pd

from restaurant_ops.config import settings

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def get_conn(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Open a connection (Row factory, foreign keys on, WAL journal)."""
    path = Path(db_path) if db_path else settings.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create all tables and indexes, then apply pending column migrations (idempotent)."""
    conn.executescript(SCHEMA_PATH.read_text())
    conn.commit()
    from restaurant_ops.db.migrations import migrate

    migrate(conn)


def query_df(sql: str, params: Sequence = (), conn: sqlite3.Connection | None = None) -> pd.DataFrame:
    """Run a SELECT and return a DataFrame (opens/closes its own conn if none given)."""
    own = conn is None
    conn = conn or get_conn()
    try:
        cur = conn.execute(sql, tuple(params))
        cols = [c[0] for c in cur.description or []]
        return pd.DataFrame([tuple(r) for r in cur.fetchall()], columns=cols)
    finally:
        if own:
            conn.close()


def execute(sql: str, params: Sequence = (), conn: sqlite3.Connection | None = None) -> int:
    """Run one write statement and commit; returns lastrowid."""
    own = conn is None
    conn = conn or get_conn()
    try:
        cur = conn.execute(sql, tuple(params))
        conn.commit()
        return cur.lastrowid
    finally:
        if own:
            conn.close()


def executemany(sql: str, rows: Iterable[Sequence], conn: sqlite3.Connection | None = None) -> int:
    """Run a write statement for many rows and commit; returns row count."""
    own = conn is None
    conn = conn or get_conn()
    try:
        cur = conn.executemany(sql, [tuple(r) for r in rows])
        conn.commit()
        return cur.rowcount
    finally:
        if own:
            conn.close()


def copy_db(src: str | Path, dst: str | Path) -> Path:
    """Copy a database (consistent snapshot via the sqlite backup API) for sim runs."""
    src, dst = Path(src), Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
        for suffix in ("-wal", "-shm"):
            Path(str(dst) + suffix).unlink(missing_ok=True)
    s = sqlite3.connect(str(src))
    d = sqlite3.connect(str(dst))
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()
    return dst
