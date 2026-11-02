"""Kitchen output: printed chits per station and the kitchen display (KDS).

Printer: every order prints one chit per station it touches (table / channel, time, each item with its course fire
time and mods, allergy lines in capitals). PRINTER_MODE=file appends to PRINTER_DIR/{service}/{station}.txt (also a
handy audit trail); PRINTER_MODE=tcp sends ESC/POS bytes to PRINTER_HOST[_<STATION>]:PRINTER_PORT; off disables it.
A printer failure is logged on the bus and never blocks the order.

KDS: `tickets(svc, station)` groups the station's live items by order with their status (scheduled / queued /
cooking / done), fire and age; cooks bump finished tickets off the screen (`kds_bumps`).
"""
from __future__ import annotations

import os
import socket
from pathlib import Path
from typing import TYPE_CHECKING

from restaurant_ops.config import settings
from restaurant_ops.data import catalog as cat
from restaurant_ops.live import events as E
from restaurant_ops.live.events import wall_now

if TYPE_CHECKING:  # pragma: no cover
    from restaurant_ops.live.service import Service

ESC_INIT = b"\x1b@"
ESC_BOLD_ON, ESC_BOLD_OFF = b"\x1bE\x01", b"\x1bE\x00"
ESC_CUT = b"\x1dV\x41\x03"
WIDTH = 42


def chit_lines(order: dict, station: str, hhmm: str) -> list[str]:
    where = f"TABLE {'+'.join(order.get('table_ids') or [])}" if order.get("table_ids") else f"ONLINE {order['order_id']}"
    out = [f"{cat.STATION_META.get(station, {}).get('name', station).upper():^{WIDTH}}", "=" * WIDTH,
           f"{where:<26}{hhmm:>16}", f"order {order['order_id']}  ({order.get('kind', 'food')})", "-" * WIDTH]
    if order.get("allergens"):
        out.append(f"!! ALLERGY: {', '.join(order['allergens']).upper()} !!")
    for it in order["items"]:
        if it["station"] != station:
            continue
        fire = it.get("fire_at", "")[11:16]
        out.append(f"1 x {it['name'][:26]:<26}{('@' + fire) if fire and fire != hhmm else 'NOW':>8}")
        for m in it.get("mods") or []:
            out.append(f"    {m.upper() if 'ALLERGY' in m.upper() else m}"[:WIDTH])
    for h in order.get("held") or []:
        if cat.DISHES_BY_ID.get(h["dish_id"]) and cat.DISHES_BY_ID[h["dish_id"]].station == station:
            out.append(f"HOLD {cat.DISHES_BY_ID[h['dish_id']].name}: allergy check with manager"[:WIDTH])
    out.append("=" * WIDTH)
    return out


class KitchenPrinter:
    def __init__(self, svc: "Service"):
        self.svc = svc
        self.printed: set[tuple[str, str, int]] = set()
        self.failures = 0
        svc.bus.subscribe(E.ORDER_CREATED, self.on_order, "printer.on_order")

    def __getstate__(self):
        return {k: v for k, v in self.__dict__.items() if k != "svc"}

    def on_order(self, ev: E.Event) -> None:
        if settings.PRINTER_MODE == "off":
            return
        o = ev.payload
        n_items = len(o["items"]) + len(o.get("held") or [])
        for st in sorted({i["station"] for i in o["items"]}):
            key = (o["order_id"], st, n_items)   # re-print when an allergen swap changes the ticket
            if key in self.printed:
                continue
            self.printed.add(key)
            self.emit(st, chit_lines(o, st, self.svc.clock.hhmm()))

    def emit(self, station: str, lines: list[str]) -> None:
        try:
            if settings.PRINTER_MODE == "tcp":
                host = os.environ.get(f"PRINTER_HOST_{station.upper()}") or settings.PRINTER_HOST
                payload = ESC_INIT + ESC_BOLD_ON + lines[0].encode("ascii", "replace") + b"\n" + ESC_BOLD_OFF + \
                    "\n".join(lines[1:]).encode("ascii", "replace") + b"\n\n\n" + ESC_CUT
                with socket.create_connection((host, settings.PRINTER_PORT), timeout=3) as s:
                    s.sendall(payload)
            else:
                d = Path(settings.PRINTER_DIR) / self.svc.service_id
                d.mkdir(parents=True, exist_ok=True)
                with open(d / f"{station}.txt", "a", encoding="utf-8") as f:
                    f.write("\n".join(lines) + "\n\n")
        except Exception as e:  # noqa: BLE001  printing must never block service
            self.failures += 1
            self.svc.bus.publish(E.AGENT_LOG, {"agent": "printer", "message": f"{station} printer failed ({type(e).__name__}); "
                                               f"the KDS still shows the ticket"}, source="printer")


def tickets(svc: "Service", station: str | None = None, include_bumped: bool = False) -> list[dict]:
    bumped = {r[0] for r in svc.conn.execute("SELECT item_id FROM kds_bumps WHERE service_id=?", (svc.service_id,))}
    now = svc.clock.t
    by_order: dict[tuple, dict] = {}
    for it in svc.kitchen.items.values():
        if it.status == "cancelled" or (station and it.station != station):
            continue
        if it.item_id in bumped and not include_bumped:
            continue
        o = svc.orders.get(it.order_id, {})
        key = (it.order_id, it.station)
        t = by_order.setdefault(key, {"order_id": it.order_id, "station": it.station, "table_ids": o.get("table_ids", []),
                                      "channel": o.get("channel"), "allergens": o.get("allergens", []), "items": [],
                                      "first_fire": it.fire_at})
        t["first_fire"] = min(t["first_fire"], it.fire_at)
        t["items"].append({"item_id": it.item_id, "dish": cat.DISHES_BY_ID[it.dish_id].name if it.dish_id in cat.DISHES_BY_ID else it.dish_id,
                           "status": it.status, "mods": it.mods, "fire_at": svc.clock.hhmm(it.fire_at), "scratch": it.scratch,
                           "bumped": it.item_id in bumped})
    out = []
    for t in by_order.values():
        st = [i["status"] for i in t["items"]]
        t["status"] = "done" if all(s == "done" for s in st) else "cooking" if "cooking" in st else \
            "queued" if "queued" in st else "scheduled"
        t["age_min"] = round(max(0.0, (now - t["first_fire"]) / 60), 1) if t["first_fire"] <= now else 0.0
        t["late"] = t["status"] != "done" and t["age_min"] > settings.KDS_LATE_MIN
        out.append(t)
    order = {"cooking": 0, "queued": 1, "done": 2, "scheduled": 3}
    return sorted(out, key=lambda t: (order[t["status"]], t["first_fire"]))


def bump(svc: "Service", order_id: str, station: str, by: str) -> int:
    ids = [i.item_id for i in svc.kitchen.items.values() if i.order_id == order_id and i.station == station]
    svc.conn.executemany("INSERT OR IGNORE INTO kds_bumps (service_id, item_id, bumped_ts, by_user) VALUES (?,?,?,?)",
                         [(svc.service_id, i, wall_now(), by) for i in ids])
    return len(ids)
