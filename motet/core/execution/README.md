## Package: execution

**Argv-style worker execution** — subprocess, Docker Engine HTTP (Unix socket), and per-workspace container flows. Built-ins such as **`core.worker_exec`**, MCP lifecycle code, and skill runners construct **`ExecutionRequest`** values and call **`run_execution`** or container-aware helpers.

### Purpose

- **Single execution API**: **`run_execution(request)`** dispatches to a configured backend (**subprocess**, **docker**, **kata\_docker**) with shared cwd/allowlist policy (see tooling docs — caller schemas often omit raw `cwd` for safety).
- **Workspace longevity**: **`run_in_workspace`** / **`run_stateful_in_workspace`** support style supervisors and long-lived workspace containers versus one-shot **`run_one_shot`** paths.
- **Shared Docker client**: **`docker_client`** centralizes Unix-socket HTTP to the Engine API for exec, archives, and container lifecycle callers.

### Core components

#### Models (`models.py`)

**`ExecutionRequest`**, **`ExecutionResult`**, **`ExecutionInputFile`** — stable payloads between tools/commands and backends.

#### Runner (`runner.py`)

**`run_execution`**: validates/assembles backend calls; primary entry tool authors target.

#### Environment manager (`environment_manager.py`)

**`run_in_workspace`**, **`run_one_shot`**, **`run_stateful_in_workspace`** — orchestration for stateful workspaces vs ephemeral runs.

#### Backends (`backends/`)

- **`subprocess.py`**: In-process/OS subprocess path (default when **`MOTET_EXEC_BACKEND=subprocess`** semantics apply).
- **`docker.py`** / **`kata_docker.py`**: Engine-backed disposable or runtime-specialized containers (Kata/runtime fields via **`HostConfig.Runtime`** where applicable).

#### Docker primitives (`docker_client.py`)

Minimal **`docker_request`** and helpers (exec, archives, inspect) reused by MCP cleanup, bundle staging, and workspace managers.

#### Supporting modules

**`workspace_container_manager`**, **`bundle_exec`**, **`capture`**, **`mcp_docker_cleanup`**, **`cwd_allowlist`**, **`image_stacks`**, **`mcp_backend`**: higher-level staging, teardown, artifact capture, and MCP integration—read file headers before extending behavior. **`mcp_docker_cleanup`** labels Motet MCP containers and sweeps orphans by manager/worker id; **`sweep_mcp_http_sidecars`** reclaims leftover HTTP sidecars by `service_id` or published host port before bind.

### Notes

- TCP **`MOTET_DOCKER_HOST`** is **not** supported in this Unix-socket client; align with **`worker_exec`** and deployment docs.
- Tool capability matrices (edge vs cloud, allowlists) are documented in **`motet/core/tools/README.md`** (**`core.worker_exec`** section).
