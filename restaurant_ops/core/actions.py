"""Database effects of executed actions, shared by the batch day planner (agents/supervisor.py) and the live service
(live/handlers.py). One implementation per action type, so the two runtimes cannot drift apart.

Every function writes through the tool registry's connection (`R.connection()`), which each runtime binds to its own
database, and is called only after `core/constraints.check_action` passed. Runtime-specific side effects (stock in
memory, bus events, the prep cook's queue) stay with the caller.

`ACTION_TYPES` is the single list of action types; constraints, both runtimes and the tests check against it.
"""
from __future__ import annotations

import json

from restaurant_ops.tools import registry as R

# every action type the constraint checker knows; each runtime must execute or explicitly record all of them
ACTION_TYPES = (
    "place_po", "reschedule_po", "substitute_supplier", "add_special", "86_dish", "substitute_ingredient", "prep_list",
    "discard_lot", "discard_lots", "adjust_forecast", "flag_variance", "throttle_online", "allergen_swap", "comp",
    "add_staff", "release_staff", "prep_batch", "donate_lots", "discount", "refund", "price_change",
)
# executed by the payments module directly (constraint-checked, never proposed through the coordinator)
DIRECT_ONLY = ("discount", "refund")
# kept as a record in `decisions`, which is what consumes them (the forecaster reads adjust_forecast rows)
RECORD_ONLY = ("adjust_forecast", "flag_variance")
# actions of the live floor only (no batch-planning meaning)
LIVE_ONLY = ("throttle_online", "allergen_swap", "comp", "add_staff", "release_staff", "prep_batch", "donate_lots",
             "discard_lots", "price_change")


def place_po(p: dict, decision_id: int | None = None) -> dict:
    return R.send_po(supplier_id=p["supplier_id"], delivery_date=p["delivery_date"], lines=p["lines"], decision_id=decision_id)


def substitute_supplier(p: dict, decision_id: int | None = None) -> dict:
    """Order from the other supplier and, when asked, cancel the original PO it replaces."""
    res = place_po(p, decision_id)
    if p.get("po_id") and p.get("cancel_original"):
        with R.connection() as c:
            c.execute("UPDATE purchase_orders SET status='cancelled' WHERE po_id=? AND status IN ('sent','pending_approval','draft')",
                      (p["po_id"],))
            c.commit()
        res["cancelled"] = p["po_id"]
    return res


def reschedule_po(p: dict) -> dict:
    with R.connection() as c:
        n = c.execute("UPDATE purchase_orders SET expected_delivery=? WHERE po_id=? AND status IN ('sent','pending_approval','draft')",
                      (p["new_delivery_date"], p["po_id"])).rowcount
        c.commit()
    if not n:
        raise ValueError(f"PO {p['po_id']} is not open")
    R.clear_cache()
    return {"po_id": p["po_id"], "expected_delivery": p["new_delivery_date"]}


def set_menu(day: str, dish_id: str, status: str, note: str) -> dict:
    with R.connection() as c:
        c.execute("INSERT OR REPLACE INTO menu_state (date, dish_id, status, note) VALUES (?,?,?,?)", (day, dish_id, status, note[:300]))
        c.commit()
    return {"date": day, "dish_id": dish_id, "status": status}


def discard_lot(p: dict, today: str) -> dict:
    with R.connection() as c:
        lot = c.execute("SELECT * FROM lots WHERE lot_id=?", (p["lot_id"],)).fetchone()
        if lot is None:
            raise ValueError(f"unknown lot {p['lot_id']}")
        qty = float(p.get("qty") or lot["qty"])
        full = qty >= float(lot["qty"]) - 1e-9
        c.execute("INSERT INTO waste_log (date, lot_id, ingredient_id, qty, value, reason) VALUES (?,?,?,?,?,?)",
                  (today, lot["lot_id"], lot["ingredient_id"], qty, round(qty * float(lot["unit_cost"]), 2), p.get("reason", "discarded")))
        if full:
            c.execute("UPDATE lots SET status='discarded' WHERE lot_id=?", (lot["lot_id"],))
        else:
            c.execute("UPDATE lots SET qty=qty-? WHERE lot_id=?", (qty, lot["lot_id"]))
        c.commit()
    return {"lot_id": p["lot_id"], "discarded_qty": qty}


def substitute_ingredient(p: dict) -> dict:
    """Swap one recipe line of a dish (same quantity). In the live service this is the service's own DB copy, so the
    swap lasts for tonight; the allergen checks read recipe_lines and therefore see it at once."""
    with R.connection() as c:
        n = c.execute("UPDATE recipe_lines SET ingredient_id=? WHERE dish_id=? AND ingredient_id=?",
                      (p["to_ingredient"], p["dish_id"], p["from_ingredient"])).rowcount
        c.commit()
    if not n:
        raise ValueError(f"{p['from_ingredient']} is not in {p['dish_id']}")
    R.clear_cache()
    return {"dish_id": p["dish_id"], "from": p["from_ingredient"], "to": p["to_ingredient"],
            "allergens_declared": p.get("allergens_declared") or []}


def record(p: dict) -> dict:
    """RECORD_ONLY actions: the decisions row is the effect (consumers read it from there)."""
    return {"recorded": True, **({"factor": p["factor"]} if "factor" in p else {})}


def copy_decision_to(conn, decision: dict, service_date: str, run_id: str) -> None:
    """Persist an executed decision into another database (the master), for effects meant to outlive a live service,
    e.g. `adjust_forecast` for tomorrow: each live service runs on its own DB copy, so without this the next service
    (copied from the master) would never see it."""
    from datetime import datetime, timezone

    conn.execute("INSERT INTO decisions (run_id, sim_date, ts, agent, action_type, payload, rationale, status, violations) "
                 "VALUES (?,?,?,?,?,?,?, 'executed', '[]')",
                 (run_id, service_date, datetime.now(timezone.utc).isoformat(timespec="seconds"), decision["agent"],
                  decision["action_type"], json.dumps(decision["payload"], default=str), (decision.get("reason") or "")[:1000]))
    conn.commit()
