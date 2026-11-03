"""Procurement agent: on stock.low (and supplier shortfalls) it checks open POs and lead times, compares supplier
quotes, negotiates with the supplier's vendor agent (sim/vendors.py) and drafts a PO; on a forecast miss it re-runs
the order optimizer for tomorrow. Supplier reliability is learned: the scorecard blends catalog priors with the PO
history at service start, and every shortfall lowers that supplier's fill rate (core/suppliers.py).

POs above LIVE_PO_APPROVAL_THRESHOLD go to the manager (timeout: no PO is sent). When the manager rejects with a
reason, the agent re-plans: a cost / budget reason -> a cheaper, smaller order that fits under the threshold;
a supplier reason -> another supplier; otherwise it holds and says why.
"""
from __future__ import annotations

import math
import re
from datetime import timedelta

from restaurant_ops.config import settings
from restaurant_ops.data import catalog as cat
from restaurant_ops.live import events as E
from restaurant_ops.live.agents.base import LiveAgent

MAX_REPLANS = 2
MIN_ORDER_OVERBUY = 2.0  # don't inflate an emergency order past this x need just to reach a supplier's minimum
COST_WORDS = re.compile(r"budget|expensive|cost|price|cheap|too much|over|\$", re.I)
SUPPLIER_WORDS = re.compile(r"supplier|vendor|metro|budget foods|freshcatch|green valley|reliab", re.I)


def _packs(qty: float, pack: float) -> float:
    return round(math.ceil(qty / pack - 1e-9) * pack, 3)


class ProcurementAgent(LiveAgent):
    name = "procurement"
    llm_reviewed = True

    def __init__(self, svc):
        super().__init__(svc)
        self.handled: dict[str, dict] = {}   # ingredient -> {replans, excluded, decision_id}
        svc.bus.subscribe(E.STOCK_LOW, self.on_low, "procurement.on_low")
        svc.bus.subscribe(E.SUPPLIER_SHORTFALL, self.on_shortfall, "procurement.on_shortfall")

    # ------------------------------------------------------------------ numbers
    def _gap(self, ing: str, trace: list, days: int = 2, quantile: str = "p90") -> dict:
        svc = self.svc
        tmr = svc.date + timedelta(days=1)
        use = self.tool(trace, "expected_usage", start_date=tmr.isoformat(), days=days, quantile=quantile, ingredient_ids=[ing])
        need = sum(u["qty"] for u in use)
        rem = self.tool(trace, "remaining_forecast")
        tonight = sum(n * svc.stock.bom[d].get(ing, 0.0) for d, n in rem.items())
        pos = self.tool(trace, "open_pos", ingredient_id=ing)
        horizon = (tmr + timedelta(days=days - 1)).isoformat()
        short = svc.shortages.get(ing) or {}
        inbound = 0.0
        for p in pos:
            if p["expected_delivery"] <= horizon:
                fill = short.get("fill_rate", 1.0) if p["supplier_id"] == short.get("supplier_id") else 1.0
                inbound += p["qty"] * fill
        on_hand = svc.stock.on_hand(ing)
        gap = need + tonight - on_hand - inbound
        return {"ingredient_id": ing, "need_next_days": round(need, 3), "tonight_remaining_use": round(tonight, 3),
                "on_hand": round(on_hand, 3), "inbound": round(inbound, 3), "gap": round(gap, 3), "open_pos": pos,
                "needed_by": tmr.isoformat()}

    def _quote(self, ing: str, qty: float, needed_by: str, trace: list, exclude: set[str], cheapest: bool = False):
        qs = [q for q in self.tool(trace, "supplier_quotes", ingredient_id=ing, qty=qty, needed_by=needed_by)
              if q["supplier_id"] not in exclude]
        if not qs:
            return None
        if cheapest:
            return min(qs, key=lambda q: (q["unit_price"], q["earliest_delivery"]))
        ok = [q for q in qs if q["meets_deadline"]]
        return ok[0] if ok else min(qs, key=lambda q: (q["earliest_delivery"], q["risk_adjusted_price"]))

    def _negotiate(self, ing: str, q: dict, qty: float, trace: list) -> tuple[float, str]:
        """Ask the supplier's vendor agent for NEGOTIATE_TARGET x list on lines worth NEGOTIATE_MIN_LINE or more."""
        price = q["unit_price"]
        if qty * price < settings.NEGOTIATE_MIN_LINE:
            return price, ""
        res = self.tool(trace, "negotiate", supplier_id=q["supplier_id"], ingredient_id=ing, qty=qty,
                        target_price=round(price * settings.NEGOTIATE_TARGET, 3))
        cp = res.get("counter_price") if isinstance(res, dict) else None
        if isinstance(cp, (int, float)) and 0 < cp < price:
            return float(cp), f"negotiated {price:.2f} -> {cp:.2f} ({(1 - cp / price) * 100:.1f}% off): {res.get('message', '')}"
        return price, f"no discount from {q['supplier']}: {res.get('message', '') if isinstance(res, dict) else ''}"

    def _po(self, ing: str, q: dict, qty: float, why: str, trace: list, gap: dict, replan: int = 0, cap: float | None = None):
        pack = q["pack_size"]
        need = max(qty, 0.0)
        qty = _packs(need, pack)
        price, neg = self._negotiate(ing, q, qty, trace)
        if qty * price < q["min_order_value"]:
            qty = _packs(q["min_order_value"] / price, pack)
            if qty > need * MIN_ORDER_OVERBUY + pack:
                return None  # the minimum order would mean buying far more than needed (and past shelf life)
        if cap is not None and qty * price > cap:
            qty = math.floor(cap / price / pack) * pack
            if qty <= 0 or qty * price < q["min_order_value"]:
                return None
        lines = [{"ingredient_id": ing, "qty": round(qty, 3), "unit_price": round(price, 3)}]
        po = self.tool(trace, "draft_po", supplier_id=q["supplier_id"], delivery_date=q["earliest_delivery"], lines=lines)
        ing_name = cat.INGREDIENTS_BY_ID[ing].name
        unit = cat.INGREDIENTS_BY_ID[ing].unit
        sup = q["supplier"]
        when = q["earliest_delivery"]
        title = (f"{'Re-planned ' if replan else ''}PO: {qty:g} {unit} {ing_name} from {sup} (${po['total']:.2f}"
                 f"{', negotiated' if price < q['unit_price'] else ''}), delivers {when}")
        return self.proposal("place_po", {k: po[k] for k in ("supplier_id", "delivery_date", "lines", "total")},
                             title, why + (f"; {neg}" if neg else ""), trace, key=f"po:{ing}", confidence=0.8 if not replan else 0.75,
                             timeout_default="reject", force=bool(replan), est_cost=po["total"],
                             context={"ingredient_id": ing, "gap": {k: v for k, v in gap.items() if k != "open_pos"},
                                      "replan": replan, "list_price": q["unit_price"], "negotiated_price": price})

    # ------------------------------------------------------------------ supplier learning
    def start_of_service(self) -> None:
        trace: list = []
        rows = self.tool(trace, "supplier_scorecard")
        worst = min(rows, key=lambda r: r["fill_rate"])
        self.log("supplier scorecard from delivery history: " + ", ".join(
            f"{r['supplier']} fill {r['fill_rate']:.0%}" + (f" ({r['observed_pos']} POs)" if r["observed_pos"] else "") for r in rows)
            + f"; least reliable: {worst['supplier']}")

    def on_shortfall(self, ev: E.Event) -> None:
        p = ev.payload
        res = self.tool(None, "learn_supplier", supplier_id=p["supplier_id"], kind="shortfall", fill=p["fill_rate"],
                        note=f"{p['ingredient_id']} short: {p['fill_rate']:.0%} fill")
        self.svc.bus.publish(E.SUPPLIER_LEARNED, {**res, "reason": f"{p['ingredient_id']} shortfall"}, source=self.name)
        self.log(f"{p['supplier_id']} reliability updated after the shortfall: fill rate {res['fill_rate_before']:.0%} -> "
                 f"{res['fill_rate']:.0%}; its quotes now carry a higher risk premium")

    # ------------------------------------------------------------------ triggers
    def on_low(self, ev: E.Event) -> None:
        p = ev.payload
        ing = p["ingredient_id"]
        h = self.handled.get(ing)
        if h and not p.get("shortage"):
            return
        if h and h.get("shortage_done"):
            return
        trace: list = []
        gap = self._gap(ing, trace)
        exclude = set()
        if p.get("shortage"):
            exclude.add(p["shortage"]["supplier_id"])
            # the live shortfall is an observation, not a tool output: record it so a reviewer (live LLM, eval, manager)
            # sees why the cheaper quote below is passed over; quotes still show the supplier's historical fill rate
            trace.append({"tool": "observed_shortfall", "args": {"ingredient_id": ing},
                          "result": {**p["shortage"], "excluded_from_quotes": True}})
        self.handled[ing] = {"replans": 0, "excluded": set(exclude), "shortage_done": bool(p.get("shortage"))}
        if gap["gap"] <= 0:
            self.log(f"{p['ingredient']}: low tonight ({p['remaining_portions']} {p['dish']} left) but open POs cover "
                     f"tomorrow (on hand {gap['on_hand']}, inbound {gap['inbound']}, need {gap['need_next_days'] + gap['tonight_remaining_use']:.2f})",
                     ingredient_id=ing)
            return
        q = self._quote(ing, gap["gap"], gap["needed_by"], trace, exclude)
        if q is None:
            self.log(f"{p['ingredient']}: no supplier available", ingredient_id=ing)
            return
        why = (f"{p['dish']} down to {p['remaining_portions']} portions vs {p['forecast_remaining']:.0f} still forecast tonight; "
               f"next 2 days need {gap['need_next_days']:.2f} (P90) + {gap['tonight_remaining_use']:.2f} tonight, on hand {gap['on_hand']:.2f}, "
               f"inbound {gap['inbound']:.2f}" + (f"; {p['shortage']['supplier_id']} is short-shipping ({p['shortage']['fill_rate']:.0%} fill)"
                                                   if p.get("shortage") else "")
               + f"; {q['supplier']} is the fastest feasible fallback ({q['earliest_delivery']})")
        prop = self._po(ing, q, gap["gap"], why, trace, gap)
        if prop is None:
            self.log(f"{p['ingredient']}: need {gap['gap']:.2f} {cat.INGREDIENTS_BY_ID[ing].unit} is too small for an emergency "
                     f"order ({q['supplier']} minimum ${q['min_order_value']:.0f}); left for tomorrow's regular order", ingredient_id=ing)
            return
        self.submit([prop], situation={"stock_low": p, "gap": {k: v for k, v in gap.items() if k != "open_pos"}, "quote": q})

    def recheck_tomorrow(self, reason: str = "") -> None:
        trace: list = []
        plan = self.tool(trace, "optimize_orders", horizon_days=2, service_quantile="p90")
        tmr = (self.svc.date + timedelta(days=1)).isoformat()
        props = []
        for o in plan.get("orders", [])[:4]:
            sup = cat.SUPPLIERS_BY_ID[o["supplier_id"]].name
            props.append(self.proposal(
                "place_po", {k: o[k] for k in ("supplier_id", "delivery_date", "lines", "total")},
                f"Add to tomorrow's order: {len(o['lines'])} lines from {sup} (${o['total']:.2f}), delivers {o['delivery_date']}",
                f"{reason}; optimizer ({plan['solver']}, {plan['status']}) re-planned the next 2 days at P90", trace,
                key=f"recheck:{o['supplier_id']}:{o['delivery_date']}", confidence=0.7, est_cost=o["total"],
                context={"tomorrow": tmr}))
        if not props:
            self.log(f"re-check after '{reason}': existing orders already cover tomorrow")
        self.submit(props, situation={"reason": reason, "plan_total": plan.get("total_cost"), "notes": plan.get("notes", [])[:3]})

    # ------------------------------------------------------------------ manager feedback
    def on_resolved(self, d: dict) -> None:
        if d["action_type"] != "place_po" or d["status"] not in ("rejected", "blocked"):
            return
        ing = (d.get("context") or {}).get("ingredient_id")
        if not ing:
            return
        h = self.handled.setdefault(ing, {"replans": 0, "excluded": set()})
        if h["replans"] >= MAX_REPLANS:
            self.log(f"{cat.INGREDIENTS_BY_ID[ing].name}: no further re-plan; holding until tomorrow's morning order", ingredient_id=ing)
            return
        h["replans"] += 1
        note = d.get("note") or ""
        trace: list = []
        gap = self._gap(ing, trace, days=1, quantile="p50")
        cap = settings.LIVE_PO_APPROVAL_THRESHOLD
        if d["status"] == "blocked":
            why_kind = "blocked by the constraint checker: " + "; ".join(v["message"] for v in d.get("violations", []) if v["severity"] == "block")
            q = self._quote(ing, max(gap["gap"], 0.1), gap["needed_by"], trace, h["excluded"] | {d["payload"]["supplier_id"]})
            qty = gap["gap"]
            cap = None
        elif SUPPLIER_WORDS.search(note) and not COST_WORDS.search(note):
            h["excluded"].add(d["payload"]["supplier_id"])
            why_kind = f"manager rejected the supplier ('{note}')"
            q = self._quote(ing, max(gap["gap"], 0.1), gap["needed_by"], trace, h["excluded"])
            qty = gap["gap"]
            cap = None
        else:
            why_kind = f"manager rejected on cost ('{note or 'no reason given'}'): cheapest supplier, tomorrow's P50 only, under ${cap:.0f}"
            q = self._quote(ing, max(gap["gap"], 0.1), gap["needed_by"], trace, h["excluded"], cheapest=True)
            qty = gap["gap"]
        if q is None or qty <= 0:
            self.log(f"{cat.INGREDIENTS_BY_ID[ing].name}: re-plan found nothing to order ({why_kind})", ingredient_id=ing)
            return
        why = (f"re-plan #{h['replans']}: {why_kind}; need {gap['need_next_days']:.2f} tomorrow (P50) + "
               f"{gap['tonight_remaining_use']:.2f} tonight vs on hand {gap['on_hand']:.2f} + inbound {gap['inbound']:.2f}")
        prop = self._po(ing, q, qty, why, trace, gap, replan=h["replans"], cap=cap)
        if prop is None and cap is not None:  # cheapest per unit did not fit: smallest feasible order from any supplier
            for alt in sorted(self.tool(trace, "supplier_quotes", ingredient_id=ing, qty=qty, needed_by=gap["needed_by"]),
                              key=lambda x: (x["min_order_value"], x["unit_price"])):
                if alt["supplier_id"] in h["excluded"]:
                    continue
                prop = self._po(ing, alt, qty, why + f"; {alt['supplier']} has the smallest workable minimum", trace, gap,
                                replan=h["replans"], cap=cap)
                if prop is not None:
                    q = alt
                    break
        if prop is None:
            limit = f"under ${cap:.0f} " if cap is not None else ""
            mins = ", ".join(f"{x['supplier']} ${x['min_order_value']:.0f}" for x in
                             self.tool(None, "supplier_quotes", ingredient_id=ing, qty=qty, needed_by=gap["needed_by"])
                             if x["supplier_id"] not in h["excluded"])
            self.log(f"{cat.INGREDIENTS_BY_ID[ing].name}: re-plan #{h['replans']} after '{note or d['status']}': tomorrow needs only "
                     f"{qty:.2f} {cat.INGREDIENTS_BY_ID[ing].unit}; no supplier can fill it {limit}without buying more than twice that "
                     f"to reach its minimum ({mins}); holding for tomorrow's regular order", ingredient_id=ing)
            return
        self.submit([prop], situation={"rejected": d["title"], "note": note, "gap": {k: v for k, v in gap.items() if k != "open_pos"}})
