"""Unit tests for the deterministic constraint checker (temp DB: schema + catalog + a few lots; no models)."""
from __future__ import annotations

from datetime import date

import pytest

from restaurant_ops.config import settings
from restaurant_ops.core.constraints import Violation, blocking, check_action, needs_approval
from restaurant_ops.data.catalog import load_catalog
from restaurant_ops.db.connection import get_conn, init_db

AS_OF = date(2026, 11, 2)

# (lot_id, ingredient_id, qty, received, expiry, supplier, unit_cost, status)
LOTS = [
    ("LOT-A", "salmon", 8.0, "2026-10-31", "2026-11-03", "sup_freshcatch", 24.0, "active"),
    ("LOT-B", "salmon", 12.0, "2026-11-01", "2026-11-06", "sup_freshcatch", 24.0, "active"),
    ("LOT-POT", "potato", 30.0, "2026-10-28", "2026-11-27", "sup_metro", 1.2, "active"),
    ("LOT-SPI", "spinach", 10.0, "2026-11-01", "2026-11-05", "sup_greenvalley", 6.0, "active"),
    ("LOT-BUT", "butter", 5.0, "2026-10-25", "2026-12-09", "sup_metro", 9.0, "active"),
    ("LOT-LEM", "lemon", 5.0, "2026-10-30", "2026-11-20", "sup_metro", 3.0, "active"),
    ("LOT-CRM", "cream", 5.0, "2026-11-01", "2026-11-11", "sup_metro", 5.5, "active"),
    ("LOT-FLR", "flour", 20.0, "2026-10-01", "2027-03-30", "sup_metro", 0.9, "active"),
]


@pytest.fixture()
def conn(tmp_path):
    c = get_conn(tmp_path / "test.db")
    init_db(c)
    load_catalog(c)
    c.executemany("INSERT INTO lots VALUES (?,?,?,?,?,?,?,?)", LOTS)
    c.execute("INSERT INTO purchase_orders VALUES ('PO-1','sup_metro','2026-11-01','2026-11-03','sent',100.0,NULL)")
    c.execute("INSERT INTO po_lines VALUES ('PO-1','flour',20,0.9,NULL)")
    c.commit()
    yield c
    c.close()


def act(action_type: str, **payload) -> dict:
    return {"action_type": action_type, "payload": payload, "rationale": "test", "agent": "test"}


def codes(vs: list[Violation], severity: str | None = None) -> set[str]:
    return {v.code for v in vs if severity in (None, v.severity)}


def run(conn, action: dict) -> list[Violation]:
    return check_action(action, conn, AS_OF)


# --------------------------------------------------------------------------- place_po


def good_po(**over) -> dict:
    p = {"supplier_id": "sup_freshcatch", "delivery_date": "2026-11-03",
         "lines": [{"ingredient_id": "salmon", "qty": 10.0, "unit_price": 24.0}]}
    p.update(over)
    return act("place_po", **p)


def test_place_po_clean(conn):
    assert blocking(run(conn, good_po())) == []


def test_place_po_lead_time(conn):
    assert "lead_time" in codes(run(conn, good_po(delivery_date="2026-11-02")), "block")


def test_place_po_min_order_value(conn):
    vs = run(conn, good_po(lines=[{"ingredient_id": "salmon", "qty": 2.0, "unit_price": 24.0}]))
    assert "min_order_value" in codes(vs, "block")


def test_place_po_supplier_does_not_carry_item(conn):
    vs = run(conn, good_po(lines=[{"ingredient_id": "ribeye", "qty": 10.0, "unit_price": 38.0}]))
    assert "supplier_carries_item" in codes(vs, "block")


def test_place_po_positive_quantities(conn):
    vs = run(conn, good_po(lines=[{"ingredient_id": "salmon", "qty": -2.0, "unit_price": 24.0}]))
    assert "positive_quantities" in codes(vs, "block")


def test_place_po_daily_budget(conn):
    big = [{"ingredient_id": "salmon", "qty": 400.0, "unit_price": 24.0}]  # 9,600 > 2 x 4,000
    assert "budget_daily" in codes(run(conn, good_po(lines=big)), "block")


def test_place_po_pack_size_is_only_a_warning(conn):
    vs = run(conn, good_po(lines=[{"ingredient_id": "salmon", "qty": 9.0, "unit_price": 24.0}]))
    assert "pack_size" in codes(vs, "warn") and blocking(vs) == []


def test_place_po_shelf_life_uses_recent_sales(conn):
    # salmon sells 2 kg/day for 28 days -> 3 days of shelf life can use ~7.5 kg; 40 kg on top of stock is too much
    conn.execute("INSERT OR IGNORE INTO pos_sales VALUES ('2026-10-20','dinner','seared_salmon',11)")
    conn.commit()
    big = good_po(lines=[{"ingredient_id": "salmon", "qty": 60.0, "unit_price": 24.0}])
    assert "shelf_life" in codes(run(conn, big), "block")


def test_missing_fields_and_unknown_supplier(conn):
    assert "missing_field" in codes(run(conn, act("place_po", supplier_id="sup_freshcatch")))
    assert "unknown_supplier" in codes(run(conn, good_po(supplier_id="sup_nope")))


# --------------------------------------------------------------------------- reschedule / substitute supplier


def test_reschedule_po_lead_time(conn):
    assert "lead_time" in codes(run(conn, act("reschedule_po", po_id="PO-1", new_delivery_date="2026-11-02")), "block")
    assert blocking(run(conn, act("reschedule_po", po_id="PO-1", new_delivery_date="2026-11-05"))) == []


def test_reschedule_unknown_po(conn):
    assert "po_not_found" in codes(run(conn, act("reschedule_po", po_id="PO-X", new_delivery_date="2026-11-05")))


def test_substitute_supplier_same_supplier_blocked(conn):
    p = good_po()["payload"]
    vs = run(conn, act("substitute_supplier", from_supplier_id="sup_freshcatch", **p))
    assert "same_supplier" in codes(vs, "block")


def test_substitute_supplier_clean(conn):
    p = {"supplier_id": "sup_metro", "delivery_date": "2026-11-04", "from_supplier_id": "sup_freshcatch",
         "lines": [{"ingredient_id": "salmon", "qty": 20.0, "unit_price": 26.9}]}
    assert blocking(run(conn, act("substitute_supplier", **p))) == []


# --------------------------------------------------------------------------- add_special


def test_add_special_clean(conn):
    vs = run(conn, act("add_special", dish_id="seared_salmon", portions=20, allergens_declared=["fish", "milk"]))
    assert blocking(vs) == []


def test_add_special_undeclared_allergen(conn):
    vs = run(conn, act("add_special", dish_id="seared_salmon", portions=20, allergens_declared=["fish"]))
    assert "allergen_undeclared" in codes(vs, "block")


def test_add_special_requires_stock(conn):
    vs = run(conn, act("add_special", dish_id="seared_salmon", portions=500, allergens_declared=["fish", "milk"]))
    assert "stock_available" in codes(vs, "block")


def test_add_special_fefo_violation(conn):
    vs = run(conn, act("add_special", dish_id="seared_salmon", portions=20, allergens_declared=["fish", "milk"],
                       uses_lots=[{"lot_id": "LOT-B", "qty": 3.0}]))
    assert "fefo" in codes(vs, "block")


def test_add_special_fefo_ok_when_oldest_lot_used(conn):
    vs = run(conn, act("add_special", dish_id="seared_salmon", portions=20, allergens_declared=["fish", "milk"],
                       uses_lots=[{"lot_id": "LOT-A", "qty": 3.0}]))
    assert "fefo" not in codes(vs)


def test_add_special_out_of_season(conn):
    vs = run(conn, act("add_special", dish_id="gazpacho", portions=5, allergens_declared=["gluten"]))
    assert "out_of_season" in codes(vs, "block")


def test_add_special_price_below_cost(conn):
    vs = run(conn, act("add_special", dish_id="seared_salmon", portions=5, price=2.0, allergens_declared=["fish", "milk"]))
    assert "negative_margin" in codes(vs, "block")


# --------------------------------------------------------------------------- 86_dish


def test_86_unknown_dish(conn):
    assert "unknown_dish" in codes(run(conn, act("86_dish", dish_id="nope", date="2026-11-02")), "block")


def test_86_past_date(conn):
    assert "past_date" in codes(run(conn, act("86_dish", dish_id="baked_cod", date="2026-11-01")), "block")


def test_86_warns_when_stock_is_plentiful(conn):
    vs = run(conn, act("86_dish", dish_id="seared_salmon", date="2026-11-02"))
    assert "unnecessary_86" in codes(vs, "warn") and blocking(vs) == []


def test_86_clean_when_no_stock(conn):
    assert run(conn, act("86_dish", dish_id="baked_cod", date="2026-11-02", meal_period="dinner")) == []


# --------------------------------------------------------------------------- substitute_ingredient


def test_substitute_ingredient_new_allergen_must_be_declared(conn):
    base = dict(dish_id="seared_salmon", from_ingredient="potato", to_ingredient="flour")
    assert "allergen_undeclared" in codes(run(conn, act("substitute_ingredient", allergens_declared=["fish", "milk"], **base)))
    ok = run(conn, act("substitute_ingredient", allergens_declared=["fish", "milk", "gluten"], **base))
    assert blocking(ok) == []


def test_substitute_ingredient_not_in_recipe_and_no_stock(conn):
    assert "not_in_recipe" in codes(run(conn, act("substitute_ingredient", dish_id="seared_salmon", from_ingredient="eggs",
                                                  to_ingredient="flour", allergens_declared=[])))
    vs = run(conn, act("substitute_ingredient", dish_id="seared_salmon", from_ingredient="potato",
                       to_ingredient="arborio_rice", allergens_declared=["fish", "milk"]))
    assert "stock_available" in codes(vs, "block")


# --------------------------------------------------------------------------- prep_list


def test_prep_list_station_mismatch(conn):
    vs = run(conn, act("prep_list", date="2026-11-02", meal_period="dinner",
                       items=[{"station": "grill", "dish_id": "seared_salmon", "portions": 10}]))
    assert "station_mismatch" in codes(vs, "block")


def test_prep_list_station_capacity(conn):
    items = [{"station": "saute", "dish_id": "seared_salmon", "portions": 200}]  # 800 min > 576 min
    vs = run(conn, act("prep_list", date="2026-11-02", meal_period="dinner", items=items))
    assert "station_capacity" in codes(vs, "block")


def test_prep_list_clean(conn):
    items = [{"station": "saute", "dish_id": "seared_salmon", "portions": 20}]
    assert blocking(run(conn, act("prep_list", date="2026-11-02", meal_period="dinner", items=items))) == []


def test_prep_list_stock_shortfall(conn):
    items = [{"station": "saute", "dish_id": "seared_salmon", "portions": 150}]  # needs 27 kg salmon, 20 on hand
    vs = run(conn, act("prep_list", date="2026-11-02", meal_period="dinner", items=items))
    assert "stock_available" in codes(vs, "block")


# --------------------------------------------------------------------------- discard_lot


def test_discard_lot_rules(conn):
    assert "lot_not_found" in codes(run(conn, act("discard_lot", lot_id="LOT-ZZZ", reason="expired")))
    assert "not_expired" in codes(run(conn, act("discard_lot", lot_id="LOT-A", reason="expired")), "block")
    assert "usable_lot" in codes(run(conn, act("discard_lot", lot_id="LOT-B", reason="discarded")), "warn")
    assert run(conn, act("discard_lot", lot_id="LOT-A", reason="spoilage")) == []
    assert "invalid_reason" in codes(run(conn, act("discard_lot", lot_id="LOT-A", reason="because")))


# --------------------------------------------------------------------------- adjust_forecast / flag_variance


def test_adjust_forecast_factor_bounds(conn):
    base = dict(date="2026-11-03", meal_period="dinner", reason="home game")
    assert blocking(run(conn, act("adjust_forecast", factor=1.2, **base))) == []
    assert "factor_out_of_range" in codes(run(conn, act("adjust_forecast", factor=2.0, **base)), "block")
    assert "factor_out_of_range" in codes(run(conn, act("adjust_forecast", factor=0.4, **base)), "block")


def test_adjust_forecast_needs_reason_and_valid_period(conn):
    assert "missing_reason" in codes(run(conn, act("adjust_forecast", date="2026-11-03", meal_period="dinner",
                                                   factor=1.1, reason="  ")))
    assert "invalid_meal_period" in codes(run(conn, act("adjust_forecast", date="2026-11-03", meal_period="brunch",
                                                        factor=1.1, reason="x")))


def test_flag_variance_without_evidence_warns(conn):
    vs = run(conn, act("flag_variance", ingredient_id="ribeye", flag="shrinkage"))
    assert "variance_not_supported" in codes(vs, "warn") and blocking(vs) == []


def test_flag_variance_with_evidence_is_clean(conn):
    rows = [(f"2026-10-{d:02d}", "ribeye", 9.3, 10.0) for d in range(26, 32)]
    conn.executemany("INSERT INTO stock_counts VALUES (?,?,?,?)", rows)
    conn.commit()
    assert run(conn, act("flag_variance", ingredient_id="ribeye", flag="shrinkage")) == []


def test_unknown_action_type(conn):
    assert codes(run(conn, {"action_type": "teleport", "payload": {}})) == {"unknown_action"}


# --------------------------------------------------------------------------- needs_approval


def test_needs_approval_thresholds():
    small = good_po()
    assert not needs_approval(small, settings)
    big = good_po(lines=[{"ingredient_id": "salmon", "qty": 100.0, "unit_price": 24.0}])  # 2,400 > 1,500
    assert needs_approval(big, settings)
    assert needs_approval(act("add_special", dish_id="x", portions=1), settings)
    assert needs_approval(act("86_dish", dish_id="x", date="2026-11-02"), settings)
    assert needs_approval(act("substitute_ingredient", dish_id="x"), settings)
    assert not needs_approval(act("prep_list", date="2026-11-02", meal_period="dinner", items=[]), settings)
    assert not needs_approval(act("adjust_forecast", date="2026-11-02", meal_period="all", factor=1.1, reason="x"), settings)
    assert not needs_approval(act("discard_lot", lot_id="LOT-A", reason="spoilage"), settings)
