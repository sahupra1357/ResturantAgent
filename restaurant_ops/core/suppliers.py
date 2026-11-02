"""Supplier performance learning: fill rate and delay probability move with observed deliveries.

`suppliers.fill_rate` / `delay_prob` start as catalog priors. `scorecard` blends them with the delivered vs ordered
quantities in the PO history (each delivered PO counts as one observation, against a prior worth PRIOR_WEIGHT
observations). `observe` folds in one new observation (a delivery, a shortfall notice, a late truck), writes the
updated numbers back to `suppliers` and appends the observation to `supplier_perf`. Supplier quotes and the
optimizer read `suppliers`, so a supplier that keeps short-shipping gets a worse risk-adjusted price. No LLM here.
"""
from __future__ import annotations

import sqlite3

from restaurant_ops.core.util import r2
from restaurant_ops.data import catalog as cat

PRIOR_WEIGHT = 8.0   # the catalog prior counts as this many observed deliveries
OBS_WEIGHT = 1.0
MIN_FILL, MAX_FILL = 0.05, 1.0

SCHEMA = """CREATE TABLE IF NOT EXISTS supplier_perf (
    id INTEGER PRIMARY KEY AUTOINCREMENT, supplier_id TEXT NOT NULL, ts TEXT NOT NULL, kind TEXT NOT NULL,
    fill REAL, late INTEGER, weight REAL NOT NULL, fill_rate_after REAL, delay_prob_after REAL, note TEXT)"""


def _ensure(conn: sqlite3.Connection) -> None:
    conn.execute(SCHEMA)


def history(conn: sqlite3.Connection, supplier_id: str | None = None) -> dict[str, dict]:
    """Observed fill per supplier from delivered / partial POs: {supplier: {fill, n_pos, ordered, delivered}}."""
    q = ("SELECT p.supplier_id, p.po_id, SUM(l.qty) AS ordered, SUM(COALESCE(l.qty_delivered, 0)) AS got "
         "FROM purchase_orders p JOIN po_lines l ON l.po_id=p.po_id WHERE p.status IN ('delivered','partial') "
         "AND l.qty_delivered IS NOT NULL")
    args: list = []
    if supplier_id:
        q += " AND p.supplier_id=?"
        args.append(supplier_id)
    out: dict[str, dict] = {}
    for r in conn.execute(q + " GROUP BY p.supplier_id, p.po_id", args):
        o = out.setdefault(r[0], {"n_pos": 0, "ordered": 0.0, "delivered": 0.0, "fills": []})
        o["n_pos"] += 1
        o["ordered"] += float(r[2])
        o["delivered"] += float(r[3])
        o["fills"].append(float(r[3]) / float(r[2]) if r[2] else 1.0)
    for o in out.values():
        o["fill"] = round(sum(o["fills"]) / len(o["fills"]), 4)
        del o["fills"]
    return out


def scorecard(conn: sqlite3.Connection, write: bool = True) -> list[dict]:
    """Blend each supplier's catalog prior with its PO history; optionally store the result in `suppliers`."""
    _ensure(conn)
    hist = history(conn)
    rows = []
    for s in cat.SUPPLIERS:
        h = hist.get(s.id)
        n = float(h["n_pos"]) if h else 0.0
        fill = (s.fill_rate * PRIOR_WEIGHT + (h["fill"] * n if h else 0.0)) / (PRIOR_WEIGHT + n)
        cur = conn.execute("SELECT fill_rate, delay_prob FROM suppliers WHERE id=?", (s.id,)).fetchone()
        delay = float(cur[1]) if cur else s.delay_prob
        if write:
            conn.execute("UPDATE suppliers SET fill_rate=? WHERE id=?", (round(fill, 4), s.id))
        rows.append({"supplier_id": s.id, "supplier": s.name, "prior_fill": s.fill_rate,
                     "observed_fill": h["fill"] if h else None, "observed_pos": int(n), "fill_rate": r2(fill, 4),
                     "delay_prob": r2(delay, 4)})
    if write:
        conn.commit()
    return rows


def observe(conn: sqlite3.Connection, supplier_id: str, ts: str, kind: str, fill: float | None = None,
            late: bool | None = None, weight: float = OBS_WEIGHT, note: str = "") -> dict:
    """One observation: kind 'delivery' | 'shortfall' | 'delay'. Returns the supplier's updated reliability."""
    _ensure(conn)
    row = conn.execute("SELECT fill_rate, delay_prob FROM suppliers WHERE id=?", (supplier_id,)).fetchone()
    if row is None:
        raise KeyError(f"unknown supplier {supplier_id}")
    f0, d0 = float(row[0]), float(row[1])
    n = PRIOR_WEIGHT + conn.execute("SELECT COALESCE(SUM(weight),0) FROM supplier_perf WHERE supplier_id=?", (supplier_id,)).fetchone()[0]
    f1, d1 = f0, d0
    if fill is not None:
        f1 = min(MAX_FILL, max(MIN_FILL, (f0 * n + float(fill) * weight) / (n + weight)))
    if late is not None:
        d1 = min(0.95, max(0.0, (d0 * n + (1.0 if late else 0.0) * weight) / (n + weight)))
    conn.execute("UPDATE suppliers SET fill_rate=?, delay_prob=? WHERE id=?", (round(f1, 4), round(d1, 4), supplier_id))
    conn.execute("INSERT INTO supplier_perf (supplier_id, ts, kind, fill, late, weight, fill_rate_after, delay_prob_after, note) "
                 "VALUES (?,?,?,?,?,?,?,?,?)", (supplier_id, ts, kind, fill, None if late is None else int(late), weight,
                                                round(f1, 4), round(d1, 4), note))
    return {"supplier_id": supplier_id, "fill_rate_before": round(f0, 4), "fill_rate": round(f1, 4),
            "delay_prob_before": round(d0, 4), "delay_prob": round(d1, 4)}
