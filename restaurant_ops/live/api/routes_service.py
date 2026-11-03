"""Service control (start, resume, clock, scenarios, approvals), state reads, reports, replay listings and analytics."""
from __future__ import annotations

import csv
import html
import io
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response

from restaurant_ops.config import settings
from restaurant_ops.live import ops as OPS
from restaurant_ops.live import replay as RP
from restaurant_ops.live.api.deps import ANY, MANAGER, guard, svc
from restaurant_ops.live.api.hub import hub, public_snapshot
from restaurant_ops.live.api.models import ResolveReq, SpeedReq, StartReq
from restaurant_ops.live.service import ServiceError

log = logging.getLogger("restaurant_ops.live")
router = APIRouter()

# =========================================================================== service + read endpoints
@router.get("/api/state")
async def state(user: dict = ANY):
    return svc().snapshot()


@router.get("/api/public/state")
async def public_state():
    return public_snapshot(svc())


@router.get("/api/health")
async def health():
    s = hub.svc
    return {"ok": s is not None, "error": hub.error, "status": s.status if s else None, "bus": s.bus.kind if s else None,
            "ws_clients": len(hub.clients), "public_clients": len(hub.public)}


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics(user: dict = MANAGER):
    return OPS.prometheus(hub.svc, ws_clients=len(hub.clients) + len(hub.public), started=hub.started)


@router.post("/api/service/start")
async def start(req: StartReq, user: dict = MANAGER):
    s = guard(lambda: hub.start_service(req.seed, req.speed, req.bus, req.period, req.date))
    return s.info()


@router.post("/api/service/resume/{service_id}")
async def resume(service_id: str, user: dict = MANAGER):
    try:
        s = hub.resume_service(service_id)
    except (FileNotFoundError, ServiceError) as e:
        raise HTTPException(404, str(e)) from e
    return {**s.info(), "pending": len(s.coordinator.pending())}


@router.post("/api/clock")
async def clock(req: SpeedReq, user: dict = MANAGER):
    return guard(lambda: svc().set_speed(req.speed))


@router.post("/api/scenarios/{name}")
async def scenario(name: str, user: dict = MANAGER):
    return guard(lambda: svc().inject(name))


@router.post("/api/decisions/{decision_id}")
async def resolve(decision_id: int, req: ResolveReq, request: Request, user: dict = MANAGER):
    s = svc()
    request.state.audit = {"decision_id": decision_id, "approved": req.approved, "note": req.note}
    d = guard(lambda: s.coordinator.resolve(decision_id, req.approved, req.note or f"by {user['username']}", req.payload,
                                            by=user["username"]))
    return s.coordinator.view(d)




# =========================================================================== analytics, reports, replay
@router.get("/api/menu/engineering")
async def menu_engineering(days: int = 28, user: dict = ANY):
    return svc().menu_engineering(days)


@router.get("/api/waste")
async def waste(days: int = 30, user: dict = ANY):
    return svc().waste(days)


@router.get("/api/suppliers")
async def suppliers(user: dict = ANY):
    s = svc()
    s.flush()
    return [dict(r) for r in s.conn.execute("SELECT id, name, lead_time_days, min_order_value, fill_rate, delay_prob, email FROM suppliers")]


@router.get("/api/llm")
async def llm_stats(user: dict = ANY):
    s = svc()
    s.flush()
    calls = [dict(r) for r in s.conn.execute("SELECT ts, agent, latency_ms, ok, tokens_in, tokens_out, cost, error FROM llm_calls "
                                              "WHERE service_id=? ORDER BY id DESC LIMIT 30", (s.service_id,))]
    return {**s.llm_guard.view(), "recent": calls}


@router.get("/api/report")
async def report(user: dict = MANAGER):
    return svc().report()


@router.get("/api/report.csv")
async def report_csv(user: dict = MANAGER):
    r = svc().report()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["section", "key", "value"])
    for k, v in r["kpis"].items():
        w.writerow(["kpi", k, v])
    for k, v in r["payments"].items():
        w.writerow(["payments", k, v])
    for d in r["sales_by_dish"]:
        w.writerow(["sales", d["name"], f"{d['qty']} / {d['sales']}"])
    for d in r["decisions"]["items"]:
        w.writerow(["decision", f"{(d['created_sim'] or '')[11:16]} {d['agent']}", f"{d['title']} -> {d['status']}"])
    for x in r["waste"]["by_reason"]:
        w.writerow(["waste", x["reason"], x["value"]])
    name = f"service-report-{r['service']['service_id']}.csv"
    return Response(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/api/report.html", response_class=HTMLResponse)
async def report_html(user: dict = MANAGER):
    r = svc().report()
    e = html.escape
    rows = "".join(f"<tr><td>{e(str(a))}</td><td>{e(str(b))}</td></tr>" for a, b in r["kpis"].items())
    pay = "".join(f"<tr><td>{e(a)}</td><td>{e(str(b))}</td></tr>" for a, b in r["payments"].items())
    sales = "".join(f"<tr><td>{e(d['name'])}</td><td>{d['qty']}</td><td>${d['sales']:.2f}</td></tr>" for d in r["sales_by_dish"])
    dec = "".join(f"<tr><td>{e((d['created_sim'] or '')[11:16])}</td><td>{e(d['agent'])}</td><td>{e(d['title'])}</td>"
                  f"<td>{e(d['status'])}</td></tr>" for d in r["decisions"]["items"])
    si = r["service"]
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Service report</title><style>
body{{font:14px/1.45 system-ui,sans-serif;max-width:900px;margin:24px auto;padding:0 16px;color:#1f2328;background:#fff}}
h1{{font-size:20px}} h2{{font-size:15px;margin-top:22px}} table{{border-collapse:collapse;width:100%}}
td,th{{border-bottom:1px solid #ddd;padding:4px 6px;text-align:left;font-size:13px}} .muted{{color:#666}}
@media print{{button{{display:none}}}}</style></head><body>
<button onclick="print()">Print / save as PDF</button>
<h1>{e(si.get('restaurant', ''))} · end-of-service report · {e(si['weekday'])} {e(si['date'])} {e(si['period'])}</h1>
<p class="muted">{e(si['service_id'])} · generated {e(r['generated'])} · bus {e(si['bus'])} · agents {e(si['llm_mode'])}</p>
<h2>KPIs</h2><table>{rows}</table>
<h2>Payments</h2><table>{pay}</table>
<h2>Sales by dish</h2><table><tr><th>Dish</th><th>Qty</th><th>Sales</th></tr>{sales}</table>
<h2>Decisions</h2><table><tr><th>Time</th><th>Agent</th><th>Decision</th><th>Status</th></tr>{dec}</table>
</body></html>"""
    return HTMLResponse(page)


@router.get("/api/services")
async def services(user: dict = ANY):
    return RP.list_services()


@router.get("/api/services/{service_id}/events")
async def service_events(service_id: str, after: int = 0, user: dict = ANY):
    return guard(lambda: RP.load_events(service_id, after))


@router.get("/api/baseline")
async def baseline(user: dict = ANY):
    """Agents vs fixed-par baseline from the latest batch simulation (python -m restaurant_ops.sim.run), if any."""
    runs = settings.DATA_DIR / "runs"
    agents = sorted(runs.glob("*_agent.db"), key=lambda p: p.stat().st_mtime, reverse=True) if runs.exists() else []
    for a in agents:
        b = a.with_name(a.name.replace("_agent.db", "_baseline.db"))
        if b.exists():
            from restaurant_ops.sim.metrics import compare

            run_id = a.name[: -len("_agent.db")]
            try:
                return {"run_id": run_id, "comparison": compare(a, b, run_id)}
            except Exception as e:  # noqa: BLE001
                return {"run_id": run_id, "error": str(e)}
    return {"run_id": None, "hint": "run: uv run python -m restaurant_ops.sim.run --days 7 --policy both --scenario all --auto-approve"}
