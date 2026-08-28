# Canonical LLM Protocol

Motet uses a **provider-agnostic canonical LLM protocol** for all model inference. Orchestration and streaming code depend only on this protocol; **provider adapters** translate between canonical types and each provider’s wire format. This keeps inference swappable and avoids provider-specific logic in the core. The registered providers and flagship ids are on [Supported Models](./03a-supported-models.md).

## Why a canonical protocol?

- **Provider switching**: Change provider or model between turns without rewriting orchestration.
- **Single code path**: Orchestration uses one request/response/stream shape; adapters handle provider differences.
- **Multimodal and tools**: Same canonical representation for messages, tool calls, and streaming events across every registered provider.

## Rule: orchestration vs adapters

- **Orchestration** (prepare_context, agentic_loop, streaming handlers) must use **canonical types only** (e.g. `Message`, `LLMRequest`, `LLMResponse`, canonical streaming events). It must **not** emit or consume provider wire formats.
- **Adapters** (per-provider modules) are responsible for:
  - **Request**: canonical → provider request schema (e.g. canonical messages → OpenAI/Anthropic message format).
  - **Response / stream**: provider response/stream → canonical outputs and events.

So when you work on inference, streaming, or tool-call handling in the core stack, you use the canonical protocol; provider-specific shapes stay inside the adapter layer.

## Canonical input and output

### Request (LLMRequest)

- **messages**: Ordered list of **Message** (roles, content).
- **tools** (optional): Tool definitions in canonical form for function calling.
- **output_contract** (optional): Structured-output constraints (provider-independent).
- **model_settings** (optional): Temperature, max_tokens, etc.
- **tracing** (optional): Request-scoped tracing metadata.

### Messages and content parts

- **Message**: `role` (user / assistant / system) and content. Content can be:
  - **Text**: plain text (a text part).
  - **Structured content parts**: e.g. **TextPart**, **MediaPart** (e.g. `media_type="image"` with `artifact_id` or materialized data). Multimodal renderers produce **canonical** parts (e.g. `MediaPart` with `base64_data` when needed); adapters then map these to provider-specific blocks (OpenAI `image_url`, Anthropic `image`, etc.).
- **Rule**: Do not embed provider-native formats (e.g. OpenAI `image_url` blocks) in orchestration; use canonical `Message` and content parts. Adapters do the encoding.

### Response (LLMResponse)

- **output_items**: Canonical list of result items (e.g. text, tool call requests).
- **output_text** (optional): Concatenated assistant text.
- **stop_reason**: Normalized stop reason (e.g. end_turn, tool_calls, max_tokens).
- **usage** (optional): Token/usage metadata (best-effort across providers).
- **citations** (optional): Citation/annotation metadata when supported.

### Tool calls (canonical)

- Tool use is represented by **canonical** tool-call and tool-result types (e.g. `ToolCallRequest`, `ToolCallResult`), not by raw provider fields like `choices[0].message.tool_calls`.
- Adapters map:
  - **To provider**: canonical tool definitions and tool-call requests → provider tool schema and tool_call blocks.
  - **From provider**: provider tool_call / tool_result → canonical types for orchestration and transcript storage.

Orchestration and transcript reconstruction use only these canonical types; never rely on provider-specific `tool_calls` or `role="tool"` message shapes outside adapters.

## Canonical streaming events

Streaming is normalized to a small set of **canonical event types** consumed by orchestration and the UI, for example:

- **text_delta**: Incremental assistant text.
- **tool_call_delta** / **tool_call_complete**: Tool call streamed or completed (canonical IDs and payloads).
- **tool_use**: Optional high-level tool-usage event (e.g. for UI).
- **citations**: Citation/annotation payload when available.
- **stop**: End of stream.
- **usage**: Token/usage info.
- **error**: Error payload.

Adapters map provider stream chunks (e.g. OpenAI `delta`, Anthropic `content_block_delta`) into these canonical events. Orchestration and frontends depend only on the canonical set.

## Model routing: ModelSpec and ModelProfile

- **ModelSpec**: Defines *what a model is* (capabilities, limits, supported adapters, default adapter, fallback adapters). Used for **routing**: which adapter/backend to use for a given model.
- **ModelProfile**: Per-tenant (and optional per-motet) **policy** (preferred adapter, built-in tool allowlists, default model_settings). Stored in Redis (or seeded from `config/model_profiles.yaml`). Routing precedence is typically: request override → ModelProfile → ModelSpec defaults → environment defaults.

When you pass **model_profile_name** (e.g. in AgentData or inference calls), the system resolves the profile for the current tenant and applies its adapter and policy overrides. This is how multi-tenant and multi-provider routing is configured without hardcoding provider logic in orchestration.

## Where this appears in the codebase

- **Canonical types**: `motet.core.types` (Message, content parts, tool-related types); model layer types for `LLMRequest`, `LLMResponse`, streaming events (see code under `motet.core.models`).
- **Orchestration**: `prepare_context`, agentic_loop, and streaming pipelines build and consume canonical messages and events only.
- **Adapters**: `motet.core.models.adapters.providers.*` implement translation to/from canonical for each registered provider. See [Supported Models](./03a-supported-models.md) for the catalog.
- **Multimodal**: Renderers (e.g. `motet.core.models.rendering.*`) produce canonical `MediaPart`/content parts; adapters then map to provider image/block formats.
- **Tool transcripts**: Stored and reconstructed in canonical form; rendered per-provider only at the adapter boundary.

## Summary

- Use the **canonical LLM protocol** for all inference and streaming in orchestration.
- Do **not** emit or depend on provider wire formats in the core; keep them in **adapters**.
- Rely on **ModelSpec** and **ModelProfile** for model/adapter routing and policy.

## Next steps

- **[Supported Models](./03a-supported-models.md)** – Providers, flagship ids, and the live catalog
- **[MCP Integration](./09-mcp-integration.md)** – Tool integration and discovery
- **[Artifacts and Multimodal Context](./20a-artifacts-and-multimodal-context.md)** – How uploads and artifacts become canonical content parts
- **[API Reference](./28-api-reference.md)** – MotetContext and API overview

## Navigation

- **[← MCP Integration](./09-mcp-integration.md)** – Tools and MCP
- **[Reasoning →](./10-reasoning.md)** – How the agent uses inference
- **[Documentation Home](./00-landing-page.md)** – Main documentation hub

---

**Last Updated**: 2026-08-25
