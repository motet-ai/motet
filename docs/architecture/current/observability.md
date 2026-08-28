# Observability

Failures are loud. Worker code does not swallow exceptions: log with context (operation, parameters, `error`, `exc_info`) and re-raise. A bare `except:` that logs nothing is a bug.

## Logging

Structured logging is `structlog` everywhere. A request/trace id is bound via contextvars and rides through the API and workers; distributed logs carry `worker_id` and `task_id`. Correlation across a turn is `task_id` / `command_id` / `conversation_id` — the same ids that appear on the command envelope and `motet.last_metadata`.

## Metrics

Prometheus, aggregated across processes: workers push samples into Redis, and the API's `/metrics` endpoint lists the worker metrics index and merges each sample. Metric families cover HTTP requests, tool latency/requests/errors, model latency/errors, scheduler queue, and circuit-breaker transitions. The registry of metric names lives in `motet/core/observability/metrics.py`.

`/metrics` is auth-gated when `require_auth_for_ops_endpoints` is set.

Token spend is not a metric here — cost aggregation is its own system, [cost.md](./cost.md).

## Tracing

A lightweight tracer records command/turn traces to JSONL files or Redis:

- `MOTET_TRACE_ENABLED` (default `false`)
- `MOTET_TRACE_BACKEND` — `file` (default, `MOTET_TRACE_DIR`) or `redis` (`MOTET_TRACE_REDIS_URL`, prefix `motet:trace:`)

OpenTelemetry export is not wired.

## Task flow and events

- `/api/v1/debug` — command lookup, `task-flow/{task_id}`, `task-events/{task_id}`, command-flow analysis, routing and memory stats. The Manage app's traces view sits on these.
- `/api/v1/events` — real-time SSE stream of the caller tenant's EventBus events (Redis pub/sub), filterable by event kind, tenant/principal scoped.
- Health: `/health` on the API; the MCP manager has its own health surface (see [mcp.md](./mcp.md)).

## Paths

- Package: `motet/core/observability/` (`logging.py`, `metrics.py`, `distributed_metrics.py`, `tracing.py`, `trace_store.py`)
- APIs: `motet/interfaces/api/v1/debug.py`, `events.py`; `/metrics` in `motet/interfaces/http.py`
- Onboarding: `docs/developer_onboarding/23-observability-debugging.md`
