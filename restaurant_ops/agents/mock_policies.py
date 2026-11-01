"""Rule-based stand-ins for the five specialists (LLM_MODE=mock): same AgentReport output, no API key.

They use only registry functions (`R.FUNCTIONS`), so every number still comes from the deterministic tools and
every proposal still goes through the constraint checker. Each policy takes the context dict built by
`base.build_context` (sim_date, trigger, reports, forecast, feedback, violations) and returns an AgentReport.
`ctx["violations"]` is non-empty on the one retry after a blocked proposal; policies then pick a safer path.
"""
from __future__ import annotations

import math
from datetime import date, timedelta

from restaurant_ops.agents.base import event_of
from restaurant_ops.agents.state import Action, AgentReport
from restaurant_ops.config import settings
from restaurant_ops.tools import registry as R

F = R.FUNCTIONS
MARGIN_FLOOR_PCT = 65.0     # specials need food cost <= 35% of price
MAX_SPECIALS = 2
MAX_86 = 3
NEGOTIATE_MIN_LINE = 300.0  # only negotiate lines worth at least this
NEGOTIATE_TARGET = 0.95     # ask for 5% off list


def _act(agent: str, t: str, payload: dict, why: str, cost: float = 0.0) -> Action:
    return Action(action_type=t, payload=payload, rationale=why, agent=agent, est_cost=round(cost, 2))


# =========================================================================== forecast


def forecast(ctx: dict) -> AgentReport:
    d = ctx["sim_date"]
    kind, params = event_of(ctx["trigger"])
    covers = F["forecast_covers"](start_date=d, days=7)
    cal = F["event_calendar"](start_date=d, days=3)
    res = F["reservations_summary"](start_date=d, days=3)
    wx = F["weather"](start_date=d, days=3)
    sig = [s for s in F["signal_report"]() if s.get("significant")]
    findings, actions = [], []
    today = [c for c in covers if c["date"] == d]
    if today:
        findings.append("covers today (p10/p50/p90): " + "; ".join(
            f"{c['meal_period']} {c['p10']}/{c['p50']}/{c['p90']} (cap {c['capacity_cap']})" for c in today))
    if sig:
        findings.append("significant signals: " + ", ".join(f"{s['signal']} {s['effect_pct']:+}%" for s in sig))
    for e in cal:
        findings.append(f"calendar: {e['date']} {e['type']} '{e['name']}' ({e.get('impact_hint') or 'no hint'}); model already uses calendar events")
    heat = [w for w in wx if w.get("heat") or w.get("alert")]
    if heat:
        findings.append("weather: " + ", ".join(f"{w['date']} {w['temp_c']}C {w.get('alert') or ''}".strip() for w in heat)
                        + " - heat shifts the dish mix toward cold dishes; covers unchanged")
    if kind == "heatwave" and not heat:
        findings.append("heatwave reported by the event but the weather feed shows no heat days yet")

    if kind == "private_booking":
        day = params.get("date") or d
        period = params.get("meal_period") or "dinner"
        party = params.get("party_size") or next(
            (r["private_covers"] for r in res if r["date"] == day and r["meal_period"] == period and r["private_covers"]), None)
        row = next((c for c in covers if c["date"] == day and c["meal_period"] == period), None)
        with R.connection() as conn:
            done = conn.execute("SELECT COUNT(*) FROM decisions WHERE action_type='adjust_forecast' AND status IN ('approved','executed') "
                                "AND rationale LIKE '%private booking%' AND payload LIKE ? AND payload LIKE ?",
                                (f'%"{day}"%', f'%"{period}"%')).fetchone()[0]
        if row is None or not party:
            findings.append(f"private booking on {day} {period}: forecast row or party size unavailable, no adjustment")
        elif done:
            findings.append(f"private booking on {day} {period} already reflected by an earlier adjustment")
        else:
            target = min(row["p50"] + float(party), row["capacity_cap"])
            factor = min(1.6, max(0.5, round(target / row["p50"], 3))) if row["p50"] else 1.0
            if factor > 1.01:
                actions.append(_act("forecast_analyst", "adjust_forecast",
                                    {"date": day, "meal_period": period, "factor": factor,
                                     "reason": f"private booking of {party} covers created today is not in the model's reservation feature"},
                                    f"private booking: {party} covers on top of p50 {row['p50']} (cap {row['capacity_cap']}) -> factor {factor}"))
                findings.append(f"private booking {party} covers on {day} {period}: forecast p50 {row['p50']}, cap {row['capacity_cap']}")
            else:
                findings.append(f"private booking on {day} {period} is already within capacity-capped forecast")
    summary = ("Forecast reviewed; " + (f"{len(actions)} adjustment proposed for the unseen event." if actions
                                          else "no unseen events require an adjustment."))
    return AgentReport(agent="forecast_analyst", summary=summary, findings=findings, actions=actions)


# =========================================================================== inventory


def inventory(ctx: dict) -> AgentReport:
    kind, params = event_of(ctx["trigger"])
    risk = F["waste_risk"](horizon_days=3)
    var = F["variance_report"](days=7)
    findings, actions = [], []
    for r in risk[:5]:
        findings.append(f"at risk: {r['lot_id']} {r['ingredient_id']} {r['qty_at_risk']} (value {r['value_at_risk']}) expires {r['expiry_date']}")
    if not risk:
        findings.append("no lots at waste risk in the next 3 days")
    discarded = set()
    for r in risk:
        if r["days_left"] < 0:
            discarded.add(r["lot_id"])
            actions.append(_act("inventory", "discard_lot", {"lot_id": r["lot_id"], "reason": "expired"},
                                f"lot expired {r['expiry_date']}, {r['qty_at_risk']} {r['ingredient_id']} (value {r['value_at_risk']})"))
    if kind == "cooler_failure":
        ids = params.get("ingredient_ids")
        lot_ids = set(params.get("lot_ids") or [])
        if not ids and not lot_ids:
            with R.connection() as conn:
                ids = [x[0] for x in conn.execute("SELECT id FROM ingredients WHERE storage='cooler' AND category=?",
                                                  (params.get("category", "dairy"),))]
        for lot in F["stock_by_lot"]():
            if lot["lot_id"] in discarded:
                continue
            if lot["lot_id"] in lot_ids or (ids and lot["ingredient_id"] in ids):
                actions.append(_act("inventory", "discard_lot", {"lot_id": lot["lot_id"], "reason": "spoilage"},
                                    f"cooler failure: {lot['qty']} {lot['ingredient_id']} lot {lot['lot_id']} is unsafe"))
        findings.append(f"cooler failure: {sum(1 for a in actions if a.payload.get('reason') == 'spoilage')} lot(s) to discard")
    for v in var:
        line = f"variance {v['ingredient_id']}: {v['variance_pct']}% ({v['flag']}), expected {v['expected']} vs counted {v['actual']}"
        findings.append(line)
        if v["flag"] in ("shrinkage", "over_portioning"):
            actions.append(_act("inventory", "flag_variance",
                                {"ingredient_id": v["ingredient_id"], "variance_pct": v["variance_pct"], "flag": v["flag"]}, line))
    summary = f"{len(risk)} lot(s) at risk, {len(var)} variance flag(s); {len(actions)} action(s) proposed."
    return AgentReport(agent="inventory", summary=summary, findings=findings, actions=actions)


# =========================================================================== purchasing


def _open_pos(supplier_id: str | None, ingredient_id: str | None) -> list[dict]:
    q = ("SELECT p.po_id, p.supplier_id, p.expected_delivery, l.ingredient_id, l.qty FROM purchase_orders p "
         "JOIN po_lines l ON l.po_id=p.po_id WHERE p.status IN ('sent','pending_approval','partial')")
    args: list = []
    if supplier_id:
        q += " AND p.supplier_id=?"
        args.append(supplier_id)
    if ingredient_id:
        q += " AND l.ingredient_id=?"
        args.append(ingredient_id)
    with R.connection() as conn:
        return [dict(r) for r in conn.execute(q + " ORDER BY p.po_id", args)]


def _counter_price(res: dict, list_price: float) -> float | None:
    for k in ("counter_price", "unit_price", "price", "offer"):
        v = res.get(k) if isinstance(res, dict) else None
        if isinstance(v, (int, float)) and 0 < v < list_price:
            return float(v)
    return None


def purchasing(ctx: dict) -> AgentReport:
    kind, params = event_of(ctx["trigger"])
    findings, actions = [], []
    exclude: list[str] = []
    retry = bool(ctx.get("violations"))
    if kind == "supplier_shortfall":
        sup = params.get("supplier_id")
        lines = _open_pos(sup, params.get("ingredient_id"))
        if not sup and lines:
            sup = lines[0]["supplier_id"]
            lines = [l for l in lines if l["supplier_id"] == sup]
        if sup:
            exclude = [sup]
        fill = float(params.get("fill_rate", 0.3))
        if lines and not retry:
            groups: dict[str, dict] = {}
            for l in lines:
                short_qty = l["qty"] * (1.0 - fill)
                quotes = [q for q in F["supplier_quotes"](ingredient_id=l["ingredient_id"], qty=short_qty, needed_by=l["expected_delivery"])
                          if q["supplier_id"] != sup]
                best = next((q for q in quotes if q["meets_deadline"]), quotes[0] if quotes else None)
                if best is None:
                    findings.append(f"no fallback supplier for {l['ingredient_id']}")
                    continue
                price = best["unit_price"]
                if best["line_total"] >= NEGOTIATE_MIN_LINE:
                    neg = F["negotiate"](supplier_id=best["supplier_id"], ingredient_id=l["ingredient_id"],
                                         qty=best["order_qty"], target_price=round(price * NEGOTIATE_TARGET, 3))
                    cp = _counter_price(neg, price)
                    if cp is not None:
                        findings.append(f"negotiated {best['supplier_id']} {l['ingredient_id']}: {price} -> {cp}")
                        price = cp
                g = groups.setdefault(best["supplier_id"], {"lines": [], "date": best["earliest_delivery"], "po": l["po_id"]})
                g["lines"].append({"ingredient_id": l["ingredient_id"], "qty": best["order_qty"], "unit_price": price})
                g["date"] = max(g["date"], best["earliest_delivery"])
                findings.append(f"{l['ingredient_id']}: {sup} fills {fill:.0%} of PO {l['po_id']}; fallback {best['supplier_id']} "
                                f"{best['order_qty']} @ {price} deliver {best['earliest_delivery']} (meets deadline {best['meets_deadline']})")
            for new_sup, g in groups.items():
                total = sum(x["qty"] * x["unit_price"] for x in g["lines"])
                actions.append(_act("purchasing", "substitute_supplier",
                                    {"supplier_id": new_sup, "from_supplier_id": sup, "po_id": g["po"], "delivery_date": g["date"],
                                     "lines": g["lines"], "total": round(total, 2)},
                                    f"{sup} short-shipping; cover the missing quantity from {new_sup} (quotes via supplier_quotes)", total))
            if actions:
                return AgentReport(agent="purchasing", summary=f"Supplier shortfall at {sup}: {len(actions)} substitute order(s) proposed.",
                                   findings=findings, actions=actions)
            findings.append("substitution not possible; re-optimizing without the failing supplier")
        elif retry:
            findings.append("substitution was blocked; re-optimizing without the failing supplier")
        elif sup:
            findings.append(f"no open PO found for {sup}; re-optimizing without it")
    plan = F["optimize_orders"](horizon_days=3, exclude_suppliers=exclude or None)
    findings.append(f"optimizer {plan['solver']} status {plan['status']}: {len(plan['orders'])} order(s), total {plan['total_cost']}")
    findings += [f"note: {n}" for n in plan.get("notes", [])[1:4]]
    for s in plan.get("shortfalls", [])[:5]:
        findings.append(f"shortfall: {s['ingredient_id']} {s['qty']}")
    for o in plan["orders"]:
        actions.append(_act("purchasing", "place_po",
                            {"supplier_id": o["supplier_id"], "delivery_date": o["delivery_date"], "lines": o["lines"], "total": o["total"]},
                            f"optimizer order for {o['supplier_id']} delivering {o['delivery_date']}, total {o['total']}", o["total"]))
    return AgentReport(agent="purchasing", summary=f"{len(actions)} purchase order(s) from the optimizer"
                       + (f" excluding {', '.join(exclude)}" if exclude else "") + ".", findings=findings, actions=actions)


# =========================================================================== menu


def menu(ctx: dict) -> AgentReport:
    d = ctx["sim_date"]
    month = date.fromisoformat(d).month
    need = (ctx.get("forecast") or {}).get("dish_need_today") or {}
    status = F["menu_status"](date=d)
    taken = {x["dish_id"] for x in status["specials"]} | {x["dish_id"] for x in status["eighty_sixed"]}
    findings, actions = [], []

    # 86: stock supports fewer portions than today's forecast need
    short = []
    for dish_id, n in need.items():
        if dish_id in taken or n < 5:
            continue
        fp = F["feasible_portions"](dish_id=dish_id)
        if fp < n:
            short.append((n - fp, dish_id, fp, n))
    eighty = set()
    for gap, dish_id, fp, n in sorted(short, reverse=True)[:MAX_86]:
        eighty.add(dish_id)
        findings.append(f"{dish_id}: stock supports {fp} portions, forecast need {n}")
        actions.append(_act("menu", "86_dish", {"dish_id": dish_id, "date": d},
                            f"feasible_portions {fp} is below forecast need {n} today"))

    # specials from at-risk (still usable) lots
    risk = [r for r in F["waste_risk"](horizon_days=3) if r["days_left"] >= 0]
    by_ing: dict[str, dict] = {}
    for r in risk:
        g = by_ing.setdefault(r["ingredient_id"], {"qty": 0.0, "value": 0.0})
        g["qty"] += r["qty_at_risk"]
        g["value"] += r["value_at_risk"]
    cands = []
    if by_ing:
        recs = {r["dish_id"]: r for r in F["recipes"]()}
        for dish_id in F["dishes_using"](ingredient_ids=list(by_ing)):
            r = recs.get(dish_id)
            if r is None or dish_id in taken or dish_id in eighty:
                continue
            if r["seasonal_months"] and month not in r["seasonal_months"]:
                continue
            hit = [(by_ing[l["ingredient_id"]], l["qty_per_portion"]) for l in r["lines"] if l["ingredient_id"] in by_ing]
            score = sum(g["value"] for g, _ in hit)
            cap = max(math.floor(g["qty"] / q) for g, q in hit)
            cands.append((score, dish_id, cap, r))
    for score, dish_id, cap, r in sorted(cands, key=lambda x: -x[0]):
        if len([a for a in actions if a.action_type == "add_special"]) >= MAX_SPECIALS:
            break
        m = F["margin_calc"](dish_id=dish_id)
        fp = F["feasible_portions"](dish_id=dish_id)
        portions = min(fp, cap, 12)
        if "error" in m or m["margin_pct"] < MARGIN_FLOOR_PCT or portions < 3:
            continue
        al = F["allergen_check"](dish_id=dish_id)
        actions.append(_act("menu", "add_special",
                            {"dish_id": dish_id, "portions": portions, "price": r["price"], "allergens_declared": al["allergens"],
                             "date": d, "meal_period": "dinner"},
                            f"uses at-risk lots worth {score:.2f}; feasible {fp}, margin {m['margin_pct']}%"))
        findings.append(f"special candidate {dish_id}: at-risk value {score:.2f}, feasible {fp}, margin {m['margin_pct']}%")
    if not actions:
        findings.append("no specials or 86's needed")
    return AgentReport(agent="menu", summary=f"{sum(a.action_type == 'add_special' for a in actions)} special(s), "
                       f"{len(eighty)} dish(es) to 86.", findings=findings, actions=actions)


# =========================================================================== prep


def prep(ctx: dict) -> AgentReport:
    d = ctx["sim_date"]
    quantile = "p10" if ctx.get("violations") else "p50"
    menu_actions = ((ctx.get("reports") or {}).get("menu") or {}).get("actions", [])
    out86 = {a["payload"].get("dish_id") for a in menu_actions
             if a["action_type"] == "86_dish" and a["payload"].get("date") == d}
    findings, actions = [], []
    for period in settings.MEAL_PERIODS:
        rows = [r for r in F["prep_quantities"](date=d, meal_period=period, forecast_quantile=quantile) if r["dish_id"] not in out86]
        if not rows:
            continue
        cap = {c["station"]: c for c in F["station_capacity"](date=d, meal_period=period)}
        items = []
        for r in rows:
            util = cap.get(r["station"], {}).get("utilisation", 0) or 0
            portions = r["portions"] if util <= 1.0 else int(r["portions"] / util)
            if portions > 0:
                items.append({"station": r["station"], "dish_id": r["dish_id"], "portions": portions})
        for s, c in cap.items():
            if c["utilisation"] > 1.0:
                findings.append(f"{period} {s}: utilisation {c['utilisation']} ({c['load_minutes']} of {c['capacity_minutes']} min); portions scaled to fit")
            elif c["utilisation"] > 0.85:
                findings.append(f"{period} {s}: utilisation {c['utilisation']}")
        if items:
            actions.append(_act("prep", "prep_list", {"date": d, "meal_period": period, "items": items},
                                f"prep_quantities {quantile} for {period}: {len(items)} dish(es)"))
    if out86:
        findings.append("excluded from prep (86 proposed): " + ", ".join(sorted(out86)))
    return AgentReport(agent="prep", summary=f"{len(actions)} prep list(s) for {d}.", findings=findings, actions=actions)


POLICIES = {"forecast_analyst": forecast, "inventory": inventory, "purchasing": purchasing, "menu": menu, "prep": prep}
