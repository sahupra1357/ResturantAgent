-- Restaurant Ops schema (see DESIGN.md "Database schema"). Dates are ISO TEXT (YYYY-MM-DD).

CREATE TABLE IF NOT EXISTS ingredients (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    unit TEXT NOT NULL CHECK (unit IN ('kg','l','ea')),
    category TEXT NOT NULL,
    storage TEXT NOT NULL CHECK (storage IN ('dry','cooler','freezer')),
    shelf_life_days INTEGER NOT NULL,
    allergens TEXT NOT NULL DEFAULT '',
    ref_cost REAL NOT NULL,
    seasonal_months TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS dishes (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL CHECK (category IN ('starter','main','dessert','side')),
    price REAL NOT NULL,
    station TEXT NOT NULL,
    is_cold INTEGER NOT NULL DEFAULT 0,
    seasonal_months TEXT NOT NULL DEFAULT '',
    prep_minutes REAL NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS recipe_lines (
    dish_id TEXT NOT NULL REFERENCES dishes(id),
    ingredient_id TEXT NOT NULL REFERENCES ingredients(id),
    qty_per_portion REAL NOT NULL,
    PRIMARY KEY (dish_id, ingredient_id)
);

CREATE TABLE IF NOT EXISTS suppliers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    lead_time_days INTEGER NOT NULL,
    min_order_value REAL NOT NULL,
    fill_rate REAL NOT NULL,
    delay_prob REAL NOT NULL,
    order_cutoff_hour INTEGER NOT NULL DEFAULT 14
);

CREATE TABLE IF NOT EXISTS supplier_items (
    supplier_id TEXT NOT NULL REFERENCES suppliers(id),
    ingredient_id TEXT NOT NULL REFERENCES ingredients(id),
    price REAL NOT NULL,
    pack_size REAL NOT NULL,
    PRIMARY KEY (supplier_id, ingredient_id)
);

CREATE TABLE IF NOT EXISTS lots (
    lot_id TEXT PRIMARY KEY,
    ingredient_id TEXT NOT NULL REFERENCES ingredients(id),
    qty REAL NOT NULL,
    received_date DATE NOT NULL,
    expiry_date DATE NOT NULL,
    supplier_id TEXT,
    unit_cost REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','depleted','discarded'))
);

CREATE TABLE IF NOT EXISTS covers (
    date DATE NOT NULL,
    meal_period TEXT NOT NULL,
    covers INTEGER NOT NULL,
    PRIMARY KEY (date, meal_period)
);

CREATE TABLE IF NOT EXISTS pos_sales (
    date DATE NOT NULL,
    meal_period TEXT NOT NULL,
    dish_id TEXT NOT NULL REFERENCES dishes(id),
    qty INTEGER NOT NULL,
    PRIMARY KEY (date, meal_period, dish_id)
);

CREATE TABLE IF NOT EXISTS reservations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date DATE NOT NULL,
    meal_period TEXT NOT NULL,
    party_size INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT 'standard' CHECK (kind IN ('standard','private')),
    created_date DATE NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date DATE NOT NULL,
    name TEXT NOT NULL,
    type TEXT NOT NULL CHECK (type IN ('holiday','sports','local','private','weather')),
    meal_period TEXT,
    impact_hint TEXT
);

CREATE TABLE IF NOT EXISTS weather (
    date DATE PRIMARY KEY,
    temp_c REAL NOT NULL,
    alert TEXT
);

CREATE TABLE IF NOT EXISTS staff_shifts (
    date DATE NOT NULL,
    meal_period TEXT NOT NULL,
    station TEXT NOT NULL,
    staff INTEGER NOT NULL,
    PRIMARY KEY (date, meal_period, station)
);

CREATE TABLE IF NOT EXISTS purchase_orders (
    po_id TEXT PRIMARY KEY,
    supplier_id TEXT NOT NULL REFERENCES suppliers(id),
    created_date DATE NOT NULL,
    expected_delivery DATE NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('draft','pending_approval','sent','delivered','partial','cancelled')),
    total REAL NOT NULL,
    decision_id INTEGER
);

CREATE TABLE IF NOT EXISTS po_lines (
    po_id TEXT NOT NULL REFERENCES purchase_orders(po_id),
    ingredient_id TEXT NOT NULL REFERENCES ingredients(id),
    qty REAL NOT NULL,
    unit_price REAL NOT NULL,
    qty_delivered REAL,
    PRIMARY KEY (po_id, ingredient_id)
);

CREATE TABLE IF NOT EXISTS stock_counts (
    date DATE NOT NULL,
    ingredient_id TEXT NOT NULL REFERENCES ingredients(id),
    counted_qty REAL NOT NULL,
    system_qty REAL NOT NULL,
    PRIMARY KEY (date, ingredient_id)
);

CREATE TABLE IF NOT EXISTS waste_log (
    date DATE NOT NULL,
    lot_id TEXT NOT NULL,
    ingredient_id TEXT NOT NULL,
    qty REAL NOT NULL,
    value REAL NOT NULL,
    reason TEXT NOT NULL CHECK (reason IN ('expired','discarded','spoilage'))
);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    sim_date DATE NOT NULL,
    ts TEXT NOT NULL,
    agent TEXT NOT NULL,
    action_type TEXT NOT NULL,
    payload JSON,
    rationale TEXT,
    status TEXT NOT NULL CHECK (status IN ('proposed','blocked','pending_approval','approved','rejected','executed')),
    violations JSON,
    chef_note TEXT
);

CREATE TABLE IF NOT EXISTS trace (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    sim_date DATE,
    ts TEXT NOT NULL,
    node TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('agent_start','tool_call','tool_result','agent_end','supervisor','constraint','approval','execute','event')),
    content JSON
);

CREATE TABLE IF NOT EXISTS kpi_daily (
    run_id TEXT NOT NULL,
    policy TEXT NOT NULL CHECK (policy IN ('agent','baseline')),
    date DATE NOT NULL,
    revenue REAL,
    food_cost REAL,
    waste_value REAL,
    waste_qty REAL,
    stockouts INTEGER,
    dishes_86d INTEGER,
    covers_served INTEGER,
    approvals INTEGER,
    PRIMARY KEY (run_id, policy, date)
);

CREATE TABLE IF NOT EXISTS menu_state (
    date DATE NOT NULL,
    dish_id TEXT NOT NULL REFERENCES dishes(id),
    status TEXT NOT NULL CHECK (status IN ('active','special','86')),
    note TEXT,
    PRIMARY KEY (date, dish_id)
);

-- Phase 4 additions (simulator)
-- Daily covers forecast (P10/P50/P90) logged by the simulator next to the realised demand, for MAPE / P90 coverage.
CREATE TABLE IF NOT EXISTS forecast_log (
    run_id TEXT NOT NULL,
    policy TEXT NOT NULL,
    date DATE NOT NULL,
    meal_period TEXT NOT NULL,
    p10 REAL,
    p50 REAL,
    p90 REAL,
    actual INTEGER,
    PRIMARY KEY (run_id, policy, date, meal_period)
);

-- Supplier fill-rate overrides injected by disruptions (e.g. salmon shortfall); read by the delivery step and the mock vendors.
CREATE TABLE IF NOT EXISTS supply_overrides (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id TEXT NOT NULL,
    ingredient_id TEXT,            -- NULL = every item from the supplier
    start_date DATE NOT NULL,
    end_date DATE NOT NULL,
    fill_rate REAL NOT NULL,
    note TEXT
);

-- Key/value facts about the seeded world (e.g. sim_start = the first simulated day; history ends the day before)
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- Indexes for the common access paths
CREATE INDEX IF NOT EXISTS idx_lots_ing_status ON lots(ingredient_id, status, expiry_date);
CREATE INDEX IF NOT EXISTS idx_lots_expiry ON lots(expiry_date);
CREATE INDEX IF NOT EXISTS idx_pos_date ON pos_sales(date);
CREATE INDEX IF NOT EXISTS idx_pos_dish ON pos_sales(dish_id, date);
CREATE INDEX IF NOT EXISTS idx_res_date ON reservations(date, meal_period);
CREATE INDEX IF NOT EXISTS idx_events_date ON events(date);
CREATE INDEX IF NOT EXISTS idx_po_status ON purchase_orders(status, expected_delivery);
CREATE INDEX IF NOT EXISTS idx_stock_ing ON stock_counts(ingredient_id, date);
CREATE INDEX IF NOT EXISTS idx_waste_date ON waste_log(date);
CREATE INDEX IF NOT EXISTS idx_decisions_run ON decisions(run_id, sim_date);
CREATE INDEX IF NOT EXISTS idx_trace_run ON trace(run_id, id);

-- ---------------------------------------------------------------------------------------------------------
-- Real-time service (restaurant_ops/live). One row per live dinner service; its DB is a copy of the seeded DB.
CREATE TABLE IF NOT EXISTS service_runs (
    service_id TEXT PRIMARY KEY,
    service_date DATE NOT NULL,
    meal_period TEXT NOT NULL,
    seed INTEGER NOT NULL,
    started_ts TEXT NOT NULL,
    bus TEXT,
    llm_mode TEXT,
    status TEXT NOT NULL DEFAULT 'running'
);

-- Append-only log of every event and decision on the bus; the whole service can be replayed from it.
CREATE TABLE IF NOT EXISTS event_log (
    service_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    wall_ts TEXT NOT NULL,
    sim_ts TEXT NOT NULL,
    type TEXT NOT NULL,
    source TEXT NOT NULL,
    payload JSON,
    PRIMARY KEY (service_id, seq)
);

-- Online ready-time quotes vs actuals (quote accuracy KPI and prep-time recalibration).
CREATE TABLE IF NOT EXISTS quote_log (
    service_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    placed_sim_ts TEXT NOT NULL,
    quoted_p50 TEXT NOT NULL,
    quoted_p90 TEXT NOT NULL,
    actual_ready TEXT,
    within_range INTEGER,
    error_min REAL,
    PRIMARY KEY (service_id, order_id)
);

CREATE INDEX IF NOT EXISTS idx_event_log_type ON event_log(service_id, type);

-- Bookings taken by the host stand during a live service (the `reservations` table stays the forecasting input).
CREATE TABLE IF NOT EXISTS bookings (
    booking_id TEXT PRIMARY KEY,
    service_id TEXT NOT NULL,
    name TEXT NOT NULL,
    phone TEXT,
    party_size INTEGER NOT NULL,
    time TEXT NOT NULL,              -- HH:MM
    status TEXT NOT NULL,            -- booked | arrived | seated | no_show | cancelled
    guest_id INTEGER,
    notes TEXT,
    source TEXT,                     -- sim | phone | web | walk_in
    created_ts TEXT NOT NULL
);

-- Guest profiles: persistent allergies, preferences, visits and loyalty.
CREATE TABLE IF NOT EXISTS guests (
    guest_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    phone TEXT UNIQUE,
    email TEXT,
    allergies TEXT NOT NULL DEFAULT '',     -- comma list of allergens (e.g. 'nuts,milk')
    preferences TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    visits INTEGER NOT NULL DEFAULT 0,
    total_spend REAL NOT NULL DEFAULT 0,
    points INTEGER NOT NULL DEFAULT 0,
    tier TEXT NOT NULL DEFAULT 'Member',
    created TEXT NOT NULL,
    last_visit TEXT
);

CREATE TABLE IF NOT EXISTS guest_visits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guest_id INTEGER NOT NULL,
    service_id TEXT NOT NULL,
    date DATE NOT NULL,
    party_size INTEGER,
    spend REAL NOT NULL DEFAULT 0,
    dishes TEXT NOT NULL DEFAULT ''
);

-- Checks and payments (dine-in bills, online prepayments, splits, tips, discounts, refunds).
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    service_id TEXT NOT NULL,
    check_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,              -- payment | refund | discount | comp
    amount REAL NOT NULL,
    tip REAL NOT NULL DEFAULT 0,
    method TEXT,
    note TEXT
);

-- Food safety: temperature log, HACCP checklist, donations of safe near-expiry stock.
CREATE TABLE IF NOT EXISTS temp_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    service_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    unit TEXT NOT NULL,
    temp_c REAL NOT NULL,
    ok INTEGER NOT NULL,
    source TEXT NOT NULL DEFAULT 'sensor'
);
CREATE TABLE IF NOT EXISTS haccp_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    service_id TEXT NOT NULL,
    item TEXT NOT NULL,
    due TEXT NOT NULL,               -- HH:MM
    done_ts TEXT,
    done_by TEXT,
    ok INTEGER,
    note TEXT
);
CREATE TABLE IF NOT EXISTS donations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date DATE NOT NULL,
    lot_id TEXT NOT NULL,
    ingredient_id TEXT NOT NULL,
    qty REAL NOT NULL,
    value REAL NOT NULL,
    partner TEXT NOT NULL
);
-- Observed supplier performance (fill rate / delays), see core/suppliers.py.
CREATE TABLE IF NOT EXISTS supplier_perf (
    id INTEGER PRIMARY KEY AUTOINCREMENT, supplier_id TEXT NOT NULL, ts TEXT NOT NULL, kind TEXT NOT NULL,
    fill REAL, late INTEGER, weight REAL NOT NULL, fill_rate_after REAL, delay_prob_after REAL, note TEXT
);

-- ---------------------------------------------------------------------------------------------------------
-- Round 3: admin / onboarding, identity, money, operations. Admin tables live in the master DB and are copied
-- into each service DB when the service starts (a service runs on the configuration it started with).
CREATE TABLE IF NOT EXISTS config_kv (key TEXT PRIMARY KEY, value JSON NOT NULL);
CREATE TABLE IF NOT EXISTS tax_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, jurisdiction TEXT NOT NULL, rate REAL NOT NULL,
    applies_to TEXT NOT NULL DEFAULT 'all',      -- food | alcohol | delivery_fee | all
    channel TEXT NOT NULL DEFAULT 'all',         -- all | dine_in | online
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS opening_hours (
    weekday INTEGER NOT NULL, meal_period TEXT NOT NULL, open TEXT NOT NULL, close TEXT NOT NULL,
    closed INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (weekday, meal_period)
);
CREATE TABLE IF NOT EXISTS floor_tables (
    id TEXT PRIMARY KEY, seats INTEGER NOT NULL, x REAL NOT NULL, y REAL NOT NULL, w REAL NOT NULL, h REAL NOT NULL,
    shape TEXT NOT NULL, section TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS stations (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, parallel INTEGER NOT NULL DEFAULT 3, cook_factor REAL NOT NULL DEFAULT 2.4,
    lunch_cooks INTEGER NOT NULL DEFAULT 1, dinner_cooks INTEGER NOT NULL DEFAULT 2, late_cooks INTEGER NOT NULL DEFAULT 1,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS delivery_zones (
    id TEXT PRIMARY KEY, label TEXT NOT NULL, fee REAL NOT NULL, drive_min REAL NOT NULL, min_order REAL NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS staff (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, role TEXT NOT NULL, station TEXT,
    hourly_rate REAL NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS roster (
    date DATE NOT NULL, meal_period TEXT NOT NULL, staff_id INTEGER NOT NULL, station TEXT,
    PRIMARY KEY (date, meal_period, staff_id)
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE, display_name TEXT NOT NULL, role TEXT NOT NULL,
    pw_hash TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, created TEXT NOT NULL, expires TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, username TEXT, role TEXT, method TEXT NOT NULL,
    path TEXT NOT NULL, status INTEGER, detail JSON
);
CREATE TABLE IF NOT EXISTS gift_cards (
    code TEXT PRIMARY KEY, initial REAL NOT NULL, balance REAL NOT NULL, issued TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active', note TEXT
);
CREATE TABLE IF NOT EXISTS idempotency (
    key TEXT PRIMARY KEY, endpoint TEXT NOT NULL, response JSON NOT NULL, created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS day_close (
    date DATE NOT NULL, meal_period TEXT NOT NULL, service_id TEXT NOT NULL, closed_ts TEXT NOT NULL, closed_by TEXT,
    report JSON NOT NULL, PRIMARY KEY (date, meal_period)
);
CREATE TABLE IF NOT EXISTS po_outbox (
    po_id TEXT PRIMARY KEY, supplier_id TEXT NOT NULL, email TEXT, pdf_path TEXT, eml_path TEXT,
    status TEXT NOT NULL, sent_ts TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS kds_bumps (
    service_id TEXT NOT NULL, item_id TEXT NOT NULL, bumped_ts TEXT NOT NULL, by_user TEXT,
    PRIMARY KEY (service_id, item_id)
);
CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT, service_id TEXT, ts TEXT NOT NULL, agent TEXT, provider TEXT, model TEXT,
    latency_ms REAL, ok INTEGER, tokens_in INTEGER, tokens_out INTEGER, cost REAL, error TEXT
);
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS gift_card_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL, ts TEXT NOT NULL, kind TEXT NOT NULL,  -- issue | redeem | refund | void
    amount REAL NOT NULL, ref TEXT, service_id TEXT
);
