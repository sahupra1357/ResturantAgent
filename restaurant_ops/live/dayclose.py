"""End of day: reconciliation, cash drawer count and an accounting export.

`close_day(svc, counted_cash, by)`:
  1. the service must be over (open checks block the close unless force=True, which records them as exceptions);
  2. reconcile: checks due vs collected by method, tips, discounts / comps, refunds, gift-card and points redemptions,
     tax by jurisdiction, cash expected vs counted (over / short);
  3. a double-entry journal (debits = credits, tested) written to EXPORT_DIR/{date}-{period}/journal.csv with a
     summary.json, and the close stored in the master DB `day_close` table (one per date and meal period).

Accounts: 1000 Cash, 1100 Card clearing, 2100 Sales tax payable (one line per jurisdiction), 2200 Tips payable,
2300 Gift card liability, 2400 Loyalty liability, 4000 Food sales, 4010 Alcohol sales, 4100 Delivery fees,
4900 Discounts & comps (contra revenue), 4950 Refunds (contra revenue), 6900 Cash over / short.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import TYPE_CHECKING

from restaurant_ops.config import settings
from restaurant_ops.live.events import wall_now

if TYPE_CHECKING:  # pragma: no cover
    from restaurant_ops.live.service import Service

ACCOUNTS = {"1000": "Cash", "1100": "Card clearing", "2100": "Sales tax payable", "2200": "Tips payable",
            "2300": "Gift card liability", "2400": "Loyalty liability", "4000": "Food sales", "4010": "Alcohol sales",
            "4100": "Delivery fees", "4900": "Discounts & comps", "4950": "Refunds", "6900": "Cash over/short"}
CLEARING = {"card": "1100", "wallet": "1100", "online": "1100", "cash": "1000", "gift_card": "2300", "points": "2400"}


class DayCloseError(RuntimeError):
    pass


def _r(x: float) -> float:
    return round(x + 1e-9, 2)


def reconcile(svc: "Service", counted_cash: float | None = None) -> dict:
    pay = svc.payments
    by_method: dict[str, float] = {}
    tips_by_method: dict[str, float] = {}
    sales = {"food": 0.0, "alcohol": 0.0, "delivery_fee": 0.0}
    tax: dict[str, float] = {}
    discounts = refunds = 0.0
    refunds_by_method: dict[str, float] = {}
    open_checks = []
    for c in pay.checks.values():
        t = pay.totals(c)
        if c.status == "open":
            open_checks.append({"check_id": c.check_id, "tables": c.table_ids, "balance": t["balance"]})
        for l in c.lines:
            sales[pay._category(l)] = sales.get(pay._category(l), 0.0) + l["price"]
        discounts += t["discounts"]
        for tl in t["tax_lines"]:
            key = f"{tl['name']} ({tl['jurisdiction']})" if tl["jurisdiction"] else tl["name"]
            tax[key] = tax.get(key, 0.0) + tl["amount"]
        for p in c.payments:
            by_method[p["method"]] = by_method.get(p["method"], 0.0) + p["amount"]
            tips_by_method[p["method"]] = tips_by_method.get(p["method"], 0.0) + p.get("tip", 0.0)
        for r in c.refunds:
            refunds += r["amount"]
            m = "card" if (r.get("ref") or "").startswith(("sim_re", "re_")) else "gift_card" if (r.get("ref") or "").startswith("gc:") else "cash"
            refunds_by_method[m] = refunds_by_method.get(m, 0.0) + r["amount"]
    expected_cash = by_method.get("cash", 0.0) + tips_by_method.get("cash", 0.0) - refunds_by_method.get("cash", 0.0)
    over_short = None if counted_cash is None else _r(counted_cash - expected_cash)
    collected = sum(by_method.values())
    due = sum(pay.totals(c)["due"] for c in pay.checks.values())
    return {"collected_by_method": {k: _r(v) for k, v in sorted(by_method.items())},
            "tips_by_method": {k: _r(v) for k, v in sorted(tips_by_method.items())},
            "refunds_by_method": {k: _r(v) for k, v in sorted(refunds_by_method.items())},
            "sales": {k: _r(v) for k, v in sales.items()}, "discounts_and_comps": _r(discounts), "refunds": _r(refunds),
            "tax_by_jurisdiction": {k: _r(v) for k, v in sorted(tax.items())}, "checks_due": _r(due), "collected": _r(collected),
            "difference_due_vs_collected": _r(due - collected), "expected_cash": _r(expected_cash),
            "counted_cash": None if counted_cash is None else _r(counted_cash), "cash_over_short": over_short,
            "open_checks": open_checks, "checks": len(pay.checks)}


def journal(rec: dict) -> list[dict]:
    """Balanced journal lines for the day (amounts rounded to cents; any rounding goes to over/short)."""
    lines: list[dict] = []

    def add(acct: str, debit: float = 0.0, credit: float = 0.0, memo: str = "") -> None:
        if abs(debit) > 0.004 or abs(credit) > 0.004:
            lines.append({"account": acct, "name": ACCOUNTS[acct], "debit": _r(debit), "credit": _r(credit), "memo": memo})

    for m, amt in rec["collected_by_method"].items():
        add(CLEARING.get(m, "1100"), debit=amt + rec["tips_by_method"].get(m, 0.0), memo=f"{m} received incl. tips")
    add("4000", credit=rec["sales"].get("food", 0.0), memo="food sales at menu price")
    add("4010", credit=rec["sales"].get("alcohol", 0.0), memo="alcohol sales")
    add("4100", credit=rec["sales"].get("delivery_fee", 0.0), memo="delivery fees")
    add("4900", debit=rec["discounts_and_comps"], memo="discounts and comps")
    for k, v in rec["tax_by_jurisdiction"].items():
        add("2100", credit=v, memo=k)
    add("2200", credit=sum(rec["tips_by_method"].values()), memo="tips owed to staff")
    for m, amt in rec["refunds_by_method"].items():
        add("4950", debit=amt, memo=f"refunds ({m})")
        add(CLEARING.get(m, "1100"), credit=amt, memo=f"refunds paid out ({m})")
    # open checks (force close) are receivable-like: their unpaid balance stays out of revenue via a contra line
    unpaid = sum(o["balance"] for o in rec["open_checks"])
    if unpaid:
        add("4900", debit=unpaid, memo="open checks at close (written off pending follow-up)")
    if rec["cash_over_short"]:
        if rec["cash_over_short"] > 0:
            add("1000", debit=rec["cash_over_short"], memo="cash over")
            add("6900", credit=rec["cash_over_short"], memo="cash over")
        else:
            add("6900", debit=-rec["cash_over_short"], memo="cash short")
            add("1000", credit=-rec["cash_over_short"], memo="cash short")
    diff = _r(sum(l["debit"] for l in lines) - sum(l["credit"] for l in lines))
    if diff:  # cent rounding across tax rules / splits
        add("6900", debit=-diff if diff < 0 else 0.0, credit=diff if diff > 0 else 0.0, memo="rounding")
    return lines


def close_day(svc: "Service", counted_cash: float | None, by: str, force: bool = False) -> dict:
    if svc.status != "ended":
        if not force:
            raise DayCloseError("the service is still running: end it (or close with force)")
        svc.end()
    rec = reconcile(svc, counted_cash)
    if rec["open_checks"] and not force:
        raise DayCloseError(f"{len(rec['open_checks'])} check(s) still open: settle them or close with force")
    lines = journal(rec)
    debits, credits = _r(sum(l["debit"] for l in lines)), _r(sum(l["credit"] for l in lines))
    out_dir = Path(settings.EXPORT_DIR) / f"{svc.date.isoformat()}-{svc.period}"
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "journal.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "account", "name", "debit", "credit", "memo"])
        w.writeheader()
        for l in lines:
            w.writerow({"date": svc.date.isoformat(), **l})
    report = {"date": svc.date.isoformat(), "meal_period": svc.period, "service_id": svc.service_id, "closed_by": by,
              "closed_ts": wall_now(), "forced": force, "reconciliation": rec, "journal": lines,
              "totals": {"debits": debits, "credits": credits, "balanced": debits == credits},
              "kpis": svc.kpis(), "files": {"journal_csv": str(out_dir / "journal.csv"), "summary_json": str(out_dir / "summary.json")}}
    (out_dir / "summary.json").write_text(json.dumps(report, indent=2, default=str))
    with svc.master() as m:
        m.execute("INSERT OR REPLACE INTO day_close (date, meal_period, service_id, closed_ts, closed_by, report) VALUES (?,?,?,?,?,?)",
                  (svc.date.isoformat(), svc.period, svc.service_id, report["closed_ts"], by, json.dumps(report, default=str)))
    svc.conn.execute("UPDATE service_runs SET status='closed' WHERE service_id=?", (svc.service_id,))
    svc.flush()
    return report


def history(conn, limit: int = 30) -> list[dict]:
    rows = conn.execute("SELECT date, meal_period, service_id, closed_ts, closed_by, report FROM day_close ORDER BY date DESC, meal_period LIMIT ?",
                        (limit,)).fetchall()
    out = []
    for r in rows:
        rep = json.loads(r["report"])
        out.append({"date": r["date"], "meal_period": r["meal_period"], "service_id": r["service_id"], "closed_ts": r["closed_ts"],
                    "closed_by": r["closed_by"], "collected": rep["reconciliation"]["collected"],
                    "cash_over_short": rep["reconciliation"]["cash_over_short"], "balanced": rep["totals"]["balanced"]})
    return out
