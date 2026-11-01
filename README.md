# Restaurant Ops Agents

A multi-agent system for an 80-seat bistro that forecasts demand, manages perishable inventory, places supplier
orders and re-plans the menu when something goes wrong. A supervisor graph (LangGraph) coordinates five agents; all
numbers come from a deterministic layer (LightGBM quantile forecasts, a MILP order optimizer, FEFO stock maths), every
action passes a constraint checker, and big decisions wait for the chef. A day-by-day simulator injects disruptions and
compares the agents against a fixed-par-level baseline.

## Design principle

Numbers come from models and rules; judgment comes from agents. An agent may propose any action, but every action
passes the constraint checker before it executes.

| Layer | Responsible for | Never does |
| --- | --- | --- |
| Forecasting models | Covers and dish mix per meal period, with P10/P50/P90 bands | Business decisions |
| Optimizer | Order quantities, supplier split, prep quantities | Interpret unusual situations |
| Deterministic constraints | Shelf life, FEFO, min order, budget, station capacity, allergens | Get overridden by an LLM |
| LLM agents | Interpret anomalies, compare alternatives, negotiate, explain, escalate | Arithmetic or stock counts |

## Architecture

```
 +-------------------------------------------------------------------------+
 |  Streamlit UI (ui/app.py)    CLI (sim/run.py)    FastAPI (tools/api.py) |
 +------------------------------+------------------------------------------+
                                |
 +------------------------------v------------------------------------------+
 |  Simulator (sim/): World, disruptions, mock vendors, baseline policy,     |
 |  metrics.   Copies the seeded DB per policy: data/runs/{run_id}_{policy}  |
 +------------------------------+------------------------------------------+
                                | one cycle per morning + per event
 +------------------------------v------------------------------------------+
 |  LangGraph supervisor (agents/supervisor.py)                              |
 |   sense -> Forecast Analyst -> Inventory -> Purchasing -> Menu -> Prep    |
 |        (live LLM ReAct agents, or rule-based mock policies)               |
 |                         |                                                 |
 |                         v                                                 |
 |   Constraint checker (core/constraints.py) -- blocked -> retry once       |
 |                         |                                                 |
 |                         v                                                 |
 |   Approval queue (interrupt) -- chef approves / rejects + note            |
 |                         |                                                 |
 |                         v                                                 |
 |   Execute + decision log + trace (decisions, trace tables)                |
 +------------------------------+------------------------------------------+
                                | agents call tools, tools return numbers
 +------------------------------v------------------------------------------+
 |  Tools layer (tools/registry.py, LangChain tools + FastAPI)               |
 +------------------------------+------------------------------------------+
 +------------------------------v------------------------------------------+
 |  Deterministic core (core/): forecasting, inventory, optimizer,           |
 |  constraints, menu, prep        -- no LLM imports                         |
 +------------------------------+------------------------------------------+
                                |
                      SQLite (data/restaurant.db)
```

## Repo layout

```
restaurant_ops/
  config.py            settings (env / .env)
  db/                  schema.sql, connection helpers
  data/                catalog, synthetic generator, seed CLI
  core/                forecasting, inventory, optimizer, constraints, menu, prep
  tools/               registry (LangChain tools) and FastAPI app
  agents/              llm, state, prompts, 5 agents, mock policies, supervisor, decision log
  sim/                 simulator, disruptions, vendors, baseline, metrics, run CLI
ui/app.py              Streamlit app (components.py: helpers)
tests/                 pytest (constraints)
DESIGN.md              contracts between modules + per-phase build notes
Idea.md                product intent and demo plan
```

## Setup

```bash
uv sync
cp .env.example .env     # then edit
```

LLM configuration (`.env`):

| Variable | Meaning |
| --- | --- |
| `LLM_MODE` | `live` (LLM agents) or `mock` (rule-based policies, no API key). Empty = `live` if the selected provider's API key is set, else `mock`. |
| `LLM_PROVIDER` | `meta` (default), `openai`, `anthropic` or `ollama`. |
| `META_API_KEY` (or `MODEL_API_KEY`), `META_BASE_URL`, `META_MODEL`, `META_REASONING_EFFORT` | Meta Model API via its OpenAI-compatible endpoint. Defaults: `https://api.meta.ai/v1`, `muse-spark-1.3`, API-default reasoning effort. |
| `OPENAI_API_KEY`, `OPENAI_MODEL`, `OPENAI_BASE_URL` | OpenAI. Default model `gpt-4.1`. |
| `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` | Anthropic. Default model `claude-sonnet-5-5`. |
| `OLLAMA_MODEL` | Local Ollama model, no key. |
| `LLM_MODEL` | Optional override: a bare model name for the selected provider, or `provider:model` (e.g. `openai:gpt-4.1`). Swapping needs no code change. |
| `LANGSMITH_*` | Optional tracing. |

Mock mode is the safe default for a demo without network access: the same graph, constraints, approvals and trace run,
but the agents' judgment is replaced by plain-code policies.

## How to run

Run in this order from the repo root.

1. Seed the database (12 months of synthetic history, suppliers, opening lots):
   ```bash
   uv run python -m restaurant_ops.data.seed            # add --force to rebuild, --sim-start YYYY-MM-DD to pick the day
   ```
   The simulation starts **today** (any weekday) and the 365 days of history end yesterday. Override with
   `--sim-start` or `SIM_START=YYYY-MM-DD` in `.env`. The date is stored in the DB (`meta.sim_start`) and every later
   run uses it. Reseeding deletes `data/models/forecast_models.pkl` so the models retrain on the new data.
2. Forecast models train lazily on first use (and are pickled to `data/models/`); nothing to do. To force a retrain:
   ```bash
   uv run python -m restaurant_ops.core.forecasting            # optional: --max-memory-mb 2048
   ```
   Training is CPU-only (LightGBM, `n_jobs=1`) and aborts with `TrainingMemoryError` if process memory passes
   `TRAIN_MAX_MEMORY_MB` (default 4096). Restart Streamlit afterwards: it keeps the old models in memory.
3. CLI simulation (agent and baseline on identical worlds, then a KPI table):
   ```bash
   uv run python -m restaurant_ops.sim.run --days 7 --policy both --scenario salmon_shortfall --auto-approve
   ```
   Drop `--auto-approve` to be asked in the terminal. Scenarios: `salmon_shortfall`, `private_booking`,
   `cooler_failure`, `heatwave`, `all`, `none`.
4. Streamlit UI:
   ```bash
   uv run streamlit run ui/app.py
   ```
   Pick policy / scenario / days in the sidebar, then **Start**, **Step day** or **Run to end**. With auto-approve off,
   a run pauses when the chef must decide; the sidebar and tab title flag it and the **Approvals** tab holds the cards.
   Tabs: Run, Agent trace, Approvals, Forecast, KPIs. **Load past run** (top of the sidebar) opens any run saved in
   `data/runs/` read-only; Start always uses a fresh run id, so a saved run is never overwritten.
5. Tools API (optional; same functions the agents call):
   ```bash
   uv run uvicorn restaurant_ops.tools.api:app
   ```
6. Tests:
   ```bash
   uv run pytest
   ```

## Scenario timeline (day 1 = the seeded start date, any weekday)

| Day | Disruption |
| --- | --- |
| 3 | Cooler failure: all dairy lots discarded (`cooler_failure`) |
| 3 | Heatwave notice: 34 C on days 4-6, cold dishes up (`heatwave`) |
| 4 | Salmon supplier will fill only 30% of day 5's delivery (`salmon_shortfall`) |
| 4 | 40-cover private dinner booked for day 6 (`private_booking`) |

`disruptions.describe(name)` prints these with the real dates and weekdays. The demo window also gets a home game on
each Wednesday and Saturday it contains. Agents see the date with its weekday plus the coming week (weekends marked),
and the date-based tools (forecast, events, reservations, weather) return a `weekday` field.

Disruptions land after the morning plan of their day and trigger an event cycle for the agents.

## KPIs

| Metric | What it shows |
| --- | --- |
| Waste ($ and %) | Perishability handling |
| Stockouts / 86's | Forecast and replenishment quality |
| Food cost % | Purchasing decisions |
| Forecast error (MAPE, P90 coverage) | Model quality |
| Human approvals needed | Autonomy with oversight |

## 3-minute demo script

1. **0:00-0:30, the problem.** Restaurants waste 4-10% of food purchases (verify the figure before using it); the drivers
   interact in ways rules cannot cover: 3 meal periods x 7 days x 3 month periods x 4 seasons x 4 event types x
   3 risk levels = 3,024 combinations before staffing, suppliers or dishes.
2. **0:30-1:15, normal Monday.** Forecast tab with bands, the signal report ("payday matters here, start of month
   doesn't"), orders placed, prep lists out.
3. **1:15-2:15, Thursday disruption.** The salmon supplier is short. In the Agent trace: Purchasing finds a fallback at
   about +12% cost, the Menu Agent swaps a special to use at-risk cod, the Prep Planner re-plans, and the chef approves
   on screen in the Approvals tab.
4. **2:15-3:00, results.** KPIs tab: agents vs fixed-par baseline across the week.

Tip: record a backup video of a seeded run; the simulator is deterministic for a given seed.

## Known limitations / untested

- **This code was built without being executed.** Only `py_compile` checks were run: no tests, no seed, no simulation,
  no UI session. Expect a first smoke run to turn up bugs; start with the seed, then `--days 1 --policy agent
  --scenario none`, then the full CLI run, then the UI.
- Live LLM agents (LangGraph `create_react_agent`, structured output) have never been exercised; mock mode is the
  most likely path to work first.
- The "+12% fallback" and other demo numbers in the script depend on the seeded world and are not guaranteed.
- The Streamlit app cannot switch tabs programmatically; it shows a banner, a sidebar alert and a flagged tab title
  when approval is needed.
- Synthetic data only; the 4-10% waste statistic in the pitch is unverified.
