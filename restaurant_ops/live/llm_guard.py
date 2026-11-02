"""Guard rails for the LLM pilot: one provider, per-service call and cost caps, a timeout, and a circuit breaker.

The agents always compute rule-based proposals first; the model only reviews them. `allow()` says whether the next
review may call the model. It refuses when the service has used LLM_MAX_CALLS_PER_SERVICE calls or
LLM_MAX_COST_PER_SERVICE dollars (tokens x LLM_PRICE_*_PER_MTOK), or after LLM_BREAKER_FAILURES consecutive failures
or timeouts (the breaker stays open for the rest of the service). A refused or failed review falls back to the
rule-based proposals, marked mode "rule" with the reason in the decision. Every call is a row in `llm_calls`.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from restaurant_ops.agents import llm
from restaurant_ops.config import settings
from restaurant_ops.live.events import wall_now

if TYPE_CHECKING:  # pragma: no cover
    from restaurant_ops.live.service import Service


def cost_of(tokens_in: int, tokens_out: int) -> float:
    return tokens_in / 1e6 * settings.LLM_PRICE_IN_PER_MTOK + tokens_out / 1e6 * settings.LLM_PRICE_OUT_PER_MTOK


class LLMGuard:
    def __init__(self, svc: "Service"):
        self.svc = svc
        self.calls = 0
        self.failures = 0
        self.consecutive = 0
        self.cost = 0.0
        self.latencies: list[float] = []
        self.tripped: str | None = None
        self.refused = 0

    def __getstate__(self):
        return {k: v for k, v in self.__dict__.items() if k != "svc"}

    def allow(self) -> tuple[bool, str]:
        if llm.is_mock():
            return False, "mock mode"
        if self.tripped:
            return False, self.tripped
        if self.calls >= settings.LLM_MAX_CALLS_PER_SERVICE:
            return False, f"call cap reached ({settings.LLM_MAX_CALLS_PER_SERVICE} per service)"
        if self.cost >= settings.LLM_MAX_COST_PER_SERVICE:
            return False, f"cost cap reached (${settings.LLM_MAX_COST_PER_SERVICE:.2f} per service)"
        return True, ""

    def record(self, agent: str, ok: bool, latency_ms: float | None, tokens_in: int = 0, tokens_out: int = 0, error: str = "") -> None:
        self.calls += 1
        c = cost_of(tokens_in, tokens_out)
        self.cost += c
        if latency_ms is not None:
            self.latencies.append(latency_ms)
        if ok:
            self.consecutive = 0
        else:
            self.failures += 1
            self.consecutive += 1
            if self.consecutive >= settings.LLM_BREAKER_FAILURES and not self.tripped:
                self.tripped = f"circuit breaker: {self.consecutive} failures in a row (last: {error}); rule-based for the rest of the service"
                self.svc.bus.publish("agent.log", {"agent": "llm", "message": self.tripped}, source="llm")
        self.svc.conn.execute("INSERT INTO llm_calls (service_id, ts, agent, provider, model, latency_ms, ok, tokens_in, tokens_out, cost, error) "
                              "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (self.svc.service_id, wall_now(), agent, settings.LLM_PROVIDER,
                                                                 settings.model_name, latency_ms, int(ok), tokens_in, tokens_out, round(c, 6),
                                                                 error[:300]))

    def view(self) -> dict:
        lat = sorted(self.latencies)
        ok, why = self.allow()
        return {"mode": "mock" if llm.is_mock() else llm.describe(), "active": ok, "why_not": why, "calls": self.calls,
                "failures": self.failures, "refused": self.refused, "cost": round(self.cost, 4),
                "cost_cap": settings.LLM_MAX_COST_PER_SERVICE, "call_cap": settings.LLM_MAX_CALLS_PER_SERVICE,
                "p50_ms": round(lat[len(lat) // 2], 1) if lat else None, "p95_ms": round(lat[int(len(lat) * 0.95)], 1) if lat else None,
                "timeout_s": settings.LLM_TIMEOUT_S, "tripped": self.tripped}
