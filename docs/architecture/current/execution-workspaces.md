# Execution / workspaces

All argv-style process execution goes through one API: build an `ExecutionRequest`, call `run_execution`. The backend is `MOTET_EXEC_BACKEND`:

| Backend | What runs |
|---|---|
| `subprocess` (default) | OS subprocess on the worker |
| `docker` | Disposable container via the Engine API |
| `kata` / `kata-fc` | Same, with the Kata VM runtime for stronger isolation |

The Docker client is Unix-socket HTTP only; a TCP `MOTET_DOCKER_HOST` is not supported. Callers do not pass a raw `cwd` — the working directory is system-determined against an allowlist.

Consumers: `core.worker_exec`, `core.workspace_shell_exec` (skills), skill runners, bundle exec, and MCP container lifecycle/cleanup.

## Workspace containers

A workspace container is a long-lived container scoped to one `(tenant, conversation, bundle, skill, image_stack)` tuple: calls within a conversation share `/scratch` and the container filesystem. Two dispatch modes:

- **Cold** — each call is a fresh `docker exec`; only the filesystem persists between calls.
- **Warm** — a supervisor runs the runner's module as a long-lived process and dispatches each call as a `handle(params)` round-trip; module-level state (loaded models, cached connections) survives between calls. Stateful mode has its own switch (`MOTET_WORKSPACE_STATEFUL_MODE_ENABLED`, default on).

Lifecycle is registry-backed: containers register with distributed in-flight activity markers, and the idle reaper (`reap_idle`) sweeps containers idle past their TTL — never one mid-exec. Per-tenant container count is capped. `MOTET_WORKSPACE_CONTAINER_ENABLED` is the operator kill-switch.

## Image stacks

An image stack is a named base image the platform knows about (builtins plus env-registered). Bundle publish selects one via `config/exec.yaml` `base_image_stack`; workspace containers fall back to `MOTET_WORKSPACE_CONTAINER_DEFAULT_IMAGE`. The registry is read-only at runtime.

## Surfaces

- HTTP: `/api/v1/workspace-containers` (inventory), `GET /api/v1/exec/image-stacks`
- Manage UI: Workspace Containers page
- Isolation choice (runc vs Kata) is deployment configuration, not per-request

## Paths

- Package: `motet/core/execution/` (`runner.py`, `models.py`, `environment_manager.py`, `backends/`, `docker_client.py`, `workspace_container_manager.py`, `image_stacks.py`)
- Registry: `motet/core/distributed/workspace_container_registry.py`
- Tools: `core.worker_exec`, `core.workspace_shell_exec`
- APIs: `motet/interfaces/api/v1/workspace_containers.py`, `image_stacks.py`
