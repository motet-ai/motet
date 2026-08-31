# Memory / artifacts

Memory is three **tags** on one store, not three APIs:

| Tier | Tag | Lives | For |
|---|---|---|---|
| Working | `wm` | One turn | Scratch for the turn in progress |
| Short-term | `stm` | The session | Conversation / session state (Redis) |
| Long-term | `ltm` | Until forgotten | Knowledge past the conversation (Valkey Search) |

Only `ltm` is semantically searchable. Retrieval walks `wm` → `stm` → `ltm` by default (keyword + semantic + recency). Promotion from short-term to long-term is **opt-in**.

Operations take **no tenant argument**. Scope is `motet.tenant_id` / `motet_id` / `principal_id` from the verified context. Missing identity raises. See [auth-oauth.md](./auth-oauth.md).

`motet.memory.store` / `recall` / `tag` / `forget` are the helper. Extra kwargs are **silently ignored** — `recall(mode="semantic")` does not do what it looks like. Use `motet.do(memory_recall, data=MemoryRecallData(...))` for the full surface.

## Artifacts

Artifacts are large, raw, or binary payloads **outside** the hot conversation path. Conversation memory holds references (`artifact_id`, filename, type, size), not bytes.

| Kind | What it is |
|---|---|
| `user_upload` | Raw upload |
| `tool_artifact` | Tool output for transcript reconstruction |
| `derived_*` | Extracted text, OCR, page images, image variants |

Never inline raw base64 into the prompt. Images go through canonical `MediaPart`; documents go through extracted text (and optional page images). Adapters map those parts at the provider boundary.

Tool transcripts reconstruct from `ToolInvocation` records plus artifact ids so the next turn has a valid tool-call / tool-result pair, not orphan output.

Conversation transcript rows may store `thinking_text` (provider reasoning), `tool_summaries` (name, status, preview; empty when this agent ran no tools), `cost_usd` (priced loop estimate), and `spawn_children` (card pointers to isolated spawn conversations, including the child's thinking and tool summaries for parent-rail reload) for chat reload. A row's tool items are that agent's invocations only — in-thread children share the conversation but not the parent's tools. A `core.spawn_agents` child persists its first turn on its own conversation (opaque id, parent/root pointers) with the spawn instruction as the user message. The parent row keeps the pointer, not nested child assistant rows. GET uses the stored `tool_summaries` for that row. Next-turn replay does not inject these display fields as assistant content or extra tool messages. Absent `cost_usd` means unknown, not free. Deleting or clearing a conversation also deletes its isolated descendants (spawn children and `isolate_conversation` steps) and their scoped memory; descendants come from the lineage children index merged with the registry's durable `parent_conversation_id` rows, so the cascade still works after the index TTL. In-thread panelists share the parent id, so they are already in that wipe. Deleting a child does not delete its parent or siblings.

Stores: Redis (still supported) and S3-compatible (SeaweedFS locally, AWS S3 on EC2). Access is tenant/principal gated.

**Artifact RAG** is opt-in and shares the local embedding sibling. See [embeddings-rag.md](./embeddings-rag.md). Do not treat LTM semantic recall as RAG.

## Paths

- Memory: `motet/core/memory/`, memory commands
- Artifacts: `motet/core/artifacts/`
- RAG: `motet/core/rag/`
- Onboarding: `docs/developer_onboarding/20-memory-management.md`, `20a-artifacts-and-multimodal-context.md`
