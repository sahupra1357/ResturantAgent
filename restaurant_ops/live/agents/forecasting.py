"""Forecasting agent (advisory): every 15 service minutes it compares covers seated with the forecast and revises
the rest of the night; nightly it publishes tomorrow's forecast with the signals that matter here.

A miss of 15% or more publishes forecast.miss and proposes an adjust_forecast for tomorrow (auto-executed: the
forecast feeds tomorrow's orders, and Procurement re-checks them). A heat alert refreshes the dish mix.
"""
from __future__ import annotations

from datetime import timedelta

from restaurant_ops.live import events as E
from restaurant_ops.live.agents.base import LiveAgent
from restaurant_ops.tools import registry as R

REVISE_EVERY_MIN = 15
MISS_PCT = 15.0
MISS_Z = 1.5        # the miss must also be statistically clear: arrivals are lumpy (parties of 1-6)
PERSISTENCE = 0.5  # share of tonight's miss carried into tomorrow's adjustment


class ForecastingAgent(LiveAgent):
    name = "forecasting"
    llm_reviewed = True

    def __init__(self, svc):
        super().__init__(svc)
        self.escalated_at: float | None = None
        svc.bus.subscribe(E.WEATHER_ALERT, self.on_weather, "forecasting.on_weather")

    def publish(self, reason: str, recheck: bool = False) -> None:
        snap = self.svc.forecast.snapshot()
        self.svc.bus.publish(E.FORECAST_UPDATED, {**snap, "reason": reason, "recheck": recheck}, source=self.name)

    def nightly(self) -> None:
        f = self.svc.forecast
        tm = next((r for r in f.tomorrow if r["meal_period"] == f.period), None)
        sig = [s for s in f.signals if s.get("significant")]
        msg = "tomorrow " + (f"{f.period} {tm['p10']:.0f}-{tm['p90']:.0f} covers (P50 {tm['p50']:.0f})" if tm else "n/a")
        if sig:
            msg += "; signals that matter here: " + ", ".join(f"{s['signal']} {s['effect_pct']:+.0f}%" for s in sig[:4])
        nosig = [s["signal"] for s in f.signals if not s.get("significant")]
        if nosig:
            msg += "; no effect: " + ", ".join(nosig[:3])
        self.log(msg)

    def on_tick(self, t_min: float) -> None:
        if t_min <= 0 or int(t_min) % REVISE_EVERY_MIN:
            return
        svc = self.svc
        rev = svc.forecast.revise(t_min, svc.metrics.covers_arrived)
        miss = rev["miss_pct"]
        reason = (f"{rev['seated']} covers arrived vs {rev['expected_so_far']:.0f} expected by {svc.clock.hhmm()}; "
                  f"tonight now {rev['p10']:.0f}-{rev['p90']:.0f} (P50 {rev['p50']:.0f}, {miss:+.0f}% vs model)")
        big = (abs(miss) >= MISS_PCT and abs(rev["z"]) >= MISS_Z
               and (self.escalated_at is None or abs(miss) >= abs(self.escalated_at) + 10))
        self.publish(reason, recheck=big)
        if big:
            self.escalated_at = miss
            svc.bus.publish(E.FORECAST_MISS, {"miss_pct": miss, "revised": rev, "base": svc.forecast.base, "reason": reason},
                            source=self.name)
            self._adjust_tomorrow(miss, reason)

    def _adjust_tomorrow(self, miss: float, reason: str) -> None:
        svc, trace = self.svc, []
        tmr = (svc.date + timedelta(days=1)).isoformat()
        fc = self.tool(trace, "forecast_covers", start_date=tmr, days=1)
        self.tool(trace, "reservations", start_date=tmr, days=1)
        factor = round(min(1.6, max(0.5, 1 + PERSISTENCE * miss / 100)), 3)
        row = next((r for r in fc if r["meal_period"] == svc.forecast.period), None)
        title = f"Adjust tomorrow's {svc.forecast.period} forecast x{factor}" + (f" (P50 {row['p50']:.0f} -> {row['p50'] * factor:.0f})" if row else "")
        self.submit([self.proposal(
            "adjust_forecast", {"date": tmr, "meal_period": svc.forecast.period, "factor": factor,
                                "reason": f"tonight running {miss:+.0f}% vs forecast; carry {PERSISTENCE:.0%} into tomorrow"},
            title, reason, trace, key=f"adjust:{tmr}", confidence=0.6, force=True)],
            situation={"tonight": svc.forecast.revised, "tomorrow": row})

    def on_resolved(self, decision: dict) -> None:
        if decision["action_type"] == "adjust_forecast" and decision["status"] == "executed":
            up = float(decision["payload"].get("factor", 1)) > 1
            R.clear_cache()
            self.svc.forecast.refresh()
            self.svc.bus.publish(E.FORECAST_UPDATED, {**self.svc.forecast.snapshot(), "reason": "tomorrow adjusted",
                                                      "recheck": False, "tomorrow_adjusted": True}, source=self.name)
            if up:  # only a surge adds to tomorrow's order; a slow night never buys more
                self.svc.procurement.recheck_tomorrow(reason=decision["title"])

    def on_weather(self, ev: E.Event) -> None:
        R.clear_cache()
        self.svc.forecast.refresh()
        self.publish(f"weather alert: {ev.payload.get('alert')} {ev.payload.get('temp_c')} C; dish mix shifts toward cold dishes",
                     recheck=True)
