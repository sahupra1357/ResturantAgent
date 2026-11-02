"""Food safety: temperature log (walk-in cooler, freezer, hot holding), the HACCP checklist, waste analytics and
donations of safe near-expiry stock.

Sensors are simulated: a reading per unit every TEMP_LOG_EVERY_MIN service minutes with seeded noise. A failed
unit drifts upward. A reading outside TEMP_LIMITS publishes temp.alert; for the walk-in cooler the Inventory agent
then quarantines the affected lots and requests a discard (the "cooler fails" scenario runs through this path).
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from typing import TYPE_CHECKING

import numpy as np

from restaurant_ops.config import settings
from restaurant_ops.live import events as E
from restaurant_ops.live.events import wall_now

if TYPE_CHECKING:  # pragma: no cover
    from restaurant_ops.live.service import Service

BASE = {"walk_in": 3.2, "freezer": -18.5, "hot_holding": 68.0}
NOISE = {"walk_in": 0.45, "freezer": 0.8, "hot_holding": 1.6}
FAIL_JUMP = {"walk_in": 6.5, "freezer": 9.0, "hot_holding": -9.0}   # first reading after a failure
FAIL_DRIFT = {"walk_in": 3.0, "freezer": 4.0, "hot_holding": -4.0}  # each later reading
UNIT_LABEL = {"walk_in": "Walk-in cooler", "freezer": "Freezer", "hot_holding": "Hot holding"}
# (item, due HH:MM, auto-completed by the sensor log for this unit)
CHECKLIST = [
    ("Walk-in cooler at or below 5 °C (opening)", "17:00", "walk_in"),
    ("Freezer at or below -15 °C (opening)", "17:00", "freezer"),
    ("Hot holding at or above 63 °C", "17:30", "hot_holding"),
    ("Sanitizer buckets at 200 ppm on every station", "17:00", None),
    ("Hand-wash stations stocked (soap, towels)", "17:00", None),
    ("Date labels on all prepped containers", "17:30", None),
    ("Mid-service cooler check", "19:30", "walk_in"),
    ("Allergen board updated with tonight's specials", "18:00", None),
    ("Closing: cooler and freezer temperatures logged", "22:30", "walk_in"),
]


class FoodSafety:
    def __init__(self, svc: "Service"):
        self.svc = svc
        self.rng = np.random.default_rng([svc.seed, 31])
        self.failed: dict[str, int] = {}     # unit -> readings since failure
        self.last: dict[str, dict] = {}
        self.alerts: list[dict] = []
        self.overdue_sent: set[int] = set()

    def __getstate__(self):
        return {k: v for k, v in self.__dict__.items() if k != "svc"}

    # ------------------------------------------------------------------ checklist
    def create_checklist(self) -> None:
        if self.svc.conn.execute("SELECT 1 FROM haccp_checks WHERE service_id=?", (self.svc.service_id,)).fetchone():
            return
        self.svc.conn.executemany("INSERT INTO haccp_checks (service_id, item, due) VALUES (?,?,?)",
                                  [(self.svc.service_id, item, due) for item, due, _ in CHECKLIST])

    def checklist(self) -> list[dict]:
        now = self.svc.clock.hhmm()
        rows = [dict(r) for r in self.svc.conn.execute("SELECT * FROM haccp_checks WHERE service_id=? ORDER BY due, id",
                                                       (self.svc.service_id,))]
        for r in rows:
            r["overdue"] = r["done_ts"] is None and r["due"] < now
        return rows

    def complete(self, check_id: int, by: str, ok: bool = True, note: str = "") -> dict:
        r = self.svc.conn.execute("SELECT * FROM haccp_checks WHERE id=? AND service_id=?", (int(check_id), self.svc.service_id)).fetchone()
        if r is None:
            raise KeyError(f"unknown checklist item {check_id}")
        self.svc.conn.execute("UPDATE haccp_checks SET done_ts=?, done_by=?, ok=?, note=? WHERE id=?",
                              (self.svc.clock.iso(), by, int(ok), note, int(check_id)))
        self.publish()
        return next(x for x in self.checklist() if x["id"] == int(check_id))

    def _auto_complete(self, unit: str, ok: bool, temp: float) -> None:
        now = self.svc.clock.hhmm()
        for item, due, u in CHECKLIST:
            if u == unit and due <= now:
                self.svc.conn.execute("UPDATE haccp_checks SET done_ts=?, done_by='sensor', ok=?, note=? WHERE service_id=? AND item=? "
                                      "AND done_ts IS NULL", (self.svc.clock.iso(), int(ok), f"{temp:.1f} °C", self.svc.service_id, item))

    # ------------------------------------------------------------------ temperatures
    def fail(self, unit: str = "walk_in") -> dict:
        self.failed[unit] = 0
        return self.log(units=[unit])

    def repair(self, unit: str) -> None:
        self.failed.pop(unit, None)

    def reading(self, unit: str) -> float:
        t = BASE[unit] + float(self.rng.normal(0, NOISE[unit]))
        if unit in self.failed:
            t += FAIL_JUMP[unit] + FAIL_DRIFT[unit] * self.failed[unit]
            self.failed[unit] += 1
        return round(t, 1)

    def log(self, units: list[str] | None = None) -> dict:
        out = {}
        for unit in units or list(BASE):
            temp = self.reading(unit)
            lo, hi = settings.TEMP_LIMITS[unit]
            ok = lo <= temp <= hi
            self.svc.conn.execute("INSERT INTO temp_log (service_id, ts, unit, temp_c, ok) VALUES (?,?,?,?,?)",
                                  (self.svc.service_id, self.svc.clock.iso(), unit, temp, int(ok)))
            self.last[unit] = {"unit": unit, "label": UNIT_LABEL[unit], "temp_c": temp, "ok": ok, "limit": [lo, hi],
                               "at": self.svc.clock.hhmm()}
            self._auto_complete(unit, ok, temp)
            out[unit] = self.last[unit]
            if not ok:
                a = {**self.last[unit], "message": f"{UNIT_LABEL[unit]} at {temp:.1f} °C (limit {lo:g} to {hi:g} °C)"}
                self.alerts.append(a)
                self.svc.bus.publish(E.TEMP_ALERT, a, source="safety")
        self.publish()
        return out

    def on_minute(self, m: int) -> None:
        if m % settings.TEMP_LOG_EVERY_MIN == 0:
            self.log()
        for r in self.checklist():
            if r["overdue"] and r["id"] not in self.overdue_sent and r["due"] < self.svc.clock.hhmm():
                due_t = self.svc.host.t_of(r["due"])
                if self.svc.clock.t - due_t >= 15 * 60:  # 15 minutes past due
                    self.overdue_sent.add(r["id"])
                    self.svc.bus.publish(E.AGENT_LOG, {"agent": "safety", "message": f"HACCP overdue: {r['item']} (due {r['due']})"},
                                         source="safety")

    def temps(self, limit: int = 60) -> list[dict]:
        return [dict(r) for r in self.svc.conn.execute("SELECT ts, unit, temp_c, ok FROM temp_log WHERE service_id=? ORDER BY id DESC LIMIT ?",
                                                       (self.svc.service_id, limit))]

    def view(self) -> dict:
        return {"units": list(self.last.values()), "checklist": self.checklist(), "alerts": self.alerts[-10:],
                "failed": sorted(self.failed)}

    def publish(self) -> None:
        self.svc.bus.publish(E.SAFETY_UPDATED, self.view(), source="safety")


# =========================================================================== waste analytics and donations


def waste_analytics(conn: sqlite3.Connection, as_of: date, days: int = 30) -> dict:
    """Waste $ and qty by reason, by ingredient and by day over the window, plus donations (not counted as waste)."""
    start = (as_of - timedelta(days=days - 1)).isoformat()
    end = as_of.isoformat()
    by_reason = [dict(r) for r in conn.execute(
        "SELECT reason, ROUND(SUM(value),2) AS value, ROUND(SUM(qty),3) AS qty, COUNT(*) AS n FROM waste_log "
        "WHERE date BETWEEN ? AND ? GROUP BY reason ORDER BY value DESC", (start, end))]
    by_ing = [dict(r) for r in conn.execute(
        "SELECT w.ingredient_id, i.name, i.category, ROUND(SUM(w.value),2) AS value, ROUND(SUM(w.qty),3) AS qty FROM waste_log w "
        "JOIN ingredients i ON i.id=w.ingredient_id WHERE w.date BETWEEN ? AND ? GROUP BY w.ingredient_id ORDER BY value DESC LIMIT 12",
        (start, end))]
    by_cat = [dict(r) for r in conn.execute(
        "SELECT i.category, ROUND(SUM(w.value),2) AS value FROM waste_log w JOIN ingredients i ON i.id=w.ingredient_id "
        "WHERE w.date BETWEEN ? AND ? GROUP BY i.category ORDER BY value DESC", (start, end))]
    daily = [dict(r) for r in conn.execute(
        "SELECT date, ROUND(SUM(value),2) AS value FROM waste_log WHERE date BETWEEN ? AND ? GROUP BY date ORDER BY date", (start, end))]
    don = [dict(r) for r in conn.execute(
        "SELECT date, ingredient_id, qty, value, partner FROM donations WHERE date BETWEEN ? AND ? ORDER BY id DESC", (start, end))]
    total = sum(r["value"] or 0 for r in by_reason)
    return {"window": {"start": start, "end": end, "days": days}, "total_value": round(total, 2), "by_reason": by_reason,
            "by_ingredient": by_ing, "by_category": by_cat, "daily": daily, "donations": don,
            "donated_value": round(sum(d["value"] for d in don), 2)}


def record_donation(conn: sqlite3.Connection, day: date, lots: list[dict], partner: str) -> dict:
    total = 0.0
    for lot in lots:
        lid = lot["lot_id"] if isinstance(lot, dict) else lot
        r = conn.execute("SELECT ingredient_id, qty, unit_cost FROM lots WHERE lot_id=? AND status='active'", (lid,)).fetchone()
        if r is None:
            continue
        value = round(float(r["qty"]) * float(r["unit_cost"]), 2)
        conn.execute("INSERT INTO donations (date, lot_id, ingredient_id, qty, value, partner) VALUES (?,?,?,?,?,?)",
                     (day.isoformat(), lid, r["ingredient_id"], float(r["qty"]), value, partner))
        conn.execute("UPDATE lots SET qty=0, status='depleted' WHERE lot_id=?", (lid,))
        total += value
    return {"donated": len(lots), "value": round(total, 2), "partner": partner, "at": wall_now()}
