# LLM protocol

Orchestration and renderers speak **canonical types only**. Provider adapters translate to and from each vendor’s wire format. Do not emit OpenAI / Anthropic / Gemini shapes in the core.

## Canonical types

- Request / response: `LLMRequest`, `LLMResponse`, `LLMStreamEvent`
- Messages: `Message` with `TextPart` and `MediaPart` (materialized data; adapters map to provider blocks)
- Tool calls: `tool_calls_canonical` with `ToolCallRequest` / `ToolCallResult`. No ChatCompletions `tool_calls` fields in orchestration
- Streaming events: `text_delta`, `tool_call_delta`, `tool_call_complete`, `tool_use`, `citations`, `stop`, `usage`, `error`
- Provider-native tools surface as canonical `tool_use` with `kind="provider"`

## Where conversion happens

| Direction | Owner |
|---|---|
| Outbound MCP names and replayed tool-call names | `model.py` (`tool_canonical_to_wire`) before `adapter.complete` / `adapter.stream` |
| Inbound provider tool calls | `inbound_tool_call_request` (`tool_wire_to_canonical`) |
| OpenAI HTTP facade (a **client** wire) | `openai_compat/translation.py` |

Do **not** add a second outbound convert in `schema_exporter` or adapter formatters. `schema_exporter` stays canonical-only. Adapters render names already converted by `model.py`.

MCP name forms:

- Canonical inside Motet: `mcp.server_id.tool_name`
- Wire at the LLM provider boundary: `mcp__server_id__tool_name`
- Workflows stay `workflow_<id>` — no wire transform

## Routing and policy

- **ModelSpec**: what a model is (capabilities, adapters, fallbacks)
- **ModelProfile**: per-tenant (optional per-motet) policy — preferred adapter, tool allowlists, default settings. Redis, or seed from `config/model_profiles.yaml`

Precedence: request override → ModelProfile → ModelSpec defaults → environment defaults.

## Structured output

`OutputContract` is the provider-independent constraint. Agents may declare it on `AgentConfig` / turn data. Local models use grammar-constrained decoding where the spec requires it.

## Paths

- Types: `motet/core/types`, `motet/core/models`
- Adapters: `motet/core/models/adapters/providers/`
- Renderers: `motet/core/models/rendering/`
- Facade: `motet/interfaces/api/openai_compat/` (`MOTET_OPENAI_COMPAT_ENABLED`, default off)
