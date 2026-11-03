# Restaurant Ops Agents — Build Design

Source of truth for the build. Product intent lives in `Idea.md`; this file pins the
contracts between modules so each build phase can be implemented independently.

## Ground rules (from Idea.md)

- **Numbers come from models and rules; judgment comes from agents.** Agents never do
  arithmetic or stock counts — they call tools, tools return numbers.
- **Every agent action passes `core/constraints.py` before it executes.** An LLM can never
  override a constraint violation.
- Deterministic and seeded: `config.SEED` drives every random generator.
- Python 3.12 (pinned via uv; LightGBM / OR-Tools wheels), SQLite, plain `sqlite3` + pandas.

## Repo layout

```
ResturantAgent/
  pyproject.toml            # uv project, python ==3.12.*
  .env.example
  README.md
  DESIGN.md / Idea.md
  restaurant_ops/
    config.py               # Settings (pydantic-settings): paths, seed, dates, thresholds, LLM
    db/
      schema.sql            # all tables (below)
      connection.py         # get_conn(), init_db(), query_df(sql, params), execute(...)
    data/
      catalog.py            # static world: 60 ingredients, 20 dishes + BOMs, 4 suppliers, stations
      generator.py          # 12 months synthetic history + reservations + events + weather + lots
      seed.py               # CLI: `python -m restaurant_ops.data.seed` → builds data/restaurant.db
    core/                   # deterministic layer — no LLM imports allowed here
      forecasting.py        # LightGBM quantile models (P10/P50/P90), signal report, MAPE
      inventory.py          # stock_by_lot, expected_usage, FEFO picking, waste risk, variance
      optimizer.py          # PuLP MILP order optimizer + greedy fallback (same interface)
      constraints.py        # validators: shelf life, FEFO, min order, budget, capacity, allergens
      menu.py               # recipes, margin_calc, allergen_check, feasible dishes
      prep.py               # prep_quantities, station_capacity
    tools/
      registry.py           # plain functions → JSON dicts; LangChain @tool wrappers; TOOLSETS per agent
      api.py                # FastAPI exposing the same functions (one contract for agents + UI)
    agents/
      llm.py                # get_llm() via langchain init_chat_model; MOCK mode
      state.py              # OpsState TypedDict + Pydantic Action / AgentReport
      prompts.py            # system prompts per agent
      base.py               # make_agent(name, tools, prompt) → ReAct subgraph w/ step limit
      forecast_analyst.py, inventory_agent.py, purchasing_agent.py, menu_agent.py, prep_planner.py
      mock_policies.py      # rule-based stand-ins used when LLM_MODE=mock (no API key demo)
      supervisor.py         # build_graph(): supervisor + subgraphs + constraint check + approval + execute
      decision_log.py       # write/read decisions table
    sim/
      simulator.py          # World: advance day, deplete stock, age lots, deliver POs (stochastic)
      disruptions.py        # 4 scenarios as injectable events
      vendors.py            # mock vendor agents for negotiation (rule-based counter-offers)
      baseline.py           # fixed-par policy with same interfaces
      metrics.py            # KPIs: waste $/%, stockouts/86s, food cost %, MAPE, P90 coverage, approvals
      run.py                # CLI: `python -m restaurant_ops.sim.run --days 7 --policy agent|baseline|both`
  live/                     # real-time service (see "Real-time layer" below)
  frontend/                 # React UI: floor, online order page, manager dashboard
  tests/
    test_constraints.py     # written, not run during build
```

## World & calendar

- 80-seat bistro, meal periods: `lunch`, `dinner`, `late`. Seat turns cap: lunch 1.5, dinner 2.0, late 0.6.
- `config.SIM_START = 2026-11-02` (Monday). History = 365 days ending `SIM_START - 1`.
  Train on first 11 months, test on last month.
- Demo week = SIM_START .. SIM_START+6. Calendar in that week: 2 home games (Wed dinner,
  Sat dinner), private dinner for 40 (added by disruption 2 on Saturday), payday = 15th and
  last day of month (also within history).
- Stations: `grill`, `saute` (incl. the fryer), `cold` (garde manger), `pastry`. Each has capacity in
  prep-minutes per meal period and staff per shift.

## Database schema (SQLite, `data/restaurant.db`)

| Table | Columns |
| --- | --- |
| ingredients | id TEXT PK, name, unit ('kg','l','ea'), category, storage ('dry','cooler','freezer'), shelf_life_days INT, allergens TEXT (comma list), ref_cost REAL, seasonal_months TEXT (comma ints, '' = all year) |
| dishes | id TEXT PK, name, category ('starter','main','dessert','side'), price REAL, station, is_cold INT, seasonal_months TEXT, prep_minutes REAL (per portion), active INT |
| recipe_lines | dish_id, ingredient_id, qty_per_portion REAL — PK(dish_id, ingredient_id) |
| suppliers | id TEXT PK, name, lead_time_days INT, min_order_value REAL, fill_rate REAL (0..1), delay_prob REAL, order_cutoff_hour INT |
| supplier_items | supplier_id, ingredient_id, price REAL (per unit), pack_size REAL — PK(supplier_id, ingredient_id) |
| lots | lot_id TEXT PK, ingredient_id, qty REAL, received_date DATE, expiry_date DATE, supplier_id, unit_cost REAL, status ('active','depleted','discarded') |
| covers | date, meal_period, covers INT — PK(date, meal_period) |
| pos_sales | date, meal_period, dish_id, qty INT — PK(date, meal_period, dish_id) |
| reservations | id INTEGER PK, date, meal_period, party_size INT, kind ('standard','private'), created_date |
| events | id INTEGER PK, date, name, type ('holiday','sports','local','private','weather'), meal_period NULL=all, impact_hint TEXT |
| weather | date PK, temp_c REAL, alert TEXT NULL ('heatwave', ...) |
| staff_shifts | date, meal_period, station, staff INT |
| purchase_orders | po_id TEXT PK, supplier_id, created_date, expected_delivery DATE, status ('draft','pending_approval','sent','delivered','partial','cancelled'), total REAL, decision_id |
| po_lines | po_id, ingredient_id, qty REAL, unit_price REAL, qty_delivered REAL NULL |
| stock_counts | date, ingredient_id, counted_qty REAL, system_qty REAL |
| waste_log | date, lot_id, ingredient_id, qty REAL, value REAL, reason ('expired','discarded','spoilage') |
| decisions | id INTEGER PK, run_id, sim_date, ts, agent, action_type, payload JSON, rationale, status ('proposed','blocked','pending_approval','approved','rejected','executed'), violations JSON, chef_note |
| trace | id INTEGER PK, run_id, sim_date, ts, node, kind ('agent_start','tool_call','tool_result','agent_end','supervisor','constraint','approval','execute','event'), content JSON |
| kpi_daily | run_id, policy ('agent','baseline'), date, revenue, food_cost, waste_value, waste_qty, stockouts INT, dishes_86d INT, covers_served INT, approvals INT |
| menu_state | date, dish_id, status ('active','special','86'), note |

Signals baked into synthetic history (generator must make these real so the model can find them):
DOW effect (Fri/Sat dinner high, Mon low), payday +9% covers (15th & month end), start-of-month
**no** effect, season + week-of-year (seasonal dishes only sell in season), holidays, home games
(+25% dinner/late, more fried/shareables), heat (>30 °C → cold dishes +40%, hot mains −20%),
capacity cap from seats × turns, plus Poisson/NB noise.

## Core contracts (deterministic layer)

All functions take a `conn` (or open one via `get_conn()`), plain args, and return
JSON-serialisable dicts/lists. Dates are ISO strings.

```python
# forecasting.py
train_models(conn) -> dict                      # trains & pickles to data/models/; returns metrics
forecast_covers(start_date, days=3) -> list[{date, meal_period, p10, p50, p90, capacity_cap}]
forecast_dish_mix(start_date, days=3) -> list[{date, meal_period, dish_id, p10, p50, p90}]
signal_report() -> list[{signal, effect_pct, significant: bool, note}]   # permutation/ablation
evaluate() -> {mape, p90_coverage, by_period}

# inventory.py
stock_by_lot(ingredient_id=None, as_of) -> list[{lot_id, ingredient_id, qty, expiry_date, days_left, unit_cost}]
expected_usage(start_date, days, quantile='p50', dish_overrides=None) -> list[{ingredient_id, date, qty}]
waste_risk(as_of, horizon_days=3) -> list[{lot_id, ingredient_id, qty_at_risk, value_at_risk, expiry_date}]
fefo_pick(ingredient_id, qty, as_of) -> list[{lot_id, qty}]
variance_report(as_of, days=7) -> list[{ingredient_id, expected, actual, variance_pct, flag}]

# optimizer.py
optimize_orders(as_of, horizon_days=3, budget=None, service_quantile='p90',
                exclude_suppliers=(), ingredient_ids=None) ->
    {status, solver ('milp'|'greedy'), orders: [{supplier_id, delivery_date, lines:[{ingredient_id, qty, unit_price}], total}], total_cost, notes}

# constraints.py
@dataclass Violation(code, message, severity ('block'|'warn'))
check_action(action: dict, conn, as_of) -> list[Violation]
#   action = {"action_type", "payload", ...}  (see Action below)
#   rules: shelf_life (order can't be used before expiry), fefo, min_order_value,
#   budget (daily/weekly), station_capacity, allergen (special must declare allergens; no
#   undeclared allergen substitution), stock_available (special/prep needs stock),
#   supplier_carries_item, positive_quantities, lead_time (delivery >= today + lead time)

# menu.py
recipes(dish_id=None) -> list[{dish_id, name, lines:[{ingredient_id, qty_per_portion}], allergens}]
margin_calc(dish_id, ingredient_cost_overrides=None) -> {dish_id, price, food_cost, margin, margin_pct}
allergen_check(dish_id, substitutions: dict[str,str] | None) -> {allergens, changed: list, ok}
dishes_using(ingredient_ids) -> list[dish_id]
feasible_portions(dish_id, as_of) -> int

# prep.py
prep_quantities(date, meal_period, forecast_quantile='p50') -> list[{station, dish_id, portions, prep_minutes}]
station_capacity(date, meal_period) -> list[{station, capacity_minutes, load_minutes, utilisation}]
```

## Tools layer

`tools/registry.py` wraps every core function above plus:
`event_calendar(start_date, days)`, `reservations_summary(start_date, days)`,
`supplier_quotes(ingredient_id, qty, needed_by)`, `send_po(...)` (only callable by the execute
node, not agents), `negotiate(supplier_id, ingredient_id, qty, target_price)` (calls
`sim/vendors.py`), `weather(start_date, days)`.

`TOOLSETS = {"forecast_analyst": [...], "inventory": [...], "purchasing": [...], "menu": [...], "prep": [...]}`
— exactly as the Agents table in Idea.md. Tools that would mutate the world are **not** given to
agents; agents *propose* actions instead.

The current simulated date comes from a context var (`tools.registry.set_sim_date()`) so agents
cannot pass a wrong `as_of`.

## Agent layer (LangGraph)

```python
class Action(BaseModel):
    action_type: Literal["adjust_forecast","place_po","reschedule_po","substitute_supplier",
                         "add_special","86_dish","substitute_ingredient","prep_list",
                         "discard_lot","flag_variance"]
    payload: dict
    rationale: str
    agent: str
    est_cost: float = 0.0

class AgentReport(BaseModel):
    agent: str
    summary: str
    actions: list[Action]
    findings: list[str]

class OpsState(TypedDict):
    run_id: str
    sim_date: str
    trigger: dict                    # {"kind": "morning"} | {"kind": "event", "event": {...}}
    forecast: dict                   # final (possibly adjusted) forecast numbers from tools
    reports: Annotated[dict[str, AgentReport], merge]
    proposed: Annotated[list[Action], add]
    validated: list[dict]            # action + violations + needs_approval
    approvals: list[dict]            # chef decisions
    executed: list[dict]
    agents_to_run: list[str]         # supervisor plan
    step: int
```

Graph: `sense → supervisor → [forecast_analyst → inventory → purchasing → menu → prep]`
(supervisor decides the plan: full order on morning trigger; on event trigger the LLM
supervisor picks the minimal set of agents to re-run, bounded by `MAX_SUPERVISOR_STEPS`)
`→ constraint_check → approval (langgraph interrupt() for items over threshold) → execute → END`.

- Each specialist = `create_react_agent` subgraph with its TOOLSET, `recursion_limit` step cap,
  and structured `AgentReport` output. Its tool calls are written to `trace`.
- Downstream agents receive upstream reports in their prompt (forecast → inventory → purchasing → menu → prep).
- `constraint_check`: runs `check_action` on every proposed action; `block` violations →
  status `blocked` (logged, fed back to supervisor once for a retry), otherwise mark
  `needs_approval` if: `place_po` total > `APPROVAL_PO_THRESHOLD` (default $1,500),
  any `add_special`, `86_dish`, `substitute_ingredient`.
- `approval`: `interrupt({"pending": [...]})`; resumed with `Command(resume={"decisions": [{id, approved, note}]})`.
  Past approvals/rejections are loaded into agent prompts as context. CLI `--auto-approve` resumes automatically.
- `execute`: applies approved actions to the world via simulator APIs (send PO, update menu_state,
  publish prep list), logs `decisions` with rationale.
- Checkpointer: `SqliteSaver` at `data/checkpoints.db`.
- `LLM_MODE=live|mock`. Mock mode swaps each specialist for `mock_policies.py` rule-based
  functions with the same `AgentReport` output, so the full loop runs without an API key.
- LLM: `init_chat_model(settings.LLM_MODEL)`; default `anthropic:claude-sonnet-5-5`; swap to
  `ollama:...` etc. by env var only.

## Simulator

`World(conn, run_id, policy)`: `today`, `advance_day()` (realise demand from hidden truth
generator for that date, deplete stock FEFO, record stockouts/86s, age lots → expired → waste_log,
deliver due POs with supplier fill rate & delay prob), `inject(event)`.
Agent and baseline runs each work on a **copy** of the seeded DB so they face the identical world
(same seed for realised demand).

Disruptions (`disruptions.py`), each `{"kind", "day_offset", "params"}`:
1. `supplier_shortfall` — salmon supplier short for Friday delivery (fill 30%).
2. `private_booking` — 40-cover private dinner Saturday.
3. `cooler_failure` — discard all dairy lots (Wednesday).
4. `heatwave` — temps 34 °C Thu–Sat → cold-dish demand shift.

`baseline.py`: fixed par levels (par = mean daily usage × (lead time + 2)), order-up-to every
morning from the cheapest supplier, no specials, no re-planning.

## UI

The Streamlit app (`ui/`) is **retired** and no longer in the repository; a leftover local `ui/__pycache__` folder
can be deleted. The UI is the React app in `frontend/` (see "Real-time layer" and "Round 2" below). The batch week
is run from the CLI (`python -m restaurant_ops.sim.run`), and its agents-vs-baseline result is shown on the
dashboard's Insights tab.

## Config defaults (`config.py`)

`SEED=42, DB_PATH=data/restaurant.db, MODELS_DIR=data/models, SIM_START=2026-11-02,
SEATS=80, APPROVAL_PO_THRESHOLD=1500, DAILY_BUDGET=4000, MAX_AGENT_STEPS=8,
MAX_SUPERVISOR_STEPS=3, LLM_MODE=mock|live (default live if ANTHROPIC_API_KEY set else mock),
LLM_MODEL=anthropic:claude-sonnet-5-5, LANGSMITH_TRACING optional`.

## Build phases

1. Foundation — pyproject, config, db, catalog, generator, seed.
2. Deterministic core — forecasting, inventory, optimizer, constraints (+tests written), menu, prep, tools registry, FastAPI.
3. Agents — LangGraph supervisor, specialists, mock policies, approval interrupt, decision log.
4. Simulator — world, disruptions, vendors, baseline, metrics, run CLI.
5. UI + README.

Build rule: **no tests, scripts or the app are run during the build.** Only dependency installs
(`uv add`/`uv sync`) and `python -m py_compile` syntax checks are allowed.

## Build notes (first build, historical)

Phase 1 (Foundation) deviations / additions:

- `pyproject.toml` created with `uv init --bare` (no build backend, no `[project.scripts]`); run modules from the repo root (`python -m restaurant_ops...`). pytest `pythonpath = ["."]`.
- Extra config fields beyond the DESIGN defaults: `HISTORY_DAYS=365`, `DEMO_DAYS=7`, `MEAL_PERIODS`, `SEAT_TURNS`, `STATIONS`, `PAYDAY_DAYS` (15 and -1 = month end), `DATA_DIR`, `CHECKPOINT_DB`, `ANTHROPIC_API_KEY`, `LANGSMITH_*`; helpers `settings.history_start`, `settings.seat_cap(period)`.
- `suppliers.order_cutoff_hour` has a default of 14 in SQL; supplier ids are `sup_freshcatch`, `sup_metro`, `sup_greenvalley`, `sup_budget`. Ingredient/dish ids are snake_case names (e.g. `salmon`, `seared_salmon`).
- Station capacity/staffing live in code (`catalog.DEFAULT_STAFFING`, `station_capacity_minutes`, `STATION_CAPACITY`), not in the DB; actual rostered staff per day is in `staff_shifts`. Capacity = staff x period minutes (lunch 180, dinner 240, late 150) x 0.8.
- `dishes.active` is 1 for all; `menu_state` starts empty (absence = active).
- Truth model lives in `data/generator.py` (`true_covers`, `true_dish_mix`, `realise_day`, `expected_covers`, `dish_rates`). Holidays are looked up from a calendar function inside the truth model (so event rows of type `holiday` are informational and must not be passed again); sports/local events are passed in as event dicts; private parties via `extra_covers`. Heat affects the dish mix only (not covers).
- Demo-week reservations are a partial stream (only bookings created before SIM_START are visible). No demo-week `covers`/`pos_sales` exist; the simulator realises them via `realise_day`, seeded per date (`day_rng`).
- Opening lots (as of SIM_START) are deliberately near expiry for `cod`, `cream`, `mozzarella`, `spinach`, `mushroom`; `ribeye` has a persistent ~6.5% shrinkage in `stock_counts` (variance detection). Historic stock_counts `system_qty` is a synthetic sawtooth, not tied to `lots`.
- Tests/scripts were not run; only py_compile checks.

### Phase 2 (Deterministic core + Tools layer)

Added files: `core/util.py` (date/conn/rounding helpers), `core/{forecasting,inventory,optimizer,constraints,menu,prep}.py`, `tools/{registry,api}.py`, `tests/test_constraints.py` (written, not run). Nothing was run; only `py_compile`.

- **Extra optional params**: every core function takes `conn=None` (falls back to `get_conn()`); `as_of`/`start_date` default to `SIM_START` when None. `forecast_*` take `adjust=True` (applies approved/executed `adjust_forecast` rows from `decisions`) and `adjustments=[{date, meal_period|'all', factor}]`.
- **Forecasting**: one covers quantile model per alpha over all periods (period is a feature), plus a single dish *share* regressor (dish as a feature, shares renormalised per date/period, out-of-season = 0); dish p10/p90 add a Poisson spread (+/-1.28 sqrt(mean)). Train = first 335 days, test = last 30 days; the pickled production models are then refit on all 365 days, while `metrics` and `signals` come from the train-only models. Reservation feature counts only bookings created >=1 day before the service date (the demo week's partial stream is therefore slightly lower than history; accepted). Pickle: `data/models/forecast_models.pkl` (bundle: models, ppc, metrics, signals). `signal_report` rows: `{signal, effect_pct, importance_pct, significant, n_active_rows, scope, note}`; significant = |effect|>=3% AND positive permutation importance (>=2 sd). Signals with <3 active rows in the test month (e.g. heat) fall back to the full history (flagged `scope=full_history`, in-sample). Heat's effect is measured on the cold-dish share, not covers. Naive (DOW x period) fallback when lightgbm is unavailable.
- **Inventory**: lots are usable through `expiry_date` inclusive. `expected_usage` also drops dishes 86'd in `menu_state`; `dish_overrides` = `{dish_id: multiplier}`. `variance_report` returns only flagged rows unless `include_ok=True`; flags: `shrinkage` (mean <= -4% and >=70% of days negative), `over_portioning` (-2..-4%), `surplus` (>= +4%). `consume_fefo(conn, ingredient_id, qty, as_of, commit=False)` -> `{picked, consumed, shortfall, cost}` (caller commits); `on_hand(ingredient_id, as_of, conn)`.
- **Optimizer**: planning window is `as_of .. as_of+horizon_days` (horizon+1 days) so a supplier with lead time = horizon is still useful. Cumulative-by-day shortage constraints make delivery timing matter; shelf life caps per-delivery and total buy; default budget = `DAILY_BUDGET x 2`. Return adds `shortfalls` and `horizon`; `status` in `optimal|feasible|greedy`; `solver='greedy'` forces the heuristic.
- **Constraints**: budget rules are `budget_daily` (POs created today + new <= DAILY_BUDGET x 2) and `budget_weekly` (7 days <= DAILY_BUDGET x 7). `needs_approval` also covers `substitute_supplier` above the PO threshold. `Violation.to_dict()` added; `blocking()` helper. Shelf-life uses a 28-day POS average (no model call), tolerance x1.25 + one pack.
- **Prep**: 86'd dishes are dropped; specials are kept (flagged `special`) rather than subtracted, since their portions come out of the same forecast. `prep_minutes` in the output is the line total (portions x per-portion minutes).
- **Registry**: tools are LangChain `StructuredTool`s; `TOOLSETS` values are lists of tool objects (`TOOLSET_NAMES` has the names); `FUNCTIONS` maps name -> plain function (also holds `send_po`, `check_action`, `needs_approval`, `forecast_accuracy`, `menu_status`, which are in no toolset). Extras: `set_db_path()` (point at a sim DB copy), `clear_cache()` (call after the world mutates), `use_sim_date()` context manager, `connection()`. Cache key = name + args + sim date + db path; `negotiate`/`send_po`/`check_action` are not cached. `reservations_summary` only shows bookings with `created_date <= sim date`.
- **API**: extra endpoints `/sim_date` (POST), `/execute/send_po`, `/check_action`; read endpoints `/decisions`, `/trace?since_id=`, `/kpis`.

### Phase 3 (Agent layer)

Added `agents/{llm,state,prompts,base,decision_log,mock_policies,forecast_analyst,inventory_agent,purchasing_agent,menu_agent,prep_planner,supervisor}.py` and `/cycle/run`, `/cycle/resume`, `/cycle/pending` in `tools/api.py`. Nothing was run; only `py_compile`.

- **Public API** (`agents/supervisor.py`): `run_cycle(run_id, sim_date, trigger, db_path=None, auto_approve=False, thread_id=None, checkpointer=None) -> dict` (final state + `status:"completed"`, `thread_id`; or `{"status":"awaiting_approval","pending":[...],"thread_id",...}`), `resume_cycle(thread_id, decisions, db_path=None, checkpointer=None)`, `get_pending(thread_id, db_path=None, checkpointer=None) -> list`, `build_graph(checkpointer=None)` (None = process-wide `SqliteSaver` at `CHECKPOINT_DB`). Pending items: `{id (decision id), agent, action_type, payload, rationale, est_cost, warnings}`; resume with `[{"id","approved","note"}]`.
- **Trigger shapes**: `{"kind":"morning"}` or `{"kind":"event","event":{"kind":K,"params":{...}}}`. K and params (all optional, policies look missing values up in the DB): `supplier_shortfall` {supplier_id, ingredient_id?, fill_rate? (default 0.3)}; `private_booking` {date, meal_period, party_size}; `cooler_failure` {ingredient_ids? | lot_ids? | category? (default dairy)}; `heatwave` {start_date?, days?, temp_c?}. A flat event dict (params at event level) is tolerated. `thread_id` = `{run_id}:{sim_date}:{morning|<event kind>}` (event kind, not just "event"); re-running the same thread resets state in `sense`.
- **State**: values are plain dicts (`AgentReport.model_dump()`), not Pydantic objects, so the SQLite checkpointer can serialise them. `proposed` and `executed` use `merge_proposed`: list = append, `replace_agent(name, actions)` = replace that agent's earlier actions (`"*"` clears). `reports` is a dict merge with a `{"__reset__": True}` key. Extra state keys: `db_path`, `context`, `sup_steps`, `retries`, `retry_agents`, `violation_feedback`, `plan_reason`; `step` is the cursor into `agents_to_run`. `state["forecast"]` = `{start_date, days, covers, dish_need_today, adjustments}` (`dish_need_today` = sum of forecast p50 per dish, used for 86 decisions); `sense` fills a baseline, the Forecast Analyst overwrites it.
- **adjust_forecast is applied early**: the Forecast Analyst node checks each `adjust_forecast` with `check_action` and, if not blocked, logs it as an `executed` decision immediately (and clears the tool cache) so downstream agents' forecast tools already include it. Blocked ones go through normal constraint_check/retry. It never needs chef approval.
- **Prompts**: system prompt per agent rendered with `<<PLACEHOLDERS>>` (str.replace) containing upstream reports, trigger, forecast, chef feedback and constraint feedback; the human message is a short task line. Live agents: `create_react_agent` (deprecated alias in langgraph 1.x, still exported by `langgraph.prebuilt`) with `response_format=AgentReport` (a separate structured-output call after the tool loop); `recursion_limit = MAX_AGENT_STEPS*2+3`. Errors become a report with the error in `findings` and no actions.
- **Retry**: constraint_check sends blocked proposals back to the supervisor once (only if `sup_steps < MAX_SUPERVISOR_STEPS`); only agents with blocked actions re-run, with their violation messages in the prompt (mock policies switch to a fallback path: purchasing re-optimizes without the failing supplier, prep uses p10). Decisions are written only on the final pass (statuses blocked / pending_approval / proposed); approval sets approved/rejected (+ chef note, default "approved"/"rejected"/"auto-approved"); execute re-checks every action, then applies it and sets `executed`.
- **Execute semantics**: place_po/substitute_supplier -> `registry.send_po` (substitute cancels the original PO only if `payload.cancel_original`); reschedule_po -> UPDATE purchase_orders; add_special/86_dish -> `menu_state` (note holds portions/meal period); discard_lot -> `waste_log` + lot `discarded` (partial qty reduces the lot); prep_list/adjust_forecast/flag_variance/substitute_ingredient -> decision row `executed` only.
- **Trace**: `decision_log.install_trace_hook()` writes tool_call/tool_result rows (compact) attributed to the current run/node via a context var; node-level rows: agent_start/agent_end, supervisor, constraint, approval, execute, and `event` (sense). `recent_feedback(n)` returns approved/rejected decisions (and executed ones with a chef note).
- **Mock policies** are plain code (they do arithmetic such as the private-booking factor = min(p50 + party, cap) / p50, specials sizing, overload scaling); only the live LLM agents are bound by "no arithmetic". `purchasing` mock calls `negotiate` and uses a counter price only if the vendor result has a numeric `counter_price|unit_price|price|offer` below list (vendors module is Phase 4).
- Open PO shortfall handling assumes the Phase 4 simulator leaves the failing supplier's PO `sent`; the mock quotes the missing share (1 - fill_rate) from the best other supplier via `supplier_quotes`.

### Phase 4 (Simulator, disruptions, vendors, baseline, metrics, run CLI)

Added `sim/{vendors,disruptions,simulator,baseline,metrics,run}.py`. Nothing was run; only `py_compile`.

- **Schema additions** (`CREATE TABLE IF NOT EXISTS`, `World.__init__` calls `init_db` so older DBs get them): `forecast_log(run_id, policy, date, meal_period, p10, p50, p90, actual)` (daily covers forecast vs realised covers, for MAPE / P90 coverage) and `supply_overrides(id, supplier_id, ingredient_id NULL, start_date, end_date, fill_rate, note)` (shortfall fill-rate overrides; read by the delivery step and by the mock vendors).
- **World**: `World(db_path, run_id, policy, seed=None)` with `today`, `pending_events`, `load_scenario(name)`, `due_events()`, `inject(disruption) -> {disruption, result, trigger}`, `create_po(...)`, `advance_day() -> summary`, `trace(...)`, `close()`. Resumes at the day after the last `kpi_daily` row of the run. Day order: deliveries (usable the same day) -> realise demand -> forecast_log -> menu_state -> service -> expiry -> stock_counts -> kpi_daily. `covers` / `pos_sales` get the sim days too (`covers` = realised demand covers, `pos_sales` = portions actually made); trained models are pickled so this does not affect forecasting.
- **Randomness**: demand = `realise_day(..., seed)` (per date); delivery delay is one draw per (seed, date, supplier), line fill is a draw per (seed, date, supplier, ingredient) with the history model `min(1, fill_rate + N(0.03, 0.06))`, <0.98 -> partial (floor 40%). A PO is delayed at most once (in-memory `_delayed`).
- **Menu effects**: 86'd dish -> its demand is lost, 40% of it shifts to other dishes of the same category; special -> +20% sales (capped by "N portions" in the note) taken from the other dishes (covers unchanged). A period restriction is read from "(lunch|dinner|late)" in the menu_state note, else all periods. Stock is allocated round-robin across dishes so none hogs a shared ingredient; `stockouts` = (meal period, dish) instances that ran out; `dishes_86d` = dishes 86'd in menu_state that day; `covers_served` = covers x made/demanded portions; `approvals` = decisions of the run/day with a non-null chef_note (they went through the queue).
- **Disruptions** (timeline from SIM_START Monday): Wed cooler_failure (all dairy lots -> waste_log `spoilage`, lots `discarded`; params get `ingredient_ids`/`lot_ids`), Wed heatwave (weather rows Thu-Sat 34 C alert `heatwave` + weather event rows; written Wednesday so the agent's forecast sees them), Thu supplier_shortfall (override: sup_freshcatch salmon fill 30% on Fri delivery; PO stays `sent`), Thu private_booking (40 covers Sat dinner: private reservation + private event; extra covers are derived from `kind='private'` reservations when the day is realised). Disruptions land after the morning plan of their day. Salmon supplier = shortest lead time then cheapest for `salmon` (sup_freshcatch).
- **Vendors**: personas per supplier (max discount, volume threshold, availability); `negotiate` is deterministic (seeded mood jitter) and caps `qty_available` by an active shortfall override; `counter_price` is numeric and below list whenever a discount is possible. `vendor_message(supplier_id, kind, ingredient_id, fill_rate, delivery_date)` writes the shortfall/delay notice (also stored in the trace by the shortfall disruption).
- **Baseline**: supplier = cheapest that can deliver fresh (lead time <= max(1, shelf_life/2)); reorder when position < usage x (lead+1), order up to usage x (lead+2); packs rounded up; supplier minimum met by scaling up (skipped if >2.5x). Writes `decisions` rows (agent='baseline', status `executed`).
- **Metrics**: `kpis(db, run_id, policy)` adds `lost_revenue` (from day_summary trace rows), `food_cost`, `purchases`, `days`, `daily`; `waste_pct` = waste / value received from POs created in the sim. `mape`/`p90_coverage` come from `forecast_log`, which stores the forecast at the start of each service (after the morning cycle and any event cycle, so approved `adjust_forecast` decisions are included). `compare()` returns `agent`, `baseline`, `delta`, `winner`; `format_comparison()` renders a text table.
- **Run/session**: `SimSession(policy, scenario, run_id, seed, days, auto_approve, source_db)` with `start()`, `step_day()`, `pending_approvals()`, `resume(decisions)`, `run_day(prompt)`, `run_all(prompt, on_day)`, `kpis()`, `close()`; every call re-points the registry DB path and sim date at its own copy, so two sessions can coexist. `run_simulation(...)` runs agent then baseline sequentially; the DB copies are `data/runs/{run_id}_{policy}.db`. Both policies share the run_id (separate DBs).

### Phase 5 (Streamlit UI + README) - historical, the Streamlit UI is retired

Added `ui/app.py`, `ui/components.py`, `README.md`. Nothing was run; only `py_compile`.

- **Threading**: every `SimSession` call (start/step_day/resume/close) runs on one dedicated worker thread (`st.cache_resource` `ThreadPoolExecutor(1)`), because `World` SQLite connections and the LangGraph SqliteSaver are thread-bound and Streamlit may rerun on different threads. The UI reads run DBs through short-lived own connections (WAL); `commit_all()` commits the worlds before KPI reads.
- **"both" mode**: sessions `{"agent","baseline"}` in `st.session_state`, same run id/seed/scenario. Stepping drives the agent first; when its day completes the baseline steps the same day. If the agent pauses (`awaiting_approval`), the baseline waits. Step/Run buttons are disabled while awaiting; Run to end stops at the first pause.
- **Approvals**: no programmatic tab switch in Streamlit, so awaiting state shows a top banner, toast, sidebar alert and a "Approvals 🔔 n" tab label. Submit calls `session.resume(decisions)` on the worker and continues the day (baseline step follows on completion).
- Day summaries come from the in-memory `SimSession` results (`step_day()["summary"]`); disruptions from `trace` rows `node='sim'`, `event='disruption'`; trace cycles are grouped by `sense` events per sim date.
- Forecast tab opens its own connection to the run DB (no registry mutation) and falls back to `settings.DB_PATH` + `SIM_START` before a run starts. Kept `restaurant_ops` imports lazy where they pull heavy deps.
- UI and run controls are locked while a run exists; use Reset for new config. Existing run DB files of the same run id are picked up (read-only views) after a page reload only if the id matches the sidebar value; sessions themselves are not restored after a reload.

### LLM providers (post-build change)
- `LLM_PROVIDER=meta|openai|anthropic|ollama`, default **meta** (Meta Llama API through its OpenAI-compatible
  endpoint `META_BASE_URL`, default `https://api.llama.com/compat/v1/`, model `META_MODEL`).
  Per-provider keys and default models live in `config.py`. `LLM_MODEL` optionally overrides the model, as a bare
  name or as `provider:model`. `LLM_MODE` auto-resolves to live when the selected provider has a key.
- Meta and OpenAI use `ChatOpenAI` (`langchain-openai`), with structured output forced to `function_calling`,
  because `AgentReport.payload` is a free-form dict that strict `json_schema` mode rejects. OpenAI reasoning models
  (o-series, gpt-5*) are created without `temperature`.


## Real-time layer (restaurant_ops/live)

The batch graph above plans a day. The live layer runs a dinner service minute by minute on one event bus.

### Event contract

Every event is `{seq, type, source, sim_ts, wall_ts, payload}` and is appended to `event_log` before dispatch.
Types (live/events.py): `order.submitted`, `order.created`, `item.fired`, `item.done`, `item.held`, `stock.low`,
`stock.out`, `lot.expiring`, `lot.quarantined`, `inventory.snapshot`, `supplier.shortfall`, `po.sent`,
`forecast.updated`, `forecast.miss`, `weather.alert`, `menu.updated`, `kitchen.load`, `quote.updated`,
`online.order`, `online.mode`, `table.updated`, `party.arrived`, `party.left`, `decision.proposed`,
`decision.resolved`, `service.started` (carries the full initial snapshot), `service.ended`, `clock.tick`,
`kpi.updated`, `scenario.injected`, `agent.log`.

State-bearing events carry snapshots (tables, kitchen load, inventory, menu, online orders, decisions, KPIs), so
`replay.fold(events)` and the UI reducer (`frontend/src/store.js`) rebuild the service from the log alone.

### Bus

`live/bus.py`. `RedisStreamsBus` XADDs to `ro:events:{service_id}` and XREADs back in order. `InProcessBus` uses
a deque. `make_bus` picks Redis when `EVENT_BUS=auto` and Redis answers. Dispatch is synchronous and non-recursive:
events published by handlers are queued behind the current one. A failing subscriber is logged in `bus.errors` and
does not stop the others.

### Decisions

A proposal is `{agent, action_type, payload, title, reason, trace, tools, confidence, key, timeout_default}`. The
coordinator dedupes by `key`: a duplicate while one is pending is merged, and one resolved within 20 sim minutes
is suppressed unless `force` is set. Then it runs a LangGraph per decision: `check` (core/constraints.py + conflict
rules) -> `approval` (`interrupt()`, resumed with `{approved, note, payload?}`) -> `verify` -> `execute`. New
action types checked by core/constraints.py: `discard_lots`, `throttle_online`, `allergen_swap` (a replacement may
never contain a declared allergen), and `comp`.

Live approval policy: specials, 86s, allergen swaps and comps always need the manager. POs need approval above
`LIVE_PO_APPROVAL_THRESHOLD`, discards above `LIVE_DISCARD_APPROVAL_VALUE`, and throttles whenever the mode is not
`open`. A timeout after `APPROVAL_TIMEOUT_S` wall seconds (while the clock runs) resolves the decision with
`timeout_default`.

### Time

`ServiceClock` runs the dinner from 17:00 to 22:30 at 0/1/10/60x. `Service.advance(wall_dt)` steps in at most
15 sim seconds. The kitchen computes exact start and done times inside a step, so the step size does not change
results. A headless run (`run_headless`) uses the same code without sleeping.


## Round 2: operations depth (restaurant_ops/live, core, data)

### Sixth agent: prep & staffing (`live/agents/prep.py`)
- Mise en place = the forecast prep list (`core/prep.py`) at service start. `Kitchen.fire_hook` consumes one
  prepped portion per fired item; with none left the item is cooked from scratch (`SCRATCH_PENALTY` x cook time),
  and `Kitchen.scratch_check` makes quotes reflect that.
- `prep_batch` (auto, stock-checked) every `PREP_REVIEW_EVERY_MIN` when prepped < 50% of what is still forecast.
- `add_staff` (manager approval) when a station waits >= `SURGE_WAIT_MIN` for `SURGE_STREAK_MIN` minutes; the
  constraint checker enforces `SURGE_LABOR_BUDGET` and `MAX_EXTRA_COOKS`. The cook arrives after
  `CALL_IN_DELAY_MIN` (`Kitchen.set_cooks`); `release_staff` (auto) after `RELEASE_CALM_MIN` calm minutes.

### Procurement: negotiation and supplier learning
- Emergency PO lines worth >= `NEGOTIATE_MIN_LINE` go through `sim/vendors.negotiate` (target
  `NEGOTIATE_TARGET` x list); the counter price is used and recorded in the decision's context.
- `core/suppliers.py`: `scorecard` blends catalog fill rates with PO history at service start; `observe` folds
  each shortfall / delivery into `suppliers.fill_rate` / `delay_prob` (`supplier_perf` keeps the history). Quotes
  and the optimizer read those columns, so unreliable suppliers carry a higher risk premium.

### Auth and roles (`live/server.py`)
Replaced in round 3 by named users (see "Round 3" below). The role tokens and `?token=` links are gone.

### Persistence (`live/persistence.py`)
Coordinator checkpoints: LangGraph `SqliteSaver` in `{service_id}.ckpt.sqlite`. Service state (world, agents'
private state, decisions) is pickled to `{service_id}.state.pkl` every `SNAPSHOT_EVERY_MIN` service minutes and on
every decision event. `Service.resume(service_id)` rebuilds the runtime (bus, agents, coordinator) around the
restored world; pending approvals resume where they paused. The server resumes the newest `running` service on
start (`RESUME_ON_START`; `--fresh` to start over).

### Host stand (`live/host.py`)
Bookings (`bookings` table) with create / cancel / no-show / arrive; simulated bookings come from tonight's
`reservations` and arrive or no-show by themselves. Overbooking: covers booked within +/-45 min may not exceed
seats x `OVERBOOK_FACTOR` unless a manager allows it. Waitlist with quoted waits (expected time until a fitting
table frees, by table state, after the parties ahead). Table operations: move, combine, split.

### Payments and delivery (`live/payments.py`)
Checks open when a party asks for the bill; due = subtotal - discounts - comps + tax (`TAX_RATE`); even or by-seat
splits; tips; discounts (<= `DISCOUNT_MAX_PCT`, reason required) and refunds (<= paid) go through the constraint
checker; every movement is a row in `payments`. A party leaves when its check is paid. Online orders are prepaid;
deliveries add the zone fee (`DELIVERY_ZONES`) and are dispatched to the first free of `DELIVERY_DRIVERS`
drivers; their quote adds driver wait + drive time and accuracy is measured at the door.

### Guest profiles (`live/crm.py`)
`guests` (persistent allergies, preferences, visits, spend, points, tier) and `guest_visits`. Orders linked to a
guest (booking, phone on the online page or the table dialog) carry the profile's allergies into the Order
agent's allergen check automatically. Paying a check records the visit and loyalty points.

### Food safety and waste (`live/safety.py`)
Simulated sensors log walk-in / freezer / hot-holding temperatures every `TEMP_LOG_EVERY_MIN` minutes
(`temp_log`); an out-of-range reading publishes `temp.alert`, and for the walk-in the Inventory agent quarantines
the dairy and requests a discard (the cooler-failure scenario now goes through the sensor). HACCP checklist
(`haccp_checks`): sensor items complete themselves, the rest via the kitchen role; overdue items are logged.
Expiry alert lead time: `EXPIRY_ALERT_DAYS`. Late in service the Inventory agent proposes donating safe lots that
expire today and are not needed (`donate_lots`, manager approval, `donations` table; not counted as waste).
`waste_analytics` breaks waste down by reason, ingredient, category and day.

### Menu engineering (`core/menu_engineering.py`)
Kasavana-Smith mix x margin classes (star / plowhorse / puzzle / dog), price suggestions under an explicitly
assumed elasticity (the synthetic history never varies prices), and seasonal rotation from `seasonal_months`.
At service start the Menu agent proposes up to two `price_change` decisions (approval; effective next service).

### POS import (`data/importer.py`)
CSV (one row per item sold) -> `covers` / `pos_sales`, with item mapping (file, id, name, fuzzy), meal period from
the time, covers from guests per check, an unmapped-item report and a dry run; the forecast pickle is removed so
the models retrain on the imported history.

### Operability (`live/ops.py`)
JSON logs (`LOG_JSON`), Prometheus `/metrics`, retention of live and batch-run DBs (`LIVE_RETENTION_DAYS`,
`LIVE_KEEP_LAST`; runs at server start, CLI `python -m restaurant_ops.live.ops`). Every live tunable lives in
`config.Settings`. Approval timeouts count service minutes (`APPROVAL_TIMEOUT_SIM_MIN`), so a paused clock freezes
them. If Redis fails mid-service the bus degrades to the in-process queue (`kind` = `redis-degraded`); the SQLite
event log is unaffected.

### Frontend
3D dining floor (three.js, lazy-loaded; `floor3d.js` holds the tested scene math): guests walk in through the
door, sit at their chairs, wait by the host stand and leave when their table is bussed; 2D floor kept as a tab.
Dashboard tabs: Operations, Front of house (bookings, waitlist, table ops, checks, guests), Kitchen & safety
(prep & staffing, HACCP, inventory, waste) and Insights (forecast, report, menu engineering, suppliers, baseline).
Approval alerts (title badge, chime, desktop notification), role sign-in, an end-of-service report (printable HTML
and CSV), vitest unit tests (`npm test`).


## Round 3: setup, money edges, identity, operations, LLM pilot

### Configuration lives in the master DB (`admin.py`, `db/migrations.py`)
New tables in `data/restaurant.db`:
- `config_kv` (profile), `tax_rules`, `opening_hours`, `floor_tables`, `stations`, `delivery_zones`, `staff`, `roster`
- identity: `users`, `sessions`, `audit_log`
- money and outputs: `gift_cards`, `gift_card_ledger`, `idempotency`, `day_close`, `po_outbox`, `kds_bumps`
- `llm_calls`, `schema_version`

`migrations.migrate` adds columns to older databases (`dishes.tax_category`, `suppliers.email`,
`payments.processor_ref`). `admin.seed_defaults` fills empty tables from the built-in world, and is idempotent.

Each service copies the master at start, then runs `apply_config`. `catalog.reload_from_db` rebuilds the module-level
catalog in place: active dishes and recipes, suppliers, stations with cook counts, items per cook and cook factor.
`forecasting.refresh_catalog` appends new dishes so model indices stay stable, and `DISH_VIEW` is rebuilt. Edits
therefore apply from the next service. A running dinner never changes under the kitchen, which keeps state
consistent without a migration story for live objects. Writes validate first, for example a price above the
recipe's food cost, a valid station, and no deleting a station that active dishes still use.

### Money
- **Taxes:** `Payments.tax_lines` applies each active rule to the check pro rata by line category (food, alcohol,
  delivery fee) and channel. One line per rule appears on the check and in the day close.
- **Discounts:** the cap per role comes from the profile (`discount_max_pct`) and is passed to the constraint
  checker.
- **Tenders:** `_take_money` routes each method:
  - card, wallet and online go to the processor adapter (`live/processor.py`: simulated, or Stripe PaymentIntents
    over httpx with idempotency keys)
  - gift cards are redeemed in the master DB with a ledger row
  - points are spent through the CRM
  - cash needs no external call

  Refunds go back the way the money came. Gift cards and users live in the master DB because they outlive a service.

### Supplier channel, printer, KDS
- **POs:** an approved PO (`_h_po`) is rendered by `po_channel.text_pdf`, a minimal PDF 1.4 writer with no
  dependency, plus an RFC 5322 e-mail. It is sent over SMTP when configured, otherwise left in the outbox as queued.
  A failed send keeps status `failed` with the error so it can be resent.
- **Printer:** `outputs.KitchenPrinter` subscribes to `order.created` and prints one chit per station, to a file or
  as ESC/POS over TCP. A printer failure becomes an `agent.log` entry and never blocks the order.
- **KDS:** tickets come from the kitchen items, grouped by order and station. Bumps are rows in `kds_bumps`.

### Identity
`live/auth.py` handles sign-in:
- **Passwords:** PBKDF2-SHA256 with 240k iterations.
- **Sessions:** only `sha256(token)` is stored.
- **Lockout:** `LOGIN_MAX_FAILURES` failures within `LOGIN_LOCK_MIN` minutes, per username.
- **Sign-out:** a password change, deactivation or role change deletes the user's sessions.
- **Last admin:** the last admin can't be demoted or deactivated. Every update validates before it writes.

The server takes a cookie (`ro_session`, HttpOnly, SameSite=Strict, Secure with `COOKIE_SECURE`) or a Bearer header.
Cookie writes need `X-Requested-With: ro-ui`, which a cross-site form can't set. Dependencies `need(roles)` set
`request.state.user`, and an HTTP middleware audits every `/api` write (refused ones too), masking password and
token fields.

Reads and `/ws` need a user. Two channels stay public:
- the online-order REST endpoints
- `/ws/public`, which passes only menu, portions, kitchen busyness, online mode, clock and order progress, with
  customer, guest and check ids stripped (`public_event`)

### Operations
- **Idempotency:** `idempotent()` stores the response per `Idempotency-Key` and endpoint in the master DB. The UI's
  offline queue (`frontend/src/offline.js`) retries table orders with the same key, in order, stopping at the first
  failure it can retry. A 4xx drops the item and reports it.
- **Snapshots:** version 2, with a SHA-256 sidecar and rotation (`SNAPSHOT_KEEP`). `migrate` upgrades v1 blobs.
  `load` falls back to older snapshots, and the server then falls back to `Service.recover_from_log`, which
  continues on the service DB (stock, menu, bookings, payments) and rebuilds decisions from `decision.*` events.
  Pending approvals stay pending.
- **Backups:** `ops.backup` copies the master and service DBs (SQLite online backup) with a manifest. `drill`
  restores into a temp directory, runs `integrity_check` and table checks, and unpickles the newest snapshot.
- **Day close:** `dayclose.reconcile` and `journal` produce balanced debits and credits (cent rounding goes to
  6900), written as CSV and JSON and stored in `day_close`.
- **Meal periods:** `Service(period, service_date)` reads opening hours, and a closed period raises.
- **Tool registry:** the global registry is re-bound to the calling service on every tool call
  (`ToolBox.bind`), so two services in one process can't read each other's database.

### LLM pilot (`live/llm_guard.py`, `live/llm_eval.py`)
`LLMGuard.allow()` gates every review on mock mode, the breaker, the call cap and the cost cap. `record()` logs
latency, tokens and cost to `llm_calls` and trips the breaker after N failures in a row. A refused or failed review
submits the rule-based proposals with the reason. `llm_eval build` turns logged manager decisions on reviewed
proposals into a JSONL eval set, and `run` scores a judge on it:
- agreement
- rejections caught
- false drops
- p50 / p95 latency
- cost

The mock judge (keep everything) is the baseline to beat.

### Floor and lobby
The plan is 1000 x 700, with a waiting area by the entrance. Parties wait there:
- every arriving party, for 2–5 minutes while checking in
- an early reservation, until about 2 minutes before its booked time (at most 15 minutes)
- 30% of walk-in groups of 3 or more, until the rest of the party arrives (3–10 minutes)
- anyone with no free table, as before

`waitlist_view` gives the reason. The 3D view queues parties in rows in the lobby with a name tag, and the 2D view
draws them in the dashed waiting area.


## Gap fixes after round 3

- **Decisions of any era.** `coordinator.normalize_decision` brings any decision to the current shape: older
  `timeout_s` / `remaining_s` countdowns, a missing `key`, `needs_approval` or `context`. Event-log recovery and
  snapshot migration both call it. A legacy pending approval keeps the share of time it had left, and recovery counts
  the service minutes since the proposal.
- **No silent no-ops.** The coordinator blocks, at the check step, any action type the live service can't execute
  (`no_executor`). It no longer records a missing executor as done.
  - `live/handlers.py` holds an executor for every type in `core.actions.ACTION_TYPES`, except discount and refund,
    which run inside payments.
  - The new live executors:
    - `reschedule_po` (needs approval)
    - `substitute_ingredient`: tonight's recipe, stock bill of materials, catalog and allergen checks change
      together
    - `prep_list`: tonight it sets new pars and batches the difference; for a later service it saves the plan in
      the master `config_kv`, and that service starts from it
  - `adjust_forecast` is also written to the master database. Tomorrow's service is copied from it, so the
    adjustment used to be lost. The latest live adjustment per date and period replaces the earlier one, because
    they multiply.
  - A test asserts both runtimes cover every action type.
- **One implementation per action.** `core/actions.py` holds the database effects for both the batch supervisor
  and the live handlers, which fixed live `substitute_supplier` not cancelling the PO it replaces. The two agent
  runtimes stay separate on purpose: the batch day planner drives the baseline comparison, while the live agents
  react to events. The unmounted batch HTTP app (`tools/api.py`) was removed.
- **Docs and config agree.** A test checks that every setting named in the README and `.env.example` exists. The
  server logs a warning at start for removed settings still present in the environment or `.env` (role tokens,
  `APPROVAL_TIMEOUT_S`).
- **Smaller modules.** `server.py` (153 lines) assembles the app from `live/api/`: hub, deps, models and routers by
  area. `service.py` (426 lines) is composed of mixins (`floor_ops`, `order_flow`, `online_ops`, `views`), with
  `world.py`, `recovery.py` and `handlers.py` alongside. Public names and every endpoint are unchanged.
- **LLM path.** Tests drive the review through the real `_MetaChatOpenAI` / `_ToolCallingChatOpenAI` clients with
  a mocked HTTP transport: request shape (json_schema vs tools), parsing, token usage, `llm_calls` rows and rule
  fallback on HTTP errors. `llm_eval smoke` and the opt-in `tests/test_llm_live.py` make one real call.
- **Test isolation.** The suite runs on a private copy of the master database. After each test it clears what that
  test's services wrote into the master (live forecast adjustments, saved prep plans).

