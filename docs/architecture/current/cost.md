# Cost / budget

Token usage is converted to estimated USD, aggregated per tenant, and capped **before** the model call. These are control-plane estimates, not billing records. Infrastructure cost is not tracked.

## Metering

Streaming and non-streaming calls record prompt, output, cache, and reasoning tokens, normalized across providers. Pricing ships with each registered **cloud** chat and reasoning model (including cache-read discounts). Local models and image models are unpriced.

A turn that made a priced call carries `cost_usd` (also on the loop usage accumulator, **top-level**, not inside `usage` — `usage` is the token envelope). A turn with no priced call has **no** `cost_usd` field. Absent means unknown, not free. Do not write `0.0` for unpriced.

Spend rolls up by tenant, principal, and conversation. Sub-agents that share the parent conversation roll up to the parent. A workflow step with `isolate_conversation` is separately attributable.

`after_finalize` hooks can export `cost_usd` and `usage`. Treat a missing cost as unknown.

## Budgets and rails

Tenant budgets are daily and monthly. An exceeded limit blocks the request before it reaches the provider. Budget checks **fail open** when the store is unreachable. Daily aggregates are kept for seven days.

The agent loop also has a per-turn `max_cost` rail (and prompt-token / iteration / tool-time rails). Hitting a rail can force a finalize write-up; `stop_reason` stays the rail. Child `core.spawn_agents` runs get a tighter spend cap than the parent.

Per-agent overrides live on `AgentConfig.max_cost_usd` / `max_prompt_tokens`.

Do not call a provider SDK from a command body. That skips routing, metering, and budgets.

## Surfaces

- HTTP: `GET/PUT /api/v1/cost/summary`, `usage`, `budget`, `events`
- Manage UI: Cost tab
- CLI: `motet-cli cost`

## Paths

- Service: `motet/core/cost/cost_tracking_service.py`
- API: `motet/interfaces/api/v1/cost.py`
- Loop accumulator: `motet/core/reasoning/react/loop_results.py`
- Onboarding: `docs/developer_onboarding/03-what-motet-can-do.md` (Cost accounting), `28-api-reference.md` (Cost and usage API)
