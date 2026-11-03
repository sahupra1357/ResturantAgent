"""Read side: supplier learning, analytics, KPIs, the end-of-service report, scenarios and speed, UI views and the snapshot. Mixed into `live.service.Service`."""
from __future__ import annotations

import math

from restaurant_ops.agents import llm
from restaurant_ops.config import settings
from restaurant_ops.core import menu_engineering as ME
from restaurant_ops.core import suppliers as SUPL
from restaurant_ops.data import catalog as cat
from restaurant_ops.live import events as E
from restaurant_ops.live import scenarios as SC
from restaurant_ops.live.floor import LAYOUT_FIXTURES, PLAN_H, PLAN_W
from restaurant_ops.live.safety import waste_analytics
from restaurant_ops.live.world import DISH_VIEW, FEED_SKIP
from restaurant_ops.tools import registry as R


class Views:
    # ================================================================== analytics / suppliers / reports
    def supplier_scorecard(self) -> list[dict]:
        self.flush()
        rows = SUPL.scorecard(self.conn)
        R.clear_cache()
        return rows

    def learn_supplier(self, supplier_id: str, kind: str, fill: float | None = None, late: bool | None = None, note: str = "") -> dict:
        self.flush()
        res = SUPL.observe(self.conn, supplier_id, self.clock.iso(), kind, fill, late, weight=2.0, note=note)
        R.clear_cache()
        return res

    def tonight_sales(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for o in self.orders.values():
            for i in o["items"]:
                out[i["dish_id"]] = out.get(i["dish_id"], 0) + 1
        return out

    def menu_engineering(self, days: int = 28) -> dict:
        self.flush()
        return ME.analyze(self.conn, self.date, days, self.tonight_sales())

    def waste(self, days: int = 30) -> dict:
        self.flush()
        return waste_analytics(self.conn, self.date, days)

    def kpis(self) -> dict:
        k = self.metrics.kpis(self.forecast)
        k.update({f"pay_{key}": v for key, v in self.payments.summary().items()})
        k["surge_labor"] = round(self.prep.surge_spent, 2) if hasattr(self, "prep") else 0.0
        k["scratch_items"] = self.prep.scratch_items if hasattr(self, "prep") else 0
        k["waitlist"] = len(self.floor.waitlist)
        k["no_shows"] = sum(1 for b in self.host.bookings.values() if b.status == "no_show")
        dq = [q for q in self.metrics.quotes if q.get("fulfilment") == "delivery"]
        k["delivery_accuracy_pct"] = round(100 * sum(q["within"] for q in dq) / len(dq), 1) if dq else None
        return k

    def report(self) -> dict:
        """End-of-service report: KPIs, payments, decisions, waste, forecast error, menu mix."""
        ds = list(self.coordinator.decisions.values())
        by_status: dict[str, int] = {}
        for d in ds:
            by_status[d["status"]] = by_status.get(d["status"], 0) + 1
        sales = self.tonight_sales()
        return {"service": self.info(), "generated": E.wall_now(), "kpis": self.kpis(), "payments": self.payments.summary(),
                "forecast": {"base": self.forecast.base, "revised": self.forecast.revised,
                             "error": getattr(self, "forecast_error", None)},
                "decisions": {"by_status": by_status,
                              "items": [{k: d.get(k) for k in ("id", "created_sim", "agent", "action_type", "title", "status", "note")}
                                        for d in ds]},
                "sales_by_dish": sorted(({"dish_id": k, "name": cat.DISHES_BY_ID[k].name, "qty": v,
                                          "sales": round(v * cat.DISHES_BY_ID[k].price, 2)} for k, v in sales.items()),
                                        key=lambda x: -x["sales"]),
                "waste": self.waste(1), "staffing": self.prep.view(), "bookings": self.host.bookings_view(),
                "safety": {"checklist": self.safety.checklist(), "alerts": self.safety.alerts}}

    def inject(self, name: str) -> dict:
        res = SC.inject(self, name)
        self.flush()
        return res

    def set_speed(self, speed: int) -> dict:
        self.clock.set_speed(speed)
        self.bus.publish(E.CLOCK_TICK, self.clock_view(), source="clock")
        return self.clock_view()

    # ================================================================== views
    def info(self) -> dict:
        return {"service_id": self.service_id, "date": self.date.isoformat(), "weekday": self.date.strftime("%A"),
                "period": self.period, "start": self.clock.start.isoformat(timespec="minutes"),
                "end": self.clock.end.isoformat(timespec="minutes"), "seed": self.seed, "bus": self.bus.kind,
                "llm_mode": "mock" if llm.is_mock() else llm.describe(), "status": self.status,
                "approval_timeout_min": settings.APPROVAL_TIMEOUT_SIM_MIN, "po_threshold": settings.LIVE_PO_APPROVAL_THRESHOLD,
                "auth_enabled": settings.AUTH_ENABLED, "recovered": getattr(self, "recovered", None),
                "restaurant": (getattr(self, "profile", None) or {}).get("name", "Bistro 80"),
                "processor": getattr(getattr(self, "processor", None), "name", "simulated"),
                "reservations": len(self.guests.bookings), "booked_covers": self.guests.booked_covers}

    def clock_view(self) -> dict:
        return {"sim_ts": self.clock.iso(), "hhmm": self.clock.hhmm(), "t": round(self.clock.t, 1), "speed": self.clock.speed,
                "minutes_left": round(self.clock.minutes_left, 1), "status": self.status, **self.floor.occupancy()}

    def inventory_view(self) -> dict:
        t_min = self.clock.t / 60
        rem = self.forecast.dish_remaining(t_min)
        portions = {d: self.available_portions(d) for d in self.menu.on_menu()}
        low = [{"dish_id": d, "name": cat.DISHES_BY_ID[d].name, "remaining": n, "forecast_remaining": rem.get(d, 0.0)}
               for d, n in portions.items() if self.menu.orderable(d) and rem.get(d, 0) >= 2 and n < math.ceil(rem.get(d, 0))]
        return {"portions": portions, "forecast_remaining": rem, "low": sorted(low, key=lambda x: x["remaining"]),
                "expiring": self.stock.expiring(1)[:15], "quarantined": len(self.stock.quarantine),
                "shortages": self.shortages, "food_cost": round(self.metrics.food_cost, 2)}

    def snapshot(self) -> dict:
        t = self.clock.t
        return {"service": self.info(), "clock": self.clock_view(),
                "layout": {"width": PLAN_W, "height": PLAN_H, "fixtures": LAYOUT_FIXTURES},
                "tables": {tid: self.floor.table_view(tid, t) for tid in self.floor.tables},
                "kitchen": self.kitchen_load(), "inventory": self.inventory_view(), "forecast": self.forecast.snapshot(),
                "menu": self.menu.snapshot(),
                "online": {"mode": self.online.mode, "reason": self.online.mode_reason,
                           "orders": {o.order_id: self.online.view(o, self.clock) for o in self.online.orders.values()}},
                "decisions": {str(d["id"]): self.coordinator.view(d) for d in self.coordinator.decisions.values()},
                "kpis": self.kpis(), "feed": list(self.feed), "dishes": DISH_VIEW,
                "host": {"bookings": self.host.bookings_view(), "waitlist": self.host.waitlist_view()},
                "checks": {c.check_id: self.payments.view(c) for c in self.payments.checks.values()
                           if c.status == "open" or (self.clock.t - (c.closed_t or 0)) < 30 * 60},
                "prep": self.prep.view(), "safety": self.safety.view(), "drivers": self.dispatcher.view(self.clock.t),
                "zones": self.zones,
                "scenarios": [{"name": k, "label": v[1]} for k, v in SC.SCENARIOS.items()]}

    def _publish_state(self) -> None:
        self.bus.publish(E.KITCHEN_LOAD, self.kitchen_load(), source="kitchen")
        self.bus.publish(E.INVENTORY_SNAPSHOT, self.inventory_view(), source="inventory")
        self.bus.publish(E.MENU_UPDATED, self.menu.snapshot(), source="menu")
        self.bus.publish(E.KPI_UPDATED, self.kpis(), source="service")
        self.bus.publish(E.CLOCK_TICK, self.clock_view(), source="clock")

    def _to_feed(self, ev: E.Event) -> None:
        if ev.type in FEED_SKIP and not (ev.type == E.QUOTE_UPDATED and ev.payload.get("notify")):
            return
        self.feed.append(feed_entry(ev))


def feed_entry(ev: E.Event) -> dict:
    p = ev.payload
    keep = {k: p[k] for k in list(p)[:14] if not isinstance(p[k], (list, dict)) or k in ("items", "held", "table_ids", "violations")}
    if ev.type in (E.DECISION_PROPOSED, E.DECISION_RESOLVED):
        keep = {k: p.get(k) for k in ("id", "agent", "action_type", "title", "status", "note", "needs_approval")}
    if ev.type == E.ORDER_CREATED:
        keep = {"order_id": p["order_id"], "channel": p["channel"], "table_ids": p.get("table_ids"), "n_items": len(p["items"]),
                "stations": p.get("stations"), "allergens": p.get("allergens"), "held": len(p.get("held", [])),
                "latency_ms": p.get("latency_ms"), "kind": p.get("kind")}
    return {"seq": ev.seq, "type": ev.type, "source": ev.source, "sim_ts": ev.sim_ts, "payload": keep}
