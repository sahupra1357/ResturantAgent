"""KPIs for a simulated run, and the agent-vs-baseline comparison. No LLM imports here.

`kpis(db_path, run_id, policy)` reads `kpi_daily`, delivered purchase value and `forecast_log` from the policy's DB copy.
Definitions:
  waste_value       sum of waste_log value over the run days (expired + discarded + cooler-failure spoilage)
  waste_pct         waste_value / value of goods received (po_lines delivered x price, POs expected on/after SIM_START) x 100
  stockouts         number of (meal period, dish) instances that ran out of stock and lost sales
  dishes_86d        dish-days on which a dish was 86'd on the menu
  food_cost_pct     food cost of consumed lots / revenue x 100
  approvals_needed  decisions that went through the chef approval queue (agent only; the baseline has none)
  mape, p90_coverage  daily covers forecast (P50 / P90 logged at the start of each service) vs realised covers
"""
from __future__ import annotations

import json
from pathlib import Path

from restaurant_ops.config import settings, sim_start_from_db
from restaurant_ops.db.connection import get_conn

LOWER_IS_BETTER = ("waste_value", "waste_pct", "stockouts", "food_cost_pct", "lost_revenue", "dishes_86d", "approvals_needed")
HIGHER_IS_BETTER = ("revenue", "covers_served")
KEYS = ("waste_value", "waste_pct", "stockouts", "dishes_86d", "food_cost_pct", "revenue", "covers_served",
        "approvals_needed", "lost_revenue", "mape", "p90_coverage")


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return default


def kpis(db_path: str | Path, run_id: str, policy: str) -> dict:
    """Aggregate KPIs of one policy's run (see module docstring). Adds `days` and `daily` (per-day kpi_daily rows)."""
    start = (sim_start_from_db(db_path) or settings.SIM_START).isoformat()  # this run's own start, not today's seed
    conn = get_conn(db_path)
    try:
        daily = [dict(r) for r in conn.execute("SELECT * FROM kpi_daily WHERE run_id=? AND policy=? ORDER BY date", (run_id, policy))]
        s = lambda k: float(sum((r[k] or 0) for r in daily))  # noqa: E731
        revenue, food_cost, waste = s("revenue"), s("food_cost"), s("waste_value")
        bought = _safe(lambda: conn.execute(
            "SELECT COALESCE(SUM(COALESCE(l.qty_delivered,0)*l.unit_price),0) FROM po_lines l JOIN purchase_orders p ON p.po_id=l.po_id "
            "WHERE p.created_date>=? AND p.status IN ('delivered','partial')", (start,)).fetchone()[0], 0.0)
        lost = 0.0
        for (c,) in _safe(lambda: conn.execute("SELECT content FROM trace WHERE run_id=? AND node='sim' AND kind='event' "
                                               "AND content LIKE '%day_summary%'", (run_id,)).fetchall(), []):
            try:
                j = json.loads(c)
                lost += float(j.get("lost_revenue_stockout", 0)) + float(j.get("lost_revenue_86", 0))
            except (ValueError, TypeError):
                pass
        fc = _safe(lambda: conn.execute("SELECT p50, p90, actual FROM forecast_log WHERE run_id=? AND policy=? AND actual IS NOT NULL",
                                        (run_id, policy)).fetchall(), [])
        rows = [(float(r[0]), float(r[1]), float(r[2])) for r in fc]
        pos = [r for r in rows if r[2] > 0]
        mape = sum(abs(a - p50) / a for p50, _, a in pos) / len(pos) if pos else None
        cov90 = sum(1 for _, p90, a in rows if a <= p90 + 1e-9) / len(rows) if rows else None
    finally:
        conn.close()
    return {"waste_value": round(waste, 2), "waste_pct": round(waste / bought * 100, 2) if bought else 0.0,
            "stockouts": int(s("stockouts")), "dishes_86d": int(s("dishes_86d")),
            "food_cost_pct": round(food_cost / revenue * 100, 2) if revenue else 0.0, "revenue": round(revenue, 2),
            "covers_served": int(s("covers_served")), "approvals_needed": int(s("approvals")),
            "lost_revenue": round(lost, 2), "food_cost": round(food_cost, 2), "purchases": round(bought, 2),
            "mape": None if mape is None else round(mape, 4), "p90_coverage": None if cov90 is None else round(cov90, 3),
            "forecast_rows": len(rows), "days": len(daily), "daily": daily}


def compare(agent_db: str | Path, baseline_db: str | Path, run_id: str) -> dict:
    """Side-by-side KPIs: {"agent": {...}, "baseline": {...}, "delta": {k: agent - baseline}, "winner": {k: 'agent'|'baseline'|'tie'}}."""
    a, b = kpis(agent_db, run_id, "agent"), kpis(baseline_db, run_id, "baseline")
    delta, winner = {}, {}
    for k in KEYS:
        if a.get(k) is None or b.get(k) is None:
            continue
        delta[k] = round(a[k] - b[k], 4)
        if k in LOWER_IS_BETTER:
            winner[k] = "agent" if a[k] < b[k] else "baseline" if a[k] > b[k] else "tie"
        elif k in HIGHER_IS_BETTER:
            winner[k] = "agent" if a[k] > b[k] else "baseline" if a[k] < b[k] else "tie"
    return {"run_id": run_id, "agent": a, "baseline": b, "delta": delta, "winner": winner}


_LABELS = {"waste_value": ("Waste ($)", "{:,.2f}"), "waste_pct": ("Waste (% of purchases)", "{:.2f}"),
           "stockouts": ("Stockouts (dish-periods)", "{:d}"), "dishes_86d": ("Dishes 86'd (dish-days)", "{:d}"),
           "food_cost_pct": ("Food cost %", "{:.2f}"), "revenue": ("Revenue ($)", "{:,.2f}"),
           "covers_served": ("Covers served", "{:d}"), "lost_revenue": ("Lost sales ($)", "{:,.2f}"),
           "approvals_needed": ("Chef approvals", "{:d}"), "mape": ("Covers MAPE", "{:.1%}"),
           "p90_coverage": ("P90 coverage", "{:.1%}")}


def format_comparison(cmp: dict) -> str:
    """Plain-text table of a `compare()` result."""
    rows = [f"{'KPI':<26}{'agent':>14}{'baseline':>14}{'delta':>14}  better"]
    for k, (label, fmt) in _LABELS.items():
        a, b = cmp["agent"].get(k), cmp["baseline"].get(k)
        if a is None or b is None:
            continue
        d = cmp["delta"].get(k, 0)
        rows.append(f"{label:<26}{fmt.format(a):>14}{fmt.format(b):>14}{d:>+14,.2f}  {cmp['winner'].get(k, '')}")
    return "\n".join(rows)
