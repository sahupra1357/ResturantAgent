# Restaurant Ops Agents – Hackathon Plan

Oct 28, 2026 · @Pradeep Sahu

A multi-agent system for a full-service restaurant that forecasts demand, manages perishable inventory, places supplier orders and re-plans the menu when something goes wrong.

## Design principle

Numbers come from models and rules; judgment comes from agents. That split avoids hand-writing thousands of context rules (3 meal periods × 7 days × 3 month periods × 4 seasons × 4 event types × 3 risk levels = 3,024 combinations before staffing, suppliers or dishes).

| Layer | Responsible for | Never does |
| --- | --- | --- |
| Forecasting models | Covers and dish mix per meal period, with uncertainty bands | Business decisions |
| Optimizer | Order quantities, supplier split, prep quantities | Interpret unusual situations |
| Deterministic constraints | Shelf life, FEFO, min order, budget, station capacity, allergens | Get overridden by an LLM |
| LLM agents | Interpret anomalies, compare alternatives, negotiate, explain, escalate | Arithmetic or stock counts |

Rule of thumb: an agent may propose any action, but every action passes the constraint checker before it executes. Agents call tools; tools return numbers.

## Signal dimensions

Each dimension is a candidate feature the model tests per restaurant, not an assumed rule. The Forecast Analyst agent reports which signals actually matter here (e.g. "payday effect: +9% covers, significant" vs "start of month: no effect").

| Dimension | How it enters the system | Feeds |
| --- | --- | --- |
| Time of day | Separate forecast per meal period (lunch, dinner, late) | Prep list, staffing, station load |
| Day of week | Categorical feature | Covers forecast, order timing |
| Day of month | Feature + payday flag; kept only if significant | Covers forecast |
| Seasonality | Week-of-year + season; seasonal ingredient calendar | Menu, supplier choice |
| Events | Calendar feed: holidays, local events, sports, private bookings | Forecast uplift, staffing |
| Perishability | Lot table with received date, shelf life, storage | FEFO picking, specials, waste risk |
| Capacity | Reservations, walk-in rate, seats, stations, staff on shift | Caps forecast; prep feasibility |
| Supply chain | Per-SKU lead time, min order, price, fill-rate history | Optimizer constraints, supplier fallback |

Interactions (e.g. Friday dinner + home game + payday) are learned by the model from history, so no combination table is ever written by hand.

## Architecture

&#91;embedded content: system architecture · 4 layers\]

The supervisor dispatches five agents; every number they use comes from the tools layer, and every action passes the constraint checker before it executes. The chef only sees decisions above the approval threshold.

## Agents

Six agents, coordinated by a supervisor graph. Each owns decisions; none owns arithmetic.

| Agent | Decides | Tools it calls |
| --- | --- | --- |
| Supervisor | Run order, conflict resolution, when to ask the chef | All agents, approval queue |
| Forecast Analyst | Whether to trust the forecast; adjusts for events the model hasn't seen | `forecast_covers`, `forecast_dish_mix`, `event_calendar`, `signal_report` |
| Inventory Agent | Waste-risk lots, variance flags (theft, over-portioning) | `stock_by_lot`, `expected_usage`, `variance_report` |
| Purchasing Agent | Supplier choice, reschedule vs substitute, negotiation | `optimize_orders`, `supplier_quotes`, `send_po`, `negotiate` (mock vendor agents) |
| Menu Agent | Tonight's specials, which dishes to 86, substitutions | `recipes`, `margin_calc`, `allergen_check` |
| Prep Planner | Prep list per station and par levels | `prep_quantities`, `station_capacity` |

Human-in-the-loop: purchase orders above a threshold, menu changes and 86's go to an approval queue for the chef. Approvals and rejections are logged and fed back as context.

## Daily decision loop

The loop runs each morning and again whenever an event arrives (supplier shortfall, large booking, stockout).

1. **Sense:** pull reservations, POS depletion, stock lots, supplier messages and the event calendar.
2. **Forecast:** the model predicts covers and dish mix per meal period for the next 3 days, with P10/P50/P90 bands.
3. **Review forecast:** the Forecast Analyst checks for anomalies the model can't see (new event, weather alert) and adjusts with a written reason.
4. **Assess stock:** the Inventory Agent computes expected usage vs on-hand by lot and flags expiry risk and variance.
5. **Optimize orders:** the solver returns order quantities and supplier split under lead time, min order, budget and shelf life.
6. **Review orders:** the Purchasing Agent checks supplier reliability, negotiates or picks fallbacks.
7. **Plan menu:** the Menu Agent turns at-risk lots into specials and 86's dishes that can't be covered.
8. **Plan prep:** the Prep Planner creates station prep lists from the final forecast.
9. **Validate and approve:** the constraint checker validates every action; items over threshold go to the chef.
10. **Execute and log:** send POs, publish specials and prep lists, and record each decision with its rationale.

## Data and simulation

Build one realistic 80-seat bistro and a simulator that replays time, so agents act on a consistent world.

- **Menu and recipes:** 20 dishes, about 60 ingredients, recipe BOMs with portion sizes, shelf life and storage type.
- **History:** 12 months of synthetic POS orders generated from the eight dimensions (with a payday effect, seasonal dishes and event spikes baked in), plus noise. Train on 11 months; test on the last one.
- **Suppliers:** 4 suppliers with overlapping SKUs, different prices, lead times, minimum orders and fill rates.
- **Reservations and events:** a booking stream plus a calendar (holidays, 2 home games, 1 private dinner for 40).
- **Simulator:** advances day by day, depletes stock from orders, ages lots, delivers POs with stochastic delays and injects disruptions.

Disruption scenarios for the demo:

1. Supplier is short on salmon for Friday delivery.
2. A 40-cover private booking lands for Saturday.
3. A walk-in cooler failure forces discarding dairy.
4. A heatwave shifts demand toward cold dishes.

If the hackathon requires real data, swap in a public restaurant sales dataset for history and keep the simulator for disruptions.

## Tech stack

| Concern | Choice | Why |
| --- | --- | --- |
| Agent orchestration | LangGraph (supervisor + subgraphs) | State, checkpoints, interrupts for human approval |
| LLM | Any strong tool-calling model; self-hosted option via Ollama/vLLM | Swap without code changes |
| Forecasting | LightGBM with quantile objective (or Prophet as fallback) | Fast to train, handles interactions, gives P10/P90 |
| Optimization | OR-Tools or PuLP (MILP) | Order quantities under hard constraints |
| Constraints | Plain Python validators | Deterministic and testable |
| Data | Postgres (or SQLite for speed) | Lots, orders, POs, decision log |
| Tools API | FastAPI, exposed to agents as tools (or MCP) | One contract for agents and UI |
| UI | Streamlit or Next.js | Agent trace timeline, approvals, KPIs |
| Observability | LangSmith or Langfuse | Shows reasoning and tool calls live |

## 48-hour build plan

Goal by hour 36: one full loop runs end to end with a disruption. The last 12 hours are polish and demo only.

### Hours 0–6: Foundation

- [ ] Lock scope: 20 dishes, 60 ingredients, 4 suppliers, 1 restaurant
- [ ] Database schema: ingredients, lots, recipes, orders, suppliers, POs, decisions
- [ ] Synthetic data generator with the eight dimensions baked in
- [ ] Repo skeleton: FastAPI tools service, LangGraph app, UI stub

### Hours 6–14: Deterministic core

- [ ] Train the quantile forecast model; print a signal-importance report
- [ ] Expected-usage and FEFO stock calculations
- [ ] Order optimizer (MILP) with lead time, min order, budget and shelf life
- [ ] Constraint checker and its unit tests
- [ ] Expose everything as tools

### Hours 14–26: Agents

- [ ] Supervisor graph and shared state
- [ ] Forecast Analyst, Inventory and Purchasing agents
- [ ] Menu Agent and Prep Planner
- [ ] Approval interrupt for the chef
- [ ] Decision log with rationale per action

### Hours 26–36: Simulator and disruptions

- [ ] Day-by-day simulator with stochastic deliveries
- [ ] Salmon shortfall and private-booking scenarios end to end
- [ ] Mock vendor agents for negotiation
- [ ] Baseline policy (fixed par levels) to compare against

### Hours 36–44: UI and metrics

- [ ] Live agent trace timeline
- [ ] Approval queue screen
- [ ] KPI panel: waste $, stockouts, food cost %, agent vs baseline

### Hours 44–48: Demo

- [ ] Record a backup video of the full run
- [ ] Rehearse the 3-minute script twice
- [ ] README with architecture and how to run

## Demo script and metrics

Three minutes, one simulated week, one disruption the judges watch the agents handle.

1. **0:00–0:30, the problem.** Restaurants waste 4–10% of food purchases (verify the figure before using it), and the drivers interact in ways rules can't cover. Show the 3,024-combination math.
2. **0:30–1:15, normal Monday.** Forecast with bands, the signal report ("payday matters here, start of month doesn't"), orders placed, prep lists out.
3. **1:15–2:15, Thursday disruption.** The salmon supplier is short. Watch the trace: Purchasing finds a fallback at +12% cost, Menu Agent swaps a special to use at-risk cod, Prep Planner re-plans, and the chef approves on screen.
4. **2:15–3:00, results.** Agents vs fixed-par baseline across the week.

| Metric | What it shows |
| --- | --- |
| Waste ($ and %) | Perishability handling |
| Stockouts / 86's | Forecast and replenishment quality |
| Food cost % | Purchasing decisions |
| Forecast error (MAPE, P90 coverage) | Model quality |
| Human approvals needed | Autonomy with oversight |

## Risks and cut list

| Risk | Mitigation |
| --- | --- |
| Agents loop or burn time | Step limits per agent; supervisor timeout; cached tool results |
| LLM invents numbers | Agents only read numbers from tool outputs; constraint checker blocks invalid actions |
| Optimizer takes too long to build | Fall back to a greedy reorder-point heuristic with the same interface |
| Live demo fails | Seeded, deterministic simulator run plus a recorded video |
| Scope creep | Cut list below, decided up front |

Cut in this order if behind schedule:

1. Mock vendor negotiation (keep fallback supplier selection)
2. Variance and theft detection
3. Third and fourth disruption scenarios
4. Next.js UI (use Streamlit)

Never cut: the end-to-end loop, the agent trace, the baseline comparison and the human approval step.
