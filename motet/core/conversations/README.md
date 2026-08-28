# Core Conversations

Conversation-related helpers: canonical transcript codec, rendering, replay, storage, and a thin conversation-state facade.

## Contents

- **`conversation_state`** — Facade for conversation state (load history, append turn).
 - **`load_history(motet, conversation_id, *, limit=250)`** — Returns `List[Tuple[created_at, Message]]` (delegates to transcript replay).
 - **`append_turn(motet, messages, assistant_response, *, agent_id=None)`** — Persists one turn (delegates to transcript storage). Optional **agent_id** (qualified registry id) attributes the assistant reply in the canonical transcript.
- **`pending_action`** — Pending-action confirmation state.
 - **`build_heuristic_marker(assistant_response, tool_shortlist)`** — Tail-question heuristic writer; returns the marker dict (`marker_id`, `source: "heuristic"`, `question`, optional `tool_shortlist`, `carried_forward`) or None.
 - **`load_pending_action(motet, conversation_id)`** — Reads the marker from the latest root assistant message (positional semantics: newer turns bury older markers). Returns `PendingActionLookup(marker, status)` with status `"fresh"`/`"stale"` derived from the carrying row's timestamp. The marker is the single source of truth for pendingness — detection happens once at write time, never via read-time text heuristics.
 - **`evaluate_pending_action(motet, conversation_id, user_text)`** — One-shot turn evaluation used by `agent_turn`: loads the marker, classifies the reply, computes the deferral carry-forward, and builds the routing hint for conversation_analysis. Returns `PendingActionTurnState(marker, status, reply, carry, routing_hint)`.
 - **`classify_confirmation_reply(text)`** — Closed confirm/decline/other partition; ambiguous closers ("ok thanks", "no thanks") map to "other".
 - **`AFFIRMATIVE_ACKS` / `NEGATIVE_ACKS` / `normalize_ack_text`** — Shared reply vocabulary and normalization. `trivial_message` composes its greeting allowlist from these groups so the turn gate and confirmation classification cannot drift.
 - **`build_carry_forward_marker(marker)`** — Capped `carried_forward` increment for unconsumed deferrals; None past the cap.
 - Config: `MOTET_PENDING_ACTION_FRESHNESS_SECONDS` (default 1800), `MOTET_PENDING_ACTION_MAX_CARRY_FORWARD` (default 2).
- **`trivial_message`** — Closed greeting/ack allowlist used by the turn gate and conversation analysis.
 - **`is_trivial_message(message)`** — True when a user message matches the allowlist (no `?`, no multimodal parts).
 - **`last_user_message(messages)`** — Last `role=user` message, or None.
- **`transcript_codec`** — Serialize/deserialize canonical `TranscriptItem` lists; build transcript for one turn.
 - **`serialize_transcript_items`** / **`deserialize_transcript_items`** — Persist in `MemoryItem.metadata`.
 - **`build_transcript_items_for_turn(..., agent_id=..., pending_action=...)`** — Used by storage; sets `Message.agent_id` and, when provided, `metadata["pending_action"]` on the final assistant message.
 - **`transcript_rendering`** — Render canonical `TranscriptItem` list to canonical `Message` list (`tool_calls_canonical`).
 - **`render_transcript_items_to_messages(items, provider_name=..., turn_agent_id=...)`** — Used at replay time; applies turn-level **agent_id** to rebuilt assistant tool-call messages.
- **`transcript_replay`** — Replay canonical `conversation_transcript` memories into `Message` lists.
 - **`get_conversation_history_from_transcripts(motet, conversation_id, *, limit=250)`** — Used internally by `load_history`. Sorts by `metadata.sequence` (Redis is required; rows without a sequence sort to position 0). Orders each user turn as `user -> sub-agents -> root assistant`, keeping each assistant `tool_calls` message glued to its immediately following `role=tool` results so reorder cannot create provider-rejected orphan tool turns. Transcript rows are envelope-encrypted; after a tenant-prefix RENAME, decrypt retries AAD with the pre-rename logical Redis key.
 - **`merge_conversation_history(current, history)`** — Dedupe merge for prepare_context. Assistant keys include `agent_id` (multi-agent safe); a missing `agent_id` on the incoming side is a wildcard so client-echoed turns without provenance still collapse against the stored copy (issue #138).
 - **`message_to_history_item(msg, created_at)`** — API history item shape for conversation_get (includes **`agent_id`** and **`parent_agent_id`** when set on the message).
- **`transcript_storage`** — Persist one turn as a conversation_transcript memory.
 - **`store_turn_transcript(..., agent_id=..., pending_action_carry=..., include_tool_invocations=..., parent_agent_id=...)`** / **`store_subagent_reply`** / **`resolve_transcript_agent_id`** — Used by finalize_turn, append_turn, and `core.spawn_agents` child replies; stores canonical transcript items in one completed memory row with deterministic `metadata.sequence`, and stores **`agent_id`** on transcript metadata. Nested rows also store **`parent_agent_id`**. writer: attaches a heuristic `pending_action` marker when the assistant response ends with a question (root turns only), or re-attaches an unconsumed carried marker passed via `pending_action_carry` (a fresh proposal wins over carry). `store_subagent_reply` writes one nested write-up with `root_turn=False` and no tool-invocation rows.
- **`ownership`** — Authoritative conversation ownership binding (issue #139).
 - **`authorize_conversation_access_sync(...)`** / **`authorize_motet_conversation_access(motet,...)`** — Bind owner on first write-path use; reject a different principal in the same tenant (403 / `ConversationAccessDenied`). Read/clear paths set `bind_if_unclaimed=False` and lazy-bind from the caller's registry for pre-ownership conversations.
 - **`require_not_owned_by_other_sync(...)`** — Non-binding guard used at the API boundary (`POST /api/v1/chat`) so a cross-principal request fails with HTTP 403 **before** dispatch. Needed because a streaming response cannot change status once headers are sent; it deliberately does not claim, since the agent may rewrite the id with a configured prefix and `agent_turn` binds the effective id.
 - **`delete_conversation_owner_sync(...)`** — Clears the ownership record (used by conversation clear/delete).
 - Key pattern: `{tenant_id}:conv:owner:{motet_id}:{conversation_id}` (issue #218). Leftover Phase 2 `{tenant}:imf:conv:…` keys are not dual-read. Ownership metadata (not principal-scoped KV) leaves room for Phase 4 membership/ACL later. Multi-agent turns under one principal are unaffected.
 - `authorize_motet_conversation_access` **skips with a warning** when the context has no `motet_id`/`tenant_id`/`principal_id`, so identity-less internal callers (schedules, system commands) are not hard-failed in the turn hot path. The attacker-reachable surface is the HTTP API, which always carries a verified principal.
- **`lineage`** — Single source of truth for the workflow `isolate_conversation` child conversation ID convention (`{parent}__{suffix}`) plus a Redis-backed parent→children index.
 - **`make_child_conversation_id(parent, suffix=...)`** — Mints child IDs (used by the workflow executor). Sanitizes suffixes; empty parents get a generated `workflow-*` base.
 - **`root_conversation_id_of(cid)`** / **`is_child_conversation_id(cid)`** — Parse side of the convention; nested children (`root__a__b`) attribute to the top-level root. The cost tracking service uses this for exact per-conversation rollups — no other module may hand-roll `__` splitting.
 - **`record_conversation_lineage_sync(tenant_id=..., child_conversation_id=...)`** / **`list_child_conversations_sync(...)`** — Best-effort parent→children index (30-day TTL) written by the executor when a step isolates; surfaced via `motet_admin.get_conversation_summary(include_children=True)` for cycle observability.

## Dependencies

- `motet.core.types` — Message, ToolCallRequest, ToolCallResult, TranscriptItem
- `motet.core.tools.tool_transcripts` — ToolInvocation, ToolInvocationStatus (codec)
- `motet.memory.recall_conversation(types=["conversation_transcript"])`

## Related

- Conversations API and persistence
- Agent/surface scoping; message-level **agent_id** on canonical transcript
- Pending-action confirmation state carried in the canonical transcript
- Canonical transcript storage and replay
