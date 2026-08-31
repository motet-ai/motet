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
 - **`message_to_history_item(msg, created_at)`** — API history item shape for conversation_get (includes **`agent_id`**, **`parent_agent_id`**, **`thinking_text`**, **`tool_summaries`**, **`cost_usd`**, and **`spawn_children`** when set on the message). Conversation GET fills each spawn card's thinking, tool summaries, and cost from the child conversation when the stored parent pointer omitted them.
- **`transcript_storage`** — Persist one turn as a conversation_transcript memory.
 - **`store_turn_transcript(..., agent_id=..., pending_action_carry=..., include_tool_invocations=..., parent_agent_id=..., thinking_text=..., tool_summaries=..., cost_usd=..., conversation_id=..., spawn_children=...)`** / **`resolve_transcript_agent_id`** — Used by finalize_turn, append_turn, and the `children` lifecycle (child brief at mint, reply after fan-in); stores canonical transcript items in one completed memory row with deterministic `metadata.sequence`, and stores **`agent_id`** on transcript metadata. Tool items on the row are that agent's invocations only (in-thread children share the conversation, not the parent's tools). Optional **`conversation_id`** writes the row on an isolated conversation. Optional **`thinking_text`**, **`tool_summaries`**, **`cost_usd`**, and **`spawn_children`** are display-only (conversation reload); they are not replayed as assistant content or extra tool messages. **`tool_summaries`** is always stored (empty when this agent ran no tools); GET uses that list for the rail. Writer: attaches a heuristic `pending_action` marker when the assistant response ends with a question (root turns only), or re-attaches an unconsumed carried marker passed via `pending_action_carry` (a fresh proposal wins over carry).
- **`ownership`** — Authoritative conversation ownership binding (issue #139).
 - **`authorize_conversation_access_sync(...)`** / **`authorize_motet_conversation_access(motet,...)`** — Bind owner on first write-path use; reject a different principal in the same tenant (403 / `ConversationAccessDenied`). Read/clear paths set `bind_if_unclaimed=False` and lazy-bind from the caller's registry for pre-ownership conversations.
 - **`require_not_owned_by_other_sync(...)`** — Non-binding guard used at the API boundary (`POST /api/v1/chat`) so a cross-principal request fails with HTTP 403 **before** dispatch. Needed because a streaming response cannot change status once headers are sent; it deliberately does not claim, since the agent may rewrite the id with a configured prefix and `agent_turn` binds the effective id.
 - **`delete_conversation_owner_sync(...)`** — Clears the ownership record (used by conversation clear/delete).
 - Key pattern: `{tenant_id}:conv:owner:{motet_id}:{conversation_id}` (issue #218). Leftover Phase 2 `{tenant}:imf:conv:…` keys are not dual-read. Ownership metadata (not principal-scoped KV) leaves room for Phase 4 membership/ACL later. Multi-agent turns under one principal are unaffected.
 - `authorize_motet_conversation_access` **skips with a warning** when the context has no `motet_id`/`tenant_id`/`principal_id`, so identity-less internal callers (schedules, system commands) are not hard-failed in the turn hot path. The attacker-reachable surface is the HTTP API, which always carries a verified principal.
- **`children`** — Child-conversation lifecycle for fan-outs (`core.spawn_agents` today; reusable by any command that runs a child agent on an isolated conversation).
 - **`create_child_conversation(motet, instruction=..., registry_agent_id=..., pointer_agent_id=..., surface_id=..., kind=..., turn_agent_id=..., spawn_contract=...)`** — Mint an isolated id, claim + register it with parentage, and write the instruction as the child's first user message (the brief) *before* the child runs, so a live card click opens a real conversation. Spawn children stay listed under the parent chat agent and record `turn_agent_id` (`core.subagent`) plus the per-task tool cage. Registration and brief are fail-soft; returns a frozen `ChildConversation` with ids and `brief_written`.
 - **`complete_child_conversation(motet, child_cid=..., reply_text=..., ...)`** — Persist the child's reply on the child conversation (inlining the brief when it was not written earlier), touch the registry row, and return the parent-turn card pointer (`child_conversation_id`, `agent_id` for live rail, `turn_agent_id` for follow-up, `title`, optional `preview` / `cost_usd` / `thinking_text` / `tool_summaries`). Returns None on a failed write so callers can degrade to pointer-only.
 - **`child_pointer(...)`** / **`parent_registry_scope(motet, fallback_agent_id)`** / **`hydrate_spawn_children(motet, cards)`** / **`spawn_contract_for_followup(row, qualified_id)`** — Pointer shape, the parent chat agent / surface used for child registry rows, GET-time fill of thinking / tool summaries / cost from each child's stored turn, and whether a follow-up `agent_turn` should apply the stored cage.
- **`lineage`** — Isolated conversations: opaque child id plus stored parent/root pointers (workflow `isolate_conversation` and `core.spawn_agents`), and a Redis-backed parent→children index.
 - **`mint_isolated_conversation(parent, tenant_id=..., kind=...)`** — Mint a unique `iso-…` child id. Empty parents get a generated `workflow-*` parent/root.
 - **`root_conversation_id_of(cid, tenant_id=...)`** / **`is_child_conversation_id(cid, tenant_id=...)`** — Read the parentage hash. Nested isolation stores the top-level chat as `root_conversation_id`. Cost rollup uses the root stamped on the child context; it does not parse the id.
 - **`record_conversation_lineage_sync(tenant_id=..., child_conversation_id=..., parent_conversation_id=..., root_conversation_id=...)`** / **`list_child_conversations_sync(...)`** / **`list_descendant_conversations_sync(...)`** / **`forget_conversation_lineage_sync(...)`** — Best-effort parentage + children index (30-day TTL) written on mint; surfaced via `motet_admin.get_conversation_summary(include_children=True)` for cycle observability. Conversation clear walks descendants and forgets lineage rows for each cleared id.

## Dependencies

- `motet.core.types` — Message, ToolCallRequest, ToolCallResult, TranscriptItem
- `motet.core.tools.tool_transcripts` — ToolInvocation, ToolInvocationStatus (codec)
- `motet.memory.recall_conversation(types=["conversation_transcript"])`

## Related

- Conversations API and persistence
- Agent/surface scoping; message-level **agent_id** on canonical transcript
- Pending-action confirmation state carried in the canonical transcript
- Canonical transcript storage and replay
