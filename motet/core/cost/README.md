## Package: cost

**Distributed cost tracking**: canonical LLM **`LLMUsage` → USD** math, persisted aggregates, budgeting, and hooks for dashboards or policy.

### Purpose

- **Single calculator**: Normalize provider/model/usage tuples into comparable costs (`CostCalculator`).
- **Persistence & queries**: Record spend and budgets via **`cost_tracking_service`** patterns (Redis-backed shared state conventions).
- **Enforcement**: **`budget_enforcer`** applies limits appropriate to tenant or deployment routing.
- **Instrumentation**: **`tracking_hooks`** centralize emit/observe integrations without scattering math.

### Core components

#### `cost_calculator.py` / package exports

**`calculate_cost_canonical`** and friends — **prefer `get_cost_calculator` from `motet.core.cost`** for imports stable across refactors.

#### `pricing.py`

Model pricing tables and lookup helpers keyed by provider + model identifiers.

#### `cost_tracking_service.py`

Write/read paths for recorded usage and aggregates used by dashboards or guardrails:

- **Event stream** (`cost:model_usage:{tenant}`): append-only audit trail of every model call with provenance (capped at 100k entries).
- **Daily aggregates** (`cost:summary:{tenant}:{date}`): tenant and per-principal daily rollups (7-day TTL).
- **Per-conversation running totals** (`cost:conversation:{tenant}:{cid}`): exact totals (cost, tokens, event count, models/providers) incremented at write time and read O(1) by `get_conversation_cost_summary` — no stream scan. Workflow `isolate_conversation` child IDs (`{parent}__suffix`) are indexed under the parent's `:children` set so `include_children=True` rollups are exact; parentage is derived via `motet.core.conversations.lineage.root_conversation_id_of` (the single owner of the child-ID convention). Keys expire after 30 days of inactivity; the stream remains the audit trail.

#### Per-turn cost (outside this package)

A turn's cost is **not** read back from Redis. `model_inference` / `model_stream` return the priced `cost_usd` for their own call, and the agentic loop sums those into the turn total (`reasoning/react/loop_results.accumulate_usage`), which `orchestration/turn/complete.extract_turn_cost` reads so `agent_turn` can hand it to `turn_hooks.after_finalize` exports. The keys above remain the audit trail and the source of truth for dashboards and budgets; the in-turn rollup exists so a completed turn can report its own cost without a stream scan.

#### `budget_enforcer.py`

Decision points when requests should be denied, downgraded, or flagged.

#### `tracking_hooks.py`

Optional integration surface for exporters (for example telemetry pipelines).

### Notes

Import **`CostCalculator`**, **`get_cost_calculator`**, **`ModelPricing`**, etc. from **`motet.core.cost`** (`__init__.py`) unless you are modifying internals.
