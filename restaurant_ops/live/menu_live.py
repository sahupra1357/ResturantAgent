"""Tonight's menu state: 86'd dishes, specials, '86 after N more' limits and 'last N' flags."""
from __future__ import annotations

from restaurant_ops.data import catalog as cat

LAST_N = 4


class LiveMenu:
    def __init__(self, service_date):
        self.date = service_date
        self.eighty_sixed: dict[str, str] = {}     # dish -> note
        self.specials: dict[str, dict] = {}        # dish -> {portions, note}
        self.limits: dict[str, int] = {}           # dish -> portions left to sell before the 86 lands
        self.last_n: dict[str, int] = {}           # dish -> portions left (<= LAST_N), shown as "last 4"
        self.season_off = {d.id for d in cat.DISHES if d.seasonal_months and service_date.month not in d.seasonal_months}

    def on_menu(self) -> list[str]:
        return [d.id for d in cat.DISHES if d.id not in self.season_off]

    def orderable(self, dish_id: str) -> bool:
        return dish_id not in self.eighty_sixed and dish_id not in self.season_off and self.limits.get(dish_id, 1) > 0

    def available(self, portions: dict[str, int]) -> dict[str, int]:
        """dish -> portions that may still be sold (stock, limits, 86 and season applied)."""
        out = {}
        for d in self.on_menu():
            if not self.orderable(d):
                continue
            n = portions.get(d, 0)
            if d in self.limits:
                n = min(n, self.limits[d])
            if n > 0:
                out[d] = n
        return out

    def sold(self, dish_id: str) -> bool:
        """Record one portion sold. Returns True when a pending '86 after N' limit just hit zero."""
        if dish_id in self.limits:
            self.limits[dish_id] -= 1
            if self.limits[dish_id] <= 0:
                del self.limits[dish_id]
                self.eighty_sixed[dish_id] = "86 limit reached"
                return True
        if dish_id in self.specials and self.specials[dish_id].get("portions"):
            self.specials[dish_id]["portions"] = max(0, int(self.specials[dish_id]["portions"]) - 1)
        return False

    def snapshot(self) -> dict:
        return {"eighty_sixed": [{"dish_id": d, "name": cat.DISHES_BY_ID[d].name, "note": n} for d, n in self.eighty_sixed.items()],
                "specials": [{"dish_id": d, "name": cat.DISHES_BY_ID[d].name, **v} for d, v in self.specials.items()],
                "limits": dict(self.limits), "last_n": dict(self.last_n), "season_off": sorted(self.season_off)}
