# OpenAI-Compatible API Facade

Inbound OpenAI wire protocol for Motet. Any client that speaks the
OpenAI API — Cursor, Open WebUI, the OpenAI SDKs, LangChain, LiteLLM — can use
Motet by setting a base URL and supplying a Motet service account token as the
API key.

The facade is **disabled by default**. Enabling it exposes Motet models, and in
the deeper modes Motet tools, to third-party software configured by end users.

## Why this exists

Motet already speaks OpenAI wire *outbound* through its provider adapters. It had
no *inbound* OpenAI surface, so Motet could not back the OpenAI client ecosystem
even though routing, vault credentials, tenancy, budgets, tools, memory, and
artifact RAG already existed on Motet paths. This package adds the inbound half
without weakening the boundary: OpenAI shapes exist only at the HTTP
edge, and every request runs through the same distributed commands as native
traffic.

## Routes

Mounted at `MOTET_OPENAI_COMPAT_PREFIX` (default `/v1`), outside `/api/v1`. This
is the documented exception: OpenAI clients hard-code the path suffix
and only let the user configure a base URL.

| Route | Purpose |
|-------|---------|
| `GET {prefix}/models` | Models this credential may use, filtered by its allowlist |
| `GET {prefix}/models/{id}` | One model card |
| `POST {prefix}/chat/completions` | Chat Completions, streaming and non-streaming |
| `POST {prefix}/responses` | Responses API, streaming and non-streaming |

`/chat/completions` also accepts Responses-shaped bodies (an `input` field
instead of `messages`), because Cursor posts them to that path.

## Execution modes

The same routes run at three depths. The mode is bound to the credential, not
chosen per request.

| Mode | Backed by | What the client gets |
|------|-----------|----------------------|
| `passthrough` (default) | `model_inference` / `model_stream` | Registry models, routing, budgets. The client owns the tool loop, so Cursor Agent keeps its IDE tools. |
| `hosted_tools` | `core.agent_loop` → Turn Runtime `start` (allowlisted Motet tools; client tools as handback) | Motet tools run server-side. Client-owned and mixed turns checkpoint and resume like agent mode. |
| `agent` | Motet agent stack (`agent_turn`) | Memory, artifact RAG, workflows, and transcripts behind the OpenAI wire. Client-declared tools are honored via turn suspension (below). |

### Client tools in agent mode

Agent mode honors tools the client declares in the request. The declared schemas ride
into the agentic loop as *handback tools*: they are offered to the model every
iteration, and on a name collision with a Motet registry tool the client's
schema wins (logged as `agentic_loop_handback_tool_shadows_registry_tool`).
The system prompt enumerates the client's tool names and steers the model to
prefer them for client-environment work (files, shell, workspace) over
similar Motet tools; Motet tools remain preferred for server-side
capabilities (scheduling, web fetch/search, workflows). Client tool names are
used verbatim — never renamed or namespaced — so `tool_calls` echo exactly
what the client declared. When the model calls one, the loop does not execute
**any** of that turn's calls at suspend time — a partial execution would fork
the wire transcript. Instead the turn suspends: its state is checkpointed to
Redis and the **full** call list comes back as a standard `tool_calls`
response (`finish_reason: "tool_calls"`), including any Motet-owned calls in
the same turn so the client's transcript stays provider-valid.

The client then does what every OpenAI tool loop does — runs the tools it
owns and resends the conversation with trailing `role: "tool"` results. The
facade recognizes those `tool_call_id`s via the checkpoint index and resumes
the suspended turn. On a mixed turn (issue #159 execute-at-resume), the
client only needs to cover the externally-owned ids; Motet executes its own
handed-back calls during resume (client observations for those ids are
discarded with a warning — stock frameworks often answer every `tool_calls`
entry). Iteration budget, usage accounting, and memory context are restored
from the checkpoint. If the client omitted session-chaining headers, Motet
may have minted a fresh `openai-{uuid}` for that POST; on a successful resume
the facade rebinds to the checkpoint's conversation so
`X-Motet-Conversation-Id`, prompt-cache affinity, and cost stay on the
suspend conversation. If the checkpoint expired
(`MOTET_TURN_CHECKPOINT_TTL_SECONDS`, default 24h) or the ids match nothing,
the request runs as a fresh agent turn over the resent history; forged ids or
missing client-owned observations are rejected with 400.

Exactly one Motet transcript is written per logical turn, at resume
completion. Disable the whole behavior with
`MOTET_OPENAI_COMPAT_AGENT_CLIENT_TOOLS=false`, which restores the previous
semantics (client tools ignored in agent mode).

Ownership of a tool-call turn is decided inside the agentic loop
(`calls_require_handback` / `classify_turn_ownership`): any externally-owned
call ⇒ hand the whole turn back. In agent mode the loop suspends with a Redis
checkpoint and Motet-owned calls from that handback run at resume (issue
#159). `hosted_tools` uses the same path: the facade dispatches `core.agent_loop`
with the allowlist as `tools` and client-declared schemas as `handback_tools`
(hooks and memory off). Facade resume resolves a `ResumeHandle` through
`runtime.resolve_resume` and stays a wire adapter onto `resume_agent_turn` —
it does not import `motet.core.checkpoints`. Chat vs Responses stream bodies
keep separate renderers; they share `execution.stream`.

### Mode selection precedence

Clients like Cursor expose only a base URL, an API key, and a model string, so
mode cannot rely on headers. Precedence, most specific first:

1. **Request extension** — `motet_mode` in the body or the `X-Motet-Facade-Mode`
 header. Rejected unless `MOTET_OPENAI_COMPAT_ALLOW_REQUEST_MODE_OVERRIDE=true`.
2. **Model alias suffix** — `openai/gpt-4o-mini:agent`. Works in any model picker.
3. **Service account binding** — `facade_mode` (and related policy: allowlist,
 `force_thinking`, `agent_id`) on the token.

The bound mode is also the **ceiling**. A request may select a weaker mode; an
attempt to escalate returns 403 rather than silently downgrading.

## Setup

```bash
export MOTET_OPENAI_COMPAT_ENABLED=true
export MOTET_OPENAI_COMPAT_PREFIX=/v1
```

Mint a credential with its policy bound:

```bash
curl -X POST https://motet.example.com/api/v1/service-accounts \
 -H "Authorization: Bearer $ADMIN_TOKEN" \
 -H "Content-Type: application/json" \
 -d '{
 "name": "cursor-desktop",
 "roles": ["member"],
 "facade_mode": "passthrough",
 "allowed_models": ["openai/gpt-4o-mini", "anthropic/claude-sonnet-4"],
 "force_thinking": true,
 "force_thinking_effort": "medium",
 "agent_id": "cursor.backend"
 }'
```

Point a client at it:

```python
from openai import OpenAI

client = OpenAI(base_url="https://motet.example.com/v1", api_key="sa_2026...")
client.chat.completions.create(model="openai/gpt-4o-mini",
 messages=[{"role": "user", "content": "hello"}],)
```

In Cursor, use Override OpenAI Base URL with the same URL and token, and add
`openai/gpt-4o-mini` as a custom model.

## Security posture

Authentication was already solved by service account tokens; **authorization and
blast radius are the work this package does**.

- **Deny-by-default models.** An empty allowlist grants nothing, so enabling the
 facade without writing policy cannot expose every vault-backed provider. A
 denied model returns 404, identical to an unknown model, so the allowlist does
 not leak what exists behind it.
- **Deny-by-default tools.** `hosted_tools` exposes only tools matching
 `MOTET_OPENAI_COMPAT_HOSTED_TOOLS_ALLOWLIST`. Execution carries the caller's
 principal into the worker, so registry scoping still applies on top.
- **Mode ceiling.** A leaked passthrough token cannot be upgraded into a
 tool-executing one by a crafted request.
- **Real credentials only.** Header-derived dev identities
 (`X-Principal-Id` under `MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS`) are rejected
 with 401 on every facade route, even when the flag is on for local development
 of the native API. Facade policy hangs off the credential, so an
 unauthenticated header must not mint one.
- **Conversation ownership.** Conversation ids are bound to the first principal
 that uses them (core enforcement in `agent_turn`). Agent mode also runs a
 pre-flight check so a cross-principal id fails with a clean 404 before
 streaming starts, indistinguishable from a nonexistent conversation.
- **Sanitized errors.** Upstream provider text is redacted of credential-looking
 substrings and truncated before it reaches the client.
- **Bounded loops.** `hosted_tools` stops at
 `MOTET_OPENAI_COMPAT_MAX_TOOL_ITERATIONS` and reports `finish_reason: length`.
- **Issue facade tokens separately** from CLI and CI accounts so revoking one
 does not break the others, and so budget attribution stays clean.

Prompt injection is in scope for the deeper modes: content the model reads can
try to invoke hosted tools. Keep the hosted allowlist to read-mostly tools unless
the tools themselves enforce confirmation.

## Sessions

OpenAI expresses continuity through a `conversation` id or a
`previous_response_id` chain; Motet uses `conversation_id`, which scopes memory,
transcripts, and conversation-scoped artifact RAG. The facade maps between them
and records `response_id -> conversation_id` in Redis, scoped to the owning
principal and tenant so one credential cannot resume another's conversation.
Both endpoints record their ids — `resp_...` from `/responses` and
`chatcmpl_...` from `/chat/completions` — so a hybrid client can chain
`previous_response_id` off either. The two fields are mutually exclusive,
matching OpenAI. When neither is present, a fresh conversation id is minted
rather than sharing a default.

An unknown or expired `previous_response_id` returns 404 rather than silently
starting a fresh conversation: memory quietly stopping would be
indistinguishable from success. Clients should drop the chain and retry without
the field if they want a new conversation.

**Stateless clients (agent mode).** Chat Completions clients such as Cursor send
neither field — they resend the full transcript every turn, which would
otherwise mint a new conversation per turn and fragment memory, cost accounting,
and prompt-cache affinity. Two mechanisms recover continuity, tried in this
order.

*Session banner (preferred).* Agent-mode replies end with a visible footer
naming the conversation:

```
---
_Motet session `openai-a3f9c21b` - tracked 2026-07-30 18:49 UTC_
```

The client echoes it back verbatim in the next request's history, so the
conversation is identified by explicit reference rather than inference. Banners
are stripped from inbound history before the agent runs, so they cost no
context and the model never imitates them; a system-prompt line asks the model
to preserve any banner it encounters while summarizing, which is what Cursor
BYOK does when it compacts history through this endpoint. Banners are skipped
on tool-call turns, where the assistant message is a call request rather than a
reply anyone reads. Controlled by `MOTET_OPENAI_COMPAT_SESSION_BANNER`
(`off` | `first` | `every`, default `every`) and
`MOTET_OPENAI_COMPAT_SESSION_BANNER_GUARD`.

*Transcript fingerprint (fallback).* After each successful agent-mode turn the
facade records a fingerprint of the transcript plus the assistant reply (roles,
text, and tool-call ids; salted with tenant+principal) mapped to the
conversation id. The next request's prefix up to the last assistant message
hashes to the same fingerprint and rejoins the conversation. This covers
clients or rewrites that drop the banner. An edited history simply misses and
starts fresh. Controlled by `MOTET_OPENAI_COMPAT_INFER_SESSION` (default on).

Any explicit session reference outranks both. The banner outranks the
fingerprint because it is an explicit reference: it survives history edits that
change the hash, and it distinguishes two chat windows whose transcripts are
byte-identical — a case the fingerprint alone cannot separate. Fingerprints are
additionally claimed atomically on first write, so a coincidental collision
leaves the earlier conversation's mapping intact instead of stranding its
opening turn in an abandoned conversation. A banner echoed back by a client is
treated as a caller-supplied conversation id and gets the same ownership check,
so a hand-edited one cannot reach another principal's conversation.

## Observability

Facade traffic is not a dark hole: it flows through normal commands, so cost
rows, command events, and traces are produced as usual. On top of that, every
response carries correlation headers:

| Header | Meaning |
|--------|---------|
| `X-Motet-Task-Id` | Task id for the underlying command |
| `X-Motet-Conversation-Id` | Motet conversation this turn belongs to |
| `X-Motet-Facade-Mode` | Mode the request actually ran in |
| `X-Motet-Model` | Resolved `provider/model` |
| `X-Motet-Stop-Reason` | Motet turn `stop_reason` when known (non-streaming). Budget stops use `max_iterations` / `max_model_calls` — send another user message (e.g. `Continue working on this task.`) for a **new** turn with a fresh budget. Not the same as resume (same-budget handback). Streaming cannot revise headers; those replies append a Continue tip before the session banner instead. |

Motet-native stream events (tool execution, reasoning steps) have no OpenAI
representation and are dropped from the client stream. They stay fully visible
server-side, which is where operators should look.

**Thinking (opt-in or force):** when the client sends `reasoning_effort`, a
Responses-shaped `reasoning` object, or `motet_enable_thinking: true`, **or**
the credential/config sets `force_thinking`, and the resolved model has
`CAP_REASONING`, thinking is requested from the adapter and surfaced as Chat
Completions `reasoning_content` (stream deltas + non-stream message field) and
Responses `reasoning` summary items. Without opt-in or force, thinking stays
collapsed. Opaque provider blocks are never put on the wire. Cursor BYOK often
omits reasoning fields — bind `force_thinking` on the service account for that
path.

## Wire behavior worth knowing

| Field | Behavior |
|-------|----------|
| `n > 1`, `logprobs`, `top_logprobs` | Rejected with 400. Accepting them silently would misreport what happened. |
| Invalid `motet_mode` / `X-Motet-Facade-Mode` | Rejected with 400 (`invalid_facade_mode`). A typo must not silently run in a different mode. |
| Unknown `previous_response_id` | 404 (`response_not_found`), never a silent fresh conversation. |
| Mixed Motet + client tool calls in one turn | Whole turn returns as `tool_calls` (wire stays coherent). Agent mode and `hosted_tools` both checkpoint via Turn Runtime; at resume Motet runs its own calls and the client covers only client-owned ids (issue #159 /). Mixed hosted turns are also logged (`openai_compat_mixed_turn_handback`). |
| Client tool call in `agent` mode | The turn suspends and the calls return as `tool_calls`; resending with `role: "tool"` results resumes the same turn. Expired checkpoints fall through to a fresh turn; forged ids or missing client-owned observations return 400 (`invalid_tool_observations`). |
| `reasoning_effort` / `reasoning` / `motet_enable_thinking` | Client opt-in for thinking. Honored only when the model has `CAP_REASONING`; otherwise silently left off. ORs with SA/config `force_thinking`. |
| `top_p`, `seed`, `stop`, penalties | Forwarded into `model_settings`; not every adapter honors them yet. |
| `response_format` / `text.format` | Mapped to a canonical `OutputContract`. |
| `stream_options.include_usage` | Emits a final usage-bearing chunk with empty `choices`. |
| `finish_reason` | Canonical stop reasons are mapped; a turn with tool calls always reports `tool_calls`. |
| Tool names | Arrive as `mcp__server__tool` and are converted to canonical `mcp.server.tool` on receipt, then back on the way out. |
| Mid-stream failure | Emits an error frame and withholds `[DONE]`, so a client cannot mistake a failure for a short answer. |
| Idle streams | Keepalive comments every `MOTET_OPENAI_COMPAT_STREAM_KEEPALIVE_SECONDS`. |
| Tool-call arguments (streaming) | Sent incrementally while the model generates them, on both endpoints. Only calls the client declared are streamed; Motet-owned calls execute here and stay off the wire. |

### Streaming a tool call's arguments

A tool call whose arguments are a whole file is thousands of output tokens inside
one call, and the model can spend minutes on it. Keepalive comments hold the
socket open through that, but a client watching for content sees an idle stream
and may abandon the request, so the argument fragments themselves go out as they
arrive — each endpoint in its own wire's terms:

| Endpoint | Fragments look like |
|----------|--------------------|
| `chat/completions` | `delta.tool_calls`, with identity (`index`, `id`, `name`) on the first frame and argument text on the rest |
| `responses` | `response.output_item.added` for a `function_call` item, then `response.function_call_arguments.delta`, closed by `.done` and `response.output_item.done` |

Fragments are released only once the tool name is known to be one the *client*
declared, because a Motet-owned call runs on this side and a client that saw it
would try to execute a tool it does not have.

Whether the terminal event repeats a streamed call depends on what that event is.
Chat Completions sends deltas, so a streamed call is dropped from the terminal
`tool_calls` chunk — a second copy would double its arguments. The Responses
`response.completed` payload is a snapshot of the whole output, so streamed calls
stay in it, under the same `fc_<call_id>` item id the stream used.

The exception is `hosted_tools` mode, where the server executes some of the
model's calls: nothing is streamed there, because ownership is not decidable
from the name alone.

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `MOTET_OPENAI_COMPAT_ENABLED` | `false` | Mount the facade |
| `MOTET_OPENAI_COMPAT_PREFIX` | `/v1` | Mount point |
| `MOTET_OPENAI_COMPAT_DEFAULT_MODE` | `passthrough` | Mode for credentials without a binding |
| `MOTET_OPENAI_COMPAT_DEFAULT_ALLOWED_MODELS` | `` (deny all) | Allowlist for credentials without one |
| `MOTET_OPENAI_COMPAT_HOSTED_TOOLS_ALLOWLIST` | `` (deny all) | Tools executable in `hosted_tools` |
| `MOTET_OPENAI_COMPAT_ALLOW_REQUEST_MODE_OVERRIDE` | `false` | Honor request-level mode selection |
| `MOTET_OPENAI_COMPAT_MAX_TOOL_ITERATIONS` | `8` | Hosted tool loop bound |
| `MOTET_OPENAI_COMPAT_AGENT_CLIENT_TOOLS` | `true` | Honor client-declared tools in `agent` mode via suspension/resume |
| `MOTET_OPENAI_COMPAT_DEFAULT_AGENT_ID` | `` | Fallback agent id when mode is `agent`, the request omits `motet_agent_id`, and the SA has no `agent_id` (prefer binding `agent_id` on the SA) |
| `MOTET_OPENAI_COMPAT_STREAM_KEEPALIVE_SECONDS` | `15` | SSE keepalive interval |
| `MOTET_OPENAI_COMPAT_SESSION_TTL_SECONDS` | `604800` | Lifetime of response-id and transcript-fingerprint records |
| `MOTET_OPENAI_COMPAT_INFER_SESSION` | `true` | Rejoin conversations for stateless clients via transcript fingerprints (`agent` mode) |
| `MOTET_OPENAI_COMPAT_SESSION_BANNER` | `every` | Visible session footer carrying the conversation id (`agent` mode): `off` \| `first` \| `every` |
| `MOTET_OPENAI_COMPAT_SESSION_BANNER_GUARD` | `true` | Ask the model to preserve banners when it summarizes history |
| `MOTET_OPENAI_COMPAT_FORCE_THINKING` | `false` | Enable thinking for `CAP_REASONING` models when the SA does not set `force_thinking` |
| `MOTET_OPENAI_COMPAT_FORCE_THINKING_EFFORT` | `medium` | Default effort when force_thinking applies without client effort |

Allowlist entries accept `provider/model`, `provider/*`, and `*`. Tool allowlist
entries accept exact names and `prefix.*`.

## Module layout

| Module | Responsibility |
|--------|----------------|
| `routes.py` | HTTP routes, mode precedence, SSE bodies, correlation headers |
| `translation.py` | OpenAI ↔ canonical conversion and model resolution |
| `execution.py` | The three execution backends |
| `streaming.py` | SSE framing, keepalives, Redis task-stream consumption |
| `sessions.py` | OpenAI session ↔ Motet conversation mapping |
| `wire.py` | Inbound request models and id helpers |
| `errors.py` | OpenAI error envelopes and sanitization |

Policy itself lives in `motet/core/security/facade_policy.py` because the
service accounts API validates against it too.

## Tests

```bash
pytest tests/unit/interfaces/api/test_openai_compat_translation.py \
 tests/unit/interfaces/api/test_openai_compat_routes.py \
 tests/unit/interfaces/api/test_openai_compat_execution.py \
 tests/unit/core/security/test_facade_policy.py
```

Integration tests that exercise real inference belong in Docker:

```bash
docker-compose -f tests/docker-compose.test.yml run --rm test-runner
```

## Example cookbooks

| Example | Mode | Purpose |
|---------|------|---------|
| [`motet-sdk/examples/bundles/openai-gateway`](../../../../motet-sdk/examples/bundles/openai-gateway/) | `passthrough` | Multi-provider OpenAI drop-in gateway (operator recipe; no agents) |
| [`motet-sdk/examples/bundles/cursor`](../../../../motet-sdk/examples/bundles/cursor/) | `agent` | IDE / Cursor backend + client-tool handback |

## Related decisions

 (this facade), (turn suspension/resume behind agent-mode
client tools), (canonical LLM protocol), (API URL
standardization and this exception), (cost and budgets),
(conversations), (auth and principals), / (tool and
principal scoping), (confirmation gating).
