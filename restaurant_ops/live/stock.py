"""Live stock: FEFO lots held in memory (write-through to SQLite), depletion by recipe BOM, remaining portions.

The hot path (an item fires -> its BOM leaves the right lots) is a few dictionary updates plus one UPDATE per lot
touched, well under the 500 ms target. Quarantined lots (e.g. a failed cooler) stay in the DB as active until a
discard is approved, but are excluded from availability at once.
"""
from __future__ import annotations

import math
import sqlite3
from collections import defaultdict
from datetime import date

from restaurant_ops.data import catalog as cat

EPS = 1e-6


class LiveStock:
    def __init__(self, conn: sqlite3.Connection, service_date: date):
        self.conn = conn
        self.date = service_date
        self.lots: dict[str, list[dict]] = defaultdict(list)
        self.quarantine: set[str] = set()
        self.bom: dict[str, dict[str, float]] = defaultdict(dict)
        for d, i, q in conn.execute("SELECT dish_id, ingredient_id, qty_per_portion FROM recipe_lines"):
            self.bom[d][i] = float(q)
        self.uses: dict[str, list[str]] = defaultdict(list)  # ingredient -> dishes
        for d, lines in self.bom.items():
            for i in lines:
                self.uses[i].append(d)
        self.food_cost = 0.0
        self.reload()

    def reload(self, ingredient_ids: list[str] | None = None) -> None:
        ds = self.date.isoformat()
        q = ("SELECT lot_id, ingredient_id, qty, expiry_date, received_date, unit_cost FROM lots WHERE status='active' "
             "AND qty>? AND received_date<=? AND expiry_date>=?")
        args: list = [EPS, ds, ds]
        if ingredient_ids:
            q += f" AND ingredient_id IN ({','.join('?' * len(ingredient_ids))})"
            args += list(ingredient_ids)
            for i in ingredient_ids:
                self.lots[i] = []
        else:
            self.lots.clear()
        for r in self.conn.execute(q + " ORDER BY expiry_date, received_date, lot_id", args):
            self.lots[r["ingredient_id"]].append({"lot_id": r["lot_id"], "qty": float(r["qty"]), "expiry_date": r["expiry_date"],
                                                  "unit_cost": float(r["unit_cost"])})

    # ------------------------------------------------------------------ availability
    def on_hand(self, ingredient_id: str) -> float:
        return sum(l["qty"] for l in self.lots.get(ingredient_id, ()) if l["lot_id"] not in self.quarantine)

    def portions(self, dish_id: str, skip: tuple | list | set = ()) -> int:
        lines = {i: q for i, q in self.bom.get(dish_id, {}).items() if i not in skip}
        if not lines:
            return 0
        return int(max(0, min(math.floor(self.on_hand(i) / q + 1e-9) for i, q in lines.items())))

    def limiting(self, dish_id: str) -> str | None:
        lines = self.bom.get(dish_id, {})
        return min(lines, key=lambda i: self.on_hand(i) / lines[i]) if lines else None

    def uses_any(self, ingredient_ids) -> list[str]:
        ids = [ingredient_ids] if isinstance(ingredient_ids, str) else list(ingredient_ids)
        return sorted({d for i in ids for d in self.uses.get(i, [])})

    def all_portions(self, dish_ids: list[str] | None = None) -> dict[str, int]:
        return {d: self.portions(d) for d in (dish_ids or list(self.bom))}

    # ------------------------------------------------------------------ depletion
    def deplete(self, dish_id: str, qty: int = 1, skip: tuple | list | set = ()) -> dict:
        """Take the BOM of `qty` portions from lots, earliest expiry first. Returns {cost, picked, shortfall, ingredients}."""
        picked, short, cost = [], {}, 0.0
        for ing, per in self.bom.get(dish_id, {}).items():
            if ing in skip:
                continue
            need = per * qty
            for lot in self.lots.get(ing, []):
                if need <= EPS:
                    break
                if lot["lot_id"] in self.quarantine or lot["qty"] <= EPS:
                    continue
                take = min(lot["qty"], need)
                lot["qty"] -= take
                need -= take
                cost += take * lot["unit_cost"]
                picked.append({"lot_id": lot["lot_id"], "ingredient_id": ing, "qty": round(take, 4)})
                self.conn.execute("UPDATE lots SET qty=?, status=? WHERE lot_id=?",
                                  (max(lot["qty"], 0.0), "depleted" if lot["qty"] <= EPS else "active", lot["lot_id"]))
            self.lots[ing] = [l for l in self.lots.get(ing, []) if l["qty"] > EPS]
            if need > EPS:
                short[ing] = round(need, 4)
        self.food_cost += cost
        return {"cost": round(cost, 4), "picked": picked, "shortfall": short,
                "ingredients": [i for i in self.bom.get(dish_id, {}) if i not in skip]}

    def set_on_hand(self, ingredient_id: str, qty: float) -> float:
        """Shrink an ingredient's usable stock to `qty` (short delivery): newest lots are cut first. Returns qty removed."""
        have = self.on_hand(ingredient_id)
        cut = max(0.0, have - qty)
        removed = cut
        for lot in sorted(self.lots.get(ingredient_id, []), key=lambda l: l["expiry_date"], reverse=True):
            if cut <= EPS:
                break
            take = min(lot["qty"], cut)
            lot["qty"] -= take
            cut -= take
            self.conn.execute("UPDATE lots SET qty=?, status=? WHERE lot_id=?",
                              (max(lot["qty"], 0.0), "depleted" if lot["qty"] <= EPS else "active", lot["lot_id"]))
        self.lots[ingredient_id] = [l for l in self.lots.get(ingredient_id, []) if l["qty"] > EPS]
        return round(removed, 4)

    # ------------------------------------------------------------------ expiry
    def expiring(self, within_days: int = 1) -> list[dict]:
        out = []
        for ing, lots in self.lots.items():
            for l in lots:
                left = (date.fromisoformat(l["expiry_date"]) - self.date).days
                if left <= within_days and l["qty"] > EPS:
                    out.append({"lot_id": l["lot_id"], "ingredient_id": ing, "qty": round(l["qty"], 3),
                                "expiry_date": l["expiry_date"], "days_left": left,
                                "value": round(l["qty"] * l["unit_cost"], 2),
                                "quarantined": l["lot_id"] in self.quarantine,
                                "unit": cat.INGREDIENTS_BY_ID[ing].unit if ing in cat.INGREDIENTS_BY_ID else ""})
        return sorted(out, key=lambda x: (x["days_left"], -x["value"]))

    def lots_where(self, category: str | None = None, storage: str | None = None) -> list[dict]:
        out = []
        for ing, lots in self.lots.items():
            meta = cat.INGREDIENTS_BY_ID.get(ing)
            if meta is None or (category and meta.category != category) or (storage and meta.storage != storage):
                continue
            for l in lots:
                out.append({"lot_id": l["lot_id"], "ingredient_id": ing, "qty": round(l["qty"], 3),
                            "value": round(l["qty"] * l["unit_cost"], 2), "expiry_date": l["expiry_date"]})
        return out
