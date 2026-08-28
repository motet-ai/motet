# Langfuse CMS (example bundle)

Opt-in demo that manages **one agent’s** system prompt via **Langfuse Cloud**
and optionally records that agent’s generation usage/cost there.

**Live CMS path:** every Chat Explorer / `agent_turn` for `langfuse-cms.prompt-manager`
fetches the Cloud prompt via `turn_hooks.context_inject` →
`langfuse-cms.inject_langfuse_prompt`. Edit in Langfuse → next message uses
it. On credential/network failure the bundle static fallback is injected
instead (turn still succeeds).

Motet does **not** ship Langfuse as platform infrastructure. Platform cost
tracking stays in Motet. This bundle talks to your Cloud project over HTTPS
with vault-backed keys.

## What you get

| Piece | Purpose |
|-------|---------|
| Agent `langfuse-cms.prompt-manager` | Chat agent; live Langfuse system prompt each turn |
| Command `inject_langfuse_prompt` | `context_inject` hook — Cloud fetch + fallback |
| Command `record_turn_to_langfuse` | `after_finalize` hook — push turn usage/cost to Cloud |
| Command `agent_turn_with_langfuse_prompt` | Optional CLI one-shot (infer + generation push) |
| Tools `get_prompt` / `list_prompts` / `update_prompt` | Manage prompts in Cloud |
| Tool `record_generation` | Manual usage/cost push for this agent |

Default Langfuse prompt name: `langfuse_cms.prompt_manager`
Default label: `production`

YAML `system_prompt` is **empty** on purpose so the injected message is the
only system prompt (Motet `context_inject` is additive and cannot replace YAML
text).

## Langfuse Cloud setup

1. Create a project at [Langfuse Cloud](https://cloud.langfuse.com)
 (or the [US region](https://us.cloud.langfuse.com) if you prefer).
2. Create API keys (public + secret) in project settings → API Keys.
3. Note the API **host** (required — do not leave EU/US ambiguous):
 - EU: `https://cloud.langfuse.com`
 - US: `https://us.cloud.langfuse.com`
4. In the Cloud UI, create a **text** prompt named `langfuse_cms.prompt_manager`,
 set its body to the system prompt you want, and label it `production`.

Workers need HTTPS egress to Langfuse Cloud. Motet compose does **not** run
Langfuse.

## Store credentials in Motet vault

Store one credential (id/key `langfuse`) with JSON like:

```json
{
 "public_key": "pk-lf-…",
 "secret_key": "sk-lf-…",
 "host": "https://us.cloud.langfuse.com"
}
```

**Use `--scope tenant` (or `motet`) for Chat Explorer.** A `principal`-scoped
credential stored by the CLI service account is invisible to your browser
login; inject then falls back to the static demo prompt and the agent will
not use your Langfuse CMS text.

```bash
motet-cli vault store --id langfuse --type api_key --scope tenant \
 --description "Langfuse Cloud for langfuse-cms demo" \
 --data '{
 "public_key": "pk-lf-…",
 "secret_key": "sk-lf-…",
 "host": "https://us.cloud.langfuse.com"
 }'
```

### Local-demo env fallback

If vault has no readable `langfuse` credential for the calling principal, the
bundle also accepts worker env vars:

| Variable | Purpose |
|----------|---------|
| `LANGFUSE_PUBLIC_KEY` | Cloud public key |
| `LANGFUSE_SECRET_KEY` | Cloud secret key |
| `LANGFUSE_HOST` | Base URL (`https://cloud.langfuse.com` or US host) |

Never bake secrets into the bundle artifact.

## Deploy the bundle

```bash
motet-cli deploy dir-deploy motet-sdk/examples/bundles/langfuse-cms
```

After reload you should see:

- Agent: `langfuse-cms.prompt-manager`
- Commands: `langfuse-cms.inject_langfuse_prompt`,
 `langfuse-cms.record_turn_to_langfuse`,
 `langfuse-cms.agent_turn_with_langfuse_prompt`
- Tools: `langfuse-cms.get_prompt`, `.list_prompts`, `.update_prompt`,
 `.record_generation`

## Try live CMS (Chat Explorer)

1. Open Chat Explorer and select agent **Langfuse CMS** (`langfuse-cms.prompt-manager`).
2. Send a message — the turn hook loads `langfuse_cms.prompt_manager` @ `production`.
3. Edit that prompt in the Langfuse Cloud UI (keep/re-apply the `production` label).
4. Send another message — the new text is used (no bundle redeploy).
5. In Langfuse Cloud, open **Tracing** — each turn is a trace named
 `langfuse-cms.agent_turn` (pushed by `after_finalize`) holding one
 generation with the model, token counts, Motet's estimated USD cost, and a
 link to the prompt version the turn ran on.

Turns in the same conversation share a Langfuse **session** (the Motet
conversation id), so a multi-turn chat reads as one session rather than a pile of
unrelated traces.

**Label tip:** saving a new prompt version does not move `production`. Point
the `production` label at the new version (or Motet will keep fetching the
old labeled text).

Optional turn context overrides (advanced):

- `langfuse_prompt_name`
- `langfuse_prompt_label`
- `langfuse_vault_key`

## Try the CLI wrapper (optional)

```bash
motet-cli command run langfuse-cms.agent_turn_with_langfuse_prompt \
 --data '{
 "message": "Say hello in one sentence.",
 "provider": "openai",
 "model_name": "gpt-4o-mini",
 "prompt_label": "production",
 "record_to_langfuse": true
 }'
```

Response fields: `prompt_source` (`langfuse` \| `fallback`),
`fallback_reason`, `langfuse_generation.status`.

Quick inject check (no model call):

```bash
motet-cli command run langfuse-cms.inject_langfuse_prompt --data '{}'
```

Look for `context_patch.langfuse_prompt_source` = `langfuse`.

## Edit the prompt from the agent

Ask the agent to list or update the Cloud prompt, or call the tools directly.
`update_prompt` creates a **new version** in Langfuse and can apply the
`production` label — the **next** turn then fetches it.

## Troubleshooting

| Symptom | Likely cause |
|---------|----------------|
| Agent talks like the Motet “demo assistant” / ignores Cloud prompt | Vault key not readable for your Chat Explorer user — use `--scope tenant` (not `principal` from CLI SA) |
| Inject reports `prompt_source: fallback` | Missing keys, wrong host (EU vs US), or empty Cloud prompt |
| `Credential access denied` in worker logs | Scope/principal mismatch on vault credential `langfuse` |
| HTTP errors to Langfuse | Wrong host for your Cloud region, or keys from a different project |
| `record_turn_to_langfuse` returns a `TypeError` about an argument after you edit `_langfuse.py` | Restart workers so they pick up the new `_langfuse.py` |

## Non-goals

- Not a Motet-wide LLM observability plugin
- Does not replace Motet cost pages
- Does not require self-hosted Langfuse (you *can* point `host` at your own
 URL; docs target Cloud)

## Implementation notes

- Uses **httpx** + Basic Auth against the Langfuse public API. No `langfuse`
 Python SDK dependency.
- Prompt reads: `/api/public/v2/prompts`.
- Turn export: one OTLP span per turn to `/api/public/otel/v1/traces`, with
 `x-langfuse-ingestion-version: 4` for real-time ingestion. The older
 `/api/public/ingestion` batch endpoint is deprecated ahead of Langfuse v4, and
 its bare `generation-create` produced an observation with no trace record — the
 Traces page stayed empty and rendered its "connect your app" onboarding, which
 looks like a credential problem but is not. A span implies its trace.
- Shared client: `commands/_langfuse.py`.
- Live path: `turn_hooks.context_inject` → `inject_langfuse_prompt` (fail-soft).
- Usage export: `turn_hooks.after_finalize` → `record_turn_to_langfuse` (fail-soft).
 Motet cost pages remain the platform source of truth.
- Turn cost reaches the hook because the agentic loop sums each priced model call
 into the turn's `cost_usd` (`react/loop_results.accumulate_usage`), which
 `agent_turn` reads via `extract_turn_cost`. A turn whose model calls are
 unpriced exports no `cost_usd` at all rather than `0.0`.
- Cost is sent as `gen_ai.usage.cost`, which Langfuse maps to a generation's own
 cost field, so it appears in cost columns and dashboards.
 `langfuse.observation.cost_details` is only parsed for spans emitted by the
 Langfuse SDK (scope name `langfuse-sdk*`) and would be silently dropped here.
- Span attributes also carry `langfuse.session.id` (Motet conversation),
 `langfuse.user.id` (principal), and `langfuse.observation.prompt.name` /
 `.version` when the turn actually ran on the Cloud prompt. On fallback the
 prompt link is omitted rather than misattributed.
