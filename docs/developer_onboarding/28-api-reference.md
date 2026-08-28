# API Reference

Quick reference for Motet APIs. For detailed documentation, see the source code and specific guides.

**See the APIs in action:** The [Chat Explorer](./36-chat-explorer.md) is a reference chat UI that uses the Conversations, Chat, Artifacts, Auth, and Models APIs to demonstrate framework capabilities.

## Canonical LLM protocol

Model inference and streaming use a **provider-agnostic canonical protocol**. Orchestration depends only on canonical types; **provider adapters** translate at the vendor boundary. See [Supported Models](./03a-supported-models.md) for the catalog and [Canonical LLM Protocol](./09a-canonical-llm-protocol.md) for the protocol.

- **Request**: `LLMRequest` (messages, tools, model_settings, etc.); messages use **Message** with canonical content parts (**TextPart**, **MediaPart**), not provider-specific blocks.
- **Response**: `LLMResponse` (output_items, output_text, stop_reason, usage, citations).
- **Streaming**: Canonical events (e.g. `text_delta`, `tool_call_delta`, `tool_call_complete`, `stop`, `usage`, `error`); adapters map provider deltas to these.
- **Tool calls**: Canonical `ToolCallRequest` / `ToolCallResult` in orchestration and transcripts; provider formats only in adapters.
- **Routing**: **ModelSpec** (per-model capabilities, supported adapters) and **ModelProfile** (per-tenant policy, preferred adapter, tool allowlists).

## MotetContext API

Resource **helpers** (`motet.tools`, `motet.memory`, `motet.agents`, etc.) are for **single operations** (one tool run, one memory op, one agent turn, list conversations, etc.); they delegate to the corresponding distributed command when context exists. Use **command composition** (`motet.do`, `motet.join`, `motet.apply`) for multi-step or parallel flows.

### Resource Access

```python
# Model inference (use helper or commands; no motet.agent)
motet.models.infer("openai", "gpt-4o-mini", messages=[...])   # Non-streaming
motet.models.stream("openai", "gpt-4o-mini", messages=[...]) # Streaming
# Or: motet.do(model_inference, data=ModelInferenceData(messages=[...]))

# Memory (helper: store, recall, tag, forget)
motet.memory.store(content="...", tags=["tag1"])
motet.memory.recall(tags=["tag1"], limit=10)
motet.memory.tag(tags=["new_tag"], op="add", memory_ids=[...])
motet.memory.forget(memory_ids=["abc123"])

# Tools (helper: use canonical names core.*, mcp.<server>.<tool>)
motet.tools.execute("core.web_search", {"query": "value"})
motet.tools.list()   # dict of canonical name -> registered tool
motet.tools.get("core.web_search")

# Agents (helper: list, get, turn)
motet.agents.list()
motet.agents.turn("core.default", messages=[...])

# Workflows (helper: list, get, run)
motet.workflows.list()
motet.workflows.run("my_workflow", context={...})

# Schedules (helper: create, list)
motet.schedules.create("tool_execution", {...}, "cron", cron_expression="0 * * * *")
motet.schedules.list()

# Commands (helper: list types, get impl, run by type)
motet.commands.list()
motet.commands.run("tool_execution", data={...})

# Conversations (helper: list, get, clear, register, rename)
motet.conversations.list(limit=100)
motet.conversations.get("conv-id")
motet.conversations.register("conv-id", title="My Chat")

# Vault, Redis, Event Bus (direct access)
motet.vault.get_api_key("openai", motet.distributed_context)
motet.redis
motet.event_bus
```

### Command Composition (PREFERRED)

```python
# Sequential execution with automatic unwrapping
result = motet.do(command, data=CommandData(...))
# Returns: data directly (or raises CommandExecutionError)

# Parallel execution with automatic unwrapping
results = motet.join([
    (command1, Data1(...)),
    (command2, Data2(...)),
    (command3, Data3(...))
])
# Returns: List[data] (or raises GatherExecutionError)

# Apply one command to many inputs
results = motet.apply(
    command,
    inputs=[{"file": "a.pdf"}, {"file": "b.pdf"}],  # List[dict], not data models
    command_template={"format": "markdown"},        # Optional: merged into every input
    batch_size=10                                   # Optional: limit concurrency
)
# Returns: List[data] (or raises ApplyExecutionError)

# Optional execution (graceful error handling)
data, error = motet.maybe(command, data=CommandData(...))
# Returns: (data, error) tuple - error is None on success

# Fire-and-forget
task_ids = motet.dispatch([
    (command1, Data1(...)),
    (command2, Data2(...))
])
# Returns: List[task_id]
```

### Context Properties

```python
# Command Metadata
motet.command_id              # Current command ID
motet.task_id                 # Current task ID
motet.conversation_id         # Current conversation ID

# Security Context
motet.tenant_id               # Current tenant ID
motet.principal_id            # Current principal (user) ID
```

Parent linkage is applied for you: when a command calls another, the child's
`parent_command_id` is set to the caller's `command_id`, so traces nest without
you passing anything along.

### Response Helpers

```python
motet.add_warning("Rate limit approaching")  # Copied onto the command envelope
motet.last_metadata                          # Metadata from the last do/join/apply/maybe
```

### Event Observation

```python
# Observe events in context
with motet.observe_events(["command_started", "command_completed"], callback):
    # Events observed during this context
    pass
```

## Command Decorator

```python
from motet_sdk import motet
from motet.core.commands.distributed import WorkerCapability

@motet.command(
    timeout_seconds=60,                   # Command timeout
    required_capabilities=[               # Required worker capabilities
        WorkerCapability.TOOL_EXECUTION,
        WorkerCapability.MODEL_INFERENCE
    ],
    priority=None,                        # Queue priority (optional)
    streaming_enabled=False,              # Emit incremental events (optional)
    preferred_pool_type=None,             # Steer to a pool type (optional)
    description=None                      # Shown in command listings (optional)
)
def my_command(data: MyData, motet: MotetContext) -> Dict[str, Any]:
    """Command description."""
    return {"result": "success"}
```

## Workflow API

### Defining workflows in Python

In-process definition and registry (product/core code). For HTTP validate/register of runtime `user.*` workflows, see [Workflows HTTP](#workflows-http). For bundle YAML, see [Workflow System](./11-workflow-system.md#bundle-workflows-yaml).

```python
from motet.core.workflow import (
    Workflow, WorkflowStep, WorkflowRegistry, WorkflowExecutor
)

# Define workflow
workflow = Workflow(
    workflow_id="my_workflow",
    name="My Workflow",
    description="Workflow description",
    required_inputs=["input1", "input2"],  # Required inputs
    input_parameters={                     # Input schemas (optional)
        "input1": {
            "type": "string",
            "description": "Input 1 description"
        }
    },
    steps={
        "step1": WorkflowStep(
            step_id="step1",
            name="Step1",
            command_type="tool_execution",
            command_data={"tool_name": "tool1", ...},
            dependencies=[],                          # Step ids that must finish first
            skip_condition="if_failed:upstream_step",  # Skip condition (optional, string)
            fallback_step_id="fallback"               # Fallback step (optional)
        )
    }
)

# Register workflow
WorkflowRegistry.register(workflow)

# Get workflow
workflow = WorkflowRegistry.get("my_workflow")

# List all workflows
all_workflows = WorkflowRegistry.list_all()

# Export for LLM function calling
schemas = WorkflowRegistry.export_for_llm_function_calling()
```

### Executing Workflows

```python
# Execute workflow
executor = WorkflowExecutor()
result = executor.execute_workflow(workflow, motet)

# Or via command
from motet.core.commands.builtin.workflow import workflow_execution
execution_data = workflow.to_execution_data(context_overrides={...})
result = motet.do(workflow_execution, data=execution_data)
```

### Workflows HTTP

Auth: same as other `/api/v1` resources (JWT, API key, or service account). Request/response schemas: interactive docs at `/redoc` or `motet/interfaces/api/v1/workflows.py`.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/workflows` | List registered workflow templates |
| POST | `/api/v1/workflows/execute` | Execute a registered workflow by id (or ephemeral steps when supported) |
| POST | `/api/v1/workflows/validate` | Dry-run parse/allowlist a YAML or JSON definition (`user.*` namespace) |
| POST | `/api/v1/workflows/register` | Validate and register a durable `user.*` workflow (callable as `workflow_<id>`) |
| DELETE | `/api/v1/workflows/{workflow_id}` | Unregister a `user.*` workflow (ownership enforced) |
| GET | `/api/v1/workflows/{workflow_id}/export` | Export a `user.*` workflow as bundle-shaped YAML |

CLI mirrors: `motet-cli workflows list|validate|register|unregister|export|execute`. Agents can use the `core.workflow_builder` tool with the same modes. Product overview: [Runtime-authored workflows](./11-workflow-system.md#runtime-authored-workflows-user).

Run control (pause/resume/cancel) uses `/api/v1/workflows/runs/...` — see the REST prefix table below and [Workflow runs](./11-workflow-system.md#workflow-runs-pause-resume-cancel).

## Conversations API

Conversations are chat sessions for a principal in a tenant. The API lists, retrieves, renames, clears, and deletes conversations. Auth: same as chat (JWT, X-API-Key, or service account). For using conversations from **commands** (list/get/clear/register/rename via `motet.conversations`), see [Conversations](./07b-conversations.md).

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/conversations` | List conversations (id, title, created_at, updated_at) |
| GET | `/api/v1/conversations/{id}` | Get conversation details (history, counts, optional summary) |
| PATCH | `/api/v1/conversations/{id}` | Rename conversation (body: `{"title": "..."}`) |
| POST | `/api/v1/conversations/{id}/clear` | Clear conversation (registry + memory/vector) |
| DELETE | `/api/v1/conversations/{id}` | Delete conversation (same as clear) |

New conversations are created implicitly when starting a chat with a new conversation ID; no separate create endpoint.

## Surfaces catalog API

Operator-managed catalog of **conversation surfaces** (channels / apps such as `demo_chat`, `openai_compat`, `cursor_ide`). Used by Chat Explorer pickers and the manage-app Surfaces page. Chat does not auto-create catalog entries — register via REST, CLI, manage UI, or bundle `config/surfaces.yaml`.

All endpoints require authentication. Create/update/delete require an admin role. Builtin surfaces cannot be deleted.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/surfaces` | List catalog surfaces (seeds builtins on first read). Response includes `can_manage`. |
| POST | `/api/v1/surfaces` | Create surface (body: `id`, optional `display_name`, `description`). |
| GET | `/api/v1/surfaces/{surface_id}` | Get one surface. |
| PATCH | `/api/v1/surfaces/{surface_id}` | Update display name / description. |
| DELETE | `/api/v1/surfaces/{surface_id}` | Delete a non-builtin surface. |

Agent allow-lists: `PUT /api/v1/agents/{qualified_id}/surfaces` (body: `allowed_surface_ids` or `clear: true`). Null/empty allow-list means all catalog surfaces.

CLI: `motet-cli surfaces …` (see [CLI Reference](./37-motet-cli-reference.md)).

## Tenants and Motets catalog API

Operator-managed catalog of **tenants** (organizations) and nested **Motets** (deployment environments such as `prod` / `staging` / `dev`). Used by the manage-app scope selector. This is separate from JWT/service-account identity claims — see [Security & Multi-Tenancy](./22-security-multi-tenancy.md).

All endpoints require authentication. Create/update/delete and `ensure-defaults` require an admin role.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/tenants` | List visible tenants. Query: `include_motets`, `status`. Response includes `can_access_all_tenants`. |
| POST | `/api/v1/tenants` | Create tenant (body: `id`, optional `name`, `description`, `status`). |
| POST | `/api/v1/tenants/ensure-defaults` | Idempotently seed default/demo catalog entries. |
| GET | `/api/v1/tenants/{tenant_id}` | Get one tenant. Query: `include_motets`. |
| PATCH | `/api/v1/tenants/{tenant_id}` | Update tenant name/description/status. |
| DELETE | `/api/v1/tenants/{tenant_id}` | Delete tenant. Query: `force=true` to also remove Motets. |
| GET | `/api/v1/tenants/{tenant_id}/motets` | List Motets for a tenant. Query: `status`. |
| POST | `/api/v1/tenants/{tenant_id}/motets` | Create Motet (body: `id`, optional `name`, `description`, `status`). |
| GET | `/api/v1/tenants/{tenant_id}/motets/{motet_id}` | Get one Motet. |
| PATCH | `/api/v1/tenants/{tenant_id}/motets/{motet_id}` | Update Motet name/description/status. |
| DELETE | `/api/v1/tenants/{tenant_id}/motets/{motet_id}` | Delete Motet. |

CLI: `motet-cli tenants …` (see [CLI Reference](./37-motet-cli-reference.md)).

## Cost and usage API

Cost tracking and budget management. All endpoints require authentication; costs are scoped to tenant.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/cost/summary` | Daily cost summary (tokens, cost USD, cache savings). Query: `date`, `tenant_id` |
| GET | `/api/v1/cost/summary/by_principal` | Daily cost broken down by principal. Query: `date`, `tenant_id` |
| GET | `/api/v1/cost/usage` | Usage summary with budget status (ok, warning, critical, exceeded). Query: `date`, `tenant_id` |
| GET | `/api/v1/cost/budget` | Get budget configuration (daily/monthly limits, alert threshold) |
| PUT | `/api/v1/cost/budget` | Update budget configuration (admin or budget_admin role). Body: limits, alert_threshold_pct |
| GET | `/api/v1/cost/events` | Recent cost events from Redis stream. Query: `count`, `start_id`, `tenant_id` |

### Querying across tenants

`tenant_id` accepts three forms on `summary`, `summary/by_principal`, `usage`, and `events`:

| Value | Result |
|-------|--------|
| omitted | Your own tenant |
| a tenant id (e.g. `acme`, `motet-global`) | That tenant only. `motet-global` is the platform tenant, not a fleet total |
| `__all__` | Every tenant in the catalog, summed. Callers without global scope get their own tenant instead |

Aggregate responses set `tenant_id` to `__all__` and list the tenants included in `aggregated_tenant_ids`. Because budgets are configured per tenant, an aggregate `usage` response returns empty `limits` and `budget_status: "not_applicable"` rather than combining limits.

```bash
# Fleet-wide spend today
curl -H "Authorization: Bearer $TOKEN" \
  "$MOTET_API/api/v1/cost/summary?tenant_id=__all__"

# Platform tenant only
curl -H "Authorization: Bearer $TOKEN" \
  "$MOTET_API/api/v1/cost/summary?tenant_id=motet-global"
```

## Artifacts API

Artifacts store large/binary payloads (user uploads, tool outputs) outside conversation memory. See [Artifacts and Multimodal Context](./20a-artifacts-and-multimodal-context.md) for full details.

**REST endpoints** (authenticated):

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/artifacts` | List artifacts (query: `kind`, `conversation_id`, `limit`, `offset`) |
| POST | `/api/v1/artifacts` | Upload file; body: multipart form; returns `artifact_id`, `filename`, `content_type`, `bytes`, `kind` |
| GET | `/api/v1/artifacts/indexing-status` | Bulk preparation/indexing status: repeat query param `artifact_id` (max 80); returns per-strategy chunk counts, prep state, index health, and eligibility |
| GET | `/api/v1/artifacts/{id}/metadata` | Get artifact metadata |
| PATCH | `/api/v1/artifacts/{id}/metadata` | Merge custom artifact metadata and normalized `artifact_tags`; tags append by default unless `merge_artifact_tags=false` |
| GET | `/api/v1/artifacts/{id}/download` | Download artifact bytes (scoped by tenant/principal) |
| GET | `/api/v1/artifacts/{id}/preview` | Preview (e.g. image) when supported |
| POST | `/api/v1/artifacts/{id}/reindex` | Queue preparation/indexing for the resolved source; optional `strategy_id`; pass `wait=true` for synchronous debug/test execution |
| GET | `/api/v1/artifacts/reindex-tasks/{task_id}` | Get queued/running/completed reindex task status |
| PATCH | `/api/v1/artifacts/{id}/indexing-policy` | Enable or disable durable indexing eligibility and optional disabled strategies for an artifact source |
| GET | `/api/v1/artifacts/preparation/strategies` | List registered preparation strategies |
| POST | `/api/v1/artifacts/preparation/plan` | Dry-run preparation strategy selection for a prospective artifact |
| DELETE | `/api/v1/artifacts/{id}` | Delete artifact |

Metadata patch requests accept `metadata`, optional `artifact_tags`, and optional `merge_artifact_tags`:

```json
{
  "metadata": {"source": "memo", "memo_asset_id": "draft_123"},
  "artifact_tags": ["jersey", "signed"],
  "merge_artifact_tags": true
}
```

**Programmatic** (artifact store):

```python
from motet.core.artifacts import get_artifact_store, ArtifactKind

store = get_artifact_store()
artifact_id = store.put(payload=..., content_type="...", kind=ArtifactKind.USER_UPLOAD, tenant_id=..., principal_id=...)
payload = store.get(artifact_id, tenant_id=..., principal_id=...)
```

## Chat API

The chat API supports both REST (SSE streaming) and WebSocket interfaces. Both use the same event vocabulary and per-agent attribution.

### REST (SSE)

`POST /api/v1/chat` with `stream: true` returns an SSE stream (`text/event-stream`). The request body:

```json
{
  "messages": [{"role": "user", "content": "Hello"}],
  "stream": true,
  "conversation_id": "conv-abc",
  "agent_id": "core.default",
  "surface_id": "demo_chat",
  "overrides": {
    "model_provider": "anthropic",
    "model_name": "claude-sonnet-4-20250514",
    "enable_thinking": true,
    "reasoning_effort": "high"
  }
}
```

**Optional `prefilled_tool_calls`.** For a fully deterministic turn — where the
caller already knows both the tool(s)/workflow(s) to run and their arguments —
the request may include a `prefilled_tool_calls` list:

```json
{
  "messages": [{"role": "user", "content": "Run the assessment"}],
  "agent_id": "core.default",
  "prefilled_tool_calls": [
    {
      "tool_name": "workflow_my_workflow",
      "arguments": {"asset_id": "asset-123"}
    }
  ]
}
```

When present, the agent runs those tool call(s) as the turn's **first action**
without a planning model call, then continues normally. Multiple entries execute
together in parallel (as a model could emit parallel tool calls in one turn); a
single deterministic action is just a one-element list. For convenience a single
object is also accepted and coerced to a one-element list. Each tool/workflow
must exist and must be permitted by the agent's tool filter; an unknown or
excluded tool fails the turn. Omit the field for normal model-driven turns.

The SSE stream emits events including: `token`, `thinking`, `turn`, `step`, `reasoning_step`, `reasoning_meta`, `reasoning`, `conversation_analyzed`, `workflow_step`, `tool_execution_started`, `tool_execution_completed`, `tool_execution_failed`, `agent_turn_start`, `agent_turn_complete`, `auth_required`, `end`, `error`. All events may include `agent_id` for multi-agent attribution; nested loops also include `parent_agent_id`. See [Streaming Responses](./13-streaming-responses.md) for full event schemas.

### WebSocket

`WS /api/v1/chat/ws` provides bidirectional real-time chat. Authentication is passed via WebSocket handshake headers (JWT, service account, or dev headers). The client sends JSON messages with the same shape as the REST request; the server sends JSON frames with the same event types.

```javascript
const ws = new WebSocket('ws://localhost:8000/api/v1/chat/ws');
ws.onopen = () => ws.send(JSON.stringify({
  messages: [{ role: 'user', content: 'Hello' }],
  stream: true,
  conversation_id: 'conv-abc',
}));
ws.onmessage = (e) => {
  const data = JSON.parse(e.data);
  // data.token for content, data.event for lifecycle/reasoning/tool events
};
```

### Client-Side Protocol (`@motet/ui-common`)

The `@motet/ui-common` package provides `reduceChatEvent` — a framework-agnostic SSE event reducer that handles all event types, per-agent attribution, thinking traces, and tool executions. This is the recommended approach for TypeScript frontends. See [Chat Explorer & Shared UI Library](./36-chat-explorer.md) for details.

## REST API endpoints overview

All REST APIs use the prefix `/api/v1/{resource}` and shared authentication (see [Security & Multi-Tenancy](./22-security-multi-tenancy.md)). When the API server is running, interactive docs (ReDoc) are available at `/redoc` and on the manage API page.

| Prefix | Purpose |
|--------|---------|
| `/api/v1/auth` | OAuth login flow (login, callback, refresh, logout). See [Security & Multi-Tenancy](./22-security-multi-tenancy.md) for the full flow. |
| `/api/v1/identity` | Current principal, tenant context |
| `/api/v1/tenants` | Tenant / Motet (environment) catalog — list/create/update/delete tenants and nested Motets; `POST /ensure-defaults` for local seed |
| `/api/v1/surfaces` | Conversation surfaces catalog — list/create/update/delete; builtins seeded on list |
| `/api/v1/commands` | Execute commands, list deployments, command status/history |
| `/api/v1/chat` | Chat (POST with SSE streaming), WebSocket (`/ws`). See Chat API section above. |
| `/api/v1/conversations` | List (GET), get (GET /:id), rename (PATCH /:id), clear (POST /:id/clear), delete (DELETE /:id) |
| `/api/v1/artifacts` | List, upload, bulk indexing status, metadata/tag patching, download, preview, reindex/task status, preparation strategies/plan, indexing policy, delete |
| `/api/v1/memories` | Store, browse, recall, tag, forget, consolidate memories |
| `/api/v1/tools` | List tools, MCP discover, plugin load |
| `/api/v1/mcp` | Per-service MCP health and control (ops **MCP Servers** page) |
| `/api/v1/workflows` | List, execute, validate/register/unregister/export `user.*` definitions; see [Workflows HTTP](#workflows-http) |
| `/api/v1/workflows/runs` | List paused workflow runs (`?status=paused`) |
| `/api/v1/workflows/runs/{workflow_run_id}` | Get a paused/running run summary |
| `/api/v1/workflows/runs/{workflow_run_id}/resume` | Resume with tagged payload (`kind`, observations/answers/decision); use `kind=operator` after an operator pause |
| `/api/v1/workflows/runs/{workflow_run_id}/pause` | Operator pause (no-op if already paused; cooperative signal if running) |
| `/api/v1/workflows/runs/{workflow_run_id}/cancel` | Cancel (immediate if paused; cooperative if running; cascades to nested child/parent) |
| `/api/v1/schedules` | CRUD scheduled commands, suspend, resume |
| `/api/v1/tasks` | Live orchestration tasks: list (`GET /live`), get, cooperative cancel (`POST /{task_id}/cancel`). Manage Tasks uses cancel on running rows. |
| `/api/v1/workers` | Worker status, readiness, warmup, lifecycle |
| `/api/v1/version` | Motet product versions for this API process, registered workers, and configured siblings (embedding-server, mcp-manager), plus a `skew` flag (authenticated) |
| `/api/v1/models` | List models (includes `requires_api_key` / `has_api_key`), get/update model config. Catalog: [Supported models](./03a-supported-models.md) |
| `/api/v1/vault` | Credentials (store, retrieve, list, delete), MCP environment/servers, health, stats, metrics |
| `/api/v1/debug` | Task flow, commands, traces, memory stats (admin + `MOTET_DEBUG_MODE=true`) |
| `/api/v1/events` | SSE stream of the caller’s tenant EventBus events (command/turn lifecycle; not the chat token stream) |
| `/api/v1/service-accounts` | Service account CRUD |
| `/api/v1/oauth` | OAuth proxy for MCP tools (third-party API auth). User login uses `/api/v1/auth`. |
| `/api/v1/cost` | Cost summary, usage with budget status, budget config (GET/PUT), cost events |
| `/api/v1/developer-docs` | List/get developer onboarding markdown, grouped by nav section, plus product `version` (no auth) |
| `/api/v1/developer-docs/search` | Lexical search of title and body (`?q=`; no auth) |

Details for a given resource live in the corresponding topic (e.g. [Artifacts](./20a-artifacts-and-multimodal-context.md), [Streaming](./13-streaming-responses.md), [Schedules](./12-scheduled-commands.md), [Security](./22-security-multi-tenancy.md)). Conversations and cost/usage are documented in the tables above. For request/response schemas, use the interactive docs at `/redoc` or the source under `motet/interfaces/api/v1/`.

### Stack version

Authenticated read of Motet product versions on the running stack. Configured siblings (`MOTET_EMBEDDING_ENDPOINT`, `MOTET_MCP_MANAGER_ENDPOINT`) are probed via their health endpoints; unconfigured siblings are omitted. `GET /health` stays a liveness probe and does not include versions. `motet-cli --version` is the local package only.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/version` | API process version, each registered worker's stamped version, configured sibling versions (embedding-server, mcp-manager), and `skew` when any worker or configured sibling is unreachable, missing a version, or disagrees with the API. |

```bash
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/v1/version
```

## OpenAI-compatible API

Motet can also serve the OpenAI HTTP API, so tools that already speak it — Cursor, Open WebUI, the OpenAI SDKs, LangChain — can use Motet by pointing at a base URL and passing a Motet service account token as the API key.

This surface is **disabled by default**. Enable it with `MOTET_OPENAI_COMPAT_ENABLED=true`; it mounts at `/v1` (configurable via `MOTET_OPENAI_COMPAT_PREFIX`), deliberately outside `/api/v1` because OpenAI clients hard-code the path suffix.

### Where this is documented

This section is the product overview. Deeper operator detail (wire quirks, session banner, thinking, streaming tool-call fragments) lives in the package README next to the implementation.

| Topic | Where |
|-------|--------|
| Enable flag, routes, modes, sessions (overview) | This section |
| Env vars | [Configuration Reference](./29-configuration-reference.md#openai-compatible-api) |
| Service-account `--facade-mode` / `--allowed-models` | [Security & Multi-Tenancy](./22-security-multi-tenancy.md) |
| Conversation `surface_id` from agent allow-list (facade) | [Conversations](./07b-conversations.md), [Chat Explorer](./36-chat-explorer.md) |
| Passthrough gateway cookbook (multi-provider drop-in) | [`openai-gateway`](../../motet-sdk/examples/bundles/openai-gateway/) example |
| Cursor / IDE agent mode + client-tool handback | [`cursor`](../../motet-sdk/examples/bundles/cursor/) example; [Example Bundles](./26-example-bundles.md#cursor--openai-compatible-ide-backend) |
| Wire behavior, session continuity for stateless clients, thinking, full config | [`motet/interfaces/api/openai_compat/README.md`](../../motet/interfaces/api/openai_compat/README.md) |

| Endpoint | Purpose |
|----------|---------|
| `GET /v1/models` | Models the credential may use, filtered by its allowlist |
| `POST /v1/chat/completions` | Chat Completions, streaming and non-streaming |
| `POST /v1/responses` | Responses API, streaming and non-streaming |

`/chat/completions` also accepts Responses-shaped bodies (an `input` field instead of `messages`) for clients such as Cursor that post that shape to the chat path. Either inbound shape, and either route, can back a model whose provider adapter is Chat Completions — Motet translates at the edge and picks the outbound adapter from the model registry.

```python
from openai import OpenAI

client = OpenAI(base_url="https://motet.example.com/v1", api_key="sa_2026...")
client.chat.completions.create(
    model="openai/gpt-4o-mini",
    messages=[{"role": "user", "content": "hello"}],
)
```

Model ids are `provider/registry_key`, matching what `GET /api/v1/models` reports.

### Execution modes

A credential is bound to one of three depths when it is created, and that binding is also its ceiling — a request can ask for a weaker mode but never a stronger one. Use this table to pick a mode; the rows are what that mode enables on the OpenAI `/v1` wire.

| | `passthrough` (default) | `hosted_tools` | `agent` |
|--|-------------------------|----------------|---------|
| **What it is** | Multi-provider model gateway | Gateway + Motet runs allowlisted tools server-side | OpenAI wire in front of the Motet agent stack |
| **Cost / budgets / traces** | Yes | Yes | Yes |
| **Registry models & vault credentials** | Yes | Yes | Yes |
| **Client `model` is binding** | Yes (no silent swap) | Yes | Motet may apply agent routing / profile policy |
| **Client-executed tools** (IDE, local shell, etc.) | Yes — client owns the whole tool loop | Mixed — Motet tools run here; client-declared tools come back as `tool_calls` | Mixed by default — Motet suspends and hands back client tools; resume continues the same turn. Set `MOTET_OPENAI_COMPAT_AGENT_CLIENT_TOOLS=false` to ignore client tools |
| **Motet tools / MCP / `workflow_*`** | No | Yes (`MOTET_OPENAI_COMPAT_HOSTED_TOOLS_ALLOWLIST`) | Yes (agent tool filter / discovery) |
| **Memory & conversation continuity** | No Motet memory | No Motet memory | Yes (stable Motet conversation) |
| **Artifact RAG / citations** | No | Only if a Motet tool reads/writes artifacts | Yes |
| **Latency class** | Lightest | Medium (bounded tool loop) | Heaviest (full agent turn) |
| **Needs Responses provider adapter?** | No | No — Motet tools use normal Chat Completions–style tool calls | No |
| **Best for** | Open WebUI, SDKs, CLIs, “just give me models” | Server-side Motet/MCP tools without memory/RAG | Cursor / IDE backends that want Motet memory, tools, and client IDE tools together |
| **Cookbook** | [`openai-gateway`](../../motet-sdk/examples/bundles/openai-gateway/) | Same gateway setup + allowlist + `--facade-mode hosted_tools` | [`cursor`](../../motet-sdk/examples/bundles/cursor/) |

Mode selection, most specific first: request `motet_mode` / `X-Motet-Facade-Mode` (only when `MOTET_OPENAI_COMPAT_ALLOW_REQUEST_MODE_OVERRIDE=true`), then a model-id suffix such as `openai/gpt-4o-mini:agent`, then the service-account binding. The bound mode remains the ceiling.

Bind mode and model access when you mint the token:

```bash
motet-cli service-account create \
    --name cursor-desktop \
    --roles member \
    --facade-mode passthrough \
    --allowed-models openai/gpt-4o-mini,anthropic/*
```

Model access is deny-by-default: a token with no allowlist can call nothing. To pick a weaker mode per request, append it to the model string, for example `openai/gpt-4o-mini:passthrough`.

### Sessions and correlation

Multi-turn continuity works through the standard OpenAI fields. `conversation` or `previous_response_id` maps to a Motet conversation, which is what scopes memory and retrieval in `agent` mode; the two fields are mutually exclusive, as they are with OpenAI. Both `/responses` (`resp_...`) and `/chat/completions` (`chatcmpl_...`) ids are recorded, so a hybrid client can chain `previous_response_id` off either. Every response also carries `X-Motet-Task-Id`, `X-Motet-Conversation-Id`, `X-Motet-Facade-Mode`, and `X-Motet-Model` so you can join a client request to Motet traces and cost records.

Stateless clients (for example Cursor) often resend the full transcript without those fields. In `agent` mode Motet recovers continuity with a visible session banner and optional transcript fingerprinting — see the package README for `MOTET_OPENAI_COMPAT_SESSION_BANNER` and `MOTET_OPENAI_COMPAT_INFER_SESSION`.

A few parameters are rejected rather than silently ignored, because a client could not tell the difference otherwise: `n` greater than 1, `logprobs`, and `top_logprobs`.

## Memory API

### Store Memory

```python
# Store in working memory
motet.memory.store(
    content="Temporary data",
    tags=["wm", "temporary"]
)

# Store in short-term memory
motet.memory.store(
    content="Session data",
    tags=["stm", "session"]
)

# Store in long-term memory
motet.memory.store(
    content="Permanent knowledge",
    tags=["ltm", "knowledge"],
    metadata={"key": "value"}
)
```

### Retrieve Memory

```python
# Recall from all tiers (default order: wm → stm → ltm)
memories = motet.memory.recall(
    tags=["important"],
    limit=10
)

# Recall from a specific tier — tiers are tags, so filter on the tier tag
stm_memories = motet.memory.recall(
    tags=["stm", "session"],
    limit=5
)

# Recall semantically rather than by keyword
semantic_memories = motet.memory.recall(
    query="customer feedback",
    tags=["feedback"],
    limit=10
)
```

### Tag Memory

```python
# Add tags to every memory in a conversation scope
motet.memory.tag(conversation_id="abc", tags=["customer", "priority"], op="add")

# Replace the tags on specific IDs
motet.memory.tag(memory_ids=["abc123"], tags=["project", "paid"], op="replace")
```

`op` is `add`, `remove`, or `replace`. Returns `{"updated": int, "ids": [...]}`.

### Forget Memory

```python
motet.memory.forget(memory_ids=["abc123"])
motet.memory.forget(conversation_id="abc", filter_tag="temporary")
```

Same selectors as tag. Returns `{"deleted": int, "ids": [...], "vector_deleted": int}`.

## Tool API

Use **canonical (namespaced) tool names**: built-ins are `core.<name>` (e.g. `core.web_search`), MCP tools are `mcp.<server_id>.<tool_name>`. Do not use wire format (`core__web_search`) when calling from Motet code.

### Execute Tool

```python
# Execute tool (canonical name; params as dict)
result = motet.tools.execute(
    "core.web_search",
    {"query": "value1", "max_results": 5}
)

# Execute with error handling
try:
    result = motet.tools.execute("core.http_get", {"url": "https://example.com"})
except Exception as e:
    logger.error("Tool execution failed", error=str(e))
    raise
```

### Discover Tools

```python
# List all available tools
tools = motet.tools.list()

# Get specific tool (canonical name)
tool_info = motet.tools.get("core.web_search")
```

## Model inference API

Use the canonical model commands (no `motet.agent`):

```python
from motet.core.commands.builtin.model import model_inference, model_stream
from motet.core.commands.command_data_classes import ModelInferenceData, ModelStreamData

# One-shot completion
response = motet.do(model_inference, data=ModelInferenceData(
    messages=[{"role": "user", "content": "Hello!"}]
))

# Streaming (returns generator or stream handle per runtime)
motet.do(model_stream, data=ModelStreamData(messages=[...]))
```

## Vault API

The vault stores credentials (API keys, tokens) with encryption and tenant isolation. **In commands** use `motet.vault`; **from UIs or scripts** use the REST API below.

### Programmatic (inside commands)

```python
from motet.core.security.vault_service import CredentialType

# The vault derives tenant and principal from the command context
context = motet.distributed_context

# Get a credential (decrypted on read); returns None when absent
credential = motet.vault.get_credential("openai_api_key", context)

# Convenience for the common provider-key case
api_key = motet.vault.get_api_key("openai", context)

# Store a credential (encrypted at rest, scoped to this tenant/principal)
motet.vault.store_credential(
    credential_key="openai_api_key",
    credential_data={"api_key": "sk-secret-xyz"},
    context=context,
    credential_type=CredentialType.API_KEY,
)
```

### MCP servers (ops)

Per-service health published by the MCP manager. `GET` uses the same monitoring auth as instance-manager status (no JWT). Mutations enqueue work on the manager; they do not run inside the API process.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/mcp/servers` | List configured MCP services (status, health, instance keys, last error, tool count). |
| POST | `/api/v1/mcp/servers/{service_id}/restart` | Enqueue restart for one service. |
| POST | `/api/v1/mcp/servers/{service_id}/disable` | Enqueue disable (stop children, keep config). |
| POST | `/api/v1/mcp/servers/{service_id}/enable` | Enqueue enable. |
| POST | `/api/v1/mcp/servers/{service_id}/register` | Enqueue register (bundle/YAML add). |
| DELETE | `/api/v1/mcp/servers/{service_id}` | Enqueue unregister. |

Local compose: manager process health is `http://localhost:9191/health`. Vault credential mappings remain under `/api/v1/vault/mcp/*`.

### Vault REST endpoints

All require authentication unless noted. Credentials are scoped by tenant and principal.

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/vault/credentials` | Store a credential (body: credential_id, credential_data, credential_type, scope, security_level, optional tenant_id, motet_id, expires_at, tags, description). |
| POST | `/api/v1/vault/credentials/retrieve` | Retrieve a credential by key (body: credential_key, optional tenant_id, motet_id). |
| GET | `/api/v1/vault/credentials` | List credentials accessible to the current principal. Query: tenant_id, motet_id, credential_type. |
| GET | `/api/v1/vault/credentials/{credential_id}` | Get metadata for one credential (no secret value). |
| DELETE | `/api/v1/vault/credentials` | Delete a credential (body: credential_id). |
| POST | `/api/v1/vault/mcp/environment` | Get environment variables for an MCP server with credentials injected from vault (body: mcp_server_id, optional tenant_id, motet_id). Used when starting or configuring MCP tools. |
| GET | `/api/v1/vault/mcp/servers` | List MCP servers that have registered credential mappings. |
| GET | `/api/v1/vault/health` | Vault health check (no auth). |
| GET | `/api/v1/vault/stats` | Vault statistics for dashboards (total/active/expired credentials, vault status). No auth for ops compatibility. |
| GET | `/api/v1/vault/metrics` | Vault client metrics (auth required). |

The **Chat Explorer** app does not call the vault API directly; when tools (e.g. MCP) need API keys, the backend resolves them via the vault in commands. The **manage/ops UI** can use list, stats, and health to show vault status. For UIs that let users store or manage credentials, use the credentials endpoints above. See [Security & Multi-Tenancy](./22-security-multi-tenancy.md) for encryption and tenant isolation.

## Event Bus API

Publishing is synchronous, and the event type is carried in `kind` — not
`type`. A dict without `kind` is published as event type `"unknown"`.

```python
# Publish an event
motet.publish_event({
    "kind": "custom_event",
    "source": "my_command",
    "data": {"order_id": "123"},
})

# Equivalent, using the bus directly
motet.event_bus.publish({"kind": "custom_event", "data": {"order_id": "123"}})
```

To react to events inside a command, use the `observe_events` context manager.
It registers and unregisters the observer for you, so there is no
long-lived `subscribe` call to clean up.

```python
seen = []

with motet.observe_events({"custom_event"}, seen.append):
    motet.do(work_command, data=WorkData(...))

# `seen` now holds the Event objects published during that block
```

`observe_events(event_types, callback, priority=None, custom_filter=None)`
takes a **set** of event type names, and the callback receives an `Event`
object (with `.event_type`, `.data`, `.source`, `.priority`), not the raw dict.

## CLI reference

The **motet-cli** (or **motet-cli** entry point) mirrors API capabilities by domain. Run `motet-cli --help` for top-level groups and `motet-cli <group> --help` for subcommands.

| Group | Purpose | Example |
|-------|---------|--------|
| `command` | Command deployment and execution | `motet-cli command run <name>` |
| `chat` | Chat with the agent (uses API) | `motet-cli chat --message "Hello"` |
| `models` | List/query models | `motet-cli models --provider openai` |
| `tools` | Call tools | `motet-cli tools call --name <tool>` |
| `memories` | Inspect, consolidate, retrieve, store | `motet-cli memories store --content "..."` |
| `traces` | List/show/watch/replay traces | `motet-cli traces list` |
| `database` | DB operations (e.g. pgvector migrate) | `motet-cli database migrate-pgvector` |
| `service-account` | Service account CRUD | `motet-cli service-account create/list/revoke` |
| `artifacts` | Artifact ls/put/get/rm/info, indexing status, reindex, strategies, plan, reindex task status, indexing policy | `motet-cli artifacts indexing-status <artifact_id>` |

CLI commands typically call the same backend as the REST API. Configuration (base URL, auth) is shared; see [Configuration Reference](./29-configuration-reference.md) and [Local Development Setup](./14-local-development-setup.md).

## Next Steps

- **[Configuration Reference](./29-configuration-reference.md)** - Complete config guide
- **[Troubleshooting Guide](./30-troubleshooting-guide.md)** - Solve problems
- **[Contributing Guide](./32-contributing-guide.md)** — feedback and pilots welcome at `hello@motet.dev`

## Navigation

- **[← Back to Documentation Home](./00-landing-page.md)** - Main documentation hub

---

**Last Updated**: 2026-08-25

**Ready for configuration?** Continue to [Configuration Reference](./29-configuration-reference.md).
