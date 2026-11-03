"""Orders: submission from tables and the online page, kitchen events, food progress, manual orders. Mixed into `live.service.Service`."""
from __future__ import annotations

import math
import time

from restaurant_ops.data import catalog as cat
from restaurant_ops.live import events as E
from restaurant_ops.live.floor import Party
from restaurant_ops.live.world import LATE_TICKET_MIN, ServiceError


class OrderFlow:
    # ================================================================== orders
    def submit_order(self, channel: str, items: list[dict], notes: str = "", party: Party | None = None, kind: str = "food",
                     customer: str = "", source: str = "sim", guest_id: int | None = None, fulfilment: str = "pickup",
                     zone: str | None = None) -> dict:
        t0 = time.perf_counter()
        if channel == "online":
            self._n_online += 1
            oid = f"W{self._n_online:04d}"
        else:
            self._n_order += 1
            tag = party.table_ids[0] if party and party.table_ids else "HOST"
            oid = f"{tag}-{self._n_order:04d}"
        if party is not None and kind == "food":
            party.food_order = oid
        payload = {"order_id": oid, "channel": channel, "items": items, "notes": notes, "kind": kind,
                   "party_id": party.id if party else None, "party_size": party.size if party else 0,
                   "table_ids": list(party.table_ids) if party else [], "large": bool(party and party.kind == "large"),
                   "customer": customer, "source": source, "t0": t0,
                   "guest_id": guest_id if guest_id is not None else (party.guest_id if party else None),
                   "fulfilment": fulfilment if channel == "online" else None, "zone": zone if channel == "online" else None}
        self.bus.publish(E.ORDER_SUBMITTED, payload, source="online" if channel == "online" else "floor")
        res = self.order_results.get(oid, {"order_id": oid, "items": [], "held": [], "rejected": []})
        if channel == "online":
            o = self.online.orders.get(oid)
            res = {**res, "online": self.online.view(o, self.clock) if o else None}
        return res

    def _on_order_created(self, ev: E.Event) -> None:
        o, t = ev.payload, self.clock.t
        new = [i for i in o["items"] if i["item_id"] not in self.counted_items]
        for i in new:
            self.counted_items.add(i["item_id"])
            self.metrics.revenue += cat.DISHES_BY_ID[i["dish_id"]].price
        if o["order_id"] not in self.order_fire:
            self.order_fire[o["order_id"]] = math.inf
            self.metrics.orders += 1
        if o["channel"] == "online":
            self._online_register(o)
            return
        p = self.floor.parties.get(o.get("party_id") or "")
        if p is None or o.get("kind") != "food":
            return
        if not o["items"] and not o.get("held"):
            p.food_done = True
            if p.state not in ("waiting",):
                p.state = "eating"
                p.check_at = t + 10 * 60
                self.floor.set_state(p, "eating")
                self._tables(p)
            return
        if p.state == "seated":
            p.state = "ordered"
            self.floor.set_state(p, "ordered")
            self._tables(p)

    def _on_item_fired(self, ev: E.Event) -> None:
        it = ev.payload
        meta = self.item_meta.get(it["item_id"], {})
        self.order_fire[it["order_id"]] = min(self.order_fire.get(it["order_id"], math.inf), self.clock.t)
        if it.get("large_party") and not any(o == it["order_id"] for _, o in self.large_fires):
            self.large_fires.append((self.clock.t, it["order_id"]))
        p = self.floor.parties.get(meta.get("party_id") or "")
        if p and meta.get("course") == "main" and p.state == "ordered":
            p.state = "entrees_fired"
            self.floor.set_state(p, "entrees_fired")
            self._tables(p)

    def _on_item_done(self, ev: E.Event) -> None:
        it = ev.payload
        meta = self.item_meta.get(it["item_id"], {})
        p = self.floor.parties.get(meta.get("party_id") or "")
        if p is None or meta.get("channel") != "dine_in" or p.food_order != it["order_id"]:
            return
        self._maybe_food_done(p, it["order_id"])

    def _maybe_food_done(self, p: Party, order_id: str) -> None:
        """When nothing of a party's food order is held or still cooking, the party starts eating."""
        t = self.clock.t
        order = self.orders.get(order_id, {})
        ids = [i["item_id"] for i in order.get("items", [])]
        if p.food_done or order.get("held") or any(self.kitchen.items[i].status != "done" for i in ids if i in self.kitchen.items):
            return
        p.food_done = True
        mains = [self.kitchen.items[i] for i in ids if i in self.kitchen.items and self.item_meta.get(i, {}).get("course") == "main"]
        if mains:
            ticket = (max(m.done_at for m in mains) - min(m.fire_at for m in mains)) / 60
            self.metrics.ticket_min.append(round(ticket, 1))
            if ticket > LATE_TICKET_MIN:
                self.metrics.late_tickets += 1
        if p.state in ("ordered", "entrees_fired", "seated"):
            p.state = "eating"
            p.check_at = t + self.guests.minutes("eat") * 60
            if self.guests.chance(0.6):
                p.dessert_at = t + self.guests.minutes("dessert_after") * 60
            self.floor.set_state(p, "eating")
            self._tables(p)

    def after_item_change(self, order: dict) -> None:
        """An item was swapped or dropped after a manager decision: refresh the online order / the party's progress."""
        if order["channel"] == "online":
            self._online_register(order)
            return
        p = self.floor.parties.get(order.get("party_id") or "")
        if p is not None and p.food_order == order["order_id"]:
            self._maybe_food_done(p, order["order_id"])

    def manual_order(self, table_id: str, items: list[dict], notes: str = "", party_size: int | None = None,
                     guest_id: int | None = None) -> dict:
        """A person clicks a table on the floor and enters an order (seats a party first if the table is free)."""
        if table_id not in self.floor.tables:
            raise ServiceError(f"unknown table {table_id}")
        t = self.floor.tables[table_id]
        p = self.floor.party_of_table(table_id)
        if p is None:
            if t.state != "available":
                raise ServiceError(f"{table_id} is {t.state}")
            self._n_party += 1
            size = int(party_size or min(t.seats, max(1, len(items))))
            p = Party(f"P{self._n_party:03d}", size, "manual", self.clock.t, guest_id=guest_id)
            self.floor.parties[p.id] = p
            self.metrics.parties += 1
            self.metrics.covers_arrived += size
            self.floor.seat(p, [table_id], self.clock.t)
            self.metrics.covers_seated += size
            self._tables(p)
        kind = "food" if not p.food_order else "extra"
        if all(cat.DISHES_BY_ID[i["dish_id"]].category == "dessert" for i in items if i["dish_id"] in cat.DISHES_BY_ID):
            kind = "dessert" if p.food_order else "food"
        if guest_id and not p.guest_id:
            p.guest_id = guest_id
        return self.submit_order("dine_in", items, notes=notes, party=p, kind=kind, source="manual", guest_id=guest_id or p.guest_id)
