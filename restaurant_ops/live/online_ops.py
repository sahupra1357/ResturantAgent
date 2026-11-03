"""The online channel: simulated web orders, the online menu, quotes, registration, progress and the online mode. Mixed into `live.service.Service`."""
from __future__ import annotations

from datetime import datetime

from restaurant_ops.config import settings
from restaurant_ops.data import catalog as cat
from restaurant_ops.live import events as E
from restaurant_ops.live.kitchen import PACK_BUFFER_MIN
from restaurant_ops.live.online import NOTIFY_SLIP_MIN, PICKUP_AFTER_MIN, OnlineOrder, order_total
from restaurant_ops.live.payments import PaymentError
from restaurant_ops.live.world import DISH_VIEW, ServiceError


class OnlineOps:
    # ================================================================== online channel
    def _sim_online_order(self) -> None:
        allowed = self.online.allowed(self.available_map())
        if not allowed:
            self.online.lost += 1
            return
        cart = self.guests.online_cart(allowed, set(self.menu.specials))
        if cart:
            self._n_cust += 1
            delivery = self.guests.aux_chance(settings.DELIVERY_SHARE)
            zone = self.guests.choice(sorted(self.zones)) if delivery and self.zones else None
            self.submit_order("online", cart, customer=f"Guest {self._n_cust}", source="sim",
                              fulfilment="delivery" if delivery else "pickup", zone=zone)

    def online_menu(self) -> dict:
        avail = self.available_map()
        allowed = self.online.allowed(avail)
        rows = []
        for d in self.menu.on_menu():
            v = dict(DISH_VIEW[d])
            v["available"] = d in allowed
            v["eighty_sixed"] = d in self.menu.eighty_sixed
            v["last_n"] = self.menu.last_n.get(d)
            v["special"] = d in self.menu.specials
            v["max_qty"] = 1 if v["last_n"] else 6
            if d in self.menu.eighty_sixed:
                continue  # hidden online at once
            rows.append(v)
        return {"mode": self.online.mode, "reason": self.online.mode_reason, "dishes": rows,
                "zones": self.zones, "taxes": [{k: r[k] for k in ("name", "rate", "applies_to", "channel")} for r in self.tax_rules]}

    def _delivery_extra(self, zone: str, ready_t: float) -> float:
        z = self.dispatcher.zone(zone)
        return self.dispatcher.driver_wait_min(ready_t) + z["drive_min"]

    def quote_preview(self, dish_ids: list[str], fulfilment: str = "pickup", zone: str | None = None) -> dict:
        load = self.kitchen_load()
        q = self.kitchen.quote(dish_ids, self.clock.t, PACK_BUFFER_MIN)
        extra = 0.0
        if fulfilment == "delivery":
            extra = self._delivery_extra(zone or next(iter(self.zones)), self.clock.t + q["p50_min"] * 60)
        p50, p90 = q["p50_min"] + extra, q["p90_min"] + extra * 1.15
        fee = self.dispatcher.zone(zone or next(iter(self.zones)))["fee"] if fulfilment == "delivery" else 0.0
        return {**q, "p50_min": round(p50, 1), "p90_min": round(p90, 1), "kitchen_p50_min": q["p50_min"], "delivery_min": round(extra, 1),
                "range_hhmm": [self.clock.hhmm(self.clock.t + p50 * 60), self.clock.hhmm(self.clock.t + p90 * 60)],
                "busyness": load["busyness"], "explanation": self.order.explain(load), "mode": self.online.mode,
                "fulfilment": fulfilment, "zone": zone, "fee": fee}

    def place_online(self, items: list[dict], customer: str = "Web guest", fulfilment: str = "pickup", zone: str | None = None,
                     phone: str = "") -> dict:
        allowed = self.online.allowed(self.available_map())
        if self.online.mode == "paused":
            raise ServiceError("online ordering is paused: the kitchen is slammed")
        if fulfilment not in ("pickup", "delivery"):
            raise ServiceError("fulfilment must be pickup or delivery")
        if fulfilment == "delivery":
            if not zone:
                raise ServiceError("delivery needs a zone")
            mino = self.zones.get(zone, {}).get("min_order", 0) or 0
            sub = sum(cat.DISHES_BY_ID[i["dish_id"]].price for i in items if i["dish_id"] in cat.DISHES_BY_ID)
            if sub < mino:
                raise ServiceError(f"zone {zone} needs a minimum order of ${mino:.2f}")
            try:
                self.dispatcher.zone(zone)
            except PaymentError as e:
                raise ServiceError(str(e)) from e
        bad = [i["dish_id"] for i in items if i["dish_id"] not in allowed]
        if bad:
            raise ServiceError(f"not available online right now: {', '.join(cat.DISHES_BY_ID[b].name for b in bad if b in cat.DISHES_BY_ID)}")
        guest_id = None
        if phone:
            g = self.crm.by_phone(phone) or self.crm.upsert(customer, phone=phone)
            guest_id = g["guest_id"]
        return self.submit_order("online", items, customer=customer, source="web", guest_id=guest_id, fulfilment=fulfilment,
                                 zone=zone if fulfilment == "delivery" else None)

    def _online_register(self, o: dict) -> None:
        t = self.clock.t
        if o["order_id"] in self.online.orders:
            oo = self.online.orders[o["order_id"]]
            oo.item_ids = [i["item_id"] for i in o["items"]]
            oo.dishes = [i["dish_id"] for i in o["items"]]
            oo.total = round(order_total(oo.dishes) + oo.fee, 2)
            return
        items = [self.kitchen.items[i["item_id"]] for i in o["items"]]
        if not items:
            return
        est = self.kitchen.estimate_items(items, t, PACK_BUFFER_MIN)
        load = self.kitchen_load()
        fulfil = o.get("fulfilment") or "pickup"
        zone = o.get("zone")
        extra = self._delivery_extra(zone, t + est["p50_min"] * 60) if fulfil == "delivery" else 0.0
        fee = self.dispatcher.zone(zone)["fee"] if fulfil == "delivery" else 0.0
        p50, p90 = t + (est["p50_min"] + extra) * 60, t + (est["p90_min"] + extra * 1.15) * 60
        dishes = [i.dish_id for i in items]
        oo = OnlineOrder(o["order_id"], self._customer_of(o), [i.item_id for i in items], dishes,
                         round(order_total(dishes) + fee, 2), t, p50, p90, p50, p90, reason=self.order.explain(load),
                         source=self._source_of(o), fulfilment=fulfil, zone=zone, fee=fee, guest_id=o.get("guest_id"),
                         extra_min=extra)
        oo.check_id = self.payments.open_online(o, fee, o.get("guest_id")).check_id
        self.online.orders[oo.order_id] = oo
        self.metrics.online_orders += 1
        self.conn.execute("INSERT OR REPLACE INTO quote_log (service_id, order_id, placed_sim_ts, quoted_p50, quoted_p90) VALUES (?,?,?,?,?)",
                          (self.service_id, oo.order_id, self.clock.iso(t), self.clock.iso(oo.quote_p50_t), self.clock.iso(oo.quote_p90_t)))
        self.bus.publish(E.ONLINE_ORDER, self.online.view(oo, self.clock), source="online")

    @staticmethod
    def _customer_of(o: dict) -> str:
        return o.get("customer") or "Guest"

    @staticmethod
    def _source_of(o: dict) -> str:
        return o.get("source") or "sim"

    def _quote_closed(self, o, promised_t: float) -> None:
        within = (o.quote_p50_t - 5 * 60) <= promised_t <= (o.quote_p90_t + 30)
        err = (promised_t - o.quote_p50_t) / 60
        self.metrics.quotes.append({"order_id": o.order_id, "within": within, "error_min": round(err, 1), "fulfilment": o.fulfilment})
        self.conn.execute("UPDATE quote_log SET actual_ready=?, within_range=?, error_min=? WHERE service_id=? AND order_id=?",
                          (self.clock.iso(promised_t), int(within), round(err, 2), self.service_id, o.order_id))

    def _online_progress(self, t: float) -> None:
        for o in self.online.active():
            if o.status == "cooking":
                its = [self.kitchen.items[i] for i in o.item_ids if i in self.kitchen.items]
                if its and all(i.status == "done" for i in its) and not self.orders.get(o.order_id, {}).get("held"):
                    ready = max(i.done_at for i in its) + PACK_BUFFER_MIN * 60
                    if t >= ready:
                        o.ready_t = ready
                        if o.fulfilment == "delivery":
                            a = self.dispatcher.assign(o.order_id, ready, o.zone)
                            o.status, o.driver_id, o.depart_t, o.delivered_t = "out_for_delivery", a["driver_id"], a["depart_t"], a["arrive_t"]
                            o.notifications.append({"at": self.clock.hhmm(t), "text": f"Out for delivery with {a['driver_id']}, "
                                                    f"arriving about {self.clock.hhmm(a['arrive_t'])}"})
                        else:
                            o.status = "ready"
                            self._quote_closed(o, ready)
                            o.notifications.append({"at": self.clock.hhmm(t), "text": "Your order is ready for pickup"})
                        self.bus.publish(E.ONLINE_ORDER, self.online.view(o, self.clock), source="online")
            elif o.status == "out_for_delivery" and o.delivered_t is not None and t >= o.delivered_t:
                o.status = "delivered"
                self._quote_closed(o, o.delivered_t)
                o.notifications.append({"at": self.clock.hhmm(o.delivered_t), "text": "Delivered. Enjoy!"})
                self.bus.publish(E.ONLINE_ORDER, self.online.view(o, self.clock), source="online")
            elif o.status == "ready" and o.ready_t is not None and t >= o.ready_t + PICKUP_AFTER_MIN * 60:
                o.status = "picked_up"
                self.bus.publish(E.ONLINE_ORDER, self.online.view(o, self.clock), source="online")

    def _refresh_quotes(self, load: dict) -> None:
        t = self.clock.t
        for o in self.online.active():
            if o.status != "cooking":
                continue
            its = [self.kitchen.items[i] for i in o.item_ids if i in self.kitchen.items]
            est = self.kitchen.estimate_items(its, t, PACK_BUFFER_MIN)
            extra = self._delivery_extra(o.zone, t + est["p50_min"] * 60) if o.fulfilment == "delivery" else 0.0
            o.cur_p50_t, o.cur_p90_t = t + (est["p50_min"] + extra) * 60, t + (est["p90_min"] + extra * 1.15) * 60
            notify = False
            ref = o.notified_p90_t or o.quote_p90_t
            if o.cur_p90_t > ref + NOTIFY_SLIP_MIN * 60 and o.override_t is None:
                o.reason = self.order.explain(load)
                o.notified_p90_t = o.cur_p90_t
                o.notifications.append({"at": self.clock.hhmm(t), "text": f"Running late: now ready "
                                        f"{self.clock.hhmm(o.cur_p50_t)}-{self.clock.hhmm(o.cur_p90_t)} ({o.reason})"})
                notify = True
            view = self.online.view(o, self.clock)
            sig = (tuple(view["current_hhmm"]), view["late"])
            if notify or self._last_quote_view.get(o.order_id) != sig:
                self._last_quote_view[o.order_id] = sig
                self.bus.publish(E.QUOTE_UPDATED, {**view, "notify": notify}, source="order")

    def override_quote(self, order_id: str, ready_hhmm: str) -> dict:
        o = self.online.orders[order_id]
        hh, mm = (int(x) for x in ready_hhmm.split(":"))
        target = datetime.combine(self.date, datetime.min.time()).replace(hour=hh, minute=mm)
        o.override_t = (target - self.clock.start).total_seconds()
        o.notifications.append({"at": self.clock.hhmm(), "text": f"Manager updated your ready time to {ready_hhmm}"})
        v = self.online.view(o, self.clock)
        self.bus.publish(E.QUOTE_UPDATED, {**v, "notify": True, "override": True}, source="manager")
        return v

    def notify_customer(self, order_id: str, text: str = "") -> dict:
        o = self.online.orders[order_id]
        o.notifications.append({"at": self.clock.hhmm(), "text": text or f"Update: ready {self.online.view(o, self.clock)['current_hhmm'][0]}"})
        v = self.online.view(o, self.clock)
        self.bus.publish(E.QUOTE_UPDATED, {**v, "notify": True}, source="manager")
        return v

    def set_online_mode(self, mode: str, reason: str = "manager") -> dict:
        if mode not in ("open", "quick_only", "paused"):
            raise ServiceError("mode must be open, quick_only or paused")
        self.online.mode, self.online.mode_reason = mode, reason
        self.bus.publish(E.ONLINE_MODE, {"mode": mode, "reason": reason}, source="manager" if reason == "manager" else "order")
        return {"mode": mode}
