# Scheduling

A schedule is a command executed later. There is no separate job system: when a schedule fires, the target runs as an ordinary distributed command through the normal capability routing (no hardcoded queue). Fire-and-forget *now* is `motet.dispatch`, not a schedule.

## Schedule types

| Type | Fires |
|---|---|
| `delayed` | Once, at `scheduled_at` |
| `recurring` | On a `cron_expression`, or every `interval_seconds` |

`ScheduledCommandManager` owns schedule metadata, cron validation, and next-execution computation.

## Who ticks the clock

The **scheduler** service is a sibling container running Celery beat (`celery -A motet.core.workers.tasks beat`). Beat periodically triggers the recurring-schedule check and an hourly cleanup of expired schedules; workers execute the fired commands.

Duplicate-fire protection is a **per-schedule** distributed lock (`lock:schedule:{schedule_id}`), not a global lock. Rapid beat intervals or concurrent checks cannot double-run one schedule.

A scheduled command carries the `tenant_id` / `principal_id` it was created with; execution resolves that identity, not a process default. See [auth-oauth.md](./auth-oauth.md).

## Surfaces

- From a command: `motet.schedules.create(...)` / `list()` / `cancel(...)` — helpers that delegate to the schedule commands.
- LLM tools: `core.schedule_command`, `core.manage_schedule`, `core.scheduled_commands_list`.
- HTTP: `/api/v1/schedules` — list, get, create, delete, suspend, resume, plus `stats/summary` and `command-types`.
- CLI: `motet-cli schedules`.

Suspend keeps the schedule and stops firing; resume recomputes the next execution. Delete removes it.

## Paths

- Manager: `motet/core/orchestration/scheduling/manager.py`, `cron_utils.py`
- Command: `motet/core/commands/builtin/schedule.py`
- Beat tasks: `motet/core/workers/schedule_tasks.py` (beat config in `celery_app.py`)
- API: `motet/interfaces/api/v1/schedules.py`
- Onboarding: `docs/developer_onboarding/12-scheduled-commands.md`
