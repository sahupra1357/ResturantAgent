"""Live executors: what each action type does to the running service once the coordinator has checked (and, where
needed, a manager approved) it. Database effects shared with the batch planner are in `core/actions.py`; this module
adds the live side (stock in memory, menu state, the prep cook, bus events).

Every type in `core.actions.ACTION_TYPES` except the payment-only ones (`DIRECT_ONLY`) has an executor here, and the
coordinator blocks any type without one at the check step instead of recording it as done
(tests/test_gaps.py::test_every_action_type_has_a_live_executor).
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Callable

from restaurant_ops.config import settings
from restaurant_ops.core import actions as ACT
from restaurant_ops.live import events as E
from restaurant_ops.live.po_channel import send_po as po_send
from restaurant_ops.live.safety import record_donation

if TYPE_CHECKING:  # pragma: no cover
    from restaurant_ops.live.service import Service

PREP_PLAN_KEY = "prep_plan:{date}:{period}"


def register(svc: "Service") -> None:
    h: dict[str, Callable[[dict], dict]] = svc.coordinator.handlers
    h["place_po"] = lambda d: purchase_order(svc, d, ACT.place_po)
    h["substitute_supplier"] = lambda d: purchase_order(svc, d, ACT.substitute_supplier)
    h["reschedule_po"] = lambda d: reschedule_po(svc, d)
    h["add_special"] = lambda d: special(svc, d)
    h["86_dish"] = lambda d: eighty_six(svc, d)
    h["substitute_ingredient"] = lambda d: substitute_ingredient(svc, d)
    h["prep_list"] = lambda d: prep_list(svc, d)
    h["discard_lots"] = lambda d: discard(svc, d)
    h["discard_lot"] = lambda d: discard(svc, {**d, "payload": {"lots": [d["payload"]]}})
    h["adjust_forecast"] = lambda d: adjust_forecast(svc, d)
    h["flag_variance"] = lambda d: ACT.record(d["payload"])
    h["throttle_online"] = lambda d: svc.set_online_mode(d["payload"]["mode"], d["payload"].get("reason") or d["title"])
    h["allergen_swap"] = lambda d: svc.order.apply_swap(d["payload"], approved=True)
    h["comp"] = lambda d: comp(svc, d)
    h["add_staff"] = lambda d: {"called_in": d["payload"]["station"], "arrives_in_min": settings.CALL_IN_DELAY_MIN}
    h["release_staff"] = lambda d: svc.prep.release(d["payload"])
    h["prep_batch"] = lambda d: svc.prep.run_batch(d["payload"])
    h["donate_lots"] = lambda d: donate(svc, d)
    h["price_change"] = lambda d: price(svc, d)


# =========================================================================== purchasing


def purchase_order(svc: "Service", d: dict, place: Callable) -> dict:
    svc.flush()
    res = place(d["payload"], d["id"])
    try:  # PDF + e-mail to the supplier (or the outbox); a mail problem never undoes the PO
        res["outbox"] = po_send(svc.conn, res["po_id"], svc.profile)["status"]
    except Exception as e:  # noqa: BLE001
        res["outbox"] = f"failed: {type(e).__name__}"
    svc.bus.publish(E.PO_SENT, {**res, "title": d["title"]}, source="procurement")
    return res


def reschedule_po(svc: "Service", d: dict) -> dict:
    svc.flush()
    res = ACT.reschedule_po(d["payload"])
    svc.inventory_dirty = True  # next deliveries (and conflict messages) read the new date
    svc.bus.publish(E.AGENT_LOG, {"agent": "procurement", "message": f"PO {res['po_id']} now due {res['expected_delivery']}"},
                    source="procurement")
    return res


# =========================================================================== menu


def special(svc: "Service", d: dict) -> dict:
    p = d["payload"]
    svc.menu.specials[p["dish_id"]] = {"portions": int(p["portions"]), "note": d["title"]}
    svc.conn.execute("INSERT OR REPLACE INTO menu_state (date, dish_id, status, note) VALUES (?,?,?,?)",
                     (svc.date.isoformat(), p["dish_id"], "special", d["title"][:300]))
    svc.bus.publish(E.MENU_UPDATED, svc.menu.snapshot(), source="menu")
    return {"special": p["dish_id"], "portions": p["portions"]}


def eighty_six(svc: "Service", d: dict) -> dict:
    p = d["payload"]
    dish = p["dish_id"]
    after = int(p.get("after_portions") or 0)
    if after > 0 and svc.available_portions(dish) > after:
        svc.menu.limits[dish] = after
        svc.inventory_dirty = True
        svc.bus.publish(E.MENU_UPDATED, svc.menu.snapshot(), source="menu")
        return {"dish_id": dish, "limit": after}
    svc._eighty_six(dish, d["title"])
    return {"dish_id": dish, "eighty_sixed": True}


def substitute_ingredient(svc: "Service", d: dict) -> dict:
    """Tonight's recipe of the dish uses the substitute: the service DB (which the allergen checks read), the stock's
    bill of materials (depletion and portions) and the catalog view all change together."""
    from restaurant_ops.live.world import apply_config

    svc.flush()
    p = d["payload"]
    res = ACT.substitute_ingredient(p)
    bom = svc.stock.bom[p["dish_id"]]
    bom[p["to_ingredient"]] = bom.pop(p["from_ingredient"])
    svc.stock.uses[p["from_ingredient"]] = [x for x in svc.stock.uses.get(p["from_ingredient"], []) if x != p["dish_id"]]
    svc.stock.uses[p["to_ingredient"]] = sorted(set(svc.stock.uses.get(p["to_ingredient"], [])) | {p["dish_id"]})
    apply_config(svc.conn)  # catalog recipes + allergens shown on the menu and online page
    svc.inventory_dirty = True
    svc.bus.publish(E.MENU_UPDATED, svc.menu.snapshot(), source="menu")
    return {**res, "scope": "tonight"}


# =========================================================================== prep


def prep_list(svc: "Service", d: dict) -> dict:
    """Tonight's list resets the pars (more than prepped -> a batch for the prep cook). A list for another service is
    saved in the master DB and becomes that service's starting pars."""
    p = d["payload"]
    items = [{"dish_id": i["dish_id"], "station": i.get("station"), "portions": int(i["portions"])} for i in p["items"]]
    if p["date"] == svc.date.isoformat() and p["meal_period"] == svc.period:
        return {"applied": "tonight", **svc.prep.apply_prep_list(items)}
    key = PREP_PLAN_KEY.format(date=p["date"], period=p["meal_period"])
    with svc.master() as m:
        m.execute("INSERT OR REPLACE INTO config_kv (key, value) VALUES (?,?)", (key, json.dumps(items)))
    return {"applied": "saved for that service", "key": key, "dishes": len(items)}


def saved_prep_plan(svc: "Service") -> list[dict] | None:
    key = PREP_PLAN_KEY.format(date=svc.date.isoformat(), period=svc.period)
    with svc.master() as m:
        r = m.execute("SELECT value FROM config_kv WHERE key=?", (key,)).fetchone()
    return json.loads(r[0]) if r else None


# =========================================================================== forecast


def adjust_forecast(svc: "Service", d: dict) -> dict:
    """The decision row in the service DB is what the forecaster reads; tomorrow's service is copied from the master,
    so the adjustment is written there as well, or it would be lost overnight."""
    p = d["payload"]
    with svc.master() as m:
        # the latest live adjustment for a date and period replaces earlier ones (they would multiply otherwise)
        m.execute("DELETE FROM decisions WHERE action_type='adjust_forecast' AND run_id LIKE 'svc-%' "
                  "AND json_extract(payload, '$.date')=? AND json_extract(payload, '$.meal_period')=?", (p["date"], p["meal_period"]))
        ACT.copy_decision_to(m, d, svc.date.isoformat(), run_id=svc.service_id)
    return {**ACT.record(p), "saved_to": "master"}


# =========================================================================== stock


def discard(svc: "Service", d: dict) -> dict:
    svc.flush()
    total, ids = 0.0, []
    for lot in d["payload"]["lots"]:
        lp = lot if isinstance(lot, dict) else {"lot_id": lot}
        row = svc.conn.execute("SELECT qty, unit_cost, ingredient_id, status FROM lots WHERE lot_id=?", (lp["lot_id"],)).fetchone()
        if row is None or row["status"] != "active":
            continue
        ACT.discard_lot({"reason": "spoilage", **lp}, svc.date.isoformat())
        total += float(row["qty"]) * float(row["unit_cost"])
        ids.append(lp["lot_id"])
    ings = sorted({r[0] for r in svc.conn.execute(
        f"SELECT ingredient_id FROM lots WHERE lot_id IN ({','.join('?' * len(ids))})", ids)}) if ids else []
    svc.stock.quarantine.difference_update(ids)
    svc.stock.reload(ings)
    svc.metrics.waste_value += total
    svc.inventory_dirty = True
    return {"discarded": len(ids), "value": round(total, 2)}


def donate(svc: "Service", d: dict) -> dict:
    svc.flush()
    p = d["payload"]
    res = record_donation(svc.conn, svc.date, p["lots"], p["partner"])
    svc.stock.reload(p.get("ingredients") or None)
    svc.inventory_dirty = True
    return res


# =========================================================================== money


def price(svc: "Service", d: dict) -> dict:
    """Price changes take effect from the next service (tonight's checks keep tonight's prices)."""
    p = d["payload"]
    svc.conn.execute("UPDATE dishes SET price=? WHERE id=?", (float(p["new_price"]), p["dish_id"]))
    return {"dish_id": p["dish_id"], "new_price": p["new_price"], "effective": "next service"}


def comp(svc: "Service", d: dict) -> dict:
    p = d["payload"]
    svc.metrics.comps += float(p["amount"])
    o = svc.online.orders.get(p["order_id"])
    if o and o.check_id:
        svc.payments.comp(o.check_id, float(p["amount"]), p.get("reason", "late order"))
    if o:
        o.notifications.append({"at": svc.clock.hhmm(), "text": f"Sorry for the wait: ${p['amount']:.2f} credited"})
        svc.bus.publish(E.QUOTE_UPDATED, {**svc.online.view(o, svc.clock), "notify": True}, source="order")
    return {"comp": p["amount"]}
