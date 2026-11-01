# Restaurant Ops Agents

A real-time, multi-agent system for an 80-seat bistro. A simulated dining floor and an online-order channel
generate orders. Five agents (Order, Inventory, Forecasting, Procurement and Menu) react to each order on one event bus:
they route items to kitchen stations, deplete stock, forecast demand, buy supplies and adjust the menu. The manager
watches every decision and its reason live, and approves or rejects the ones that need a human.

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

## The five agents

| Agent | Triggered by | Decides | Tools | Needs the manager |
| --- | --- | --- | --- | --- |
| Order | `order.submitted`, `kitchen.load`, `quote.updated` | Parses mods and allergies, allergen-checks each item, routes it to a station, sets course fire times; asks to throttle online orders when slammed | parse_order, allergen_check, route_to_station, station_queue | Allergen conflict it can't resolve (timeout: item dropped); online throttle (timeout: no change); comps |
| Inventory | every `item.fired`; hourly sweep | Depletes by recipe BOM (FEFO), flags low stock vs tonight's forecast and lots near expiry, variance; quarantines on cooler failure | deplete_by_recipe, stock_by_lot, expiry_risk, variance_report | Discards over $50 (timeout: discard, food safety) |
| Forecasting | every 15 service min; nightly; weather alerts | Revises tonight's covers from what's seated; tomorrow's forecast with the signals that matter; adjusts tomorrow on a big miss | forecast_covers, forecast_dish_mix, reservations, weather, signal_report | No (advisory) |
| Procurement | `stock.low`, supplier shortfall, forecast miss, manager rejection | Checks open POs and lead times, compares supplier quotes, drafts POs, picks fallbacks, re-plans on rejection | optimize_orders, supplier_quotes, open_pos, draft_po (send_po runs in the coordinator) | POs over $300 (timeout: no PO sent) |
| Menu | `stock.low`, `lot.expiring`, quarantine, stock-out | "86 salmon after 4 more", specials that use at-risk stock (margin and allergens checked), last-N flags online | recipes, remaining_portions, margin_calc, allergen_check, dishes_using | Every 86 and every special (timeout: no special; an 86 with no stock left auto-approves) |

The coordinator resolves conflicts between agents. Example: the Menu agent wants a salmon special while salmon is short and Procurement has
nothing arriving before Friday. The coordinator blocks the special and says why.

## Repo layout

```
restaurant_ops/
  config.py              settings (env / .env)
  db/                    schema.sql (+ service_runs, event_log, quote_log), connection helpers
  data/                  catalog (20 dishes, 60 ingredients, 4 stations, 4 suppliers), synthetic generator, seed CLI
  core/                  forecasting, inventory, optimizer, constraints (incl. live action types), menu, prep
  tools/                 registry (LangChain tools) and the tools FastAPI app
  agents/                batch planning graph (supervisor), LLM access, decision log, mock policies
  live/                  the real-time service
    events.py bus.py clock.py        event types, Redis Streams / in-process bus, service clock
    kitchen.py stock.py intraday.py  kitchen-load + ETA model, live FEFO stock, intraday forecast
    floor.py guests.py online.py     dining floor, guest + online demand simulator, online orders
    menu_live.py metrics.py          tonight's menu state, KPIs
    agents/                          order, inventory, forecasting, procurement, menu (+ shared base, LLM review)
    coordinator.py                   LangGraph check -> interrupt -> verify -> execute
    scenarios.py service.py          disruptions; the orchestrator
    server.py run.py replay.py       FastAPI + WebSocket, headless runner, replay
  sim/                   day-by-day batch simulator + fixed-par baseline (the "agents vs baseline" slide)
frontend/                React (Vite): floor plan, online order page, manager dashboard; one WebSocket store
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
| `LIVE_PO_APPROVAL_THRESHOLD`, `LIVE_DISCARD_APPROVAL_VALUE`, `APPROVAL_TIMEOUT_S` | Approval policy for the live service (defaults $300, $50, 120 s). |

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
   A dinner service starts right away at 10x. Use the clock buttons for pause, 1x, 10x or 60x. **New service**
   starts a fresh seeded dinner. The disruption buttons inject a party of 12, salmon short, cooler failure,
   heatwave or forecast miss. Click a table to place an order by hand. **Online page ↗** opens the customer page
   (`/#/online`). **Replay…** plays back any logged service from its event log.

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
6. Tests:
   ```bash
   uv run pytest
   ```

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

## Known limitations

- Synthetic data only. Prices, demand and the demo numbers come from the seeded world and are not real-world
  figures.
- Live LLM mode has not been exercised against a real provider in this repo's tests. Mock mode is what the tests
  and the demo run.
- The approval timeout counts wall seconds while the clock runs. At 60x, 120 s of real time is two hours of service.
  Pause the clock to discuss a decision.
- One service runs at a time per backend process. Each service gets its own DB copy under `data/live/`.
