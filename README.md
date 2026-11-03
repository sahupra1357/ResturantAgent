# Restaurant Ops Agents

A real-time, multi-agent system for an 80-seat bistro. A simulated dining floor and an online-order channel
generate orders. Six agents (Order, Inventory, Forecasting, Procurement, Menu, and Prep & staffing) react to each
order on one event bus: they route items to kitchen stations, deplete stock and mise en place, forecast demand, buy
and negotiate supplies, adjust the menu and call in cooks. Around them sit the operations a real restaurant needs:
bookings and a waitlist, checks and payments, delivery, guest profiles with persistent allergies and loyalty, food
safety (temperature log, HACCP, donations), menu engineering and a POS import. The manager watches every decision
and its reason live on a 3D dining floor and dashboard, and approves or rejects the ones that need a human.

## Design principle

Numbers come from models and rules; judgment comes from agents. An agent may propose any action, but every action
passes the constraint checker before it executes. Agents call tools; tools return numbers.

| Layer | Responsible for | Never does |
| --- | --- | --- |
| Forecasting models | Covers and dish mix per meal period, P10/P50/P90 (LightGBM quantile) | Business decisions |
| Optimizer / kitchen-load model | PO quantities and supplier split (MILP); ready-time quotes (queue model) | Interpret unusual situations |
| Deterministic constraints | Shelf life, FEFO, min order, budget, station capacity, allergens | Get overridden by an LLM |
| LLM agents | Interpret anomalies, compare alternatives, explain, escalate | Arithmetic or stock counts |

## Architecture

```
  Dining floor (guest simulator,     Online order page          Manager dashboard
  hand-placed orders)                (quote before checkout)    (decision feed, approval queue, KPIs)
        |  order.submitted                 |                          ^  WebSocket: every event
        v                                  v                          |  REST: approve / reject / edit
 +------------------------------------------------------------------------------------------------+
 |  Event bus: Redis Streams (ro:events:{service}) or in-process queue  +  append-only event_log  |
 +------------------------------------------------------------------------------------------------+
   |            |               |                 |                  |                 ^
   v            v               v                 v                  v                 |
 Order       Inventory      Forecasting       Procurement          Menu         decision.proposed
 agent       agent          agent             agent                agent        decision.resolved
 parse,      FEFO deplete,  15-min revision,  open POs, quotes,    86 / special,       |
 allergens,  stock.low,     tomorrow +        draft PO, re-plan    last-N flags        |
 route, fire lot.expiring   signals           on rejection                             |
   |            |               |                 |                  |                 |
   +------------+---------------+--------+--------+------------------+                 |
                                         v proposals                                   |
                 Coordinator (LangGraph): dedupe -> constraint check + cross-agent conflicts
                                          -> approval interrupt (timeout = safe default) -> verify -> execute
                                         |
              Tools layer (tools/registry.py, live/tools.py): every number the agents use
                                         |
     Deterministic core: forecasting (LightGBM), optimizer (MILP), constraints, FEFO inventory,
                         kitchen-load / ETA model (live/kitchen.py)            -- no LLM imports
                                         |
                       SQLite (one copy of the seeded DB per service: data/live/{service_id}.db)
```

The hot path is deterministic: an order is parsed, allergen-checked and routed to its stations in well under a
millisecond (target < 2 s), and stock depletion per fired item is sub-millisecond (target < 500 ms). LLM reasoning,
when enabled, runs on a worker thread and only reviews the Menu, Procurement and Forecasting agents' rule-based
candidates. The floor never waits on a model.

## The six agents

| Agent | Triggered by | Decides | Tools | Needs the manager |
| --- | --- | --- | --- | --- |
| Order | `order.submitted`, `kitchen.load`, `quote.updated` | Parses mods and allergies, allergen-checks each item, routes it to a station, sets course fire times; asks to throttle online orders when slammed | parse_order, allergen_check, route_to_station, station_queue | Allergen conflict it can't resolve (timeout: item dropped); online throttle (timeout: no change); comps |
| Inventory | every `item.fired`; hourly sweep | Depletes by recipe BOM (FEFO), flags low stock vs tonight's forecast and lots near expiry, variance; quarantines on cooler failure | deplete_by_recipe, stock_by_lot, expiry_risk, variance_report | Discards over $50 (timeout: discard, food safety) |
| Forecasting | every 15 service min; nightly; weather alerts | Revises tonight's covers from what's seated; tomorrow's forecast with the signals that matter; adjusts tomorrow on a big miss | forecast_covers, forecast_dish_mix, reservations, weather, signal_report | No (advisory) |
| Procurement | `stock.low`, supplier shortfall, forecast miss, manager rejection | Checks open POs and lead times, compares supplier quotes, drafts POs, picks fallbacks, re-plans on rejection | optimize_orders, supplier_quotes, open_pos, draft_po (send_po runs in the coordinator) | POs over $300 (timeout: no PO sent) |
| Menu | `stock.low`, `lot.expiring`, quarantine, stock-out; service start | "86 salmon after 4 more", specials that use at-risk stock (margin and allergens checked), last-N flags online; price changes from menu engineering | recipes, remaining_portions, margin_calc, allergen_check, dishes_using, menu_engineering | Every 86, special and price change (timeout: no special / no change; an 86 with no stock left auto-approves) |
| Prep & staffing | service start, every fired item, `kitchen.load`, every 10 min | Prep list from the forecast; burns mise en place down as items fire (none left = cooked from scratch, 30% slower); schedules prep batches; calls a cook in when a station stays behind, releases them when calm | prep_quantities, remaining_forecast, remaining_portions, station_queue | Call-ins (labour budget enforced by the constraint checker; timeout: no call-in) |

Procurement also negotiates: emergency lines worth $150 or more go to the supplier's vendor agent for 5% off, and
the counter price is used. Supplier reliability is learned. Fill rates blend catalog priors with the PO history
at service start, and every shortfall lowers that supplier's fill rate, so its quotes carry a higher risk premium.

The coordinator resolves conflicts between agents. Example: the Menu agent wants a salmon special while salmon is short and Procurement has
nothing arriving before Friday. The coordinator blocks the special and says why.

## Repo layout

```
restaurant_ops/
  config.py              settings (env / .env); every live tunable lives here
  db/                    schema.sql (+ live tables: event_log, bookings, guests, payments, temp_log, haccp_checks, ...)
  data/                  catalog (20 dishes, 60 ingredients, 4 stations, 4 suppliers), synthetic generator, seed CLI,
                         importer.py (POS CSV -> covers / pos_sales)
  core/                  forecasting, inventory, optimizer, constraints (incl. live action types), menu, prep,
                         suppliers.py (reliability learning), menu_engineering.py,
                         actions.py (DB effects of every action type, shared by the batch and live runtimes)
  tools/                 registry (LangChain tools); the agents' only way to read and write the world
  agents/                batch planning graph (supervisor), LLM access, decision log, mock policies
  live/                  the real-time service
    events.py bus.py clock.py        event types, Redis Streams / in-process bus (degrades on Redis failure), clock
    kitchen.py stock.py intraday.py  kitchen-load + ETA model, live FEFO stock, intraday forecast
    floor.py guests.py online.py     dining floor, guest + online demand simulator, online orders
    host.py payments.py crm.py       bookings / waitlist / table ops, checks / payments / delivery, guest profiles
    safety.py                        temperature log, HACCP checklist, waste analytics, donations
    menu_live.py metrics.py          tonight's menu state, KPIs
    agents/                          order, inventory, forecasting, procurement, menu, prep (+ base, LLM review)
    coordinator.py handlers.py       LangGraph check -> interrupt -> verify -> execute; the executor per action type
    service.py world.py              the orchestrator (lifecycle, step loop) and its shared constants
    floor_ops.py order_flow.py       guests / seating / waiting area; order flow     (mixins of Service)
    online_ops.py views.py           online channel; analytics, report, snapshot     (mixins of Service)
    persistence.py recovery.py       snapshots (versioned, checksummed); restore from snapshot or event log
    scenarios.py ops.py              disruptions; JSON logs, Prometheus metrics, retention, backups
    server.py api/                   FastAPI app; routers by area (auth, service, floor, kitchen, office, admin, ws)
    run.py replay.py llm_eval.py     headless runner, replay, LLM smoke test + eval set
  sim/                   day-by-day batch simulator + fixed-par baseline (the "agents vs baseline" slide)
frontend/                React (Vite): 3D + 2D floor, online order page, manager dashboard; one WebSocket store;
                         vitest tests in src/__tests__
tests/                   pytest
```

## Setup

```bash
uv sync
```

```bash
cd frontend && npm install && npm run build && cd ..
```

`.env` is optional (`cp .env.example .env`). Without an LLM API key the agents run in mock mode (rule-based) and
the whole demo works offline. LLM settings:

| Variable | Meaning |
| --- | --- |
| `LLM_MODE` | `live` (LLM reviews Menu / Procurement / Forecasting proposals) or `mock`. Empty = `live` if the provider's API key is set. |
| `LLM_PROVIDER`, `LLM_MODEL` | `meta` (default), `openai`, `anthropic` or `ollama`; `LLM_MODEL` may be `provider:model`. |
| `*_API_KEY`, `*_MODEL`, `*_BASE_URL` | Per-provider settings (see `.env.example`). |
| `EVENT_BUS` | `auto` (Redis Streams if `REDIS_URL` answers, else in-process), `redis`, or `memory`. |
| `REDIS_URL` | Default `redis://localhost:6379/0`. |
| `LIVE_PO_APPROVAL_THRESHOLD`, `LIVE_DISCARD_APPROVAL_VALUE`, `APPROVAL_TIMEOUT_SIM_MIN` | Approval policy for the live service (defaults $300, $50, and 20 service minutes before the safe default applies; a paused clock freezes the countdown). |

Redis is optional. To use Redis Streams, start a server before the backend:

```bash
redis-server --daemonize yes
```

## How to run

1. Seed the world (12 months of synthetic history, suppliers, opening lots). The first forecast trains the models:
   ```bash
   uv run python -m restaurant_ops.data.seed --force
   ```
2. Start the live service (backend + UI on http://127.0.0.1:8000):
   ```bash
   uv run python -m restaurant_ops.live.server
   ```
   Sign in (see below). A dinner service starts right away at 10x. The floor fills the screen (**Side by side** puts
   the dashboard next to it) and opens in **3D**. Guests walk in through the entrance and wait in the waiting area
   while they check in with the host. A party that is early for its booking, waiting for the rest of its group, or
   has no free table also waits there, and each waiting party has a name tag. Guests then sit at their tables and
   leave when they've paid. Drag to rotate, scroll to
   zoom, and click a table to order (the 2D floor is a tab). Sign in as manager to use the controls. Use the clock
   buttons for pause, 1x, 10x or 60x. **New service**
   starts a fresh seeded dinner. The disruption buttons inject a party of 12, salmon short, cooler failure,
   heatwave or forecast miss. Click a table to place an order by hand. **Online page ↗** opens the customer page
   (`/#/online`), with pickup or delivery by zone. **Replay…** plays back any logged service from its event log.
   The dashboard has five tabs:
   - **Operations:** approvals, kitchen, decision feed, online orders, KPIs.
   - **Front of house:** bookings, waitlist, table moves, checks, guests.
   - **Kitchen & safety:** prep and staffing, HACCP and temperatures, inventory, waste.
   - **Insights:** forecast, end-of-service report, menu engineering, supplier reliability, baseline, LLM pilot.
   - **Back office** (manager): day close, gift cards, purchase orders, backups and drill, LLM pilot, audit log.

   **Alerts** sounds a chime and shows a desktop notification for new approvals.

   For frontend development, run `npm run dev` in `frontend/` (port 5173, proxied to the backend).
3. Headless, seeded run (no browser), e.g. the salmon-short demo:
   ```bash
   uv run python -m restaurant_ops.live.run --scenario salmon_short@18:45 --scenario party_of_12@19:15 --policy demo
   ```
   Policies: `demo` (approve, but reject an ingredient's first emergency PO as over budget), `approve`, `reject`,
   `timeout` (nobody answers: safe defaults apply).
4. Replay a logged service from the command line:
   ```bash
   uv run python -m restaurant_ops.live.replay
   ```
5. Agents vs fixed-par baseline over a week (batch simulator; feeds the baseline panel on the dashboard):
   ```bash
   uv run python -m restaurant_ops.sim.run --days 7 --policy both --scenario all --auto-approve
   ```
6. Tests (backend, then frontend):
   ```bash
   uv run pytest
   ```
   ```bash
   cd frontend && npm test
   ```
7. Import real POS history (CSV, one row per item sold), then retrain on it with the next run:
   ```bash
   uv run python -m restaurant_ops.data.importer pos_export.csv --dry-run
   ```
   Columns `check_id, opened_at, item, qty, guests` (rename with `--columns '{"item": "Menu Item"}'`). Items map by
   id, name or fuzzy match, or by an `--items-map` CSV (`pos_item,dish_id`). Drop `--dry-run` to write, and add
   `--set-sim-start` to make the day after the export the first live day.
8. Clean up old service files (also runs when the server starts):
   ```bash
   uv run python -m restaurant_ops.live.ops --dry-run
   ```

### Sign-in, roles and audit

Everyone signs in with a named account. The browser gets an HttpOnly, SameSite=Strict session cookie, and scripts
use `Authorization: Bearer <token>` (from `POST /api/auth/login`). Tokens never go in URLs. Cookie-authenticated
writes also need the `X-Requested-With: ro-ui` header, which the UI sends. Everything needs a signed-in user except
`/api/health`, the customer online-order endpoints and `/ws/public` (menu availability, kitchen busyness and order
progress, with no names or tables).

On first start the server creates an `admin` account. Its password comes from `ADMIN_PASSWORD`, or is generated and
written to `data/FIRST_RUN_ADMIN.txt` (mode 600; change it in Setup → Users, then delete the file). With
`DEMO_USERS=true` (the default) it also creates `manager` / `manager-demo`, `server` / `server-demo` and
`kitchen` / `kitchen-demo`. Turn that off anywhere real.

| Role | Can |
| --- | --- |
| admin | Everything, plus Setup (menu, recipes, tables, taxes, hours, stations, zones, suppliers, staff), users, imports, restore |
| manager | Approvals, scenarios, clock, new service (period and date), online mode and quote overrides, refunds, discounts up to the manager cap, gift cards, day close, backups and drill, reports, audit log, roster |
| server | Table orders, bookings, waitlist, table moves, checks and payments, discounts up to the server cap, guest profiles |
| kitchen | Kitchen display (bump tickets), HACCP checklist, temperature log |

Passwords are PBKDF2-hashed, and only hashes of session tokens are stored. Five failed logins lock the username for
5 minutes. A password change, deactivation or role change signs that user out everywhere, and the last admin
can't be demoted or deactivated. Every write, including refused ones and failed logins, is in the audit log
(Back office → Audit log, or `/api/admin/audit`), with passwords never recorded. `AUTH_ENABLED=false` turns
sign-in off for a local demo.

### Setup (admin)

**Setup** (`/#/admin`) edits the restaurant without touching code:
- **Restaurant profile:** name, address, loyalty earn and point value, discount caps per role, overbooking factor.
- **Tax rules:** several jurisdictions, each applying to food, alcohol, delivery fees or all, on any channel.
- **Opening hours:** per weekday and meal period. A closed period can't be started.
- **Tables:** position and size on the 1000 x 700 plan, with shape and section.
- **Kitchen stations:** items per cook, cook-time factor and cooks per period.
- **Delivery zones:** fee, drive time and minimum order.
- **Ingredients**, and **dishes with recipes**, showing food cost and food-cost % live.
- **Suppliers:** with PO e-mail and a price list.
- **Staff and a roster:** rostered cooks set each station's capacity for that shift.
- **Users.**

Edits are validated (for example, a price below the recipe's food cost is refused, and a station that active dishes
use can't be deleted). They are saved to the master database and **apply from the next service**, so a running
dinner never changes under the kitchen. **Import** takes CSV for a menu with recipes, inventory counts or deliveries
(as lots), and POS history, and each has a dry run. The same importers are on the command line:

```bash
uv run python -m restaurant_ops.data.importer menu.csv --kind menu --dry-run
```

```bash
uv run python -m restaurant_ops.data.importer stock.csv --kind inventory --as-of 2026-11-02 --replace-stock
```

### Payments, gift cards, purchase orders, kitchen output

- **Card payments** go through a processor adapter. The default is `PAYMENT_PROCESSOR=simulated`, which declines
  token `tok_decline` and amounts over $5,000. `PAYMENT_PROCESSOR=stripe` with `STRIPE_SECRET_KEY` charges through
  Stripe PaymentIntents (with idempotency keys) and refunds against the original charge. The processor reference is
  stored with each payment.
- **Gift cards** can be issued, checked, redeemed and voided, with a ledger. **Loyalty points** can pay too, at the
  profile's point value.
- **Purchase orders** that the agents place (and you approve) are rendered as a PDF and e-mailed to the supplier
  over SMTP (`SMTP_HOST`, etc.). Without SMTP they wait in `data/outbox/` as PDF + .eml. You can view or resend
  them in Back office.
- **Kitchen chits** print per station when an order is routed. `PRINTER_MODE=file` writes them to
  `data/printer/<service>/<station>.txt`, and `PRINTER_MODE=tcp` sends ESC/POS to a network printer
  (`PRINTER_HOST`, or `PRINTER_HOST_<STATION>` per station; port 9100).
- **Kitchen display** (`/#/kds`) shows tickets by station, oldest first. A ticket turns red after
  `KDS_LATE_MIN`, and **Bump** clears it.

### Day close, backups, offline capture

- **Day close** (Back office) reconciles the shift:
  - checks due against money collected by method
  - tips, discounts and comps, refunds
  - gift-card and points redemptions
  - tax per jurisdiction
  - expected cash against your drawer count (over / short)

  It writes a balanced double-entry journal (`data/exports/<date>-<period>/journal.csv`) and `summary.json`. Open
  checks block the close unless you force it.
- **Backups:** **Back up now** and `python -m restaurant_ops.live.ops backup` take consistent SQLite copies of the
  master database and the service databases, snapshots and checkpoints, with a SHA-256 manifest. The newest
  `BACKUP_KEEP` are kept. **Run restore drill** (`ops drill`) restores the latest backup into a scratch folder,
  integrity-checks every database and loads the newest snapshot. Admins can restore from the UI or with
  `ops restore <id>`.
- **Snapshots** are versioned and migrated on load. Each has a checksum, and the last `SNAPSHOT_KEEP` are kept. If
  the newest snapshot is damaged, the server loads an older one. If none is readable, it rebuilds the service from
  its database and event log, and pending approvals stay pending.
- **Offline order capture:** when the server can't be reached, a table order is saved on the device with an
  idempotency key, and a banner shows the queue. Orders are sent in order when the connection returns. A retry
  after a lost response never creates a second order.
- **Meal periods:** **New service…** picks lunch, dinner or late, and a date.

### Install

With Docker (app + Redis, data in a volume):

```bash
docker compose up -d
```

Without Docker (installs, builds the UI, seeds and runs the backup drill; `--service` adds a systemd unit):

```bash
scripts/install.sh
```

### LLM pilot

In live mode one configured provider reviews the Menu, Procurement and Forecasting proposals. The review has hard
limits per service:
- `LLM_MAX_CALLS_PER_SERVICE` calls and `LLM_MAX_COST_PER_SERVICE` dollars
- `LLM_TIMEOUT_S` per call
- a circuit breaker after `LLM_BREAKER_FAILURES` failures in a row

When any limit is hit, the proposals go out rule-based and say why ("LLM review skipped: …"). Every call (latency,
tokens, cost, error) is logged to `llm_calls`, and the LLM panel shows the numbers. Build an eval set from logged
services (proposals a manager approved or rejected) and score a model against the rule-based baseline before widening
the pilot:

```bash
uv run python -m restaurant_ops.live.llm_eval build
```

```bash
uv run python -m restaurant_ops.live.llm_eval run --judge live --limit 50
```

Before a pilot, check the provider end to end with one real review call. It goes through the same client, structured
output and token accounting as the service, and prints the judgment, latency, tokens and cost (exit code 1 on
failure):

```bash
uv run python -m restaurant_ops.live.llm_eval smoke
```

The same check runs as a test when you opt in (it is skipped without a key):

```bash
RUN_LIVE_LLM=1 uv run pytest tests/test_llm_live.py -s
```

The regular suite drives the live review path through the real Meta and OpenAI client classes with the HTTP
transport mocked. That covers request shape, structured-output parsing, token usage, logging and the fall back to
rules on an HTTP error, but not a real model's answers.

### Restarts

The service saves its state every 5 service minutes and on every decision. Approvals that are waiting are LangGraph
interrupts checkpointed in SQLite. When the server restarts, it resumes the interrupted dinner, and pending
approvals can still be approved. Use `--fresh` to start a new dinner instead.

## Online ready-time quotes

The quote comes from a deterministic kitchen-load model (`live/kitchen.py`). Each item has a station; the order is
ready when its slowest item is done:

    T_ready = t_now + max_i (W_s(i) + P_i) + B

`W` is the current wait at that station (queued minutes ÷ cook slots), `P` is the item's calibrated cook time and
`B` is a 3-minute packing buffer. The page shows a P50–P90 range. P90 inflates waits and cook times by the 90th
percentile of observed actual/estimated ratios. Every finished item recalibrates its station (EWMA), and every
online order logs quoted vs actual (`quote_log`), which gives the quote-accuracy KPI.

Busyness is calm, busy or slammed. It comes from station utilisation plus the covers the Forecasting agent expects
in the next 30 minutes. When the kitchen stays slammed, the Order agent asks to switch online ordering to quick
items only or pause it. 86'd dishes disappear from the online page at once, and "last 4" dishes are limited to
one per order.

## Disruption scenarios

| Button | What happens |
| --- | --- |
| Party of 12 | A walk-in party of 12 pre-orders (mostly grill). The grill wait jumps, online quotes stretch and customers are notified. The Order agent asks to throttle online orders. |
| Salmon short | FreshCatch fills only 30% and 6 salmon portions are left. Inventory publishes stock.low. Menu proposes "86 salmon after 4 more" and a replacement special. Procurement drafts a fallback PO (over the threshold) and re-plans after a rejection with a reason. |
| Cooler fails | Dairy lots are quarantined at once. Inventory requests a discard, and Menu 86s the dishes left with no usable dairy. |
| Heatwave | 34 °C for 3 days. The forecast shifts the dish mix toward cold dishes and stock is re-checked against it. |
| Forecast miss | Walk-ins run about 75% above the model, so covers end well over forecast. The Forecasting agent revises, adjusts tomorrow, and Procurement re-checks tomorrow's order. |

## 3-minute demo script

Run the dinner at 60x with the floor on the left and the dashboard on the right.

1. **0:00–0:20, the problem.** The factors interact: 3 meal periods × 7 days × 3 month periods × 4 seasons ×
   4 event types × 3 risk levels = 3,024 combinations before staffing, suppliers or dishes. Rules can't keep up
   during service.
2. **0:20–1:00, service starts.** Tables fill. Click a table and order "Pan-Seared Salmon" with mods "no dairy, nut
   allergy". The result shows it routed to sauté, with butter and cream left out, in under a millisecond. The
   decision feed updates.
3. **1:00–1:30, online order.** Open the online order page. Add a ribeye and see "ready 19:25–19:35". Press
   **Party of 12**. The grill load jumps, the quote moves out live, and the Order agent asks to limit online orders
   to quick items. Approve it.
4. **1:30–2:20, disruption.** Press **Salmon short**. Inventory flags 6 portions left, Menu proposes "86 Pan-Seared
   Salmon after 4 more" plus a special, and Procurement drafts a fallback PO over the threshold. Approve the special
   and the 86. Reject the PO with a reason ("over budget, wait for FreshCatch"). Procurement re-plans a smaller,
   cheaper order, or holds and says why.
5. **2:20–3:00, results.** Show the service KPIs (latency, quote accuracy, ticket times, 86s caught, waste, food cost,
   auto vs approval). Then replay the decision log with **Replay…**.

Tip: record a backup video of a seeded run. A headless run with the same seed and the same manager policy
reproduces exactly.

## KPIs

| Metric | What it shows |
| --- | --- |
| Order-to-station latency (p50/p95/max ms) | Real-time behaviour |
| Ready-time quote accuracy (% within quoted range), MAE | Kitchen-load model and online experience |
| Ticket times and late tickets | Kitchen pacing |
| Stockouts, and 86s caught before a guest ordered | Inventory and Menu agents |
| Waste $ and food cost % | Perishability and procurement decisions |
| Forecast error (tonight vs model P50; P10–P90 coverage) | Forecasting agent |
| Decisions auto-executed vs sent for approval | Autonomy with oversight |
| Collected, tips, discounts, refunds | Payments |
| Delivery on-time (% within the quoted door time) | Dispatch |
| Waitlist, no-shows, covers lost to waiting | Front of house |
| Surge labour $ and items cooked from scratch | Prep & staffing |

`/metrics` exposes the live numbers in Prometheus format (per-station waits and cooks included). The end-of-service
report is a printable page (`/api/report.html`, Print → Save as PDF) and a CSV (`/api/report.csv`).

## Known limitations

- Synthetic data only. Prices, demand and the demo numbers come from the seeded world and are not real-world
  figures.
- No real provider is called in CI. The live review path is tested through the real client classes with mocked HTTP.
  Run `llm_eval smoke` (or the opt-in `tests/test_llm_live.py`) with your key to check a real model.
- Approval timeouts count service minutes (20 by default), so a paused clock freezes them.
- Price suggestions assume an elasticity: the synthetic history never varies prices, so it can't be estimated. With
  real POS history that includes price changes, this is where an estimate would plug in.
- Simulated guests pay through the simulated processor. Stripe is wired up but only tested against a mocked HTTP
  transport here. Delivery has no maps or driver app integration.
- Setup changes apply from the next service, not mid-service.
- The Docker image and `docker-compose.yml` were not built in this repo's CI environment (no Docker daemon). The
  install script and the app run under the same commands as the documented local setup.
- Each live service runs on its own copy of the master database. Only state meant to outlive a service is written
  back: gift cards, users and the audit log, day closes, an approved "adjust tomorrow's forecast", and a prep list
  saved for a later service. Stock, POs and orders of a service stay in its copy.
- If Redis fails mid-service, the bus falls back to the in-process queue. Events published to Redis but not yet read
  at that moment are not re-dispatched, though they remain in the SQLite event log.
- One service runs at a time per backend process. Each service gets its own DB copy under `data/live/`.
