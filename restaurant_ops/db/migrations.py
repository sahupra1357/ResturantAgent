"""Versioned schema migrations for columns added after a database was created.

`schema.sql` only creates missing tables (CREATE TABLE IF NOT EXISTS); a column added to an existing table needs an
ALTER. Each migration runs once, in order, and the version is stored in `schema_version`. Migrations are idempotent
(they check `PRAGMA table_info` first), so a half-applied run can simply be repeated.
"""
from __future__ import annotations

import sqlite3


def has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})"))


def add_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    if not has_column(conn, table, column):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _m1(c: sqlite3.Connection) -> None:
    add_column(c, "dishes", "tax_category", "TEXT NOT NULL DEFAULT 'food'")
    add_column(c, "suppliers", "email", "TEXT NOT NULL DEFAULT ''")
    add_column(c, "payments", "processor_ref", "TEXT")


MIGRATIONS = [(1, _m1)]
LATEST = MIGRATIONS[-1][0]


def current(conn: sqlite3.Connection) -> int:
    r = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    return int(r[0] or 0)


def migrate(conn: sqlite3.Connection) -> list[int]:
    done = []
    v = current(conn)
    for version, fn in MIGRATIONS:
        if version > v:
            fn(conn)
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
            done.append(version)
    conn.commit()
    return done
