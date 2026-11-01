"""Order optimizer: PuLP MILP (CBC) with a greedy reorder-point fallback (identical return shape).

Planning window: usage days as_of .. as_of+H (H = horizon_days, so H+1 days) so that suppliers with a
lead time up to H days are useful. Target = expected usage at `service_quantile` minus usable on-hand
(FEFO-simulated against lot expiry) minus inbound 'sent' POs. Decision variables are integer pack counts
per supplier x ingredient x delivery day; a binary per supplier x delivery day enforces the minimum
order value. Shelf life caps how much can be bought, using the same rule as the `place_po` constraint check
(`constraints.shelf_life_limit`) so optimizer plans are never blocked for shelf life.
Solver: HiGHS (via highspy) when installed, else CBC. No LLM imports here.
"""
from __future__ import annotations

import math
import sqlite3
from collections import defaultdict
from datetime import timedelta

from restaurant_ops.config import settings
from restaurant_ops.core.constraints import shelf_life_limit
from restaurant_ops.core.inventory import EPS, expected_usage, usage_by_ingredient
from restaurant_ops.core.util import conn_ctx, iso, r2, rq, to_date

TIME_LIMIT_S = 10
SHORTAGE_MULT = 6.0  # shortage penalty per unit = SHORTAGE_MULT x ingredient ref cost (lost sales >> food cost)
FILL_RISK_WEIGHT = 0.3  # penalty = weight x (1 - fill_rate + delay_prob) x spend
PO_FIXED_PENALTY = 2.0  # discourages fragmenting into many tiny POs
OVERBUY_DAYS = 2  # extra days of usage that may be bought beyond the window when shelf life allows
DEFAULT_BUDGET_DAYS = 2  # default budget = DAILY_BUDGET x this


def _plan(c: sqlite3.Connection, a, H: int, quantile: str, ingredient_ids) -> dict[str, dict]:
    """Per-ingredient requirement data (demand, residual after on-hand FEFO, inbound, caps)."""
    N = H + 1
    days = [a + timedelta(days=i) for i in range(N)]
    usage = usage_by_ingredient(expected_usage(a.isoformat(), N, quantile, conn=c))
    shelf = {r["id"]: (int(r["shelf_life_days"]), float(r["ref_cost"])) for r in
             c.execute("SELECT id, shelf_life_days, ref_cost FROM ingredients")}
    lots = defaultdict(list)
    for r in c.execute("SELECT ingredient_id, qty, expiry_date FROM lots WHERE status='active' AND qty>? "
                       "AND received_date<=? AND expiry_date>=? ORDER BY expiry_date", (EPS, a.isoformat(), a.isoformat())):
        lots[r["ingredient_id"]].append([float(r["qty"]), to_date(r["expiry_date"])])
    inbound = defaultdict(lambda: [0.0] * N)
    for r in c.execute("SELECT l.ingredient_id, l.qty, l.qty_delivered, p.expected_delivery FROM po_lines l "
                       "JOIN purchase_orders p ON p.po_id=l.po_id WHERE p.status='sent' AND p.expected_delivery<=?",
                       ((a + timedelta(days=H)).isoformat(),)):
        t = max(0, (to_date(r["expected_delivery"]) - a).days)
        inbound[r["ingredient_id"]][t] += max(0.0, float(r["qty"]) - float(r["qty_delivered"] or 0.0))
    plan = {}
    for ing, per_day in usage.items():
        if ingredient_ids and ing not in ingredient_ids:
            continue
        demand = [float(per_day.get(d.isoformat(), 0.0)) for d in days]
        if sum(demand) <= EPS:
            continue
        ls = [list(x) for x in lots.get(ing, [])]
        onhand = sum(q for q, _ in ls)
        resid = []
        for t, d in enumerate(days):
            need = demand[t]
            for lot in ls:
                if need <= 1e-12:
                    break
                if lot[1] >= d and lot[0] > 0:
                    take = min(lot[0], need)
                    lot[0] -= take
                    need -= take
            resid.append(need)
        L, ref = shelf[ing]
        room = []  # per delivery day: max new qty the shelf-life check allows (excluding its one-pack slack)
        for d in days:
            lim = shelf_life_limit(c, ing, a, d)
            room.append(math.inf if lim is None else lim["usable"] - lim["have"])
        plan[ing] = {"demand": demand, "resid": resid, "inbound": list(inbound[ing]), "onhand": onhand,
                     "rate": sum(demand) / N, "shelf": L, "ref_cost": ref, "N": N, "room": room}
    return plan


def _max_packs(p: dict, t: int, pack: float) -> int:
    """Most packs one delivery on day-index t may carry without failing the shelf-life check."""
    room = p["room"][t]
    return 10**6 if room == math.inf else max(0, math.floor((room + pack) / pack + 1e-9))


def _short_total(p: dict) -> float:
    """Residual need after inbound over the whole window (never negative)."""
    return max(0.0, sum(p["resid"]) - sum(p["inbound"]))


def _line_cap(p: dict, t: int) -> float:
    """Max useful quantity for one delivery on day-index t (shelf-life aware)."""
    return p["rate"] * max(0.0, min(p["shelf"], p["N"] + OVERBUY_DAYS - t))


def _total_cap(p: dict) -> float:
    """Max total new stock worth buying for this ingredient over the window."""
    extra = OVERBUY_DAYS if p["shelf"] > p["N"] else 0
    return max(0.0, sum(p["demand"]) + p["rate"] * extra - p["onhand"] - sum(p["inbound"]))


def _load_suppliers(c: sqlite3.Connection, exclude) -> tuple[dict, dict]:
    sups = {r["id"]: dict(r) for r in c.execute("SELECT * FROM suppliers") if r["id"] not in exclude}
    items: dict[str, dict[str, dict]] = defaultdict(dict)
    for r in c.execute("SELECT supplier_id, ingredient_id, price, pack_size FROM supplier_items"):
        if r["supplier_id"] in sups:
            items[r["ingredient_id"]][r["supplier_id"]] = {"price": float(r["price"]), "pack": float(r["pack_size"])}
    return sups, items


def _risk_mult(s: dict) -> float:
    return 1.0 + FILL_RISK_WEIGHT * ((1.0 - float(s["fill_rate"])) + float(s["delay_prob"]))


def _build_orders(qty: dict, a, price_of) -> list[dict]:
    """qty: {(supplier, delivery_idx): {ingredient: units}} -> order dicts."""
    orders = []
    for (sid, t), lines in sorted(qty.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        ls = [{"ingredient_id": i, "qty": rq(q), "unit_price": r2(price_of(sid, i), 3)} for i, q in sorted(lines.items()) if q > EPS]
        if ls:
            orders.append({"supplier_id": sid, "delivery_date": (a + timedelta(days=t)).isoformat(), "lines": ls,
                           "total": r2(sum(x["qty"] * x["unit_price"] for x in ls))})
    return orders


def _solve_milp(plan, sups, items, a, H, budget) -> tuple[str, dict[tuple, dict[str, float]], list[str]]:
    import pulp  # PuLP 4.x: variables are created through the problem (prob.add_variable)

    prob = pulp.LpProblem("orders", pulp.LpMinimize)
    need = {i: p for i, p in plan.items() if sum(p["resid"]) > EPS}
    x, ub = {}, {}
    for ing, p in need.items():
        for sid, it in items.get(ing, {}).items():
            lead = int(sups[sid]["lead_time_days"])
            for t in range(max(lead, 0), H + 1):
                cap = _line_cap(p, t)
                n = max(1, math.ceil(min(cap, _total_cap(p) + it["pack"]) / it["pack"] - 1e-9))
                n = min(n, _max_packs(p, t, it["pack"]))
                if n <= 0:
                    continue
                x[(sid, ing, t)] = prob.add_variable(f"x_{len(x)}", 0, n, cat="Integer")
                ub[(sid, ing, t)] = n
    notes: list[str] = []
    if not x:
        return "optimal", {}, ["no supplier can deliver the required items inside the window"]
    y = {(sid, t): prob.add_variable(f"y_{k}", cat="Binary") for k, (sid, t) in enumerate({(k[0], k[2]) for k in x})}
    short = {i: prob.add_variable(f"s_{k}", 0) for k, i in enumerate(need)}
    spend = {k: items[k[1]][k[0]]["price"] * items[k[1]][k[0]]["pack"] * v for k, v in x.items()}
    obj = pulp.lpSum(spend[k] * _risk_mult(sups[k[0]]) for k in x)
    obj += pulp.lpSum(PO_FIXED_PENALTY * v for v in y.values())
    obj += pulp.lpSum(SHORTAGE_MULT * need[i]["ref_cost"] * short[i] for i in need)
    prob += obj
    for ing, p in need.items():
        for t in range(p["N"]):
            supply = pulp.lpSum(items[ing][k[0]]["pack"] * v for k, v in x.items() if k[1] == ing and k[2] <= t)
            prob += short[ing] + supply >= sum(p["resid"][: t + 1]) - sum(p["inbound"][: t + 1])
        if items.get(ing):
            prob += pulp.lpSum(items[ing][k[0]]["pack"] * v for k, v in x.items() if k[1] == ing) <= \
                _total_cap(p) + max(it["pack"] for it in items[ing].values())
            # shelf life, cumulatively: once a plan's earlier POs are sent they count as inbound when `execute`
            # re-checks the later ones, so everything delivered up to day t must fit the day-t room
            slack = min(it["pack"] for it in items[ing].values())
            for t in range(p["N"]):
                if p["room"][t] != math.inf:
                    upto = [items[ing][k[0]]["pack"] * v for k, v in x.items() if k[1] == ing and k[2] <= t]
                    if upto:
                        prob += pulp.lpSum(upto) <= max(0.0, p["room"][t]) + slack
    for k, v in x.items():
        prob += v <= ub[k] * y[(k[0], k[2])]
    for (sid, t), yv in y.items():
        tot = pulp.lpSum(spend[k] for k in x if k[0] == sid and k[2] == t)
        prob += tot >= float(sups[sid]["min_order_value"]) * yv
    prob += pulp.lpSum(spend.values()) <= budget
    highs = pulp.HiGHS(msg=False, timeLimit=TIME_LIMIT_S, gapRel=0.01)
    stats = prob.solve(highs if highs.available() else pulp.PULP_CBC_CMD(msg=False, timeLimit=TIME_LIMIT_S, gapRel=0.01))
    optimal = stats.status == pulp.LpSolveStatus.Optimal
    if not optimal and not stats.has_solution:
        raise RuntimeError(f"MILP status {stats.status.name}")
    qty: dict[tuple, dict[str, float]] = defaultdict(dict)
    for k, v in x.items():
        n = round(v.value() or 0.0)
        if n > 0:
            qty[(k[0], k[2])][k[1]] = n * items[k[1]][k[0]]["pack"]
    label = "optimal" if optimal else "feasible"
    if label == "feasible":
        notes.append(f"solver hit the {TIME_LIMIT_S}s limit; best feasible solution returned")
    return label, qty, notes


def _solve_greedy(plan, sups, items, a, H, budget) -> tuple[dict[tuple, dict[str, float]], list[str]]:
    """Reorder-point heuristic: cover the residual need from the best risk-adjusted supplier, round to packs."""
    notes: list[str] = []
    qty: dict[tuple, dict[str, float]] = defaultdict(dict)
    caps: dict[tuple, float] = {}
    for ing, p in sorted(plan.items()):
        need = _short_total(p)
        if need <= EPS or ing not in items:
            continue
        opts = [(it["price"] * _risk_mult(sups[sid]), sid, it) for sid, it in items[ing].items()
                if int(sups[sid]["lead_time_days"]) <= H]
        if not opts:
            notes.append(f"{ing}: no supplier can deliver inside the window")
            continue
        _, sid, it = min(opts, key=lambda o: o[0])
        t = max(int(sups[sid]["lead_time_days"]), 0)
        need = min(need, _total_cap(p) + it["pack"])
        room = _max_packs(p, t, it["pack"])
        if room <= 0:
            notes.append(f"{ing}: skipped, on hand + inbound already fill its shelf life")
            continue
        packs = min(room, max(1, math.ceil(need / it["pack"] - 1e-9)))
        qty[(sid, t)][ing] = packs * it["pack"]
        caps[(sid, t, ing)] = min(room, max(1.0, math.ceil(min(_line_cap(p, t), _total_cap(p) + it["pack"]) / it["pack"] - 1e-9)))
    value = lambda sid, d: sum(q * items[i][sid]["price"] for i, q in d.items())  # noqa: E731
    for key in list(qty):
        sid, t = key
        minv = float(sups[sid]["min_order_value"])
        lines = qty[key]
        guard = 0
        while value(sid, lines) < minv and guard < 200:  # top up with extra packs where caps allow
            guard += 1
            cand = [(items[i][sid]["pack"] * items[i][sid]["price"], i) for i in lines
                    if lines[i] / items[i][sid]["pack"] < caps[(sid, t, i)]]
            if not cand:
                break
            _, i = max(cand)
            lines[i] += items[i][sid]["pack"]
        if value(sid, lines) < minv - 1e-9:
            notes.append(f"dropped {sid} order (below minimum {minv:.0f}): {', '.join(sorted(lines))}")
            del qty[key]
    spent = 0.0
    for key in sorted(qty, key=lambda k: value(k[0], qty[k])):
        v = value(key[0], qty[key])
        if spent + v > budget:
            notes.append(f"dropped {key[0]} order ({v:.0f}) - over budget")
            del qty[key]
        else:
            spent += v
    return qty, notes


def optimize_orders(as_of: str | None = None, horizon_days: int = 3, budget: float | None = None,
                    service_quantile: str = "p90", exclude_suppliers: tuple | list = (),
                    ingredient_ids: list[str] | None = None, conn: sqlite3.Connection | None = None,
                    solver: str = "milp") -> dict:
    """Order quantities and supplier split under lead time, min order, budget and shelf life.

    Returns {status ('optimal'|'feasible'|'greedy'), solver ('milp'|'greedy'),
             orders: [{supplier_id, delivery_date, lines:[{ingredient_id, qty, unit_price}], total}],
             total_cost, notes, shortfalls: [{ingredient_id, qty}], horizon}.
    `solver='greedy'` forces the heuristic. If the MILP errors out the greedy result is returned with a note.
    """
    a = to_date(as_of)
    H = max(1, int(horizon_days))
    excl = set(exclude_suppliers or ())
    with conn_ctx(conn) as c:
        budget = float(budget) if budget is not None else settings.DAILY_BUDGET * DEFAULT_BUDGET_DAYS
        plan = _plan(c, a, H, service_quantile, set(ingredient_ids) if ingredient_ids else None)
        sups, items = _load_suppliers(c, excl)
    price_of = lambda sid, ing: items[ing][sid]["price"]  # noqa: E731
    notes: list[str] = [f"window {a.isoformat()}..{(a + timedelta(days=H)).isoformat()}, quantile {service_quantile}, budget {budget:.0f}"]
    status, used = "optimal", "milp"
    qty: dict[tuple, dict[str, float]] = {}
    if solver == "milp":
        try:
            status, qty, n = _solve_milp(plan, sups, items, a, H, budget)
            notes += n
        except Exception as e:  # noqa: BLE001
            notes.append(f"MILP failed ({type(e).__name__}: {e}); used greedy fallback")
            solver = "greedy"
    if solver != "milp":
        qty, n = _solve_greedy(plan, sups, items, a, H, budget)
        notes += n
        status, used = "greedy", "greedy"
    orders = _build_orders(qty, a, price_of)
    # shortfalls: window-end need not covered by on-hand + inbound + new orders arriving in the window
    new = defaultdict(float)
    for (sid, _t), lines in qty.items():
        for i, q in lines.items():
            new[i] += q
    short = [{"ingredient_id": i, "qty": rq(_short_total(p) - new.get(i, 0.0))} for i, p in sorted(plan.items())
             if _short_total(p) - new.get(i, 0.0) > 1e-3]
    if short:
        notes.append(f"{len(short)} ingredient(s) remain short (supplier lead time / caps / budget)")
    return {"status": status, "solver": used, "orders": orders, "total_cost": r2(sum(o["total"] for o in orders)),
            "notes": notes, "shortfalls": short, "horizon": {"as_of": a.isoformat(), "days": H, "quantile": service_quantile}}
