"""Gift cards: stored-value liability in the master database (cards outlive a single service).

Every movement is a ledger row (issue / redeem / refund / void); the card's balance is kept in `gift_cards` and the
two always agree (tested). Codes are random, grouped for reading aloud, and never reused.
"""
from __future__ import annotations

import secrets
import sqlite3

from restaurant_ops.live.events import wall_now

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I


class GiftCardError(ValueError):
    pass


def new_code() -> str:
    raw = "".join(secrets.choice(ALPHABET) for _ in range(12))
    return "-".join(raw[i:i + 4] for i in range(0, 12, 4))


def issue(conn: sqlite3.Connection, amount: float, note: str = "", service_id: str | None = None) -> dict:
    amount = round(float(amount), 2)
    if not 1 <= amount <= 1000:
        raise GiftCardError("gift cards are 1..1000")
    code = new_code()
    conn.execute("INSERT INTO gift_cards (code, initial, balance, issued, status, note) VALUES (?,?,?,?,?,?)",
                 (code, amount, amount, wall_now(), "active", note))
    conn.execute("INSERT INTO gift_card_ledger (code, ts, kind, amount, ref, service_id) VALUES (?,?,?,?,?,?)",
                 (code, wall_now(), "issue", amount, note, service_id))
    conn.commit()
    return get(conn, code)


def get(conn: sqlite3.Connection, code: str) -> dict:
    r = conn.execute("SELECT * FROM gift_cards WHERE code=?", ((code or "").strip().upper(),)).fetchone()
    if r is None:
        raise GiftCardError("unknown gift card")
    return dict(r)


def redeem(conn: sqlite3.Connection, code: str, amount: float, ref: str = "", service_id: str | None = None) -> dict:
    g = get(conn, code)
    amount = round(float(amount), 2)
    if g["status"] != "active":
        raise GiftCardError(f"gift card is {g['status']}")
    if amount <= 0 or amount > g["balance"] + 1e-9:
        raise GiftCardError(f"gift card balance is {g['balance']:.2f}")
    conn.execute("UPDATE gift_cards SET balance=ROUND(balance-?, 2) WHERE code=?", (amount, g["code"]))
    conn.execute("INSERT INTO gift_card_ledger (code, ts, kind, amount, ref, service_id) VALUES (?,?,?,?,?,?)",
                 (g["code"], wall_now(), "redeem", -amount, ref, service_id))
    conn.commit()
    return get(conn, g["code"])


def credit(conn: sqlite3.Connection, code: str, amount: float, ref: str = "", service_id: str | None = None) -> dict:
    g = get(conn, code)
    conn.execute("UPDATE gift_cards SET balance=ROUND(balance+?, 2) WHERE code=?", (round(float(amount), 2), g["code"]))
    conn.execute("INSERT INTO gift_card_ledger (code, ts, kind, amount, ref, service_id) VALUES (?,?,?,?,?,?)",
                 (g["code"], wall_now(), "refund", round(float(amount), 2), ref, service_id))
    conn.commit()
    return get(conn, g["code"])


def void(conn: sqlite3.Connection, code: str, reason: str) -> dict:
    g = get(conn, code)
    conn.execute("UPDATE gift_cards SET status='void', balance=0 WHERE code=?", (g["code"],))
    conn.execute("INSERT INTO gift_card_ledger (code, ts, kind, amount, ref) VALUES (?,?,?,?,?)",
                 (g["code"], wall_now(), "void", -g["balance"], reason))
    conn.commit()
    return get(conn, g["code"])


def ledger(conn: sqlite3.Connection, code: str | None = None, service_id: str | None = None) -> list[dict]:
    q, args = "SELECT * FROM gift_card_ledger WHERE 1=1", []
    if code:
        q += " AND code=?"
        args.append(code.strip().upper())
    if service_id:
        q += " AND service_id=?"
        args.append(service_id)
    return [dict(r) for r in conn.execute(q + " ORDER BY id", args)]
