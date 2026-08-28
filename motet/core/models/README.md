## Package: models

**Distributed model management system** with unified interfaces, provider abstractions, and distributed command execution.

### Purpose
- **Distributed Model Operations**: All model operations execute as distributed commands
- **Adapter-First Execution**: Canonical adapters handle provider protocols
- **Model Specs Registry**: Centralized model capabilities and specifications
- **Llama.cpp Model Profiles**: Family-specific local GGUF prompt/tool protocol behavior
- **Resilience Integration**: Circuit breakers, retries, and fault tolerance
- **Observability**: Comprehensive tracing, metrics, and monitoring

The product catalog (providers, flagship ids, how to list the live registry) is [Supported models](../../../docs/developer_onboarding/03a-supported-models.md).

### Components
- `__init__.py`: `ModelRegistry`, helpers.
- `provider_credentials.py`: Whether a provider needs a cloud API key and whether one is configured (env/config, then vault).

### Notes
- Prefer adapters for execution; the model registry is spec-only and used for capability checks.
- Keep local-model executable prompt policy in `local/profiles/`; `ModelSpec` remains declarative.
- Adapter registries implement the shared `BaseRegistry[T]` Protocol in `motet.core.types`.
- Adapters are the only execution path.

### Implemented
- OpenAI/Anthropic/Moonshot/DeepSeek/xAI/Meta/Gemini adapters with streaming, retries, and metrics/tracing.
- DeepSeek V4 Responses adapter (`deepseek-v4-flash`, `deepseek-v4-pro`) is the default path and maps `deepseek.web_search` to `{"type": "web_search"}`. Chat Completions remains registered as fallback (thinking toggle + `reasoning_content` replay).
- xAI Grok 4.5 / 4.6 on `XAIResponsesAdapter` map `xai.web_search` to `{"type": "web_search"}`. Function tools can share that Responses request. Always-on reasoning + `prompt_cache_key` as before ($2/$0.50 cached/$6 per 1M tokens on 4.6).
- Meta Muse Spark (`muse-spark-1.1`, `muse-spark-1.2`) on `MetaResponsesAdapter` at `https://api.meta.ai/v1`. Maps `meta.web_search` to `{"type": "web_search"}`. Always-on reasoning (`none` is a 400; thinking-off sends `minimal`; `max` clamps to `xhigh`). Inherits OpenAI `store=false` + encrypted reasoning replay. Standard-tier pricing $1.25 / $0.15 cached / $4.25 per 1M tokens. Contributor (train-on-your-data) is not registered.
- OpenAI Responses stateless reasoning replay: `store=false` on every call (no server-side retention, ZDR-compatible); with thinking enabled, encrypted reasoning items are captured into `reasoning_blocks` and replayed verbatim across tool iterations. xAI does not inherit this (own `_finalize_responses_params`).
- Anthropic Messages maps `web_search` / `anthropic.web_search` to `web_search_20250305`. Citation URLs come from text-block `web_search_result_location` rows and `web_search_tool_result` items so `core.web_search` can keep the native LLM path.
- Anthropic thinking replay: thinking text surfaces as `reasoning_content`; signed `thinking`/`redacted_thinking` blocks are captured into `reasoning_blocks` and, when thinking is enabled, replayed verbatim ahead of text/tool_use blocks for chain-of-thought continuity.
- Claude Opus 5 (`claude-opus-5`): registered with $5/$25 per MTok pricing; adaptive thinking on by default. When Motet `enable_thinking` is false, the Anthropic adapter sends `thinking.type=disabled`, clamping effort to `high` for Opus 5+ only (that family 400s on disabled + `xhigh`/`max`; Sonnet 5 accepts `max`, Fable/Mythos reject `disabled` entirely). Adaptive-thinking Claude models default to `high` effort when the caller sets none, matching Anthropic's default.
- Canonical reasoning effort (`ReasoningEffort` in `motet/core/types.py`): `low < medium < high < xhigh < max`. Providers accept different subsets, so adapters map the canonical value onto provider vocabulary via `normalize_reasoning_effort(...)` rather than passing it through — a request never fails because a model lacks the rung the caller asked for. Verified: Anthropic takes the full ladder; OpenAI accepts `max` only on gpt-5.6 Responses (elsewhere, including Chat Completions, clamp to `xhigh`); xAI and Meta reject `max` (clamped to `xhigh`); Meta thinking-off uses `minimal` (not on the Motet ladder); DeepSeek exposes only `high`/`max`; Kimi K3 is pinned to `max`.
- Prompt-cache prefix stability: the Anthropic adapter fuses only *stable* system content into the cached system block; per-turn injections (pending action, memory recall, hook output — flagged `cache_volatile` in message metadata) become separate uncached blocks after the breakpoint, so they never invalidate the cached prefix. Combined with the sticky per-conversation tool shortlist in the agentic loop, the tools+system prefix now survives across turns for all prefix-based provider caches (Anthropic breakpoints, OpenAI `prompt_cache_key`, Gemini/DeepSeek implicit).
- Trailing-turn invariant (`message_history_sanitizer.py`): `needs_user_turn` / `assert_trailing_user_turn` are the single source of truth for "a history handed to a model must end on a user turn or tool result". The agentic loop uses `needs_user_turn` to repair turns assembled without input; the Anthropic adapter calls `assert_trailing_user_turn` before the API call, because Anthropic reads a trailing assistant turn as an assistant prefill and Opus 4.5+ refuses it. The canonical protocol has no prefill concept, so the shape always means a turn had no user input — failing locally names that cause instead of an opaque provider 400.
- Gemini thought signatures: Gemini 3+ binds a `thought_signature` to each functionCall part and rejects multi-turn tool calls without it; signatures are captured on `ToolCallRequest.thought_signature` (base64url), persisted in canonical tool-call dicts, and re-attached to the Part on replay.
- Model specs and capability checks via registry helpers.
- `output_limits.py`: when request/profile omit `max_tokens`, model commands and adapters fill from `ModelSpec.max_output_tokens`. Adapters use `fallback=None` (no invented 8k); unknown models omit the wire field. Anthropic requires `max_tokens` and raises if neither request nor ModelSpec provides it.
- `ModelSpec.released_at` (optional public launch date) backfilled from `_MODEL_RELEASED_AT` for recency sorting.
- Capability/canonical contract tests (`test_adapter_capability_contract.py`, `test_adapter_canonical_contract.py`) covering every ModelSpec.
- Opt-in live API matrix (`tests/integration/test_adapter_live_capability_matrix.py`): default canary = newest `released_at` month then cheapest input price (stream+tools); override with `MOTET_LIVE_ADAPTER_CASES`. Set `MOTET_LIVE_ADAPTER_MATRIX=1` plus provider keys. The tool-call case uses an MCP-named tool (`mcp.test.add_two_numbers`): `model.py` wires names outbound, adapters return canonical `mcp.*` via `inbound_tool_call_request`, and round 2 replays `tool_calls_canonical`. Models with a `*.web_search` builtin also exercise native search: citation URLs on the adapter response, the same URLs after `model_inference` serialization, and mixing that builtin with a function tool.
- Local model family profiles for llama.cpp chat formatting, stops, thinking controls, and tool handshakes, including Qwen, Gemma 4, Phi, Llama 3, and Hermes variants.
### Planned
- Expanded provider matrix and model routing.
- Token accounting and budget enforcement per model.
- Fine-grained error mapping and typed exceptions per provider.

