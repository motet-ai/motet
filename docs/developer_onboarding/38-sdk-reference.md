# Motet SDK Reference

This page lists the public API you use when writing bundles. Install the SDK with `pip install motet-sdk` (or use the repo’s editable install). The runtime injects the real implementations when your bundle runs in Motet; when you test locally or in CI, the SDK provides types and stubs so you can run and test commands without a full stack.

## Package version

- **`__version__`** — SDK package version (`from motet_sdk import __version__`). It matches the Motet runtime version and `motet-cli --version`. It comes from `motet-sdk/pyproject.toml`, not from file headers. To inspect versions on a running stack (API, workers, and configured siblings), use `motet-cli version` (`GET /api/v1/version`).

## Decorator

- **`@motet.command(...)`** — Preferred decorator for bundle commands. Use on functions with signature `(data: YourData, motet: MotetContext) -> ...`. In the SDK this is a no-op so you can call the function directly; in the runtime it registers the command and injects context. Optional `description=` sets the help/discovery summary; if omitted, the runtime uses the first line of the function docstring.
- **`@distributed_command(...)`** — Alias for `@motet.command(...)`.
- **`@motet.tool(description, name=None, ...)`** — Register a bundle tool function. The tool receives `params: Dict[str, Any]` and is registered as `{bundle_id}.{name}` when loaded by the bundle loader. Use `resolve_current_identity()` inside the function if you need identity — see [Identity in bundle tools](#identity-in-bundle-tools).
- **`get_motet_context()`** — Returns the current `MotetContext` when running in the runtime; returns `None` in SDK-only mode. Prefer passing `motet` as the second argument to your command instead.
- **`resolve_current_identity(*, system_defaults=None)`** — Resolve the current principal identity from the ambient execution context. Returns an `IdentityContext` (frozen dataclass with `tenant_id`, `motet_id`, `principal_id`). Raises `ValueError` when no identity is available (unless `system_defaults` is provided). Use this in `@motet.tool` functions that need to know who is calling them — see [Identity in bundle tools](#identity-in-bundle-tools) below.

## Context type

- **`MotetContext`** — Protocol (type) for the second parameter of your command. The runtime supplies a real implementation. Use it for:
  - **Identifiers:** `task_id`, `conversation_id`, `command_id`, `tenant_id`, `principal_id`, `motet_id`, `metadata`, `stream_key`
  - **Resource helpers:** `memory` (store/recall/tag/forget), `tools` (execute/get/list with canonical names), `agents` (list/get/turn), `models` (list/get/infer/stream), `workflows` (list/get/run), `schedules` (create/list), `commands` (list/get/run by type), `conversations` (list/get/clear/register/rename). These delegate to the corresponding distributed commands when context exists.
  - **Other resources:** `vault`, `event_bus`, `artifact_store`, `redis`, `stack`
  - **Composition:** `do()`, `join()`, `apply()`, `maybe()`, `dispatch()`
  - **Streaming:** `ensure_stream()`, `stream_event()`, `stream_token()`, `publish_event()`
  - **Helpers:** `add_warning()`, `last_metadata`, `resolve_conversation_id()`, `log_fields()`, `observe_events()`

## Model inference and streaming

Registered providers and flagship ids: [Supported models](./03a-supported-models.md).

Use the **canonical model commands** instead of a context property. From a bundle command, call:

- **`motet.do(model_inference, data=ModelInferenceData(...))`** — non-streaming inference; returns a dict with `content`, `finish_reason`, usage, etc.
- **`motet.do(model_stream, data=ModelStreamData(...))`** — streaming; tokens are written to the task stream.

For **structured output** (e.g. generative UI or any JSON-schema-constrained response), set `output_contract` on `ModelInferenceData`/`ModelStreamData`. The runtime maps it to each provider's native mechanism — JSON-schema-constrained decoding for local models, `response_format` for OpenAI Chat Completions and Moonshot, `text.format` for OpenAI Responses, `response_json_schema` for Gemini, tool-forcing for Anthropic — and degrades gracefully when a model can't enforce it.

Import the commands and data types from the runtime: `from motet.core.commands.builtin.model import model_inference, model_stream` and `from motet.core.commands.command_data_classes import ModelInferenceData, ModelStreamData`. Use `motet.tenant_id`, `motet.principal_id`, etc. when building `RequestContext` if needed.

## Concurrency

Use **pool-agnostic concurrency primitives** from the SDK so your code works on all worker pool types (fork, threads, gevent). When the bundle runs in the Motet runtime, this module is replaced by the runtime’s implementation; when you run or test outside the runtime, the SDK provides a threading-only fallback.

Import from the package or the submodule:

- **`from motet_sdk import WorkerLock, worker_sleep, WorkerExecutor`**
- **`from motet_sdk.concurrency import WorkerLock, WorkerRLock, WorkerEvent, WorkerSemaphore, WorkerLocal, WorkerThread, WorkerExecutor, worker_sleep, run_async_safe`**

Use these instead of `threading.Lock`, `time.sleep`, or `ThreadPoolExecutor` in bundle code so that on gevent workers you get cooperative locking and yielding. Use **`run_async_safe(coro)`** to run async code from sync command code (e.g. `httpx.AsyncClient`, Playwright) in a pool-aware way; in the runtime it uses the correct strategy for gevent/fork/threads.

Example (lock + async HTTP):

```python
from motet_sdk.concurrency import WorkerLock, worker_sleep, run_async_safe

# Sync: lock and cooperative sleep
lock = WorkerLock()
with lock:
    worker_sleep(0.1)

# Async from sync command (e.g. inside a @motet.command)
async def fetch(url: str):
    async with httpx.AsyncClient() as client:
        return (await client.get(url)).json()

data = run_async_safe(fetch("https://api.example.com/data"))
```

For in-process fan-out, prefer **`WorkerExecutor`** over `ThreadPoolExecutor` so gevent workers use cooperative greenlets instead of spawning many OS threads. See [Concurrency Primitives](./19-concurrency-primitives.md#workerexecutor---pool-aware-concurrent-execution).

```python
from motet_sdk.concurrency import WorkerExecutor

with WorkerExecutor(max_workers=20) as executor:
    results = list(executor.map(process_item, items))
```

## Capabilities

- **`WorkerCapability`** — Enum of worker capabilities (e.g. `TOOL_EXECUTION`, `MODEL_INFERENCE`, `MEMORY_OPERATIONS`, `LOCAL_INFERENCE` for on-device/local LLM inference, `WORKER_SHELL_EXEC` for `core.worker_exec`). Use in `@motet.command(required_capabilities=[...])` so the command is routed to workers that have those capabilities. `LOCAL_INFERENCE` is advertised only by workers that can reach a usable local model.

## Models

- **`IdentityContext`** — Frozen dataclass carrying `tenant_id`, `motet_id`, and `principal_id`. Returned by `resolve_current_identity()`. Use it directly when constructing `system_defaults` for system-scoped tools.
- **`BaseCommandData`** — Base Pydantic model for command input. Subclass it and add your fields (e.g. `message: str`). The runtime may add fields like `conversation_history`; your subclass can ignore them or use them.
- **`CommandError`** — Structured error type (`type`, `message`, `details`, `recoverable`, `retry_recommended`) for building or parsing error responses.
- **`CommandMetadata`** — Execution metadata (`command_id`, `command_type`, `execution_time_ms`, `worker_id`, etc.) for observability.
- **`CommandExecutionError`** — Raised by `motet.do()` when a child command fails. Catch this in bundle commands; the runtime raises the same class (`from motet_sdk import CommandExecutionError`).
- **`GatherExecutionError`** — Raised by `motet.join()` when parallel execution fails. `partial_results` is the same unwrapped list a successful join would have returned (domain data per child, or `{_error: True, ...}`).
- **`ApplyExecutionError`** — Raised by `motet.apply()` when every batch item fails.

## Manifest

- **`BundleManifest`** — Pydantic model for `manifest.yaml` (`format_version`, `name`, `version`, `description`).
- **`load_manifest(path: Path)`** — Load and parse `manifest.yaml` from a bundle directory; raises if missing or invalid.
- **`validate_manifest(path: Path)`** — Returns `None` if valid, or an error message string.

## Testing

- **`MockMotetContext`** — Test double for `MotetContext`. Construct it with the IDs and resources you need (`memory`, `tools`, `models`, `agents`, `workflows`, `schedules`, `commands`, `conversations`, `vault`, `event_bus`, `artifact_store`); override `do`, `join`, `apply`, `maybe` (or other methods) in tests to stub sub-commands. Resources you do not pass stay `None`, so a command reaching for one it was not given fails in the test instead of silently no-opping. Use it when unit-testing bundle commands without a running Motet runtime.

```python
from unittest.mock import Mock
from motet_sdk.testing import MockMotetContext

motet = MockMotetContext(
    models=Mock(infer=Mock(return_value={"content": "generated text"})),
    tools=Mock(execute=Mock(return_value={"main_content": "page text"})),
)
result = my_command(MyData(query="x"), motet)
```

A worked example lives in `tests/unit/examples/test_deep_research_bundle.py`, which tests the `deep-research` example bundle's commands with mocked LLM, tool, and memory resources.

## Example

```python
from motet_sdk import motet, MotetContext, BaseCommandData, WorkerCapability
from pydantic import Field

class MyData(BaseCommandData):
    query: str = Field(..., description="Search query")

@motet.command(
    timeout_seconds=60,
    required_capabilities=[WorkerCapability.TOOL_EXECUTION, WorkerCapability.MODEL_INFERENCE],
)
def my_command(data: MyData, motet: MotetContext):
    from motet.core.commands.builtin.model import model_inference
    from motet.core.commands.command_data_classes import ModelInferenceData
    # Canonical model call (no motet.agent)
    response = motet.do(model_inference, data=ModelInferenceData(messages=[{"role": "user", "content": data.query}]))
    answer = response.get("content", "")
    return {"answer": answer}
```

## Identity in bundle tools

Bundle **commands** receive identity through `MotetContext` (`motet.tenant_id`, `motet.principal_id`, `motet.motet_id`).

Bundle **tools** (`@motet.tool`) are plain functions — they don't receive a `MotetContext` parameter. If your tool needs to scope data by user or tenant, use `resolve_current_identity`:

```python
from motet_sdk import motet, resolve_current_identity

@motet.tool(description="List items for the current user", name="my_items")
def my_items(params):
    identity = resolve_current_identity()
    # identity.tenant_id, identity.principal_id, identity.motet_id
    items = fetch_items_for(identity.principal_id)
    return {"items": items}
```

`resolve_current_identity` resolves identity from the ambient execution context (the command that invoked the tool). If no identity is available it raises `ValueError` — this surfaces misconfiguration early instead of silently defaulting.

For **system-scoped tools** that run outside a user context (e.g. background maintenance), pass `system_defaults`:

```python
from motet_sdk import motet, resolve_current_identity, IdentityContext

SYSTEM_IDENTITY = IdentityContext(
    tenant_id="default", motet_id="default", principal_id="system:my_tool"
)

@motet.tool(description="System maintenance", name="cleanup")
def cleanup(params):
    identity = resolve_current_identity(system_defaults=SYSTEM_IDENTITY)
    # Falls back to SYSTEM_IDENTITY when no user context exists
    ...
```

In **tests**, `resolve_current_identity()` raises `ValueError` since there is no runtime context. Pass `system_defaults` or test commands with `MockMotetContext` instead.

## CLI

The same package provides **`motet-cli`** for bundle and local workflows: `bundle init`, `bundle lint`, `bundle hot-deploy`, `local up`, `local down`, `local status`, etc. **`motet-cli chat`** calls `POST /api/v1/chat`, persists `conversation_id` in `~/.motet/config.json` between runs, supports **`--provider`**, **`--model-name`**, and **`--model-profile`** (merged into request `overrides` as `model_provider`, `model_name`, and `model_profile_name`), and artifact RAG flags aligned with the API: `--artifact-rag-scope` (`conversation` \| `principal` \| `motet`), repeatable `--artifact-id` and `--artifact-tag`, `--artifact-collection-id`, and `--allow-broader-artifact-rag-scope`. For a **device (edge) worker** on your machine that connects to a **remote** Motet deployment, use **`motet-cli device`**: `register` with optional **`--read-path`** / **`--write-path`** (host directories for `core.file_read` / `core.file_write`), `configure` to change paths without re-registering, `start` / `stop` / `doctor`, plus `list`, `revoke`, `status`, `logs`, `build`, and `update` as needed. On `device start`, clipboard, shell-exec, and process-control host bridges default **on**; shell and process-control only apply when **`MOTET_SHELL_BRIDGE_CWD_ALLOWLIST`** and **`MOTET_PROCESS_CONTROL_CWD_ALLOWLIST`** are set on the host. Disable bridges with **`--no-clipboard-bridge`**, **`--no-shell-exec-bridge`**, or **`--no-process-control-bridge`**. Full walkthrough: [Local development setup — edge worker](./14-local-development-setup.md#option-3-edge-worker-for-a-remote-motet-deployment). Command list: [Motet CLI Reference](./37-motet-cli-reference.md#edge-worker-device).
