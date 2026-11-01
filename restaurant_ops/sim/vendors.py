"""Mock vendor agents: rule-based, seeded counter-offers and supplier notices. No LLM imports here.

Each supplier has a persona (max discount, the order value at which that discount is fully unlocked,
how much of a requested quantity it usually has in stock). `negotiate` is deterministic: the small
"mood" jitter is derived from settings.SEED + supplier + ingredient + quantity, never from a shared stream.
An active supplier shortfall (table `supply_overrides`) caps `qty_available`.
"""
from __future__ import annotations

import sqlite3
import zlib
from contextlib import contextmanager
from typing import Iterator

from restaurant_ops.config import settings
from restaurant_ops.data import catalog as cat

# persona: max_discount (fraction of list), volume_threshold ($ line value that unlocks the full discount),
# availability (usual share of a requested quantity in stock), style (flavour for messages)
PERSONAS: dict[str, dict] = {
    "sup_freshcatch": {"max_discount": 0.06, "volume_threshold": 600.0, "availability": 0.98, "style": "premium seafood house, firm on price"},
    "sup_metro": {"max_discount": 0.08, "volume_threshold": 800.0, "availability": 0.99, "style": "big wholesaler, volume-driven"},
    "sup_greenvalley": {"max_discount": 0.05, "volume_threshold": 300.0, "availability": 0.95, "style": "local farm, friendly, limited stock"},
    "sup_budget": {"max_discount": 0.03, "volume_threshold": 1200.0, "availability": 0.92, "style": "discounter, already cheap, little room"},
}
DEFAULT_PERSONA = {"max_discount": 0.03, "volume_threshold": 1000.0, "availability": 0.95, "style": "generic vendor"}
MIN_DISCOUNT = 0.005  # below this the vendor just holds its list price


def _mood(*keys) -> float:
    """Deterministic value in [0, 1) from the global seed and the given keys."""
    return (zlib.crc32(f"{settings.SEED}|" .encode() + "|".join(str(k) for k in keys).encode()) % 1000) / 1000.0


@contextmanager
def _open(conn: sqlite3.Connection | None) -> Iterator[sqlite3.Connection | None]:
    """Yield `conn`, else the tools' database connection, else None (catalog fallback)."""
    if conn is not None:
        yield conn
        return
    try:
        from restaurant_ops.tools import registry as R

        cm = R.connection()
        c = cm.__enter__()
    except Exception:  # noqa: BLE001
        yield None
        return
    try:
        yield c
    finally:
        cm.__exit__(None, None, None)


def _list_price(c: sqlite3.Connection | None, supplier_id: str, ingredient_id: str) -> float | None:
    if c is not None:
        r = c.execute("SELECT price FROM supplier_items WHERE supplier_id=? AND ingredient_id=?",
                      (supplier_id, ingredient_id)).fetchone()
        return None if r is None else float(r[0])
    for si in cat.SUPPLIER_ITEMS:
        if si.supplier_id == supplier_id and si.ingredient_id == ingredient_id:
            return si.price
    return None


def active_override(c: sqlite3.Connection | None, supplier_id: str, ingredient_id: str | None,
                    as_of: str | None = None) -> dict | None:
    """Most recent supply override for the supplier/ingredient that has not ended by `as_of` (default: sim date)."""
    if c is None:
        return None
    if as_of is None:
        try:
            from restaurant_ops.tools import registry as R

            as_of = R.get_sim_date()
        except Exception:  # noqa: BLE001
            as_of = settings.SIM_START.isoformat()
    try:
        r = c.execute("SELECT * FROM supply_overrides WHERE supplier_id=? AND (ingredient_id IS NULL OR ingredient_id=?) "
                      "AND end_date>=? ORDER BY id DESC LIMIT 1", (supplier_id, ingredient_id, as_of)).fetchone()
    except sqlite3.Error:
        return None
    return None if r is None else dict(r)


def negotiate(supplier_id: str, ingredient_id: str, qty: float, target_price: float,
              conn: sqlite3.Connection | None = None) -> dict:
    """Counter-offer from a mock vendor.

    Returns {supplier_id, ingredient_id, list_price, counter_price, accepted, qty_available, message}.
    counter_price is numeric and below list_price whenever a discount is given; accepted=True means the vendor
    agrees to `target_price` itself. Allowed discount = max_discount x (0.25 + 0.75 x min(1, line value / threshold))
    x a small seeded mood factor. qty_available is the usual stock share, capped by an active shortfall.
    """
    p = PERSONAS.get(supplier_id, DEFAULT_PERSONA)
    qty, target = max(0.0, float(qty)), float(target_price)
    with _open(conn) as c:
        lp = _list_price(c, supplier_id, ingredient_id)
        ov = active_override(c, supplier_id, ingredient_id)
    if lp is None:
        return {"supplier_id": supplier_id, "ingredient_id": ingredient_id, "list_price": None, "counter_price": None,
                "accepted": False, "qty_available": 0.0, "message": f"{supplier_id} does not carry {ingredient_id}."}
    avail_share = float(p["availability"])
    note = ""
    if ov is not None:
        avail_share = min(avail_share, float(ov["fill_rate"]))
        note = f" Heads up: we are short on {ingredient_id} right now and can only cover about {avail_share:.0%} of that quantity."
    qty_available = round(qty * avail_share, 3)
    line_value = qty * lp
    scale = 0.25 + 0.75 * min(1.0, line_value / p["volume_threshold"])
    mood = 0.9 + 0.1 * _mood(supplier_id, ingredient_id, round(qty, 1))
    allowed = p["max_discount"] * scale * mood
    ask = 1.0 - target / lp if lp > 0 else 0.0
    if allowed < MIN_DISCOUNT:
        counter, accepted = lp, False
        msg = f"List price {lp:.2f} is our best for this volume."
    elif ask <= 0:
        counter, accepted = lp, True
        msg = f"Happy to keep {lp:.2f}, no discount needed."
    elif ask <= allowed:
        counter, accepted = round(target, 3), True
        msg = f"Deal at {target:.2f} ({ask:.1%} off list) for {qty:g}."
    else:
        counter, accepted = round(lp * (1.0 - allowed), 3), False
        msg = f"We can't do {target:.2f}; best offer is {counter:.2f} ({allowed:.1%} off list) for {qty:g}."
    return {"supplier_id": supplier_id, "ingredient_id": ingredient_id, "list_price": round(lp, 3),
            "counter_price": round(counter, 3), "accepted": accepted, "qty_available": qty_available,
            "message": msg + note}


def vendor_message(supplier_id: str, kind: str = "shortfall", ingredient_id: str | None = None,
                   fill_rate: float | None = None, delivery_date: str | None = None) -> str:
    """Text of a supplier notice in the vendor's voice (kind: 'shortfall' | 'delay')."""
    name = cat.SUPPLIERS_BY_ID[supplier_id].name if supplier_id in cat.SUPPLIERS_BY_ID else supplier_id
    item = ingredient_id.replace("_", " ") if ingredient_id else "several items"
    when = f" for {delivery_date}" if delivery_date else ""
    if kind == "delay":
        return f"{name}: our truck is running a day late{when}. Your {item} will arrive tomorrow instead."
    f = 0.3 if fill_rate is None else float(fill_rate)
    return (f"{name}: apologies, supply of {item} is tight{when}. We can only fill about {f:.0%} of your order; "
            f"please arrange cover for the rest elsewhere.")
