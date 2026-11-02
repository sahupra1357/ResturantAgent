"""Prep & staffing agent (the 6th live agent): mise-en-place burn-down and surge staffing.

Prep: at service start the prep list (forecast mix, core/prep.py) becomes tonight's mise en place per dish. Every
fired item uses one prepped portion; with none left the item is cooked from scratch (SCRATCH_PENALTY x slower).
Every PREP_REVIEW_EVERY_MIN minutes the agent compares prepped portions with what is still forecast tonight and
schedules a batch for the prep cook (auto-executed; the constraint checker refuses a batch the stock can't cover).

Staffing: when a station stays behind (wait >= SURGE_WAIT_MIN for SURGE_STREAK_MIN minutes) the agent asks the
manager to call a cook in. The constraint checker enforces the surge labour budget (SURGE_LABOR_BUDGET); the cook
arrives CALL_IN_DELAY_MIN later and adds a station slot set. After RELEASE_CALM_MIN calm minutes the extra cook is
released (auto). Deterministic: the hot path never waits on a model.
"""
from __future__ import annotations

import math

from restaurant_ops.config import settings
from restaurant_ops.data import catalog as cat
from restaurant_ops.live import events as E
from restaurant_ops.live.agents.base import LiveAgent
from restaurant_ops.tools import registry as R

BATCH_TRIGGER = 0.5      # prep a batch when prepped < this x still forecast
BATCH_MAX = 16
PREP_MIN_PER_PORTION = 0.6  # prep-cook minutes per portion (fraction of the dish's labour minutes)


class PrepAgent(LiveAgent):
    name = "prep"

    def __init__(self, svc):
        super().__init__(svc)
        self.prep: dict[str, dict] = {}
        self.prep_busy_until = 0.0
        self.batch_queue: list[dict] = []    # {dish_id, portions, done_t}
        self.behind: dict[str, int] = {}
        self.calm: dict[str, int] = {}
        self.extra: dict[str, int] = {}
        self.arrivals: list[dict] = []       # {station, t, decision_id}
        self.surge_spent = 0.0
        self.scratch_items = 0
        svc.bus.subscribe(E.KITCHEN_LOAD, self.on_load, "prep.on_load")

    def attach(self) -> None:
        self.svc.kitchen.fire_hook = self.on_fire
        self.svc.kitchen.scratch_check = self.scratch_factor

    # ------------------------------------------------------------------ mise en place
    def start_of_service(self) -> None:
        svc, trace = self.svc, []
        rows = R.prep_quantities(date=svc.date.isoformat(), meal_period=svc.period, forecast_quantile="p50")
        trace.append({"tool": "prep_quantities", "args": {"date": svc.date.isoformat(), "meal_period": svc.period}, "result": {"n": len(rows)}})
        for r in rows:
            n = int(r["portions"])
            self.prep[r["dish_id"]] = {"dish_id": r["dish_id"], "station": r["station"], "par": n, "prepped": n, "used": 0,
                                       "scratch": 0, "batches": 0}
        self.attach()
        self.publish(f"prep list from the forecast mix: {sum(v['par'] for v in self.prep.values())} portions across "
                     f"{len(self.prep)} dishes")

    def scratch_factor(self, dish_id: str) -> float:
        p = self.prep.get(dish_id)
        return 1.0 if p and p["prepped"] > 0 else settings.SCRATCH_PENALTY

    def on_fire(self, it) -> float:
        p = self.prep.setdefault(it.dish_id, {"dish_id": it.dish_id, "station": it.station, "par": 0, "prepped": 0, "used": 0,
                                              "scratch": 0, "batches": 0})
        if p["prepped"] > 0:
            p["prepped"] -= 1
            p["used"] += 1
            return 1.0
        p["scratch"] += 1
        self.scratch_items += 1
        return settings.SCRATCH_PENALTY

    def on_tick(self, t_min: float) -> None:
        svc = self.svc
        now = svc.clock.t
        for b in [b for b in self.batch_queue if b["done_t"] <= now]:
            self.batch_queue.remove(b)
            p = self.prep[b["dish_id"]]
            p["prepped"] += b["portions"]
            p["batches"] += 1
            self.log(f"prep batch ready: {b['portions']} {cat.DISHES_BY_ID[b['dish_id']].name}")
        for a in [a for a in self.arrivals if a["t"] <= now]:
            self.arrivals.remove(a)
            st = svc.kitchen.stations[a["station"]]
            res = svc.kitchen.set_cooks(a["station"], st.cooks + 1, now)
            self.extra[a["station"]] = self.extra.get(a["station"], 0) + 1
            svc.bus.publish(E.STAFF_UPDATED, {**res, "change": "+1", "reason": "called-in cook arrived"}, source=self.name)
        if int(t_min) % settings.PREP_REVIEW_EVERY_MIN == 0 and t_min > 0:
            self.review_prep()
        if int(t_min) % 5 == 0:
            self.publish()

    def review_prep(self) -> None:
        svc = self.svc
        now = svc.clock.t
        if self.prep_busy_until > now or self.batch_queue:
            return
        rem = svc.forecast.dish_remaining(now / 60)
        cands = []
        for d, p in self.prep.items():
            need = rem.get(d, 0.0)
            if need >= 3 and p["prepped"] < BATCH_TRIGGER * need and svc.menu.orderable(d):
                cands.append((need - p["prepped"], d, need))
        if not cands:
            return
        gap, d, need = max(cands)
        p = self.prep[d]
        stock = svc.available_portions(d) - p["prepped"]
        portions = int(min(BATCH_MAX, math.ceil(gap), stock))
        if portions < 2:
            return
        trace = [{"tool": "remaining_forecast", "args": {}, "result": {d: need}},
                 {"tool": "remaining_portions", "args": {"dish_id": d}, "result": {"portions": svc.available_portions(d), "prepped": p["prepped"]}}]
        dish = cat.DISHES_BY_ID[d]
        minutes = round(portions * dish.prep_minutes * PREP_MIN_PER_PORTION / max(settings.PREP_COOKS, 1), 1)
        self.submit([self.proposal(
            "prep_batch", {"dish_id": d, "portions": portions, "station": dish.station, "prep_minutes": minutes},
            f"Prep {portions} more {dish.name} ({minutes:.0f} min prep)",
            f"{p['prepped']} prepped vs {need:.0f} still forecast tonight; cooking from scratch is "
            f"{(settings.SCRATCH_PENALTY - 1) * 100:.0f}% slower", trace, key=f"prep:{d}:{int(now // 600)}", confidence=0.8)])

    def run_batch(self, payload: dict) -> dict:
        now = self.svc.clock.t
        start = max(now, self.prep_busy_until)
        done = start + float(payload["prep_minutes"]) * 60
        self.prep_busy_until = done
        self.batch_queue.append({"dish_id": payload["dish_id"], "portions": int(payload["portions"]), "done_t": done})
        self.publish()
        return {"ready_at": self.svc.clock.hhmm(done)}

    # ------------------------------------------------------------------ staffing
    def on_load(self, ev: E.Event) -> None:
        svc = self.svc
        for r in ev.payload["stations"]:
            s = r["station"]
            if r["wait_min"] >= settings.SURGE_WAIT_MIN:
                self.behind[s] = self.behind.get(s, 0) + 1
                self.calm[s] = 0
            elif r["utilisation"] < 0.3:
                self.calm[s] = self.calm.get(s, 0) + 1
                self.behind[s] = 0
            else:
                self.behind[s] = self.calm[s] = 0
            pending = any(a["station"] == s for a in self.arrivals)
            if (self.behind.get(s, 0) >= settings.SURGE_STREAK_MIN and not pending
                    and self.extra.get(s, 0) < settings.MAX_EXTRA_COOKS):
                cost = settings.COOK_HOURLY_RATE * settings.SURGE_SHIFT_HOURS
                trace = [{"tool": "station_queue", "args": {}, "result": {k: r[k] for k in ("station", "cooks", "queue_depth", "wait_min")}}]
                self.behind[s] = 0
                self.submit([self.proposal(
                    "add_staff", {"station": s, "add": 1, "hours": settings.SURGE_SHIFT_HOURS, "hourly_rate": settings.COOK_HOURLY_RATE,
                                  "surge_spent": self.surge_spent, "cost": cost, "arrives_in_min": settings.CALL_IN_DELAY_MIN},
                    f"Call in a {s} cook (${cost:.0f}, arrives in {settings.CALL_IN_DELAY_MIN:.0f} min): {s} {r['wait_min']:.0f} min behind",
                    f"{s} wait >= {settings.SURGE_WAIT_MIN:.0f} min for {settings.SURGE_STREAK_MIN} min with {r['cooks']} cooks; "
                    f"surge labour spent ${self.surge_spent:.0f} of ${settings.SURGE_LABOR_BUDGET:.0f}", trace,
                    key=f"add_staff:{s}", confidence=0.75, timeout_default="reject", est_cost=cost)])
            if self.extra.get(s, 0) > 0 and self.calm.get(s, 0) >= settings.RELEASE_CALM_MIN:
                self.calm[s] = 0
                self.submit([self.proposal("release_staff", {"station": s, "remove": 1},
                                           f"Release the extra {s} cook: calm for {settings.RELEASE_CALM_MIN} min",
                                           "station load low; saves labour", [], key=f"release:{s}", confidence=0.85)])

    def on_resolved(self, d: dict) -> None:
        if d["action_type"] == "add_staff" and d["status"] == "executed":
            p = d["payload"]
            self.surge_spent += float(p["add"]) * float(p["hours"]) * float(p["hourly_rate"])
            self.arrivals.append({"station": p["station"], "t": self.svc.clock.t + settings.CALL_IN_DELAY_MIN * 60, "decision_id": d["id"]})
            self.publish()

    def release(self, payload: dict) -> dict:
        svc, s = self.svc, payload["station"]
        if self.extra.get(s, 0) <= 0:
            return {"error": "no extra cook on this station"}
        st = svc.kitchen.stations[s]
        res = svc.kitchen.set_cooks(s, st.cooks - 1, svc.clock.t)
        self.extra[s] -= 1
        svc.bus.publish(E.STAFF_UPDATED, {**res, "change": "-1", "reason": "released"}, source=self.name)
        self.publish()
        return res

    # ------------------------------------------------------------------ view
    def view(self) -> dict:
        svc = self.svc
        rem = svc.forecast.dish_remaining(svc.clock.t / 60)
        rows = [{**p, "name": cat.DISHES_BY_ID[d].name, "forecast_remaining": rem.get(d, 0.0)} for d, p in sorted(self.prep.items())]
        return {"dishes": rows, "prep_busy_until": svc.clock.hhmm(self.prep_busy_until) if self.prep_busy_until > svc.clock.t else None,
                "batches_pending": [{**b, "ready_at": svc.clock.hhmm(b["done_t"])} for b in self.batch_queue],
                "cooks": {s: st.cooks for s, st in svc.kitchen.stations.items()}, "extra": dict(self.extra),
                "arriving": [{"station": a["station"], "at": svc.clock.hhmm(a["t"])} for a in self.arrivals],
                "surge_spent": round(self.surge_spent, 2), "surge_budget": settings.SURGE_LABOR_BUDGET,
                "scratch_items": self.scratch_items}

    def publish(self, reason: str = "") -> None:
        self.svc.bus.publish(E.PREP_UPDATED, {**self.view(), "reason": reason}, source=self.name)
