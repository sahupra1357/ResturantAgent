"""Service KPIs and the demo metrics (latency, quote accuracy, ticket times, stockouts caught, waste, food cost,
forecast error, autonomy)."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


def _pct(xs: list[float], q: float) -> float | None:
    return round(float(np.percentile(xs, q)), 3) if xs else None


@dataclass
class Metrics:
    covers_seated: int = 0
    covers_arrived: int = 0       # demand: every party that walked in or arrived for a booking
    covers_lost: int = 0          # parties that gave up waiting
    parties: int = 0
    orders: int = 0
    online_orders: int = 0
    revenue: float = 0.0
    food_cost: float = 0.0
    waste_value: float = 0.0
    comps: float = 0.0
    stockouts: int = 0            # a guest ordered something with no stock left (not caught)
    short_items: int = 0          # item fired with an ingredient shortfall
    eighty_six: int = 0
    eighty_six_caught: int = 0    # 86 landed while stock was still left (caught before a guest ordered it)
    decisions_auto: int = 0
    decisions_approval: int = 0
    decisions_blocked: int = 0
    order_latency_ms: list = field(default_factory=list)
    depletion_ms: list = field(default_factory=list)
    ticket_min: list = field(default_factory=list)    # dine-in: first fire -> last main done
    late_tickets: int = 0
    quotes: list = field(default_factory=list)        # {within, error_min}

    def kpis(self, forecast=None) -> dict:
        within = [q["within"] for q in self.quotes]
        fc = {}
        if forecast is not None and forecast.base:
            p50 = float(forecast.base.get("p50") or 0)
            fc = {"forecast_p50": p50, "forecast_p10": forecast.base.get("p10"), "forecast_p90": forecast.base.get("p90"),
                  "revised_p50": forecast.revised.get("p50"),
                  "covers_vs_forecast_pct": round((self.covers_arrived / p50 - 1) * 100, 1) if p50 else None}
        return {
            "covers": self.covers_seated, "covers_arrived": self.covers_arrived, "covers_lost": self.covers_lost, "parties": self.parties, "orders": self.orders,
            "online_orders": self.online_orders, "revenue": round(self.revenue, 2), "food_cost": round(self.food_cost, 2),
            "food_cost_pct": round(self.food_cost / self.revenue * 100, 1) if self.revenue else None,
            "waste_value": round(self.waste_value, 2), "comps": round(self.comps, 2),
            "stockouts": self.stockouts, "eighty_six": self.eighty_six, "eighty_six_caught": self.eighty_six_caught,
            "ticket_avg_min": round(float(np.mean(self.ticket_min)), 1) if self.ticket_min else None,
            "ticket_p90_min": _pct(self.ticket_min, 90), "late_tickets": self.late_tickets,
            "quote_accuracy_pct": round(100 * sum(within) / len(within), 1) if within else None,
            "quote_mae_min": round(float(np.mean([abs(q["error_min"]) for q in self.quotes])), 1) if self.quotes else None,
            "quotes_closed": len(within),
            "order_latency_p50_ms": _pct(self.order_latency_ms, 50), "order_latency_p95_ms": _pct(self.order_latency_ms, 95),
            "order_latency_max_ms": round(max(self.order_latency_ms), 3) if self.order_latency_ms else None,
            "depletion_p95_ms": _pct(self.depletion_ms, 95),
            "decisions_auto": self.decisions_auto, "decisions_approval": self.decisions_approval,
            "decisions_blocked": self.decisions_blocked, **fc,
        }
