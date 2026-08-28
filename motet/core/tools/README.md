## Package: tools

**Comprehensive tool ecosystem** with distributed execution, MCP integration, and intelligent discovery for AI operations.

### Purpose
- **Distributed Tool Execution**: All tool operations execute as distributed commands
- **MCP Integration**: Robust Model Context Protocol server management and discovery
- **Intelligent Discovery**: Embedding-first tool selection
- **Parameter Injection**: Server-side injection of context and credentials after LLM selection
- **Built-in Tools**: Enhanced HTTP, file, memory, and browser automation tools
- **Context Management**: Priority-based content processing and truncation

### Core Components

#### MCP Integration (Motet Streams Architecture)
- **`mcp_motet/`**: Motet Streams MCP integration
 - **`mcp_motet/manager/`**: Sibling MCP process owner (lifecycle, supervisor, Redis control plane;). Public import remains `proxy.mcp_instance_manager`. ``GET /health`` includes ``motet_version`` for ``GET /api/v1/version``. HTTP `start_server` sidecars reclaim leftover containers for the same `service_id` / host port after manager restart so bind failure cannot leave the service `failed` forever. Attach-to-singleton HTTP instances (second tenant/motet key, same fixed port) reuse the owner’s rewritten Docker `base_url` instead of probing `127.0.0.1` inside the manager container.
 - **`mcp_motet/client/motet_mcp_client.py`**: Lightweight Redis Streams client for workers. I/O stream keys are `[{tenant_id}:]mcp:[{manager_id}:]mcp-…` (issue #235 /). `motet:mcp:` signals stay shared.
 - **`mcp_motet/proxy/motet_mcp_proxy.py`**: MCP server proxy with Motet Streams bridge
 - **`mcp_motet/protocol.py`**, **`mcp_motet/stream_encryption.py`**: Stream envelope and at-rest encryption for the control plane
 - **`mcp_motet/transports/`**: Stdio and HTTP transport implementations

#### Tool Discovery & Execution
- **`distributed_discovery.py`**: Embedding-first `ToolDiscoveryService` — ranked `ToolCandidate` lists via `FunctionDiscoveryVectorStore`. Shared types: `ToolDiscoveryContext`, `ToolCandidate`. No native function-calling path (NFC removed, #112).
- **`function_discovery_vector_store.py`**: Hybrid search (Valkey vector KNN + app-layer keyword fusion) across tools, workflows, and distributed commands
- **`parameter_injection.py`** / **`parameter_sources.py`**: `ParameterInjectionService` fills user context, credentials, and system parameters *after* LLM tool selection; `ParameterSource` classifies where each parameter may originate
- **`meta_tool_policy.py`**: Shared authorization for `core.tool_call` and disclosure filtering for `core.tools_search`, so generic dispatch cannot bypass an agent's `ToolFilter`
- **`schema_normalizer.py`**: Normalizes Pydantic, MCP, and OpenAPI schemas to one shape
- Tool parsing (`parse_line`) lives in `registry.py`, not a separate parser module
- **Command**: `tool_discovery` (`motet/core/commands/builtin/tool.py`) — worker-side embedding discovery returning candidate payloads for a query

#### Built-in Tools (`builtin/`)
- **HTTP Tools**: Enhanced web requests with content extraction and browser automation
- **File Tools**: `core.file_read` / `core.file_write` / `core.file_edit` / `core.file_search` / `core.file_grep` require **`EDGE_EXECUTION`** plus **`EDGE_FILE_READ`**, **`EDGE_FILE_WRITE`**, or **`EDGE_FILE_SEARCH`** respectively (device worker with mounts/allowlists only; not cloud workers). `core.file_edit` does exact string replacement (unique match unless `replace_all`); `core.file_grep` searches file contents with regex (ripgrep when available, Python fallback). Generic **`file_operations`** remains for other commands (e.g. artifacts) that still run on cloud workers. The self-hosted **app-builder** worker (own compose project `motet-sdk/examples/bundles/app-builder/deploy/docker-compose.yml`, managed via `deploy/app-builder.sh`) is an edge worker whose allowlists are locked to `/srv/app-builder/imf`.
- **Clipboard Tools**: `core.clipboard_read` / `core.clipboard_write` (`EDGE_EXECUTION` + `EDGE_CLIPBOARD`). `motet-cli device start` spawns a **host** loopback bridge and sets `MOTET_CLIPBOARD_BRIDGE_*` so Docker workers reach the real OS clipboard; `--no-clipboard-bridge` skips that.
- **Host exec**: `core.host_exec` (`TOOL_EXECUTION` + `EDGE_EXECUTION` + `EDGE_SHELL_EXEC`) runs **argv-only** subprocesses on the **host** via **`device start --shell-exec-bridge`** + host **`MOTET_SHELL_BRIDGE_CWD_ALLOWLIST`** (optional **`MOTET_SHELL_BRIDGE_COMMAND_ALLOWLIST`**). The caller-facing schema does **not** accept `cwd`; the tool always generates a unique per-call run dir under **`MOTET_HOST_EXEC_DEFAULT_CWD_ROOT`** (or first bridge allowlist prefix) and returns it as `effective_cwd`. Bridge-only (no in-container fallback).
- **Worker exec**: `core.worker_exec` (`TOOL_EXECUTION` + `WORKER_SHELL_EXEC`) runs argv in the **worker** domain via `motet.core.execution.run_execution`. The caller-facing schema does **not** accept `cwd`; the tool always generates a unique per-call run dir under **`MOTET_WORKER_EXEC_DEFAULT_CWD_ROOT`** (or first worker allowlist prefix, falling back to `/var/motet/worker-exec`). **`MOTET_EXEC_BACKEND=subprocess`** (default) runs in the worker process; **`docker`** / **`container`**, **`kata`**, and **`kata-fc`** run a disposable container via the Engine HTTP API (unix socket; Kata sets **`HostConfig.Runtime`**), bind-mounting an allowlisted cwd on Linux nodes with the runtime registered. Requires **`MOTET_WORKER_EXEC_CWD_ALLOWLIST`**. Optional **`bundle_id`** stages the deployed bundle from `MOTET_PLUGIN_ROOT/<bundle-slug>/` into the generated cwd and merges **`oci_image_ref`** from the published bundle catalog when **`config/exec.yaml`** defines an **`exec`** block (Phase 3). That file may declare **`requirements_path`**; validate stores **`requirements_sha256`** in the catalog for CI image builds. Images create **`/var/motet/worker-exec`** (0700); avoid world-writable **`/tmp`** outside quick local tests. See `29-configuration-reference.md` for **`MOTET_WORKER_EXEC_DOCKER_*`**, **`MOTET_DOCKER_*`**, and **`MOTET_KATA_DOCKER_RUNTIME`**, and for the no-`cwd`-in-schema policy.
- **Edge exec**: `core.edge_exec` (`TOOL_EXECUTION` + `EDGE_EXECUTION` + `WORKER_SHELL_EXEC`) is the edge-routed sibling of `core.worker_exec` — same schema and `run` implementation, but the extra `EDGE_EXECUTION` capability means the router only places it on edge workers (never cloud workers' disposable containers). Use it when argv must run against an edge-mounted working tree, e.g. `git` / targeted pytest in the app-builder clone at `/srv/app-builder/imf`. Plain `core.worker_exec` matches both cloud and edge workers, so routing is non-deterministic for those workloads. Both tools set `contextualize_observation=False` so programmatic callers (e.g. `app-builder.run_tests`) receive intact `stdout`/`stderr`/`returncode`.
- **Process control**: `core.process_control` (`EDGE_EXECUTION` + `EDGE_PROCESS_CONTROL`) lists or terminates **host** processes whose cwd/exe is under the bridge allowlist via **`device start --process-control-bridge`**, host **`MOTET_PROCESS_CONTROL_CWD_ALLOWLIST`** or **`MOTET_SHELL_BRIDGE_CWD_ALLOWLIST`**. Terminate allows **SIGTERM** / **SIGKILL** / **SIGINT** only.
- **Memory Tools**: `core.memory_store` / `core.memory_recall` for remember + hybrid look-up; `core.memory_tag` for targeted retag
- **Artifact Tools**: `core.search_artifacts` performs scoped artifact RAG retrieval for agent follow-up searches using the same policy and provenance backend as context preparation. It supports deterministic narrowing by selected artifact IDs and artifact tags. `core.artifact_read` returns windowed full derived text for a source or derived artifact ID, and `core.artifact_view` stages video poster/keyframe images for visual inspection; the agentic loop delivers staged frames as a correlated sidecar user message. All three are pinned into the exposed tool set on turns whose user message carries attachments.
- **Browser Tools**: Playwright-based web automation and screenshot capabilities
- **Utility Tools**: Math evaluation, note-taking, and tool introspection

#### Infrastructure
- **`registry.py`**: Central tool registry with validation and execution
- **`schema_exporter.py`**: Registry → `CanonicalToolSchema` only. Provider JSON and name sanitizing are adapter-owned (`motet/core/commands/builtin/model.py` outbound + `inbound_tool_call_request`). Dispatch does not convert leftover `mcp__` / `core__` names (issue #225).
- **`tool_calls_parser.py`**: Extracts `tool_calls_canonical` from model_inference payloads (no leftover `tool_calls` fallback; issue #225).
- **`context_manager.py`**: Priority-based content processing and truncation
- **`protocol.py`**: Consistent result formatting and error handling. `ok(..., cache_control=...)` stamps a freshness directive (`no-store` / `same-turn` / `max-age=N`).
- **`cache_control.py`**: Response-level observation freshness. The agentic loop replays a short `[cached]` notice on a fresh same-signature hit instead of re-executing. Default is `no-store`. Snapshot built-ins (`core.http_get`, `core.http_get_browser`, `core.web_search`) attach `same-turn` on a usable success. `core.http_post` does not. Origin HTTP `Cache-Control` is not forwarded. Keys inherited from `core.spawn_agents` are marked `inherited_from=spawn`; that hit points at the spawn observation because the parent does not have the child's page body, and at the child's tool `artifact_id` when the fetch was offloaded.
- **`result_formatting.py`**: Tool-result previews plus `extract_text_from_mcp_result` (shared MCP envelope unwrap for observations and `core.transform` `mcp_text`)
- **`tool_transcripts.py`**: `ToolInvocation` models — the provider-neutral canonical record of a tool execution
- **`transcript_service.py`**: Rebuilds schema-correct transcripts from persisted `ToolInvocation` records; `parse_and_dedupe_tool_invocation_memories` is shared by `finalize_turn` and `reconstruct_tool_transcripts`
- **`rendering/`**: Transcript renderers (`openai.py`, `plaintext.py`) behind a common `base.py` interface
- **`arguments_offload.py`**: Offloads oversized tool-call arguments to the artifact store (see below)

### Key APIs
- `register(name, description, func, tool_schema=..., triggers=[...], category=...,...)`
 - Validation via Pydantic `schema`
 - Parsing via `triggers` and registry `parse_line`
 - Token estimation via `estimate_tokens` callable or defaults
- Observation controls: `contextualize_observation` (tool result context management/truncation)
 - Resilience: `max_retries`, `retry_backoff_seconds`, breaker tuning
 - Planner hints: `default_timeout_seconds`, `suggested_max_calls`, `cost_class`
 - `data_types`, `keywords`: Tool capability declarations for discovery
 - `context_requirement`: Context management configuration
- `execute(name, params, allow=..., deny=..., timeout=..., role=...)`: Enforces allow/deny, role policies, validation, breakers, retries, metrics/tracing, **automatic context management**
 - Before Pydantic validation, boolean arguments are coerced to `null` for `string | null` fields (small local models emit e.g. `execute_js: false`;) — see `_coerce_boolean_null_params`
- `parse_line(text) -> {name, params, priority}`: Maps inline triggers to tool actions
- `estimate(name, params) -> int`: Token estimate for planning/budgeting
- `describe -> list[dict]`: Introspection for inventory (name, description, category, schema, observation policy, planner hints)
- **`ToolDiscoveryService.discover_tools`**: Embedding-only ranked candidates
- `ContextManager.process_tool_response`: Content processing and truncation
- `ParameterInjectionService.inject`: Fills context, credential, and system parameters after tool selection

### Built-ins
- **HTTP Tools** (category: `http`)
 - `http_get`: Basic HTTP GET for APIs and simple static content
 - `http_get_browser`: Browser-based HTTP GET with JavaScript execution (preferred for web pages)
 - Full browser automation via Playwright, SPA support, iframe access
 - Dynamic content loading, framework detection, screenshot capability
 - Custom JavaScript execution, wait conditions, headless/visible modes
 - `main_content` keeps up to 80k chars (was a silent 10k slice). `content_length` is the pre-clip extract; `truncated` is set when the rail hits. Observation clipping / artifact offload still decide what the model sees.
 - `http_post`: HTTP POST with allow/deny domain checks, per-tool breaker
 - All HTTP tools: context management and priority-based content processing
- **Search Tools** (category: `search`)
 - `core.web_search`: LLM-native web search when the current model supports it **and** returns URL-bearing citations; otherwise ``ddgs`` metasearch (real SERP results); DuckDuckGo Instant Answers is last resort. Optional `provider` / `model_name` params (or stamped `motet.metadata`) enable the LLM path from workflows/bundles. Observability field `web_search_path` is `llm` | `ddgs` | `duckduckgo_instant`. Env: `MOTET_WEB_SEARCH_BACKEND` (`auto`/`llm`/`ddgs`/`instant_answers`), optional `MOTET_WEB_SEARCH_DDGS_BACKEND`.
- **Orchestration Tools** (category: `orchestration`)
 - `core.handoff`: per-agent delegation. In the tool catalog like `core.spawn_agents`. The handler fail-closes unless this agent declared teammates (`AgentConfig.handoffs`) and the target is on that list. The schema is also pinned when the list is non-empty and depth is under 2. The child turn shares principal, tenant, and conversation; the result is one observation with usage roll-up.
 - `core.spawn_agents`: parallel sub-agent fan-out. Takes `tasks: List[{instruction, tools}]` (bare instruction strings are coerced); each becomes one sub-agent via the `agent_loop` command on its own worker, and all results return as a single observation in task order. Rails: width capped at 8 (over the cap is rejected, not truncated); sub-agents inherit the parent's `tool_filter_metadata` with `core.spawn_agents` subtracted, so recursion is blocked by tool removal rather than a depth counter; each task's declared `tools` become the child's catalog (`AgentData.tools`), filtered through the parent's `exclude_tools` so declaring cannot widen a child past its parent and so `core.tools_search` / `core.tool_call` cannot reopen the grant unless the task sets `discover: true` (declared names stay a pin, discovery stays on); children get a static worker system prompt that names the child's 10-round / 8-tool / 60s tool-time rails (identical across siblings so the cache prefix holds) rather than the parent transcript or the Motet assistant fallback; the loop stops a child when accumulated join wall-clock tool time hits 60s (`max_tool_time`; parent turns stay off); the live observation is each child's full write-up (a tool artifact is only a clip sidecar); children write tokens and thinking to the parent task stream with ``agent_id`` ``{parent}.spawn-N``; successful write-ups are stored on the parent conversation as non-root transcript rows so a refresh can rebuild the nested turn; the parent inherits the children's snapshot-tool keys so an exact same-turn repeat is a refetch veto pointing at that observation (the parent does not get the child's page body); the loop's trailing wrap-up on the last two rounds is shared with parent turns; no handback tools reach a child; `fail_fast=False` so one dead branch degrades the observation rather than the turn. Budget, stall, and error stops are reported as `incomplete` rather than counted as successes, unless the loop finalized a tools-off write-up (`finalized=True`), in which case the findings count as a success; a fan-out where no child answered fails the tool call. Discovery-mode agents only — a parent with no delegable filter is refused rather than given a guessed one.
- `math_eval` (category: `math`)
- `core.current_time` (category: `system`): wall-clock datetime (UTC + optional IANA timezone). Used when absolute timestamps are needed; delayed schedules should prefer `delay_seconds` on `core.schedule_command`.
- `core.schedule_command` (category: `system`): schedule Motet commands (delayed/recurring/conditional). Delayed runs accept `scheduled_at` (absolute ISO 8601) or `delay_seconds` (relative from now).
- `file_read` (category: `filesystem`) with allowlist and size cap (do not store observations by default)
- `file_write` (category: `filesystem`): write/append UTF-8 under `MOTET_FILE_WRITE_ALLOWLIST`
- `file_edit` (category: `filesystem`,): exact `old_string` → `new_string` replacement; fails on missing or non-unique matches unless `replace_all`
- `file_search` (category: `filesystem`): glob path listing under allowlist
- `file_grep` (category: `filesystem`,): regex content search under allowlist (`rg` preferred)
- `core.memory_store`, `core.memory_recall`, `core.memory_tag`, `core.memory_forget` (category: `memory`)
 - `core.memory_store`: remember-style write via MotetContext (stamps `conversation_id` / `agent_id`). `persist=true` (default) queues LTM vector indexing so hybrid recall can find it. Keyword-pinned on remember/recall intent.
 - `core.memory_recall`: query-based hybrid/semantic recall (`MemoryManager.hybrid_retrieve` / `memory_recall` command). Default agent retrieve path. Chat already injects hybrid results each turn; the tool is for explicit look-up.
 - `core.memory_tag`: add/remove/set tags. Requires `memory_ids`, `conversation_id`, or `filter_tag`.
 - `core.memory_forget`: delete targeted memories from KV and the vector index. Same selectors as tag. Not on the store/recall pin list; forget-intent phrases pin it separately. Does not wrap HTTP operator clear.
 - `core.note`: no-op comment for the current turn only — not persistence.
 - HTTP `POST /api/v1/memories/find` and `/tag` call `MemoryManager` directly (no find-by-tag tool).
- `core.search_artifacts` (category: `artifacts`): searches indexed artifact/document chunks for citation-ready evidence. Conversation scope is the default; broader `principal`/`motet` scopes require deterministic caller metadata and are downgraded to conversation scope otherwise. `artifact_ids` and `artifact_tags` only narrow the resolved scope.
- `core.artifact_read` (category: `artifacts`,): reads full derived text (extracted text or video transcript) by source or derived artifact ID with `offset_chars`/`max_chars` windowing; returns a `not_ready` result while derivation is pending.
- `core.artifact_view` (category: `artifacts`,): selects video poster/keyframe images by `timestamp_ms` or `keyframe_index` (capped by `max_frames`) and returns staged media artifact IDs; the agentic loop appends the frames as `MediaPart`s on a synthetic sidecar user message and evicts stale sidecar images after a fixed number of iterations.
- `tools_list` (category: `system`): list configured tools for planning/reasoning; filters on category/name/mcp; observation contextualized, not stored
- `tool_describe` (category: `system`): describe a single tool including schema and `x-imf` hints; observation contextualized, not stored
- `tools_search` (category: `system`): hybrid embedding + keyword ranking via `FunctionDiscoveryVectorStore` for tools and workflows (same store as shortlist discovery); ranks tools and workflows separately and returns the top `limit` tools plus up to 3 workflows (omitted when they have no score signal versus the tool floor) so an MCP sibling wall cannot hide a composed workflow; lexical substring/regex scan as fallback; schemas included by default for `core.tool_call` disclosure. The Python `include_workflows` flag is hidden from the LLM schema (`x-imf-hide-from-llm`); agent policy uses `ToolFilter.no_workflows` / `exclude_workflows` rather than letting the model opt out of the workflow slice.
- `tool_call` (category: `system`): generic dispatch for authorized tools (`tool_execution`) and `workflow_*` names (`workflow_execution`) without residency. Authorization mirrors disclosure: the agent's `ToolFilter` metadata (`meta_tool_policy`) plus `expose_to_agents`, so anything `tools_search` hides is also undispatchable. The target tool's result is returned **verbatim**, so a dispatched call yields the same payload and observation text as a direct call; `tool_call` only reports its own dispatch-phase failures (unknown name, denial, recursion, parameter validation), tagged `meta.phase = "dispatch"`. Workflows keep an `ok` envelope because `workflow_execution` returns step results rather than a tool payload
- `core.docs_read` (category: `system`): windowed read of the curated developer-onboarding corpus (same filesystem as `GET /api/v1/developer-docs`). Omit `doc_id` to list the agent-facing catalog (`11-workflow-system`, `17-building-workflows`); optional `section` slices a heading. Product docs — not tenant artifacts. Hybrid `docs_search` is not in this slice.
- `core.workflow_builder` (category: `system`): validate / execute / register / unregister / export bundle-shaped YAML as a `user.*` workflow. Description is a short recipe plus a pointer at `core.docs_read` for the YAML contract.

### Advanced Features

#### Tool Discovery System
- **Embedding-first routing**: `FunctionDiscoveryVectorStore` pre-filters the catalog; the agentic loop’s main `model_stream` call selects tools and extracts parameters (no separate NFC LLM round-trip)
- **`ToolDiscoveryService.discover_tools`**: Returns ranked `ToolCandidate` lists from the embedding store (used by `tool_discovery` and other single-shot callers)
- **Context-Aware**: `ToolDiscoveryContext` distinguishes user prompts vs reasoning traces for search queries
- **Workflow Integration**: Workflow schemas are reserved/prepended in the agentic loop tool list; co-service MCP expansion is not used
- **Parameter injection**: Still handled by `ParameterInjectionService` at `tool_execution` time (not by a discovery short-circuit)
- **Hybrid ranking**: Valkey vector KNN fused (RRF) with a BM25-style keyword half — IDF plus document-length normalization over the entry manifest, which carries each item's name, description, and keywords. Workflow entries carry the same description/keywords fields as tools (schema v4); keywords are `workflow_discovery_keywords` (author tags plus tokens from the workflow id, name, and step `tool_name`s) so `workflow_navigate_screenshot` matches "browser" / "playwright" / "url". The post-fusion boost weights coverage of the *original* query terms by IDF, so covering rare terms counts and covering filler does not. Command entry descriptions come from first-class `CommandRegistration.description` (set at `@motet.command` / class registration time from the authoring docstring, tool-parity; #194). Entry shape is versioned (`_ENTRY_SCHEMA_VERSION`); a manifest written with an older shape is rejected so the writer rebuilds, since entry changes do not alter document content hashes.
- **Index integrity**: A full reindex drops and recreates the vector index rather than deleting keys under a live index, because deletions apply to the vector graph asynchronously and rewriting the same keys leaves documents counted in `FT.INFO num_docs` yet unreachable by KNN. After a rebuild, written vs. searchable counts are compared and a mismatch logs `function_discovery_index_incomplete` at error level. `FT.SEARCH` also needs an explicit `LIMIT`: it returns 10 rows by default regardless of the `KNN` count, which silently truncated the fusion pool. `user.*` workflows are indexed as `workflow:{tenant_id}:{id}` from the Redis catalog so two tenants can share the same callable name (issue #234).
- **Cross-worker coordination** (#156): The index is shared and rebuilding it is destructive, so `ensure_shared_index` is the entry point rather than `index_tools_and_workflows` — it adopts a published index when one exists, and otherwise takes the writer lock, re-checks (the previous holder was probably rebuilding), and rebuilds only if still needed. A worker that cannot take the lock waits for the winner instead of rebuilding alongside it; the wait is bounded and giving up logs `function_discovery_writer_lock_wait_timeout`. The manifest is published to Redis (`function_discovery_manifest_redis_key`) alongside the index it describes, because `persist_dir` is a per-container path and workers share no filesystem — a file-only manifest made every restarting worker believe no index existed. Incremental publishes merge into the shared copy (removals tracked explicitly), so a worker holding only part of the catalog cannot evict another's MCP tools.

#### Context Management System
- **Priority-Based Processing**: Critical, High, Medium, Low content prioritization
- **Intelligent Truncation**: Word-boundary aware, preserves important content
- **Tool-Specific Requirements**: Each tool can declare context needs (max_tokens, strategies)
- **Content Strategies**: Truncate, Prioritize, Summarize, Chunk, Compress
- **Automatic Processing**: Integrated into tool execution pipeline

#### Parameter Injection
- **Model fills the schema**: Tool arguments come from the provider's native function calling against the exported JSON schema. There is no separate extraction pass over conversation history.
- **Server-side injection**: `ParameterInjectionService` supplies parameters the model must *not* choose — principal and tenant context, vault credentials, system values — after selection and before execution.
- **Declarative sources**: `ParameterSource` marks each field's origin via Pydantic `Field` metadata, so an injected parameter cannot be overridden by model output.

#### Enhanced HTTP Capabilities
- **Content Extraction**: Automatic JSON, HTML, XML parsing and structuring
- **Modern Web Support**: JavaScript execution, SPA rendering, iframe access
- **API Specialization**: GitHub API, REST API optimized processing
- **Browser Automation**: Full Playwright integration for dynamic content
- **Screenshot Support**: Visual content capture for complex pages

### Tool transcripts + artifact storage
The tool system uses a **two-tier persistence model** to enable **schema-correct tool transcript reconstruction** while keeping large/sensitive payloads out of the hot conversation recall path:

- **`tool_invocation` (conversation memory)**: A structured record (tool name, arguments, tool_call_id, status, timestamps, optional preview) stored in `MemoryManager` for auditing and transcript reconstruction.
- **Tool artifacts (artifact store)**: Optional raw tool payloads stored behind an `ArtifactStoreProtocol` (Redis MVP), referenced by `artifact_id` from the invocation record.

#### Oversized offloads (cost control)
- **Oversized tool results**: Results whose JSON exceeds `tool_result_artifact_min_bytes` (default 8KB) are stored as `TOOL_ARTIFACT` even when the tool is not allowlisted (denylist and sensitive-name deny still apply). These offloads carry a TTL (`tool_result_artifact_ttl_seconds`, default 7 days) and skip derivations — they exist so agents can `core.artifact_read` a clipped observation's full payload within the cycle, not for long-term storage. Allowlisted tool artifacts keep persistent + derived behavior.
- **Oversized tool arguments** (`arguments_offload.py`): Tool-call arguments over `tool_invocation_arguments_max_bytes` are offloaded to `ArtifactKind.TOOL_ARGUMENTS` (`arguments_artifact_id` on the invocation); the inline `arguments_json` becomes a capped valid-JSON preview. Transcript replay hydrates the full unmodified JSON before provider round-trips (`hydrate_transcript_tool_arguments`). If the arguments artifact cannot be stored, the tool call fails rather than persisting truncated arguments that would break replay.

#### Context rebuild (prepare_context)
- `prepare_context` recalls `tool_invocation` items for the conversation and reconstructs **provider-safe** tool transcripts using a renderer:
 - OpenAI-style: `assistant(tool_calls_canonical=[...])` → `tool(tool_call_id=..., content=...)`
- If reconstruction cannot be made schema-correct (invalid/unknown records, missing artifacts), it **fails closed** and omits that segment.

#### Observation / previews (UI + debugging)
Tools may still produce **capped preview text** for inspection and UI display, but previews are **not** the canonical transcript format. The canonical source of truth for replay/reconstruction is `tool_invocation` metadata (+ `artifact_id` when available).

### Reasoning & planning integration (generic & decoupled)
- Planner uses only the registry for parsing and estimating:
 - No hard-coded routing (e.g., `math:`/`http:` fallbacks removed)
 - If parsing fails, a `note` task is emitted
- Budgeting is generic:
 - `token_estimate_overrides: dict[tool_name, int]`
 - `max_calls_by_category: dict[category, int|None]`
 - `max_calls_by_tool: dict[tool_name, int|None]`
 - Category read from `RegisteredTool.category`
- Timeouts: per-tool `default_timeout_seconds` if provided; otherwise global default

### Endpoints
- `GET /api/v1/tools`: Name → `{description, schema}` for every registered tool (auth required)
- `GET /api/v1/tools/describe`: Structured inventory from `describe`, no schemas (auth required)
- `POST /api/v1/tools/execute`: Execute a tool; body is `{"name":..., "params": {...}}`, optional `X-Role` header for role-based allow/deny (auth required)
- `GET /api/v1/mcp/servers`: MCP service inventory and health
- `POST /api/v1/mcp/servers/{service_id}/restart|disable|enable|register`: Manager control plane

### MCP interoperability
MCP servers are owned by the **sibling manager process** (`mcp_motet/manager/`,), not by an in-process client bridge. Workers reach them over Redis Streams via `mcp_motet/client/`; there is no per-worker MCP server, which is why worker memory does not grow with the number of servers attached.

- **Configuration, not discovery calls**: services are declared in `mcp_instance_manager.yaml` (see `config/mcp_instance_manager.yaml`) and started by the manager. Set `MOTET_MCP_ENABLED=true`.
- **Registration**: remote tools register as `mcp.<server_id>.<tool_name>` (canonical dotted form). The `mcp__server__tool` wire form is applied only at the provider boundary — see the MCP Tool Naming rules in `AGENTS.md`.
- **Input schemas**: Pydantic v2 JSON Schema maps directly to MCP `input_schema`.
- **Non-standard metadata** (category, triggers, observation policy, planner hints, retries) is exposed as JSON Schema extensions under `x-imf` in `describe`, so MCP-aware UIs can consume it without breaking standard tooling.
- **Result normalization**: prefers `structuredContent` when present; otherwise aggregates `TextContent` into a single `text` field; otherwise returns raw `content` descriptors. Shared unwrap lives in `result_formatting.extract_text_from_mcp_result`.
- **`mcp_adapters/`**: OpenAPI-backed adapter server plus request-safety checks, for exposing HTTP APIs as MCP tools.

### Examples

Inspect and call tools with the CLI:
```bash
# List registered tools with names and descriptions
motet-cli tools list

# Descriptions for all tools (add --json-output for raw JSON)
motet-cli tools describe

# Call a tool
motet-cli tools call \
 --name mcp.weather.get_current_conditions \
 --params '{"location":"San Francisco, CA"}' \
 --timeout 15
```

Same thing over HTTP:
```bash
curl -X POST \
 -H "Content-Type: application/json" \
 -H "X-API-Key: $MOTET_API_KEY" \
 http://localhost:8000/api/v1/tools/execute \
 -d '{"name":"core.http_get","params":{"url":"https://example.com"}}'
```

In-process, from a command:
```python
result = motet.tools.execute("core.http_get",
 {"url": "https://api.example.com"},
 timeout=30,)
```

Memory tools (chat triggers):
```text
# Retag items in a conversation scope
tag: conversation:abc add customer,priority

# Retag specific IDs (applies to KV and vector when IDs exist in LTM)
tag: ids:abc123,def456 set project,paid

# Recall working memory only
recall: tags=conversation scope=wm limit=3
```

Local quickstart:
```bash
motet-cli local up
# MCP services start from mcp_instance_manager.yaml
# Open http://localhost:8000/chat-explorer
```

### Runtime stack access
- Tools can access runtime services (e.g., memory/vector) via `set_runtime_stack(stack)` wired at startup

### Custom tools
- Custom tools are delivered via **bundles**: add `tools/*.py` to a bundle and deploy; workers load them via the `core.reload_bundle` command with namespaced registration (`bundle_id.tool_name`).
- Author them with `@motet.tool(description=..., name=...)`. Tools are **functions**, not classes — the schema is generated from the signature, so type hints and the description are what the model sees. See `motet-sdk/examples/bundles/*/tools/` for working examples.

### Tests
- `tests/unit/core/tools/` covers per-tool observation policy and categories, registry `describe`, planner budgeting (caps/overrides), and per-tool timeouts.
- Integration tests require the full stack: `docker-compose -f tests/docker-compose.test.yml run --rm test-runner`.
