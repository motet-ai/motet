# Supported Models

This is the catalog a buyer and an operator both need: which providers Motet speaks natively, which model ids you can select today, and what “supported” actually includes. Switching provider is a credential and a registry key. Orchestration stays on one request and stream shape; adapters absorb the vendor wire.

The live list is the model registry. This page names providers and flagship ids so you can decide whether the stack fits. Aliases, dated snapshots, and newly registered models show up on `GET /api/v1/models` as soon as they land.

## What “supported” means

A model is supported when all of the following are true:

- It has a **ModelSpec** — capabilities, token limits, adapter routing, and (for cloud chat and reasoning models) shipped pricing for metering.
- A **provider adapter** translates Motet’s canonical protocol to that vendor’s API.
- **Contract tests** cover every spec: capabilities and the canonical request/response/stream shape.

That is more than “we can POST to the vendor.” Chat models get streaming and tools. Thinking is replayed across tool rounds where the vendor has a thinking protocol. Vision is declared per spec. Several providers expose a native web-search builtin. Prompt-cache prefixes stay stable across turns. OpenAI Responses calls send `store=false` (no server-side retention). Reasoning effort is a single ladder (`low` through `max`); adapters map it onto whatever rungs that model accepts, so a request does not fail because you asked for a rung the vendor does not name.

## Providers

Seven hosted APIs, plus llama.cpp on your workers, plus a mock for tests:

| Provider | Registry id | Credential | Notes |
|---|---|---|---|
| OpenAI | `openai` | `MOTET_OPENAI_API_KEY` | Responses by default; Chat Completions fallback on many chat models. Image-generation models are registered separately. |
| Anthropic | `anthropic` | `MOTET_ANTHROPIC_API_KEY` | Messages API. Account availability varies; some ids are date-stamped. |
| Google Gemini | `gemini` | `MOTET_GEMINI_API_KEY` (or `GEMINI_API_KEY` / `GOOGLE_API_KEY`) | Native `generateContent`. Preview ids change when Google promotes a model. |
| xAI | `xai` | `MOTET_XAI_API_KEY` | Grok via Responses. |
| Meta | `meta` | `MOTET_META_API_KEY` | Muse Spark via Responses. |
| DeepSeek | `deepseek` | `MOTET_DEEPSEEK_API_KEY` | V4 Responses by default; Chat Completions fallback. |
| Moonshot | `moonshot` | `MOTET_MOONSHOT_API_KEY` | Kimi via Chat Completions. |
| Local (llama.cpp) | `local` | none — GGUF on the worker | Unpriced. Paths via `MOTET_LOCAL_MODEL_DIR` / `MOTET_LOCAL_MODEL_PATHS`. |
| Mock | `mock` | none | `mock-small` for tests and UI contracts. |

Stack default is `openai` / `gpt-4o-mini`. See [Quick Start](./04-quick-start-guide.md#model-api-key) for the local key, and [Configuration Reference](./29-configuration-reference.md#model-configuration) for the full env set.

## Flagship models

Ids below are **registry keys** — the `name` you pass as `model_name`, and the second half of `provider/registry_key` on the OpenAI-compatible `/v1` facade. Previous-generation and dated ids stay registered; they are omitted here on purpose.

| Provider | Flagship registry keys | What they are good for |
|---|---|---|
| OpenAI | `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`, `gpt-4o`, `gpt-4o-mini`, `o3` | Current GPT-5.6 family (Sol / Terra / Luna; `gpt-5.6` is Sol), general GPT-4o, and o-series reasoning. Tools, vision, and `openai.web_search` on the chat models. |
| Anthropic | `claude-fable-5`, `claude-opus-5`, `claude-sonnet-5`, `claude-haiku-4-5-20251001` | Long-horizon, flagship, balanced, and fast. Tools, vision, thinking, and `anthropic.web_search`. |
| Gemini | `gemini-3.1-pro-preview`, `gemini-3-flash-preview`, `gemini-2.5-pro`, `gemini-2.5-flash` | Gemini 3.x previews plus the 2.5 line. Tools and vision; thought signatures on Gemini 3+ tool rounds. |
| xAI | `grok-4.6`, `grok-4.5` | Grok with tools, vision, thinking, and `xai.web_search`. |
| Meta | `muse-spark-1.2`, `muse-spark-1.1` | Muse Spark with tools, vision, thinking, and `meta.web_search`. |
| DeepSeek | `deepseek-v4-pro`, `deepseek-v4-flash` | V4 reasoning with tools and `deepseek.web_search`. |
| Moonshot | `kimi-k3`, `kimi-k2.5`, `kimi-k2.7-code` | Kimi K3 (vision, always-on thinking) and K2.x. K2 models expose `moonshot.web_search`; K3 does not yet — use `core.web_search` or MCP. |

Also registered, not listed above: GPT-5.x minors and GPT-4.1, Claude 4.x snapshots, Gemini 2.5 Flash-Lite / 3.1 Flash-Lite, remaining Kimi K2 ids, and OpenAI image models (`gpt-image-2`, `gpt-image-1.5`, `gpt-image-1`, `dall-e-3`). Image models generate images; they are not the agent chat loop.

Gemini and Kimi K3 have no provider-native search builtin in the registry. Use [Motet’s `core.web_search`](./21-tool-ecosystem.md) or an MCP server.

## Local models

These are the GGUF families the local adapter and llama.cpp profiles know how to prompt, stop, and hand tools to. They are not “any file on Hugging Face.”

| Registry key | Family |
|---|---|
| `gemma-4-e4b` | Gemma 4 |
| `gemma-4-26b-a4b` | Gemma 4 MoE |
| `gemma-3-4b` | Gemma 3 |
| `hermes-4-14b` | Hermes |
| `llama-3.1-8b-instruct` | Llama 3 |
| `ministral-3-8b-instruct` | Ministral |
| `phi-4-mini` | Phi |
| `qwen3-8b-instruct` | Qwen |

Local inference is unpriced: a turn that used only a local model has no `cost_usd`, same as a turn that made no priced call. See [Cost and usage API](./28-api-reference.md#cost-and-usage-api).

## How to see the live catalog

```bash
curl -s http://localhost:8000/api/v1/models
motet-cli models
motet-cli models --provider anthropic
```

```python
motet.models.list()
motet.models.list(provider="anthropic")
motet.models.get("anthropic", "claude-opus-5")
```

Each row includes `provider`, `name` (the registry key), `display_name`, `capabilities`, adapters, any native builtin tools, `requires_api_key`, and `has_api_key`. `has_api_key` is a boolean (environment or vault) and never includes the secret. Chat Explorer’s composer model selector (`provider : model`) uses those flags to show a key icon and to keep models without a key in the list but unselectable. The Models page in Manage reads the same endpoint.

On the OpenAI-compatible facade, model ids are `provider/registry_key` — for example `anthropic/claude-opus-5`. See [API Reference](./28-api-reference.md#openai-compatible-api).

## How to pick a model

Stack default:

```bash
MOTET_MODEL_PROVIDER=anthropic
MOTET_MODEL_NAME=claude-opus-5
MOTET_ANTHROPIC_API_KEY=...
```

An agent config can set `model_provider` and `model_name` per agent. Chat Explorer and `motet-cli chat` take `--provider` / `--model-name` as a per-turn override. From a command:

```python
motet.models.infer("anthropic", "claude-opus-5", messages=[...])
```

Routing order is request override, then the tenant **ModelProfile**, then the ModelSpec defaults, then the environment defaults. The protocol behind that is [Canonical LLM Protocol](./09a-canonical-llm-protocol.md).

## Where it stops

- **Registered is not the same as callable on your key.** Vendor accounts differ — especially Anthropic — and preview ids can disappear when a vendor promotes a model.
- **This page is not the full registry.** Use `GET /api/v1/models` when you need every alias and dated snapshot.
- **Local support is the families in the table**, plus the llama.cpp profile for that family. Dropping an arbitrary GGUF on disk does not make it a Motet model.
- **Image models are image output**, not chat. They do not run the agent loop.
- **Pricing is in the spec, not copied here.** Rates change; metering reads the registry. Image models are registered without a price, so they do not contribute `cost_usd`.
- **A live canary against the vendor is opt-in** (`MOTET_LIVE_ADAPTER_MATRIX`). Contract tests run in CI; they do not spend against your keys.
- **New models appear when a ModelSpec is registered** and an adapter can serve them. Until `GET /api/v1/models` lists the id, the stack does not have it.

## Next steps

- **[Canonical LLM Protocol](./09a-canonical-llm-protocol.md)** — the request/stream shape adapters translate
- **[Configuration Reference](./29-configuration-reference.md#model-configuration)** — keys, defaults, thinking, live-matrix flags
- **[What Motet Can Do](./03-what-motet-can-do.md)** — cost budgets and streaming around those calls
- **[Local Development Setup](./14-local-development-setup.md)** — putting a key in the compose stack

## Navigation

- **[← What Motet Can Do](./03-what-motet-can-do.md)**
- **[Canonical LLM Protocol →](./09a-canonical-llm-protocol.md)**
- **[Documentation Home](./00-landing-page.md)**

---

**Last Updated**: 2026-08-26
