"""Intraday forecast: tonight's P10/P50/P90 covers (LightGBM, via the tools layer) spread over the service by an
arrival curve, revised from the covers actually seated.

    expected_so_far(t) = P50 x share of arrivals before t
    ratio              = seated / expected_so_far   (once enough covers are expected)
    revised total      = seated + remaining P50 x (1 + (ratio - 1) x weight(t))

weight(t) grows with the share of the night already seen, so one early rush does not swing the whole forecast.
"""
from __future__ import annotations

import math
from datetime import timedelta

from restaurant_ops.config import settings
from restaurant_ops.tools import registry as R

# share of dinner arrivals per 30-minute slot from 17:00 (last seating 21:30)
DINNER_CURVE = [0.07, 0.10, 0.14, 0.16, 0.17, 0.14, 0.10, 0.06, 0.04, 0.02]
SLOT_MIN = 30
MIN_EXPECTED = 8.0
SHRINK_K = 30.0         # covers of evidence needed before the observed ratio gets half weight
COVER_VAR_FACTOR = 3.2  # Var(arrived covers) ~ 3.2 x expected (parties arrive in lumps of 1-6)


def _curve(period: str) -> list[float]:
    c = DINNER_CURVE if period == "dinner" else [1 / 6] * 6
    s = sum(c)
    return [x / s for x in c]


class IntradayForecast:
    def __init__(self, service_date, period: str = "dinner"):
        self.date = service_date
        self.period = period
        self.curve = _curve(period)
        self.base: dict = {}
        self.mix: list[dict] = []
        self.tomorrow: list[dict] = []
        self.signals: list[dict] = []
        self.revised: dict = {}
        self.history: list[dict] = []  # {t_min, seated, expected, revised_p50}
        self.refresh()

    def refresh(self) -> None:
        """(Re)load tonight's model forecast, dish mix, tomorrow and the signal report from the tools layer."""
        ds = self.date.isoformat()
        rows = [r for r in R.forecast_covers(start_date=ds, days=1) if r["meal_period"] == self.period]
        self.base = rows[0] if rows else {"p10": 0, "p50": 0, "p90": 0, "capacity_cap": settings.seat_cap(self.period)}
        self.mix = R.forecast_dish_mix(start_date=ds, days=1, meal_period=self.period)
        tmr = (self.date + timedelta(days=1)).isoformat()
        self.tomorrow = R.forecast_covers(start_date=tmr, days=1)
        try:
            self.signals = R.signal_report()
        except Exception:  # noqa: BLE001
            self.signals = []
        if not self.revised:
            self.revised = {"p10": self.base["p10"], "p50": self.base["p50"], "p90": self.base["p90"], "ratio": 1.0}

    # ------------------------------------------------------------------ curve
    def share_before(self, t_min: float) -> float:
        if t_min <= 0:
            return 0.0
        acc = 0.0
        for k, s in enumerate(self.curve):
            a = k * SLOT_MIN
            if t_min >= a + SLOT_MIN:
                acc += s
            elif t_min > a:
                acc += s * (t_min - a) / SLOT_MIN
        return min(acc, 1.0)

    def expected_so_far(self, t_min: float, which: str = "p50") -> float:
        return float(self.base.get(which, 0.0)) * self.share_before(t_min)

    def revise(self, t_min: float, seated: int, extra_factor: float = 1.0) -> dict:
        """`seated` = covers that have arrived so far (seated, waiting or walked out): demand, not table turns."""
        exp = self.expected_so_far(t_min)
        share = self.share_before(t_min)
        ratio = seated / exp if exp >= MIN_EXPECTED else 1.0
        weight = exp / (exp + SHRINK_K) if exp >= MIN_EXPECTED else 0.0
        z = (seated - exp) / math.sqrt(COVER_VAR_FACTOR * exp) if exp >= MIN_EXPECTED else 0.0
        mult = (1 + (ratio - 1) * weight) * extra_factor
        rem = {q: max(0.0, float(self.base[q]) * (1 - share)) for q in ("p10", "p50", "p90")}
        cap = float(self.base.get("capacity_cap") or settings.seat_cap(self.period))
        out = {q: round(min(cap, seated + rem[q] * mult), 1) for q in rem}
        out.update(ratio=round(ratio, 3), z=round(z, 2), seated=int(seated), expected_so_far=round(exp, 1), share_seen=round(share, 3),
                   miss_pct=round((out["p50"] / max(float(self.base["p50"]), 1.0) - 1) * 100, 1))
        self.revised = out
        self.history.append({"t_min": round(t_min, 1), "seated": int(seated), "expected": round(exp, 1),
                             "revised_p50": out["p50"]})
        return out

    def expected_between(self, t0_min: float, t1_min: float) -> float:
        """Covers expected to arrive in [t0, t1] at the revised level."""
        total = float(self.revised.get("p50", self.base.get("p50", 0)))
        base = float(self.base.get("p50", 0)) or 1.0
        scale = total / base if base else 1.0
        return float(self.base.get("p50", 0)) * scale * max(0.0, self.share_before(t1_min) - self.share_before(t0_min))

    def remaining_fraction(self, t_min: float) -> float:
        return max(0.0, 1.0 - self.share_before(t_min))

    def dish_remaining(self, t_min: float) -> dict[str, float]:
        """Portions per dish still expected tonight (P50 mix x remaining share x revision ratio)."""
        base = float(self.base.get("p50", 0)) or 1.0
        mult = float(self.revised.get("p50", base)) / base
        frac = self.remaining_fraction(t_min)
        return {r["dish_id"]: round(r["p50"] * frac * mult, 1) for r in self.mix}

    def dish_weights(self) -> dict[str, float]:
        return {r["dish_id"]: max(0.0, float(r["p50"])) for r in self.mix}

    def curve_points(self) -> list[dict]:
        pts = []
        for k in range(len(self.curve) + 1):
            t = k * SLOT_MIN
            sh = self.share_before(t)
            pts.append({"t_min": t, "p10": round(self.base["p10"] * sh, 1), "p50": round(self.base["p50"] * sh, 1),
                        "p90": round(self.base["p90"] * sh, 1)})
        return pts

    def snapshot(self) -> dict:
        tm = [r for r in self.tomorrow if r["meal_period"] == self.period]
        sig = sorted([s for s in self.signals if s.get("significant")], key=lambda s: -abs(s.get("effect_pct") or 0))
        return {"period": self.period, "base": self.base, "revised": self.revised, "curve": self.curve_points(),
                "history": self.history[-80:],
                "tomorrow": [{k: r.get(k) for k in ("date", "weekday", "meal_period", "p10", "p50", "p90")} for r in self.tomorrow],
                "tomorrow_period": tm[0] if tm else None,
                "signals": [{k: s.get(k) for k in ("signal", "effect_pct", "significant", "note")} for s in sig[:5]],
                "all_signals": [{k: s.get(k) for k in ("signal", "effect_pct", "significant", "note")} for s in self.signals]}
