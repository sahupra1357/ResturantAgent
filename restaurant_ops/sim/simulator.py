"""The simulated restaurant world: advance day by day against a (copied) SQLite database.

`World(db_path, run_id, policy, seed)` owns one connection to that DB. Each `advance_day()`:
  1. delivers POs due today (supplier fill rate / delay probability, seeded per date + supplier [+ ingredient]);
  2. realises demand from the hidden truth (`generator.realise_day`) with the day's weather / events / private covers;
  3. applies menu_state (86'd dishes -> lost sales, part of the demand shifts; specials get a demand boost);
  4. depletes stock FEFO per BOM, records stockouts (a dish that cannot be made -> lost sales), revenue and food cost;
  5. expires lots whose last usable day was today (waste_log), writes stock_counts and kpi_daily;
  6. moves to the next day.
Demand and delivery randomness depend only on (seed, date, supplier, ingredient), never on call order, so the agent and
baseline worlds face identical luck. Every public method commits, so the agent graph (own connections) never hits a lock.
No LLM imports here.
"""
from __future__ import annotations

import copy
import json
import re
import sqlite3
import zlib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from restaurant_ops.config import settings
from restaurant_ops.core import inventory as inv
from restaurant_ops.core.util import r2, rq, to_date
from restaurant_ops.data import generator as gen
from restaurant_ops.db.connection import get_conn, init_db
from restaurant_ops.sim import disruptions as D

EPS = 1e-6
SHIFT_SHARE = 0.4  # share of an 86'd dish's lost demand that moves to other dishes of the same category
SPECIAL_BOOST = 0.2  # a special sells 20% more (taken from the other dishes: covers do not change)
DELIVERY_FILL_BONUS = (0.03, 0.06)  # same fill model as the generated PO history: min(1, fill_rate + N(0.03, 0.06))
MIN_FILL = 0.4
PERIOD_RE = re.compile(r"\((lunch|dinner|late)\)")
PORTIONS_RE = re.compile(r"^\s*(\d+)\s+portions")


def _h(s: str) -> int:
    return zlib.crc32(s.encode())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _apportion(total: int, weights: dict[str, float]) -> dict[str, int]:
    """Split an integer `total` over keys proportionally to weights (largest remainder, deterministic)."""
    tw = sum(w for w in weights.values() if w > 0)
    if total <= 0 or tw <= 0:
        return {}
    raw = {k: total * max(w, 0.0) / tw for k, w in weights.items()}
    out = {k: int(v) for k, v in raw.items()}
    left = total - sum(out.values())
    for k in sorted(raw, key=lambda k: (-(raw[k] - out[k]), k))[:left]:
        out[k] += 1
    return {k: v for k, v in out.items() if v > 0}


class World:
    """One policy's simulated restaurant (one DB copy, one connection)."""

    def __init__(self, db_path: str | Path, run_id: str, policy: str, seed: int | None = None):
        self.db_path = Path(db_path)
        self.run_id = run_id
        self.policy = policy
        self.seed = settings.SEED if seed is None else int(seed)
        self.conn = get_conn(self.db_path)
        init_db(self.conn)  # idempotent; adds the Phase 4 tables to older databases
        last = self.conn.execute("SELECT MAX(date) FROM kpi_daily WHERE run_id=? AND policy=?", (run_id, policy)).fetchone()[0]
        self.today: date = to_date(last) + timedelta(days=1) if last else settings.SIM_START
        self.pending_events: list[dict] = []
        self._delayed: set[str] = set()
        self.dishes = {r["id"]: dict(r) for r in self.conn.execute("SELECT * FROM dishes")}
        self.ingredients = {r["id"]: dict(r) for r in self.conn.execute("SELECT * FROM ingredients")}
        self.suppliers = {r["id"]: dict(r) for r in self.conn.execute("SELECT * FROM suppliers")}
        self.bom = inv._bom(self.conn)

    # ------------------------------------------------------------------ helpers

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    def _rng(self, *keys: int) -> np.random.Generator:
        return np.random.default_rng([self.seed, *keys])

    def trace(self, kind: str, content: dict, node: str = "sim", sim_date: date | str | None = None) -> None:
        """Write one trace row (no commit; callers commit)."""
        self.conn.execute("INSERT INTO trace (run_id, sim_date, ts, node, kind, content) VALUES (?,?,?,?,?,?)",
                          (self.run_id, str(sim_date or self.today), _now(), node, kind, json.dumps(content, default=str)))

    def _invalidate(self) -> None:
        """Drop cached tool results after the world changed."""
        try:
            from restaurant_ops.tools import registry as R

            R.clear_cache()
        except Exception:  # noqa: BLE001
            pass

    def weekday(self, d: date | None = None) -> str:
        return (d or self.today).strftime("%a")

    # ------------------------------------------------------------------ disruptions

    def load_scenario(self, name: str) -> list[dict]:
        """Queue a scenario's disruptions (each with its resolved 'date') in `pending_events`."""
        self.pending_events = D.get_scenario(name)
        return self.pending_events

    def due_events(self) -> list[dict]:
        """Queued disruptions that land today."""
        return [e for e in self.pending_events if e["date"] == self.today.isoformat()]

    def inject(self, disruption: dict) -> dict:
        """Apply a disruption to the world now and drop it from the queue.

        Returns {"disruption": enriched copy, "result": summary, "trigger": supervisor event trigger}.
        """
        dis = copy.deepcopy(disruption)
        result = D.apply_to_world(self, dis)
        self.pending_events = [e for e in self.pending_events
                               if not (e.get("kind") == dis["kind"] and e.get("date") == disruption.get("date"))]
        self.trace("event", {"event": "disruption", "kind": dis["kind"], "params": dis.get("params"), "result": result})
        self.conn.commit()
        self._invalidate()
        return {"disruption": dis, "result": result, "trigger": D.to_trigger(dis)}

    # ------------------------------------------------------------------ purchase orders (also used by the baseline)

    def create_po(self, supplier_id: str, delivery_date: str, lines: list[dict], decision_id: int | None = None) -> dict:
        """Insert a 'sent' purchase order created today (same id scheme as tools.registry.send_po)."""
        tag = f"PO-{self.today.isoformat().replace('-', '')[2:]}-"
        n = self.conn.execute("SELECT COUNT(*) FROM purchase_orders WHERE po_id LIKE ?", (tag + "%",)).fetchone()[0]
        po_id = f"{tag}{n + 1:03d}"
        total = round(sum(float(l["qty"]) * float(l["unit_price"]) for l in lines), 2)
        self.conn.execute("INSERT INTO purchase_orders (po_id, supplier_id, created_date, expected_delivery, status, total, decision_id) "
                          "VALUES (?,?,?,?,?,?,?)", (po_id, supplier_id, self.today.isoformat(), delivery_date, "sent", total, decision_id))
        self.conn.executemany("INSERT INTO po_lines (po_id, ingredient_id, qty, unit_price, qty_delivered) VALUES (?,?,?,?,NULL)",
                              [(po_id, l["ingredient_id"], float(l["qty"]), float(l["unit_price"])) for l in lines])
        self.conn.commit()
        self._invalidate()
        return {"po_id": po_id, "supplier_id": supplier_id, "expected_delivery": delivery_date, "total": total}

    # ------------------------------------------------------------------ advance one day

    def advance_day(self) -> dict:
        """Run today's service and overnight processing, then move to tomorrow. Returns the day's summary."""
        d, ds = self.today, self.today.isoformat()
        deliveries = self._deliver(d)
        demand = self._realise(d)
        self._log_forecast(d, demand)
        menu = self._menu(d)
        sales, shifted = self._apply_menu(demand, menu)
        made, stock = self._serve(d, sales)
        expired = self._expire(d)
        self._stock_counts(d)

        revenue = sum(self.dishes[k]["price"] * q for p in made.values() for k, q in p.items())
        pos_rows = [(ds, p, k, q) for p, items in made.items() for k, q in items.items() if q > 0]
        self.conn.executemany("INSERT OR REPLACE INTO pos_sales VALUES (?,?,?,?)", pos_rows)
        self.conn.executemany("INSERT OR REPLACE INTO covers VALUES (?,?,?)", [(ds, p, demand[p]["covers"]) for p in demand])
        n86 = len(menu["eighty_sixed"])
        covers_demand = sum(demand[p]["covers"] for p in demand)
        orig = {p: sum(demand[p]["sales"].values()) for p in demand}
        covers_served = sum(round(demand[p]["covers"] * (sum(made[p].values()) / orig[p] if orig[p] else 1.0)) for p in demand)
        waste = self.conn.execute("SELECT COALESCE(SUM(value),0), COALESCE(SUM(qty),0) FROM waste_log WHERE date=?", (ds,)).fetchone()
        approvals = self.conn.execute("SELECT COUNT(*) FROM decisions WHERE run_id=? AND sim_date=? AND chef_note IS NOT NULL "
                                      "AND agent<>'baseline'", (self.run_id, ds)).fetchone()[0]
        lost_stock = sum(self.dishes[k]["price"] * q for k, q in stock["lost"].items())
        summary = {"date": ds, "weekday": self.weekday(d), "revenue": r2(revenue), "food_cost": r2(stock["cost"]),
                   "waste_value": r2(waste[0]), "waste_qty": rq(waste[1]), "stockouts": stock["stockouts"], "dishes_86d": n86,
                   "covers_demand": covers_demand, "covers_served": int(covers_served), "approvals": int(approvals),
                   "lost_revenue_stockout": r2(lost_stock), "lost_revenue_86": r2(shifted["lost_value"]),
                   "stockout_dishes": sorted(stock["lost"]), "deliveries": deliveries,
                   "expired": [{"ingredient_id": e["ingredient_id"], "qty": e["qty"], "value": e["value"]} for e in expired]}
        self.conn.execute("INSERT OR REPLACE INTO kpi_daily VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                          (self.run_id, self.policy, ds, summary["revenue"], summary["food_cost"], summary["waste_value"],
                           summary["waste_qty"], summary["stockouts"], n86, summary["covers_served"], summary["approvals"]))
        self.trace("event", {"event": "day_summary", **{k: v for k, v in summary.items() if k not in ("deliveries", "expired")}})
        self.today = d + timedelta(days=1)
        self.conn.commit()
        self._invalidate()
        return summary

    # ------------------------------------------------------------------ 1. deliveries

    def _override_fill(self, supplier_id: str, ingredient_id: str, ds: str) -> float | None:
        r = self.conn.execute("SELECT fill_rate FROM supply_overrides WHERE supplier_id=? AND (ingredient_id IS NULL OR ingredient_id=?) "
                              "AND start_date<=? AND end_date>=? ORDER BY id DESC LIMIT 1", (supplier_id, ingredient_id, ds, ds)).fetchone()
        return None if r is None else float(r[0])

    def _deliver(self, d: date) -> list[dict]:
        ds, tomorrow = d.isoformat(), (d + timedelta(days=1)).isoformat()
        pos = self.conn.execute("SELECT po_id, supplier_id, expected_delivery FROM purchase_orders "
                                "WHERE status='sent' AND expected_delivery<=? ORDER BY po_id", (ds,)).fetchall()
        out, delay_draw = [], {}
        for po in pos:
            sid, po_id = po["supplier_id"], po["po_id"]
            sup = self.suppliers[sid]
            if sid not in delay_draw:  # one draw per date + supplier
                delay_draw[sid] = float(self._rng(d.toordinal(), _h(sid), 0).random())
            if po_id not in self._delayed and po["expected_delivery"] == ds and delay_draw[sid] < float(sup["delay_prob"]):
                self._delayed.add(po_id)
                self.conn.execute("UPDATE purchase_orders SET expected_delivery=? WHERE po_id=?", (tomorrow, po_id))
                msg = f"{po_id} from {sid} delayed one day"
                self.trace("event", {"event": "delivery_delayed", "po_id": po_id, "supplier_id": sid, "new_date": tomorrow, "message": msg})
                out.append({"po_id": po_id, "supplier_id": sid, "status": "delayed"})
                continue
            lines, full = [], True
            for ln in self.conn.execute("SELECT ingredient_id, qty, unit_price FROM po_lines WHERE po_id=? ORDER BY ingredient_id", (po_id,)).fetchall():
                ing = ln["ingredient_id"]
                f = self._override_fill(sid, ing, ds)
                if f is None:
                    rng = self._rng(d.toordinal(), _h(sid), _h(ing))
                    f = min(1.0, float(sup["fill_rate"]) + float(rng.normal(*DELIVERY_FILL_BONUS)))
                    f = 1.0 if f >= 0.98 else max(f, MIN_FILL)
                qty = float(ln["qty"]) * f
                got = float(int(qty)) if self.ingredients[ing]["unit"] == "ea" else round(qty, 3)
                full = full and got >= float(ln["qty"]) - 1e-6
                self.conn.execute("UPDATE po_lines SET qty_delivered=? WHERE po_id=? AND ingredient_id=?", (got, po_id, ing))
                if got > EPS:
                    shelf = int(self.ingredients[ing]["shelf_life_days"])
                    self.conn.execute("INSERT OR REPLACE INTO lots VALUES (?,?,?,?,?,?,?,?)",
                                      (f"LOT-{d:%y%m%d}-{ing}-{po_id}", ing, got, ds, (d + timedelta(days=shelf)).isoformat(),
                                       sid, float(ln["unit_price"]), "active"))
                lines.append({"ingredient_id": ing, "ordered": float(ln["qty"]), "delivered": got})
            status = "delivered" if full else "partial"
            self.conn.execute("UPDATE purchase_orders SET status=? WHERE po_id=?", (status, po_id))
            self.trace("event", {"event": "delivery", "po_id": po_id, "supplier_id": sid, "status": status,
                                 "lines": [l for l in lines if l["delivered"] < l["ordered"] - 1e-6] or f"{len(lines)} lines in full"})
            out.append({"po_id": po_id, "supplier_id": sid, "status": status})
        self.conn.commit()
        return out

    # ------------------------------------------------------------------ 2. demand

    def _realise(self, d: date) -> dict[str, dict]:
        events = [e for e in gen.events_for(self.conn, d) if e["type"] in ("sports", "local")]
        extra = {r[0]: int(r[1]) for r in self.conn.execute(
            "SELECT meal_period, SUM(party_size) FROM reservations WHERE date=? AND kind='private' GROUP BY meal_period", (d.isoformat(),))}
        return gen.realise_day(d, temp_c=gen.weather_for(self.conn, d), events=events, extra_covers=extra, seed=self.seed)

    def _log_forecast(self, d: date, demand: dict[str, dict]) -> None:
        """Store today's P10/P50/P90 covers (as planned at the start of service, incl. approved adjustments) with the realised covers."""
        try:
            from restaurant_ops.core import forecasting as F

            for r in F.forecast_covers(d.isoformat(), 1, conn=self.conn):
                self.conn.execute("INSERT OR REPLACE INTO forecast_log VALUES (?,?,?,?,?,?,?,?)",
                                  (self.run_id, self.policy, r["date"], r["meal_period"], r["p10"], r["p50"], r["p90"],
                                   demand[r["meal_period"]]["covers"]))
        except Exception as e:  # noqa: BLE001  a forecast problem must not stop the simulation
            self.trace("event", {"event": "forecast_log_failed", "error": f"{type(e).__name__}: {e}"})

    # ------------------------------------------------------------------ 3. menu state

    def _menu(self, d: date) -> dict:
        """{'eighty_sixed': {dish: {periods}}, 'specials': {dish: (periods, cap|None)}} from menu_state for the day."""
        out = {"eighty_sixed": {}, "specials": {}}
        for r in self.conn.execute("SELECT dish_id, status, note FROM menu_state WHERE date=?", (d.isoformat(),)):
            note = (r["note"] or "")[:40]
            m = PERIOD_RE.search(note)
            periods = {m.group(1)} if m else set(gen.PERIODS)
            if r["status"] == "86":
                out["eighty_sixed"][r["dish_id"]] = periods
            elif r["status"] == "special":
                pm = PORTIONS_RE.match(note)
                out["specials"][r["dish_id"]] = (periods, int(pm.group(1)) if pm else None)
        return out

    def _apply_menu(self, demand: dict[str, dict], menu: dict) -> tuple[dict[str, dict[str, int]], dict]:
        """Realised demand -> orders that reach the kitchen. 86'd dishes lose their sales (part shifts to the
        same category); specials sell +20% (taken from the other dishes)."""
        sales = {p: dict(demand[p]["sales"]) for p in demand}
        lost_value = 0.0
        for p in sales:
            s = sales[p]
            for dish, periods in menu["eighty_sixed"].items():
                if p not in periods or s.get(dish, 0) <= 0:
                    continue
                lost = s[dish]
                s[dish] = 0
                shift = int(round(SHIFT_SHARE * lost))
                cat = self.dishes[dish]["category"]
                rec = {k: q for k, q in s.items() if q > 0 and k not in menu["eighty_sixed"] and self.dishes[k]["category"] == cat}
                rec = rec or {k: q for k, q in s.items() if q > 0 and k not in menu["eighty_sixed"]}
                for k, n in _apportion(shift, rec).items():
                    s[k] += n
                moved = sum(_apportion(shift, rec).values())
                lost_value += self.dishes[dish]["price"] * (lost - moved)
            for dish, (periods, cap) in menu["specials"].items():
                if p not in periods or s.get(dish, 0) <= 0 or dish in menu["eighty_sixed"]:
                    continue
                gain = int(round(SPECIAL_BOOST * s[dish]))
                if cap is not None:
                    gain = min(gain, cap)
                donors = {k: q for k, q in s.items() if q > 0 and k != dish and k not in menu["specials"]
                          and k not in menu["eighty_sixed"]}
                take = _apportion(min(gain, sum(donors.values())), donors)
                for k, n in take.items():
                    s[k] -= n
                s[dish] += sum(take.values())
        return sales, {"lost_value": lost_value}

    # ------------------------------------------------------------------ 4. service

    def _serve(self, d: date, sales: dict[str, dict[str, int]]) -> tuple[dict[str, dict[str, int]], dict]:
        """Make the portions the stock allows (round-robin across dishes so no dish hogs a shared ingredient),
        then deplete lots FEFO. Returns ({period: {dish: made}}, {cost, stockouts, lost: {dish: portions}})."""
        ds = d.isoformat()
        avail = {r[0]: float(r[1]) for r in self.conn.execute(
            "SELECT ingredient_id, SUM(qty) FROM lots WHERE status='active' AND qty>? AND expiry_date>=? AND received_date<=? "
            "GROUP BY ingredient_id", (EPS, ds, ds))}
        used: dict[str, float] = {}
        made: dict[str, dict[str, int]] = {}
        lost: dict[str, int] = {}
        stockouts = 0
        for p in gen.PERIODS:
            left = {k: int(q) for k, q in sales[p].items() if q > 0 and k in self.bom}
            made[p] = {k: 0 for k in left}
            active = sorted(left)
            while active:
                nxt = []
                for dish in active:
                    need = self.bom[dish]
                    if all(avail.get(i, 0.0) + 1e-9 >= q for i, q in need.items()):
                        for i, q in need.items():
                            avail[i] = avail.get(i, 0.0) - q
                            used[i] = used.get(i, 0.0) + q
                        made[p][dish] += 1
                        left[dish] -= 1
                        if left[dish] > 0:
                            nxt.append(dish)
                    else:
                        lost[dish] = lost.get(dish, 0) + left[dish]
                        stockouts += 1
                active = nxt
        cost = 0.0
        for ing, qty in sorted(used.items()):
            cost += inv.consume_fefo(self.conn, ing, qty, ds)["cost"]
        return made, {"cost": cost, "stockouts": stockouts, "lost": lost}

    # ------------------------------------------------------------------ 5. overnight

    def _expire(self, d: date) -> list[dict]:
        """Lots whose last usable day was today leave stock as waste ('expired')."""
        ds = d.isoformat()
        out = []
        for r in self.conn.execute("SELECT lot_id, ingredient_id, qty, unit_cost FROM lots WHERE status='active' AND qty>? "
                                   "AND expiry_date<=? ORDER BY lot_id", (EPS, ds)).fetchall():
            value = round(float(r["qty"]) * float(r["unit_cost"]), 2)
            self.conn.execute("INSERT INTO waste_log (date, lot_id, ingredient_id, qty, value, reason) VALUES (?,?,?,?,?,?)",
                              (ds, r["lot_id"], r["ingredient_id"], r["qty"], value, "expired"))
            self.conn.execute("UPDATE lots SET status='discarded' WHERE lot_id=?", (r["lot_id"],))
            out.append({"lot_id": r["lot_id"], "ingredient_id": r["ingredient_id"], "qty": rq(r["qty"]), "value": value})
        return out

    def _stock_counts(self, d: date) -> None:
        """End-of-day count per ingredient: system = active lots; counted adds noise (ribeye keeps its ~6.5% shrinkage)."""
        ds = d.isoformat()
        system = {r[0]: float(r[1]) for r in self.conn.execute(
            "SELECT ingredient_id, SUM(qty) FROM lots WHERE status='active' AND qty>? GROUP BY ingredient_id", (EPS,))}
        rng = self._rng(d.toordinal(), 7)
        rows = []
        for ing_id, ing in self.ingredients.items():
            sysq = system.get(ing_id, 0.0)
            shrink = gen.SHRINKAGE_PCT if ing_id == gen.SHRINKAGE_INGREDIENT else 0.0
            counted = sysq * (1 - shrink + float(rng.normal(0, 0.012)))
            nd = 0 if ing["unit"] == "ea" else 3
            rows.append((ds, ing_id, round(max(0.0, counted), nd), round(sysq, nd)))
        self.conn.executemany("INSERT OR REPLACE INTO stock_counts VALUES (?,?,?,?)", rows)
