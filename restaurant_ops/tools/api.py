"""FastAPI service exposing the same functions as the agent tools (one contract for agents and UI).

Run: `uvicorn restaurant_ops.tools.api:app`. Every endpoint takes an optional `sim_date` query param;
without it the registry's current sim date (default SIM_START) is used.
"""
from __future__ import annotations

import json
from typing import Any, Callable

from fastapi import Body, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from restaurant_ops.config import settings
from restaurant_ops.tools import registry as R

app = FastAPI(title="Restaurant Ops Tools API", version="0.1.0")


def _call(fn: Callable, sim_date: str | None, **kw) -> Any:
    """Run a registry function under an optional sim date; map errors to HTTP 400."""
    try:
        with R.use_sim_date(sim_date):
            return fn(**kw)
    except (ValueError, KeyError, TypeError) as e:
        raise HTTPException(status_code=400, detail=f"{type(e).__name__}: {e}") from e


def _rows(sql: str, params: tuple = ()) -> list[dict]:
    with R.connection() as c:
        rows = [dict(r) for r in c.execute(sql, params)]
    for r in rows:
        for k in ("payload", "violations", "content"):
            if k in r and isinstance(r[k], str):
                try:
                    r[k] = json.loads(r[k])
                except ValueError:
                    pass
    return rows


# --------------------------------------------------------------------------- health & sim date


@app.get("/health")
def health() -> dict:
    """Liveness plus the current sim date and database path."""
    return {"status": "ok", "sim_date": R.get_sim_date(), "db": str(settings.DB_PATH), "llm_mode": settings.LLM_MODE, "llm": f"{settings.LLM_PROVIDER}:{settings.model_name}"}


class SimDate(BaseModel):
    sim_date: str


@app.post("/sim_date")
def set_sim_date(body: SimDate) -> dict:
    """Set the process-wide sim date (used when requests carry no `sim_date`)."""
    return {"sim_date": R.set_sim_date(body.sim_date)}


# --------------------------------------------------------------------------- forecasting


@app.get("/forecast/covers")
def forecast_covers(start_date: str | None = None, days: int = 3, sim_date: str | None = None):
    return _call(R.forecast_covers, sim_date, start_date=start_date, days=days)


@app.get("/forecast/dish_mix")
def forecast_dish_mix(start_date: str | None = None, days: int = 3, meal_period: str | None = None,
                      sim_date: str | None = None):
    return _call(R.forecast_dish_mix, sim_date, start_date=start_date, days=days, meal_period=meal_period)


@app.get("/forecast/signals")
def signal_report(sim_date: str | None = None):
    return _call(R.signal_report, sim_date)


@app.get("/forecast/accuracy")
def forecast_accuracy(sim_date: str | None = None):
    return _call(R.forecast_accuracy, sim_date)


@app.get("/events")
def event_calendar(start_date: str | None = None, days: int = 7, sim_date: str | None = None):
    return _call(R.event_calendar, sim_date, start_date=start_date, days=days)


@app.get("/reservations")
def reservations_summary(start_date: str | None = None, days: int = 7, sim_date: str | None = None):
    return _call(R.reservations_summary, sim_date, start_date=start_date, days=days)


@app.get("/weather")
def weather(start_date: str | None = None, days: int = 7, sim_date: str | None = None):
    return _call(R.weather, sim_date, start_date=start_date, days=days)


# --------------------------------------------------------------------------- inventory & purchasing


@app.get("/inventory/stock")
def stock_by_lot(ingredient_id: str | None = None, sim_date: str | None = None):
    return _call(R.stock_by_lot, sim_date, ingredient_id=ingredient_id)


@app.get("/inventory/expected_usage")
def expected_usage(start_date: str | None = None, days: int = 3, quantile: str = "p50",
                   sim_date: str | None = None):
    return _call(R.expected_usage, sim_date, start_date=start_date, days=days, quantile=quantile)


@app.get("/inventory/waste_risk")
def waste_risk(horizon_days: int = 3, sim_date: str | None = None):
    return _call(R.waste_risk, sim_date, horizon_days=horizon_days)


@app.get("/inventory/variance")
def variance_report(days: int = 7, sim_date: str | None = None):
    return _call(R.variance_report, sim_date, days=days)


class OptimizeRequest(BaseModel):
    horizon_days: int = 3
    budget: float | None = None
    service_quantile: str = "p90"
    exclude_suppliers: list[str] = Field(default_factory=list)
    ingredient_ids: list[str] | None = None


@app.post("/purchasing/optimize")
def optimize_orders(req: OptimizeRequest, sim_date: str | None = None):
    return _call(R.optimize_orders, sim_date, **req.model_dump())


@app.get("/purchasing/quotes")
def supplier_quotes(ingredient_id: str, qty: float, needed_by: str, sim_date: str | None = None):
    return _call(R.supplier_quotes, sim_date, ingredient_id=ingredient_id, qty=qty, needed_by=needed_by)


class NegotiateRequest(BaseModel):
    supplier_id: str
    ingredient_id: str
    qty: float
    target_price: float


@app.post("/purchasing/negotiate")
def negotiate(req: NegotiateRequest, sim_date: str | None = None):
    return _call(R.negotiate, sim_date, **req.model_dump())


class POLine(BaseModel):
    ingredient_id: str
    qty: float
    unit_price: float


class SendPORequest(BaseModel):
    supplier_id: str
    delivery_date: str
    lines: list[POLine]
    decision_id: int | None = None


@app.post("/execute/send_po")
def send_po(req: SendPORequest, sim_date: str | None = None):
    """Execute-node endpoint (mutates the world). Not exposed to agents as a tool."""
    d = req.model_dump()
    return _call(R.send_po, sim_date, supplier_id=d["supplier_id"], delivery_date=d["delivery_date"],
                 lines=d["lines"], decision_id=d["decision_id"])


# --------------------------------------------------------------------------- menu & prep


@app.get("/menu/recipes")
def recipes(dish_id: str | None = None, sim_date: str | None = None):
    return _call(R.recipes, sim_date, dish_id=dish_id)


@app.get("/menu/margin")
def margin_calc(dish_id: str, sim_date: str | None = None):
    return _call(R.margin_calc, sim_date, dish_id=dish_id)


class AllergenRequest(BaseModel):
    dish_id: str
    substitutions: dict[str, str] | None = None


@app.post("/menu/allergen_check")
def allergen_check(req: AllergenRequest, sim_date: str | None = None):
    return _call(R.allergen_check, sim_date, dish_id=req.dish_id, substitutions=req.substitutions)


@app.get("/menu/dishes_using")
def dishes_using(ingredient_ids: list[str] = Query(...), sim_date: str | None = None):
    return _call(R.dishes_using, sim_date, ingredient_ids=ingredient_ids)


@app.get("/menu/feasible")
def feasible_portions(dish_id: str, sim_date: str | None = None):
    return _call(R.feasible_portions, sim_date, dish_id=dish_id)


@app.get("/menu/status")
def menu_status(date: str | None = None, sim_date: str | None = None):
    return _call(R.menu_status, sim_date, date=date)


@app.get("/prep/quantities")
def prep_quantities(date: str, meal_period: str, forecast_quantile: str = "p50", sim_date: str | None = None):
    return _call(R.prep_quantities, sim_date, date=date, meal_period=meal_period, forecast_quantile=forecast_quantile)


@app.get("/prep/capacity")
def station_capacity(date: str, meal_period: str, sim_date: str | None = None):
    return _call(R.station_capacity, sim_date, date=date, meal_period=meal_period)


# --------------------------------------------------------------------------- constraint checker


@app.post("/check_action")
def check_action(action: dict = Body(...), sim_date: str | None = None):
    """Validate an action: {violations:[...], blocked: bool, needs_approval: bool}."""
    v = _call(R.check_action, sim_date, action=action)
    return {"violations": v, "blocked": any(x["severity"] == "block" for x in v),
            "needs_approval": _call(R.needs_approval, sim_date, action=action)}


# --------------------------------------------------------------------------- read endpoints for the UI


@app.get("/decisions")
def decisions(run_id: str | None = None, status: str | None = None, limit: int = 200):
    """Decision log (newest first)."""
    where, params = [], []
    if run_id:
        where.append("run_id=?")
        params.append(run_id)
    if status:
        where.append("status=?")
        params.append(status)
    sql = "SELECT * FROM decisions" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT ?"
    return _rows(sql, (*params, limit))


@app.get("/trace")
def trace(run_id: str | None = None, since_id: int = 0, limit: int = 500):
    """Agent trace events in id order (use since_id to poll)."""
    where, params = ["id>?"], [since_id]
    if run_id:
        where.append("run_id=?")
        params.append(run_id)
    return _rows("SELECT * FROM trace WHERE " + " AND ".join(where) + " ORDER BY id LIMIT ?", (*params, limit))


@app.get("/kpis")
def kpis(run_id: str | None = None, policy: str | None = None):
    """Daily KPI rows (agent vs baseline)."""
    where, params = [], []
    if run_id:
        where.append("run_id=?")
        params.append(run_id)
    if policy:
        where.append("policy=?")
        params.append(policy)
    sql = "SELECT * FROM kpi_daily" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY date, policy"
    return _rows(sql, tuple(params))


# --------------------------------------------------------------------------- agent cycle (Phase 3)


class CycleRequest(BaseModel):
    run_id: str
    sim_date: str
    trigger: dict = Field(default_factory=lambda: {"kind": "morning"})
    db_path: str | None = None
    auto_approve: bool = False
    thread_id: str | None = None


class ResumeRequest(BaseModel):
    thread_id: str
    decisions: list[dict]  # [{"id", "approved", "note"}]
    db_path: str | None = None


def _sup():
    from restaurant_ops.agents import supervisor  # lazy: pulls in langgraph

    return supervisor


@app.post("/cycle/run")
def cycle_run(req: CycleRequest):
    """Run a planning cycle: final state, or {status: awaiting_approval, pending, thread_id}."""
    return json.loads(json.dumps(_sup().run_cycle(**req.model_dump()), default=str))


@app.post("/cycle/resume")
def cycle_resume(req: ResumeRequest):
    """Resume a paused cycle with chef decisions."""
    return json.loads(json.dumps(_sup().resume_cycle(req.thread_id, req.decisions, req.db_path), default=str))


@app.get("/cycle/pending")
def cycle_pending(thread_id: str, db_path: str | None = None):
    """Approval items a paused cycle is waiting on."""
    return {"thread_id": thread_id, "pending": _sup().get_pending(thread_id, db_path)}
