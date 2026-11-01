"""Static world for an 80-seat bistro: 60 ingredients, 20 dishes (with BOMs), 4 suppliers, stations.

Plain Python data structures plus `load_catalog(conn)` to write them into the database.
No LLM imports here.
"""
from __future__ import annotations

import sqlite3
import zlib
from dataclasses import dataclass

# --------------------------------------------------------------------------- types


@dataclass(frozen=True)
class Ingredient:
    id: str
    name: str
    unit: str  # 'kg' | 'l' | 'ea'
    category: str  # seafood|dairy|meat|produce|dry|frozen|bakery
    storage: str  # dry|cooler|freezer
    shelf_life_days: int
    allergens: tuple[str, ...]
    ref_cost: float  # per unit
    seasonal_months: tuple[int, ...] = ()  # () = available all year


@dataclass(frozen=True)
class Dish:
    id: str
    name: str
    category: str  # starter|main|dessert|side
    price: float
    station: str  # grill|saute|fry|garde_manger|pastry
    is_cold: bool
    prep_minutes: float  # active labour minutes per portion
    recipe: dict[str, float]  # ingredient_id -> qty per portion (ingredient units)
    seasonal_months: tuple[int, ...] = ()
    popularity: float = 5.0  # relative base popularity (used by the hidden truth model)
    tags: tuple[str, ...] = ()  # e.g. 'fried', 'shareable'


@dataclass(frozen=True)
class Supplier:
    id: str
    name: str
    lead_time_days: int
    min_order_value: float
    fill_rate: float
    delay_prob: float
    order_cutoff_hour: int = 14


@dataclass(frozen=True)
class SupplierItem:
    supplier_id: str
    ingredient_id: str
    price: float  # per ingredient unit
    pack_size: float  # order multiple, in ingredient units


# --------------------------------------------------------------------------- ingredients

_A = lambda s: tuple(x for x in s.split(",") if x)  # noqa: E731  allergen helper
_M = lambda s: tuple(int(x) for x in s.split(",") if x)  # noqa: E731  months helper

# (id, name, unit, category, storage, shelf_life_days, allergens, ref_cost, seasonal_months)
_ING_ROWS = [
    # seafood (6)
    ("salmon", "Atlantic Salmon", "kg", "seafood", "cooler", 3, "fish", 24.0, ""),
    ("cod", "Cod Fillet", "kg", "seafood", "cooler", 3, "fish", 19.0, ""),
    ("sea_bass", "Sea Bass Fillet", "kg", "seafood", "cooler", 3, "fish", 26.0, ""),
    ("shrimp", "Shrimp (peeled)", "kg", "seafood", "cooler", 3, "crustaceans", 22.0, ""),
    ("mussels", "Mussels", "kg", "seafood", "cooler", 2, "molluscs", 9.0, ""),
    ("calamari", "Calamari Rings", "kg", "seafood", "cooler", 4, "molluscs", 12.0, ""),
    # dairy & eggs (8)
    ("cream", "Heavy Cream", "l", "dairy", "cooler", 10, "milk", 5.5, ""),
    ("butter", "Butter", "kg", "dairy", "cooler", 45, "milk", 9.0, ""),
    ("milk", "Whole Milk", "l", "dairy", "cooler", 7, "milk", 1.2, ""),
    ("mozzarella", "Mozzarella", "kg", "dairy", "cooler", 14, "milk", 9.0, ""),
    ("parmesan", "Parmesan", "kg", "dairy", "cooler", 60, "milk", 22.0, ""),
    ("eggs", "Eggs", "ea", "dairy", "cooler", 28, "eggs", 0.30, ""),
    ("goat_cheese", "Goat Cheese", "kg", "dairy", "cooler", 14, "milk", 18.0, ""),
    ("mascarpone", "Mascarpone", "kg", "dairy", "cooler", 12, "milk", 10.0, ""),
    # meat (6)
    ("ribeye", "Beef Ribeye", "kg", "meat", "cooler", 5, "", 38.0, ""),
    ("beef_mince", "Ground Beef", "kg", "meat", "cooler", 3, "", 11.0, ""),
    ("chicken_breast", "Chicken Breast", "kg", "meat", "cooler", 4, "", 8.0, ""),
    ("chicken_wings", "Chicken Wings", "kg", "meat", "cooler", 4, "", 6.0, ""),
    ("pork_belly", "Pork Belly (guanciale)", "kg", "meat", "cooler", 6, "", 10.0, ""),
    ("prosciutto", "Prosciutto", "kg", "meat", "cooler", 21, "", 30.0, ""),
    # produce (22)
    ("tomato", "Tomato", "kg", "produce", "cooler", 6, "", 3.0, ""),
    ("cherry_tomato", "Cherry Tomato", "kg", "produce", "cooler", 7, "", 6.0, ""),
    ("cucumber", "Cucumber", "kg", "produce", "cooler", 7, "", 2.2, ""),
    ("romaine", "Romaine Lettuce", "kg", "produce", "cooler", 5, "", 3.5, ""),
    ("arugula", "Arugula", "kg", "produce", "cooler", 4, "", 14.0, ""),
    ("spinach", "Baby Spinach", "kg", "produce", "cooler", 4, "", 6.0, ""),
    ("onion", "Onion", "kg", "produce", "dry", 30, "", 1.4, ""),
    ("garlic", "Garlic", "kg", "produce", "dry", 30, "", 8.0, ""),
    ("potato", "Potato", "kg", "produce", "dry", 30, "", 1.2, ""),
    ("carrot", "Carrot", "kg", "produce", "cooler", 21, "", 1.3, ""),
    ("lemon", "Lemon", "kg", "produce", "cooler", 21, "", 3.0, ""),
    ("lime", "Lime", "kg", "produce", "cooler", 21, "", 3.5, ""),
    ("avocado", "Avocado", "kg", "produce", "cooler", 4, "", 6.0, ""),
    ("mushroom", "Mushroom", "kg", "produce", "cooler", 5, "", 7.0, ""),
    ("bell_pepper", "Bell Pepper", "kg", "produce", "cooler", 8, "", 4.0, ""),
    ("basil", "Basil", "kg", "produce", "cooler", 4, "", 20.0, ""),
    ("parsley", "Parsley", "kg", "produce", "cooler", 6, "", 12.0, ""),
    ("asparagus", "Asparagus", "kg", "produce", "cooler", 5, "", 9.0, "3,4,5,6"),
    ("pumpkin", "Pumpkin", "kg", "produce", "dry", 30, "", 1.8, "9,10,11,12"),
    ("strawberry", "Strawberry", "kg", "produce", "cooler", 4, "", 8.0, "5,6,7,8"),
    ("zucchini", "Zucchini", "kg", "produce", "cooler", 7, "", 2.8, "6,7,8,9"),
    ("fennel", "Fennel", "kg", "produce", "cooler", 8, "", 3.5, ""),
    # dry goods & pantry (14)
    ("flour", "Flour", "kg", "dry", "dry", 180, "gluten", 0.9, ""),
    ("sugar", "Sugar", "kg", "dry", "dry", 365, "", 1.1, ""),
    ("arborio_rice", "Arborio Rice", "kg", "dry", "dry", 365, "", 3.5, ""),
    ("spaghetti", "Spaghetti", "kg", "dry", "dry", 365, "gluten", 2.2, ""),
    ("olive_oil", "Olive Oil", "l", "dry", "dry", 365, "", 8.0, ""),
    ("frying_oil", "Frying Oil", "l", "dry", "dry", 90, "", 2.5, ""),
    ("breadcrumbs", "Breadcrumbs", "kg", "dry", "dry", 180, "gluten", 3.0, ""),
    ("dark_chocolate", "Dark Chocolate", "kg", "dry", "dry", 365, "milk,soy", 12.0, ""),
    ("cocoa_powder", "Cocoa Powder", "kg", "dry", "dry", 365, "", 10.0, ""),
    ("balsamic", "Balsamic Vinegar", "l", "dry", "dry", 730, "sulphites", 6.0, ""),
    ("white_wine", "Cooking White Wine", "l", "dry", "dry", 90, "sulphites", 6.0, ""),
    ("chicken_stock", "Chicken Stock", "l", "dry", "dry", 180, "", 2.0, ""),
    ("capers", "Capers", "kg", "dry", "dry", 365, "", 14.0, ""),
    ("bbq_sauce", "BBQ Sauce", "l", "dry", "dry", 180, "soy", 4.0, ""),
    # bakery (2)
    ("baguette", "Baguette", "ea", "bakery", "dry", 2, "gluten", 1.2, ""),
    ("brioche_bun", "Brioche Bun", "ea", "bakery", "dry", 3, "gluten,milk,eggs", 0.55, ""),
    # frozen (2)
    ("frozen_fries", "Frozen Fries", "kg", "frozen", "freezer", 180, "", 2.4, ""),
    ("vanilla_ice_cream", "Vanilla Ice Cream", "l", "frozen", "freezer", 120, "milk,eggs", 7.0, ""),
]

INGREDIENTS: list[Ingredient] = [
    Ingredient(i, n, u, c, s, sl, _A(al), rc, _M(sm)) for i, n, u, c, s, sl, al, rc, sm in _ING_ROWS
]
INGREDIENTS_BY_ID: dict[str, Ingredient] = {i.id: i for i in INGREDIENTS}

# --------------------------------------------------------------------------- dishes

DISHES: list[Dish] = [
    # --- starters
    Dish("caesar_salad", "Caesar Salad", "starter", 13.0, "garde_manger", True, 2.5,
         {"romaine": 0.12, "parmesan": 0.025, "eggs": 1, "baguette": 0.15, "olive_oil": 0.02, "lemon": 0.03},
         popularity=7),
    Dish("gazpacho", "Chilled Gazpacho", "starter", 11.0, "garde_manger", True, 2.0,
         {"tomato": 0.25, "cucumber": 0.10, "bell_pepper": 0.08, "onion": 0.03, "garlic": 0.01,
          "olive_oil": 0.02, "baguette": 0.10},
         seasonal_months=(5, 6, 7, 8, 9), popularity=4),
    Dish("sea_bass_ceviche", "Sea Bass Ceviche", "starter", 17.0, "garde_manger", True, 3.0,
         {"sea_bass": 0.12, "lime": 0.05, "onion": 0.03, "avocado": 0.08, "cherry_tomato": 0.05},
         popularity=4),
    Dish("goat_cheese_salad", "Goat Cheese & Arugula Salad", "starter", 14.0, "garde_manger", True, 2.5,
         {"goat_cheese": 0.05, "arugula": 0.06, "cherry_tomato": 0.06, "balsamic": 0.015, "olive_oil": 0.015},
         popularity=4),
    Dish("crispy_calamari", "Crispy Calamari", "starter", 15.0, "fry", False, 1.5,
         {"calamari": 0.15, "flour": 0.03, "frying_oil": 0.08, "lemon": 0.03},
         popularity=7, tags=("fried", "shareable")),
    Dish("bbq_wings", "BBQ Chicken Wings", "starter", 14.0, "fry", False, 1.5,
         {"chicken_wings": 0.35, "frying_oil": 0.08, "bbq_sauce": 0.04, "butter": 0.01},
         popularity=6, tags=("fried", "shareable")),
    Dish("pumpkin_soup", "Roasted Pumpkin Soup", "starter", 10.0, "saute", False, 2.0,
         {"pumpkin": 0.30, "cream": 0.04, "onion": 0.04, "chicken_stock": 0.25, "butter": 0.01, "milk": 0.05},
         seasonal_months=(10, 11, 12, 1, 2), popularity=5),
    Dish("asparagus_prosciutto", "Asparagus & Prosciutto", "starter", 15.0, "saute", False, 3.0,
         {"asparagus": 0.15, "prosciutto": 0.04, "parmesan": 0.02, "olive_oil": 0.01, "lemon": 0.02},
         seasonal_months=(3, 4, 5, 6), popularity=3),
    # --- mains
    Dish("seared_salmon", "Pan-Seared Salmon", "main", 29.0, "saute", False, 4.0,
         {"salmon": 0.18, "potato": 0.15, "spinach": 0.08, "butter": 0.02, "lemon": 0.03, "cream": 0.03},
         popularity=9),
    Dish("baked_cod", "Cod Provencal", "main", 27.0, "saute", False, 4.0,
         {"cod": 0.18, "cherry_tomato": 0.08, "capers": 0.01, "fennel": 0.08, "olive_oil": 0.02, "white_wine": 0.05},
         popularity=5),
    Dish("ribeye_steak", "Grilled Ribeye", "main", 42.0, "grill", False, 5.0,
         {"ribeye": 0.30, "butter": 0.02, "garlic": 0.01, "potato": 0.20, "parsley": 0.005, "mushroom": 0.08},
         popularity=6),
    Dish("smash_burger", "Bistro Smash Burger", "main", 19.0, "grill", False, 3.5,
         {"beef_mince": 0.20, "brioche_bun": 1, "mozzarella": 0.03, "tomato": 0.05, "romaine": 0.03, "onion": 0.03},
         popularity=9, tags=("shareable",)),
    Dish("chicken_milanese", "Chicken Milanese", "main", 24.0, "saute", False, 4.0,
         {"chicken_breast": 0.22, "breadcrumbs": 0.05, "eggs": 1, "flour": 0.02, "lemon": 0.03,
          "arugula": 0.05, "olive_oil": 0.03},
         popularity=5),
    Dish("mushroom_risotto", "Wild Mushroom Risotto", "main", 22.0, "saute", False, 5.0,
         {"arborio_rice": 0.09, "mushroom": 0.12, "parmesan": 0.03, "butter": 0.02, "chicken_stock": 0.30,
          "white_wine": 0.05, "onion": 0.03},
         popularity=5),
    Dish("carbonara", "Spaghetti Carbonara", "main", 21.0, "saute", False, 3.5,
         {"spaghetti": 0.12, "pork_belly": 0.06, "eggs": 2, "parmesan": 0.03},
         popularity=6),
    Dish("seafood_linguine", "Shrimp & Mussel Linguine", "main", 26.0, "saute", False, 4.5,
         {"spaghetti": 0.12, "shrimp": 0.10, "mussels": 0.15, "garlic": 0.01, "white_wine": 0.05,
          "cherry_tomato": 0.05, "parsley": 0.005},
         popularity=4),
    # --- sides
    Dish("parmesan_fries", "Parmesan Truffle Fries", "side", 9.0, "fry", False, 1.0,
         {"frozen_fries": 0.25, "frying_oil": 0.05, "parmesan": 0.02, "parsley": 0.003},
         popularity=9, tags=("fried", "shareable")),
    Dish("grilled_vegetables", "Grilled Summer Vegetables", "side", 10.0, "grill", False, 3.0,
         {"zucchini": 0.15, "bell_pepper": 0.08, "carrot": 0.08, "olive_oil": 0.02, "balsamic": 0.01},
         seasonal_months=(6, 7, 8, 9), popularity=3),
    # --- desserts
    Dish("chocolate_fondant", "Chocolate Fondant", "dessert", 12.0, "pastry", False, 3.0,
         {"dark_chocolate": 0.06, "butter": 0.04, "eggs": 1, "sugar": 0.03, "flour": 0.01,
          "cocoa_powder": 0.005, "vanilla_ice_cream": 0.06},
         popularity=6),
    Dish("strawberry_tiramisu", "Strawberry Tiramisu", "dessert", 12.0, "pastry", True, 2.5,
         {"mascarpone": 0.06, "strawberry": 0.08, "eggs": 1, "sugar": 0.03, "cream": 0.04},
         seasonal_months=(5, 6, 7, 8), popularity=4),
]
DISHES_BY_ID: dict[str, Dish] = {d.id: d for d in DISHES}

# --------------------------------------------------------------------------- stations & staffing

STATIONS: list[str] = ["grill", "saute", "fry", "garde_manger", "pastry"]
MEAL_PERIOD_MINUTES: dict[str, int] = {"lunch": 180, "dinner": 240, "late": 150}
STAFF_EFFICIENCY = 0.8  # share of a shift that is productive prep/cook time

# Default staff per station per meal period
DEFAULT_STAFFING: dict[str, dict[str, int]] = {
    "grill": {"lunch": 1, "dinner": 2, "late": 1},
    "saute": {"lunch": 2, "dinner": 3, "late": 1},
    "fry": {"lunch": 1, "dinner": 2, "late": 1},
    "garde_manger": {"lunch": 2, "dinner": 2, "late": 1},
    "pastry": {"lunch": 1, "dinner": 1, "late": 1},
}


def station_capacity_minutes(station: str, meal_period: str, staff: int | None = None) -> float:
    """Prep-minute capacity of a station for one meal period (default staffing unless given)."""
    n = DEFAULT_STAFFING[station][meal_period] if staff is None else staff
    return n * MEAL_PERIOD_MINUTES[meal_period] * STAFF_EFFICIENCY


STATION_CAPACITY: dict[str, dict[str, float]] = {
    s: {p: station_capacity_minutes(s, p) for p in MEAL_PERIOD_MINUTES} for s in STATIONS
}

# --------------------------------------------------------------------------- suppliers

SUPPLIERS: list[Supplier] = [
    Supplier("sup_freshcatch", "FreshCatch Seafood", 1, 120.0, 0.92, 0.08, 14),
    Supplier("sup_metro", "Metro Wholesale", 2, 250.0, 0.97, 0.05, 16),
    Supplier("sup_greenvalley", "Green Valley Farms", 1, 100.0, 0.90, 0.12, 12),
    Supplier("sup_budget", "Budget Foods Co.", 3, 400.0, 0.85, 0.20, 18),
]
SUPPLIERS_BY_ID: dict[str, Supplier] = {s.id: s for s in SUPPLIERS}

# supplier -> {category: price multiplier vs ref_cost}; a category missing = not carried
_SUPPLIER_CATEGORY_MULT: dict[str, dict[str, float]] = {
    "sup_freshcatch": {"seafood": 1.00},
    "sup_metro": {"seafood": 1.12, "dairy": 1.00, "meat": 1.00, "produce": 1.10,
                  "dry": 1.00, "frozen": 1.00, "bakery": 1.00},
    "sup_greenvalley": {"produce": 0.95, "dairy": 1.04},
    "sup_budget": {"seafood": 0.92, "dairy": 0.92, "meat": 0.90, "dry": 0.88, "frozen": 0.90, "bakery": 0.95},
}
_PACK_BY_CATEGORY = {"seafood": 2.0, "meat": 2.5, "produce": 5.0, "dairy": 1.0, "dry": 5.0, "frozen": 10.0, "bakery": 1.0}
_PACK_OVERRIDE = {
    "eggs": 30, "baguette": 10, "brioche_bun": 12, "milk": 4, "cream": 2, "olive_oil": 5, "frying_oil": 15,
    "balsamic": 1, "white_wine": 3, "chicken_stock": 4, "vanilla_ice_cream": 4, "bbq_sauce": 2, "garlic": 1,
    "basil": 0.5, "parsley": 0.5, "arugula": 1, "capers": 1, "butter": 5, "parmesan": 2, "prosciutto": 1,
    "dark_chocolate": 2, "cocoa_powder": 1, "mushroom": 2, "avocado": 2, "mussels": 5, "mascarpone": 1,
    "goat_cheese": 1, "spinach": 2, "cherry_tomato": 2,
}


def build_supplier_items() -> list[SupplierItem]:
    """Overlapping SKUs: price = ref_cost x supplier/category multiplier x small stable jitter."""
    items: list[SupplierItem] = []
    for sup in SUPPLIERS:
        mults = _SUPPLIER_CATEGORY_MULT[sup.id]
        for ing in INGREDIENTS:
            mult = mults.get(ing.category)
            if mult is None:
                continue
            if sup.id == "sup_greenvalley" and ing.seasonal_months and ing.category == "produce":
                mult *= 0.92  # local farm is cheaper for in-season produce
            jitter = 0.0 if ing.id == "salmon" else ((zlib.crc32(f"{sup.id}:{ing.id}".encode()) % 9) - 4) / 200
            price = round(ing.ref_cost * mult * (1 + jitter), 3)
            pack = _PACK_OVERRIDE.get(ing.id, _PACK_BY_CATEGORY[ing.category])
            if sup.id == "sup_budget" and ing.id != "eggs":
                pack = pack * 2  # bulk packs
            items.append(SupplierItem(sup.id, ing.id, price, float(pack)))
    return items


SUPPLIER_ITEMS: list[SupplierItem] = build_supplier_items()


# --------------------------------------------------------------------------- accessors


def ingredient_by_id(ingredient_id: str) -> Ingredient:
    """Look up an ingredient by id."""
    return INGREDIENTS_BY_ID[ingredient_id]


def dish_by_id(dish_id: str) -> Dish:
    """Look up a dish by id."""
    return DISHES_BY_ID[dish_id]


def in_season(months: tuple[int, ...], month: int) -> bool:
    """True if a seasonal_months tuple allows `month` (empty tuple = all year)."""
    return not months or month in months


def dish_allergens(dish_id: str) -> list[str]:
    """Sorted union of the allergens of a dish's ingredients."""
    out: set[str] = set()
    for ing_id in DISHES_BY_ID[dish_id].recipe:
        out.update(INGREDIENTS_BY_ID[ing_id].allergens)
    return sorted(out)


def suppliers_for(ingredient_id: str) -> list[SupplierItem]:
    """All supplier SKUs for an ingredient."""
    return [si for si in SUPPLIER_ITEMS if si.ingredient_id == ingredient_id]


def recipe_unit_cost(dish_id: str) -> float:
    """Food cost per portion at reference ingredient costs."""
    d = DISHES_BY_ID[dish_id]
    return sum(INGREDIENTS_BY_ID[i].ref_cost * q for i, q in d.recipe.items())


def _csv(values: tuple) -> str:
    return ",".join(str(v) for v in values)


def load_catalog(conn: sqlite3.Connection) -> dict[str, int]:
    """Write the static catalog into the database (idempotent); returns row counts."""
    cur = conn.cursor()
    cur.executemany(
        "INSERT OR REPLACE INTO ingredients VALUES (?,?,?,?,?,?,?,?,?)",
        [(i.id, i.name, i.unit, i.category, i.storage, i.shelf_life_days, _csv(i.allergens),
          i.ref_cost, _csv(i.seasonal_months)) for i in INGREDIENTS],
    )
    cur.executemany(
        "INSERT OR REPLACE INTO dishes VALUES (?,?,?,?,?,?,?,?,?)",
        [(d.id, d.name, d.category, d.price, d.station, int(d.is_cold), _csv(d.seasonal_months),
          d.prep_minutes, 1) for d in DISHES],
    )
    cur.executemany(
        "INSERT OR REPLACE INTO recipe_lines VALUES (?,?,?)",
        [(d.id, ing, q) for d in DISHES for ing, q in d.recipe.items()],
    )
    cur.executemany(
        "INSERT OR REPLACE INTO suppliers VALUES (?,?,?,?,?,?,?)",
        [(s.id, s.name, s.lead_time_days, s.min_order_value, s.fill_rate, s.delay_prob,
          s.order_cutoff_hour) for s in SUPPLIERS],
    )
    cur.executemany(
        "INSERT OR REPLACE INTO supplier_items VALUES (?,?,?,?)",
        [(si.supplier_id, si.ingredient_id, si.price, si.pack_size) for si in SUPPLIER_ITEMS],
    )
    conn.commit()
    return {
        "ingredients": len(INGREDIENTS), "dishes": len(DISHES),
        "recipe_lines": sum(len(d.recipe) for d in DISHES),
        "suppliers": len(SUPPLIERS), "supplier_items": len(SUPPLIER_ITEMS),
    }
