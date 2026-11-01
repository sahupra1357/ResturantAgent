"""Headless, seeded live service (no browser, no wall-clock sleeps):

    python -m restaurant_ops.live.run --scenario salmon_short@19:00 --scenario party_of_12@19:30 --policy demo

Manager policies: demo (approve specials / 86s / throttles / allergen swaps / discards, reject an ingredient's first
emergency PO as over budget, approve the re-plan), approve, reject, timeout (nobody answers: safe defaults apply).
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime

from restaurant_ops.live.service import Service


def demo_policy():
    rejected: set[str] = set()

    def policy(d: dict):
        t = d["action_type"]
        if t in ("place_po", "substitute_supplier"):
            key = d.get("key", "")
            if key.startswith("po:") and key not in rejected:
                rejected.add(key)
                return (False, "Over budget for an emergency order - find something cheaper or wait for FreshCatch")
            return (True, "ok, within reason")
        if t == "comp":
            return (True, "approved comp")
        return (True, "approved")

    return policy


POLICIES = {
    "demo": demo_policy,
    "approve": lambda: (lambda d: (True, "approved")),
    "reject": lambda: (lambda d: (False, "manager: no")),
    "timeout": lambda: None,
}


def parse_schedule(items: list[str], svc: Service) -> list[tuple[float, str]]:
    out = []
    for it in items or []:
        name, _, at = it.partition("@")
        if at:
            hh, mm = (int(x) for x in at.split(":"))
            t = datetime.combine(svc.date, datetime.min.time()).replace(hour=hh, minute=mm)
            t_min = (t - svc.clock.start).total_seconds() / 60
        else:
            t_min = 120.0
        out.append((t_min, name))
    return sorted(out)


def run(scenarios: list[str] | None = None, policy: str = "demo", seed: int | None = None, bus: str = "memory",
        minutes: float | None = None, source_db: str | None = None) -> Service:
    pol = POLICIES[policy]()
    svc = Service(seed=seed, speed=60, bus_kind=bus, auto_policy=pol, source_db=source_db)
    svc.schedule = parse_schedule(scenarios or [], svc)
    svc.run_headless(minutes=minutes)
    return svc


def summary(svc: Service) -> dict:
    ds = list(svc.coordinator.decisions.values())
    by: dict[str, int] = {}
    for d in ds:
        by[d["status"]] = by.get(d["status"], 0) + 1
    return {"service_id": svc.service_id, "db": str(svc.db_path), "bus": svc.bus.kind, "kpis": svc.metrics.kpis(svc.forecast),
            "decisions": by, "bus_errors": svc.bus.errors[:5],
            "timeline": [f"{d['created_sim'][11:16]} [{d['agent']}] {d['title']} -> {d['status']}"
                         + (f" ({d['note']})" if d.get("note") and d["status"] not in ("executed",) else "") for d in ds]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run one seeded live dinner service headless")
    ap.add_argument("--scenario", action="append", default=[], help="name[@HH:MM], repeatable")
    ap.add_argument("--policy", default="demo", choices=sorted(POLICIES))
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--bus", default="memory", choices=["memory", "redis", "auto"])
    ap.add_argument("--minutes", type=float, default=None, help="stop after this many service minutes")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    svc = run(a.scenario, a.policy, a.seed, a.bus, a.minutes)
    s = summary(svc)
    if a.json:
        print(json.dumps(s, indent=2, default=str))
    else:
        print(f"service {s['service_id']}  bus={s['bus']}  db={s['db']}")
        for line in s["timeline"]:
            print("  " + line)
        print("decisions:", s["decisions"])
        for k, v in s["kpis"].items():
            print(f"  {k:<26}{v}")
        if s["bus_errors"]:
            print("bus errors:", *s["bus_errors"], sep="\n  ")
    svc.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
