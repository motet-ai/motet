"""
Motet - Agent Turn Command

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-31

Description:
    The `agent_turn` command: one complete turn of agent execution.

    `agent_turn` is the root owner of turn hooks and turn lifecycle events. It
    resolves AgentConfig + ToolFilter, runs the configured hooks via
    ``turn/hooks.py`` (conversation_analysis, context injection, skill catalog,
    memory_reset / prepare_context / finalize_turn, after_finalize), delegates
    the reasoning itself to Turn Runtime ``start`` / ``continue_after_budget``
    (in-process;). After ``resolve_turn_mode``, a ``no_tools`` turn streams
    one reply and everything else calls ``run_agent`` on the same worker —
    no command hop and no second entry point. Emits the standard turn
    stream events the UI expects. Implements unified task-level streaming.

    the turn gates finalize and lifecycle on ``TurnResult.kind``. A
    `suspended` result — an externally-owned tool handed back to the caller —
    skips finalize_turn and emits a suspended-terminal stream event instead of
    `end`. `resume_agent_turn` (turn/resume_agent_turn.py) continues it later.

    Issue #188: ``continue_after_budget`` rehydrates the latest
    ``checkpoint_kind=budget_continue`` snapshot via LoopStateSnapshot with a
    **fresh** budget policy (new turn; prior turn already finalized). Missing
    snapshot falls back to transcript + steering, still as a new turn.

Dependencies:
    - motet.core.commands: @motet.command, MotetContext, payload models
    - motet.core.agents: agent registry and prompt policy
    - motet.core.reasoning.react.agent: ``run_agent`` in-process
    - turn.no_tools: greeting / forced no-tools reply
    - motet.core.tools.schema_exporter: tool schema for the model call
    - turn.prepare / turn.complete / turn.hooks: input-shaping, hooks, completion

Usage:
    from motet.core.orchestration.turn import agent_turn
    from motet.core.commands.command_data_classes import AgentTurnData

    result = motet.do(
        agent_turn,
        data=AgentTurnData(agent_id="cursor.backend", messages=[...], context={...}),
    )

Notes:
    - Neighbours in this package, since the names are easy to confuse:
      `phases.py` holds the surrounding *commands* (memory_reset,
      prepare_context, finalize_turn,...), while `prepare.py`, `hooks.py`, and
      `complete.py` hold plain *helpers* this module calls around reasoning.
      `outcome.py` classifies the result; `resume_agent_turn.py` restarts a
      suspended turn.
    - Hooks extracted to turn/hooks.py (issue #147 Priority 1 complete).
    - The phase commands and `get_motet_context` are lazy-imported at call time.
      `phases.py` owns the single context binding, so a test that patches
      `turn.phases.get_motet_context` also redirects this module.
    - Prompt policy (client_system_primary): inbound client system messages stay
      primary; the agent's system_prompt is an appendix (cursor OpenAI-compat).
"""


from typing import Any, Callable, Dict, List, Optional
import structlog

from motet import motet
from motet.core.commands.capabilities import WorkerCapability
from motet.core.commands.command_data_classes import AgentTurnData
from motet.core.orchestration.turn.complete import (
    _collect_generated_media,
    _iter_tool_result_dicts,
    _media_type_for_content_type,
    _validate_and_enrich_media,
    complete_agent_turn,
    extract_response_text,
    extract_thinking_text,
    extract_tool_summaries,
    extract_spawn_children,
    extract_turn_cost,
    extract_turn_usage,
    resolve_turn_model,
)
from motet.core.conversations.trivial_message import last_user_message
from motet.core.orchestration.turn.gate import resolve_turn_mode
from motet.core.orchestration.turn.hooks import (
    _leading_system_insert_at,
    persist_history_only,
    run_after_finalize_hooks,
    run_finalize_hook,
    run_pre_reasoning_hooks,
)
from motet.core.orchestration.turn.prepare import (
    _coerce_reasoning_effort,
    ensure_required_tool,
    ensure_tool_schema,
    extract_turn_input_text,
    resolve_turn_model_policy,
    to_turn_messages,
)
from motet.core.agents.prompt_policy import (
    assemble_turn_history,
    extract_protected_prefix,
    prompt_policy_from_agent,
)
from motet.core.reasoning.react.agent_data import (
    DEFAULT_MODEL_NAME,
    DEFAULT_MODEL_PROVIDER,
)
from motet.core.types import Message
from motet.core.workers.observers import EventPriority

logger = structlog.get_logger(__name__)


def get_motet_context() -> Any:
    """Resolve the Motet context through ``turn.phases``.

    Deliberately indirect. ``phases`` owns the single ``get_motet_context``
    binding for the turn package, so resolving through it means one patch —
    ``monkeypatch.setattr(phases, 'get_motet_context', ...)`` — redirects the
    phase commands and the turn itself together. Importing the symbol straight
    from the decorator here would bind it at import time and silently escape
    that patch.
    """
    from motet.core.orchestration.turn import phases

    return phases.get_motet_context()


# Config classes have been replaced by CommandData classes in command_data_classes.py
# ADR-0109: tool-call sanitizer helpers moved to motet.core.orchestration.context.tool_calls;
# the temporary compatibility wrappers previously kept here have been removed.
# Issue #147: _coerce_reasoning_effort lives in turn/prepare.py (re-exported above).


def _inherit_parent_context(
    metadata: Dict[str, Any],
    effective_context: Dict[str, Any],
    keys: tuple[str, ...],
) -> None:
    """Fill gaps in effective_context from parent command metadata.

    Called early in agent_turn so the resolved context is available for agent
    config lookup, role checks, and conversation prefix resolution.  Payload
    values (already in effective_context) take precedence.  Pass
    ``DELEGATED_CONTEXT_KEYS`` so identity/model and artifact RAG auth both
    inherit.
    """
    for key in keys:
        if key in metadata and metadata[key] is not None and key not in effective_context:
            effective_context[key] = metadata[key]


def _build_child_metadata(
    parent_metadata: Dict[str, Any],
    effective_context: Dict[str, Any],
    keys: tuple[str, ...],
    *,
    qualified_id: str,
    is_root_turn: bool,
) -> Dict[str, Any]:
    """Build metadata dict that child commands will inherit.

    Preserves non-overlay keys from the parent (internal routing fields) and
    overlays the resolved effective_context values for ``keys`` (typically
    ``DELEGATED_CONTEXT_KEYS``: schedule/identity + artifact RAG auth).
    Always stamps ``agent_id`` with the canonical qualified id (ADR-0083)
    and, for root turns, sets ``conversation_primary_agent_id`` for
    transcript replay ordering.
    """
    child_meta = {
        **parent_metadata,
        **{k: effective_context[k] for k in keys
           if k in effective_context and effective_context[k] is not None},
        "agent_id": qualified_id,
    }
    if is_root_turn:
        child_meta["conversation_primary_agent_id"] = qualified_id
    return child_meta


def _resolve_transcript_primary(
    metadata: Dict[str, Any],
    qualified_id: str,
    parent_command_id: Optional[str],
    resolve_agent_id_fn: Any,
) -> tuple[str, bool]:
    """Determine the conversation-primary agent and whether this turn belongs to it.

    In a multi-agent conversation the *primary* is the top-level agent the user
    is chatting with.  Sub-agents (workflow delegates, panel members, etc.) still
    finalize transcripts, but their ``root_turn`` flag is ``False`` so transcript
    replay can distinguish main responses from supporting ones.

    Returns ``(root_agent_id, is_root_turn)`` for ``FinalizeTurnData``.
    """
    raw_primary = metadata.get("conversation_primary_agent_id")
    if isinstance(raw_primary, str) and raw_primary.strip():
        primary = resolve_agent_id_fn(raw_primary.strip())
        return primary, qualified_id == primary

    if not parent_command_id:
        return qualified_id, True

    return qualified_id, False


def _agent_data_for_turn(
    *,
    query: str,
    history: List[Any],
    qualified_id: str,
    agent_config: Any,
    provider: str,
    model_name: str,
    model_profile_name: Optional[str],
    enable_thinking: bool,
    reasoning_effort: Any,
    resolved_tools: Any,
    metadata: Dict[str, Any],
    effective_context: Dict[str, Any],
) -> Any:
    """Build ``AgentData`` for this turn's ``run_agent`` call."""
    from motet.core.reasoning.react.agent_data import AgentData

    ctx = effective_context
    meta = dict(metadata or {})

    raw_max_tools = getattr(agent_config, "max_tools", None)
    if raw_max_tools is None:
        raw_max_tools = meta.get("max_tools", ctx.get("max_tools", 20))
    try:
        max_tools = max(1, min(int(raw_max_tools), 100))
    except (TypeError, ValueError):
        max_tools = 20

    raw_max_model_calls = ctx.get(
        "max_model_calls", getattr(agent_config, "max_model_calls", None)
    )
    max_model_calls = None
    if raw_max_model_calls is not None:
        try:
            max_model_calls = max(1, int(raw_max_model_calls))
        except (TypeError, ValueError):
            max_model_calls = None

    raw_max_cost = ctx.get("max_cost_usd", getattr(agent_config, "max_cost_usd", None))
    max_cost_usd = None
    if raw_max_cost is not None:
        try:
            max_cost_usd = max(0.0, float(raw_max_cost))
        except (TypeError, ValueError):
            max_cost_usd = None

    raw_max_prompt = ctx.get(
        "max_prompt_tokens", getattr(agent_config, "max_prompt_tokens", None)
    )
    max_prompt_tokens = None
    if raw_max_prompt is not None:
        try:
            max_prompt_tokens = max(0, int(raw_max_prompt))
        except (TypeError, ValueError):
            max_prompt_tokens = None

    try:
        temperature = float(ctx.get("temperature", getattr(agent_config, "temperature", 0.2)))
    except (TypeError, ValueError):
        temperature = 0.2

    return AgentData(
        agent_id=qualified_id,
        use_task_stream=True,
        input=query,
        conversation_history=list(history),
        max_iterations=getattr(agent_config, "max_iterations", 20),
        max_model_calls=max_model_calls,
        max_cost_usd=max_cost_usd,
        max_prompt_tokens=max_prompt_tokens,
        max_tools=max_tools,
        model_provider=provider or DEFAULT_MODEL_PROVIDER,
        model_name=model_name or DEFAULT_MODEL_NAME,
        model_profile_name=model_profile_name,
        temperature=temperature,
        tools=resolved_tools,
        enable_thinking=bool(enable_thinking),
        reasoning_effort=reasoning_effort or "medium",
        metadata=meta,
        tool_filter_metadata=meta.get("tool_filter_metadata"),
        skill_refs=meta.get("skill_refs"),
        prefilled_tool_calls=ctx.get("prefilled_tool_calls"),
        handback_tool_names=ctx.get("handback_tool_names"),
        handback_tools=ctx.get("handback_tools"),
    )


# Issue #147: media helpers live in turn/complete.py (re-exported above for
# orchestration.py / test import compatibility).


def _suspended_turn_response(
    motet: Any,
    payload: Any,
    qualified_id: str,
    parent_command_id: Optional[str],
    analysis_metadata: Dict[str, Any],
    prepared_context_info: Dict[str, Any],
    persist_history: Optional[Callable[[str], None]] = None,
) -> Optional[Dict[str, Any]]:
    """
    ADR-0127 / issue #147: gate suspended (and other non-finalize) outcomes.

    Compatibility wrapper around ``classify_loop_outcome`` +
    ``apply_turn_outcome_gate``. Returns None for finalize-eligible outcomes.
    """
    from motet.core.orchestration.turn.outcome import apply_turn_outcome_gate, classify_loop_outcome

    outcome = classify_loop_outcome(payload)
    if outcome.should_finalize:
        return None
    return apply_turn_outcome_gate(
        motet,
        outcome,
        payload,
        qualified_id,
        parent_command_id,
        analysis_metadata,
        prepared_context_info,
        persist_history,
    )


@motet.command(
    description="Execute a full agent turn with orchestration-owned lifecycle: prepare context, run the agent loop, finalize conversation and memory.",
    timeout_seconds=600,
    priority=EventPriority.HIGH,
    required_capabilities=[WorkerCapability.REASONING, WorkerCapability.TOOL_EXECUTION],
    streaming_enabled=True,
)
def agent_turn(data: AgentTurnData) -> Dict[str, Any]:
    """
    Execute an agent-configured turn with explicit lifecycle ownership.

    This command is the root owner for turn hooks and turn lifecycle events.
    It resolves AgentConfig + ToolFilter, runs configured hooks, delegates the
    loop to Turn Runtime ``start`` / ``continue_after_budget`` (in-process;
    ADR-0134), and emits standard turn events for UI compatibility.
    """
    import json

    # Lazy so a test patching `turn.phases` is picked up here: binding these at
    # module level would capture the originals before the patch is applied.
    from motet.core.orchestration.turn.phases import (
        _extract_analysis_metadata,
        finalize_turn,
        memory_reset,
        prepare_context,
    )

    motet = get_motet_context()
    motet_cmd = getattr(motet, "_command", None)
    effective_context: Dict[str, Any] = dict(data.context or {})

    from motet.core.commands.command_data_classes import (
        DELEGATED_CONTEXT_KEYS,
        RegisterConversationData,
    )

    if motet_cmd is not None:
        _inherit_parent_context(
            motet_cmd.distributed_context.metadata or {},
            effective_context,
            DELEGATED_CONTEXT_KEYS,
        )

    from motet.core.agents import (
        ensure_conversation_id_prefix,
        get_agent_registry,
        get_discovery_filter_metadata,
        principal_may_access_agent,
        resolve_agent_id,
        resolve_tools,
    )
    from motet.core.reasoning.react import run_agent
    from motet.core.orchestration.turn.runtime import continue_after_budget, start
    from motet.core.orchestration.turn.no_tools import answer_without_tools
    from motet.core.tools.schema_exporter import ToolSchemaExporter
    from motet.core.workers.events import Event
    from motet.core.commands.builtin.conversation import conversation_register
    from motet.core.commands.builtin.conversation_analysis import conversation_analysis, ConversationAnalysisData

    # Resolve agent config first (single source of truth for all agent-specific behavior).
    # Prefer top-level AgentTurnData.agent_id; fall back to context for older callers.
    qualified_id = resolve_agent_id(
        data.agent_id or effective_context.get("agent_id")
    )
    effective_context["agent_id"] = qualified_id
    registry = get_agent_registry()
    agent_config = registry.get(qualified_id)
    if not agent_config:
        raise RuntimeError(f"Unknown agent: {qualified_id}")

    # Enforce role-based visibility in the root command (single source of truth).
    principal_roles = list(effective_context.get("principal_roles", []) or [])
    if not principal_roles:
        role_hint = effective_context.get("role")
        if isinstance(role_hint, str) and role_hint:
            principal_roles = [role_hint]
    if not principal_may_access_agent(agent_config, principal_roles):
        raise RuntimeError(f"Role not authorized for agent '{qualified_id}'")

    spawn_contract = None
    try:
        from motet.core.conversations.children import spawn_contract_for_followup
        from motet.core.conversations.registry import get_conversation_sync

        spawn_row = get_conversation_sync(
            str(getattr(motet, "motet_id", None) or ""),
            str(getattr(motet, "tenant_id", None) or ""),
            str(getattr(motet, "principal_id", None) or ""),
            str(getattr(motet, "conversation_id", None) or ""),
        )
        spawn_contract = spawn_contract_for_followup(spawn_row, qualified_id)
    except Exception as exc:
        logger.warning(
            "spawn_followup_contract_lookup_failed",
            conversation_id=getattr(motet, "conversation_id", None),
            error=str(exc),
            error_type=type(exc).__name__,
        )
        spawn_contract = None

    # Apply conversation prefix in the root command and propagate to child commands.
    prefixed_conversation_id = ensure_conversation_id_prefix(
        motet.conversation_id,
        getattr(agent_config, "conversation_id_prefix", None),
    )
    if prefixed_conversation_id and prefixed_conversation_id != motet.conversation_id:
        try:
            if motet_cmd is not None:
                motet_cmd.distributed_context.conversation_id = prefixed_conversation_id  # type: ignore[attr-defined]
            effective_context["conversation_id"] = prefixed_conversation_id
            if getattr(motet, "stack", None) is not None:
                setattr(motet.stack, "_current_conversation_id", prefixed_conversation_id)
        except Exception as conv_err:
            logger.warning(
                "agent_turn_conversation_prefix_apply_failed",
                requested=motet.conversation_id,
                prefixed=prefixed_conversation_id,
                error=str(conv_err),
            )

    # Issue #139: bind/authorize conversation ownership before registry touch or memory.
    # Same-principal multi-agent / child turns continue; cross-principal access is rejected.
    from motet.core.conversations.ownership import authorize_motet_conversation_access

    authorize_motet_conversation_access(motet, bind_if_unclaimed=True)

    parent_command_id = getattr(getattr(motet, "distributed_context", None), "parent_command_id", None)

    if motet_cmd is not None:
        motet_cmd.distributed_context.metadata = _build_child_metadata(
            motet_cmd.distributed_context.metadata or {},
            effective_context,
            DELEGATED_CONTEXT_KEYS,
            qualified_id=qualified_id,
            is_root_turn=not parent_command_id,
        )

    # Register/touch conversation in the registry so it appears in conversation lists (ADR-0083: scope on create).
    if motet.conversation_id:
        try:
            surface_id = effective_context.get("surface_id") if isinstance(effective_context.get("surface_id"), str) else None
            motet.dispatch(
                [
                    (
                        conversation_register,
                        RegisterConversationData(
                            conversation_id=motet.conversation_id,
                            title=None,
                            agent_id=qualified_id,
                            surface_id=surface_id,
                        ),
                    )
                ]
            )
        except Exception as reg_err:
            logger.debug("agent_turn_conversation_registry_touch_failed", error=str(reg_err))

    # Normalize request messages and prepend configured system prompt.
    system_prompt = getattr(agent_config, "system_prompt", "") or ""
    if spawn_contract and spawn_contract.get("discover"):
        agent_meta = getattr(agent_config, "metadata", None) or {}
        discovery_prompt = (
            agent_meta.get("discovery_system_prompt")
            if isinstance(agent_meta, dict)
            else None
        )
        if discovery_prompt:
            system_prompt = str(discovery_prompt)
    prompt_policy = prompt_policy_from_agent(agent_config)
    history: List[Message] = assemble_turn_history(
        to_turn_messages(data.messages),
        system_prompt,
        prompt_policy,
    )
    protected_system_prefix = extract_protected_prefix(history)
    if prompt_policy != "motet_system_primary":
        logger.info(
            "agent_turn_prompt_policy",
            prompt_policy=prompt_policy,
            protected_system_count=len(protected_system_prefix),
            agent_id=getattr(agent_config, "agent_id", None),
        )

    # Resolve tools inside the root command so API/WS/CLI flows share one path.
    tool_registry = getattr(getattr(motet, "stack", None), "tool_registry", None) or motet.tools
    schema_exporter = ToolSchemaExporter(tool_registry)
    resolved_tools = resolve_tools(
        agent_config.tool_filter,
        tool_registry,
        schema_exporter,
        max_tools=getattr(agent_config, "max_tools", None),
    )
    if spawn_contract and not spawn_contract.get("discover"):
        declared = [
            str(name).strip()
            for name in (spawn_contract.get("tools") or [])
            if str(name).strip()
        ]
        if declared:
            from motet.core.tools.builtin.spawn_agents import resolve_child_tool_schemas

            caged = resolve_child_tool_schemas(motet, declared)
            if caged:
                resolved_tools = caged

    # Build model policy from request context over config defaults over stack defaults.
    stack_cfg = getattr(getattr(motet, "stack", None), "config", None)
    model_policy = resolve_turn_model_policy(effective_context, agent_config, stack_cfg)
    provider = model_policy.provider
    model_name = model_policy.model_name
    model_profile_name = model_policy.model_profile_name
    enable_thinking = model_policy.enable_thinking
    reasoning_effort = model_policy.reasoning_effort

    input_text = extract_turn_input_text(history)

    # ADR-0121 Phase 1 reader: evaluate the pending-action marker from the
    # latest root assistant message before conversation analysis, so a
    # trivial-looking reply ("ok") answering "Should I send it?" routes as a
    # confirmation instead of skip-classifying as a pleasantry. The helper
    # loads the marker, classifies the reply, computes the deferral
    # carry-forward, and builds the analysis routing hint; failures degrade
    # to nothing-pending routing.
    from motet.core.conversations.pending_action import (
        build_pending_action_system_message,
        evaluate_pending_action,
    )

    pending = evaluate_pending_action(motet, motet.conversation_id, input_text)

    # Ensure a task-level stream exists and emit lifecycle events.
    motet.ensure_stream(ttl_seconds=3600)
    motet.stream_event("start", command_type="agent_turn")
    # Explicit per-agent non-terminal lifecycle marker (complements generic `start`).
    motet.stream_event("agent_turn_start", agent_id=qualified_id)
    cmd_metadata = (
        getattr(getattr(motet_cmd, "distributed_context", None), "metadata", None) or {}
    ) if motet_cmd is not None else {}
    finalize_root_agent_id, finalize_root_turn = _resolve_transcript_primary(
        cmd_metadata, qualified_id, parent_command_id, resolve_agent_id,
    )
    # Grab a deterministic sequence number early so sub-agents that start
    # later get higher numbers. The sequence is passed to finalize_turn via
    # FinalizeTurnData and then stored on the completed transcript row.
    # Redis is a hard requirement — if INCR fails the system is unhealthy.
    from motet.core.conversations.transcript_storage import allocate_transcript_sequence

    assert motet.redis is not None, "Redis is required for transcript sequence allocation"
    reserve_sequence = allocate_transcript_sequence(
        motet.conversation_id,
        motet.redis,
        tenant_id=getattr(motet, "tenant_id", None),
    )

    # Forward reasoning_meta from EventBus for trace/UI parity.
    def forward_reasoning_event(event: Event) -> None:
        if event.event_type == "reasoning_meta":
            meta_data = {
                "complexity": event.data.get("complexity", "unknown"),
                "strategy": event.data.get("strategy", "unknown"),
                "sources": event.data.get("sources", []),
            }
            motet.stream_event("reasoning_meta", data=json.dumps(meta_data))

    def task_filter(event: Event) -> bool:
        return event.data.get("task_id") == motet.task_id

    turn_hooks = getattr(agent_config, "turn_hooks", None)
    analysis_metadata: Dict[str, Any] = {}
    prepared_context_info: Dict[str, Any] = {}
    final_response = ""
    turn_result: Dict[str, Any] = {}

    def _persist_history_only(assistant_response: str) -> None:
        persist_history_only(
            motet=motet,
            turn_hooks=turn_hooks,
            finalize_turn=finalize_turn,
            history=history,
            assistant_response=assistant_response,
            qualified_id=qualified_id,
            finalize_root_turn=finalize_root_turn,
            finalize_root_agent_id=finalize_root_agent_id,
            reserve_sequence=reserve_sequence,
            pending_action_carry=pending.carry,
            thinking_text=extract_thinking_text(turn_result),
            tool_summaries=extract_tool_summaries(turn_result),
            cost_usd=extract_turn_cost(turn_result),
            spawn_children=extract_spawn_children(turn_result),
        )

    def _maybe_suspended_turn_response(result: Any) -> Optional[Dict[str, Any]]:
        from motet.core.orchestration.turn.runtime.result import TurnResult, TurnResultKind

        if isinstance(result, TurnResult):
            if result.kind not in (
                TurnResultKind.SUSPENDED,
                TurnResultKind.AUTH_REQUIRED,
            ):
                return None
            payload = result.payload
        else:
            payload = result
        return _suspended_turn_response(
            motet, payload, qualified_id, parent_command_id,
            analysis_metadata, prepared_context_info,
            _persist_history_only,
        )

    with motet.observe_events(
        event_types={"reasoning_meta"},
        callback=forward_reasoning_event,
        priority=EventPriority.LOW,
        custom_filter=task_filter,
    ):
        motet.stream_event("turn", state="PREPARING")

        prep = run_pre_reasoning_hooks(
            motet=motet,
            turn_hooks=turn_hooks,
            history=history,
            pending=pending,
            effective_context=effective_context,
            agent_config=agent_config,
            input_text=input_text,
            resolved_tools=resolved_tools,
            schema_exporter=schema_exporter,
            protected_system_prefix=protected_system_prefix,
            conversation_analysis=conversation_analysis,
            ConversationAnalysisData=ConversationAnalysisData,
            memory_reset=memory_reset,
            prepare_context=prepare_context,
            extract_analysis_metadata=_extract_analysis_metadata,
            build_pending_action_system_message=build_pending_action_system_message,
            model_provider=provider,
            model_name=model_name,
        )
        analysis_metadata = prep.analysis_metadata
        prepared_context_info = prep.prepared_context_info
        turn_skill_ref_payload = prep.turn_skill_ref_payload
        skill_catalog_ref_objs = prep.skill_catalog_ref_objs
        skill_catalog_messages = prep.skill_catalog_messages
        explicit_skill_messages = prep.explicit_skill_messages
        resolved_tools = prep.resolved_tools

        motet.stream_event("turn", state="THINKING")

        metadata = dict(getattr(agent_config, "metadata", None) or {})
        metadata.setdefault("configured_agent_id", getattr(agent_config, "agent_id", "agent"))
        metadata.setdefault("configured_agent_qualified_id", qualified_id)
        if turn_skill_ref_payload:
            metadata["skill_refs"] = turn_skill_ref_payload
        if skill_catalog_ref_objs:
            metadata["skill_catalog_refs"] = [
                r.model_dump(mode="json", exclude_none=True) for r in skill_catalog_ref_objs
            ]

        stored_filter = (
            spawn_contract.get("tool_filter_metadata")
            if isinstance(spawn_contract, dict)
            else None
        )
        if isinstance(stored_filter, dict) and stored_filter:
            discovery_filter_metadata = dict(stored_filter)
        else:
            discovery_filter_metadata = get_discovery_filter_metadata(
                getattr(agent_config, "tool_filter", None),
            ) or {}

        def _turn_has_attachments(messages: List[Message]) -> bool:
            for msg in reversed(messages):
                if getattr(msg, "role", None) == "user":
                    return bool(getattr(msg, "attachments", None))
            return False

        if _turn_has_attachments(history):
            for tool_name in (
                "core.artifact_read",
                "core.search_artifacts",
                "core.artifact_view",
            ):
                discovery_filter_metadata = ensure_required_tool(discovery_filter_metadata, tool_name)

        # ADR-0121: on a fresh confirm, pin the prior turn's tool shortlist so
        # tool discovery does not run semantic search over the literal text
        # "ok". Routing-only for heuristic markers — the loop still plans the
        # call; no prefilled execution (that requires Phase 2 agent-declared
        # markers with complete staged parameters).
        pending_pinned_tools: List[str] = []
        if (
            pending.marker is not None
            and pending.status == "fresh"
            and pending.reply == "confirm"
        ):
            shortlist = pending.marker.get("tool_shortlist")
            if isinstance(shortlist, list):
                pending_pinned_tools = [str(t) for t in shortlist if t]
            for tool_name in pending_pinned_tools:
                discovery_filter_metadata = ensure_required_tool(discovery_filter_metadata, tool_name)
                resolved_tools = ensure_tool_schema(resolved_tools, tool_name, schema_exporter)

        if skill_catalog_messages:
            discovery_filter_metadata = ensure_required_tool(
                discovery_filter_metadata,
                "core.activate_skill",
            )
        if explicit_skill_messages:
            discovery_filter_metadata = ensure_required_tool(
                discovery_filter_metadata,
                "core.workspace_shell_exec",
            )

        from motet.core.tools.builtin.handoff import MAX_HANDOFF_DEPTH

        raw_handoff_depth = effective_context.get("handoff_depth")
        if raw_handoff_depth is None:
            raw_handoff_depth = metadata.get("handoff_depth")
        try:
            handoff_depth = int(raw_handoff_depth) if raw_handoff_depth is not None else 0
        except (TypeError, ValueError):
            handoff_depth = 0
        handoff_path = [
            str(item).strip()
            for item in (
                effective_context.get("handoff_path")
                or metadata.get("handoff_path")
                or []
            )
            if str(item).strip()
        ]
        offered_handoffs = [
            str(item).strip()
            for item in (getattr(agent_config, "handoffs", None) or [])
            if str(item).strip()
            and str(item).strip() not in handoff_path
            and str(item).strip() != qualified_id
        ]
        metadata["handoff_depth"] = handoff_depth
        metadata["handoff_path"] = handoff_path
        if offered_handoffs and handoff_depth < MAX_HANDOFF_DEPTH:
            resolved_tools = ensure_tool_schema(
                resolved_tools, "core.handoff", schema_exporter
            )
            discovery_filter_metadata = ensure_required_tool(
                discovery_filter_metadata, "core.handoff"
            )
            metadata["handoffs"] = offered_handoffs

        # Always forward discovery filter metadata so agent YAML
        # required_tools / prefix / exclude survive even when no skill/pending
        # pins are present (Cursor facade turns).
        if discovery_filter_metadata:
            metadata["tool_filter_metadata"] = discovery_filter_metadata

        from motet.core.orchestration.turn.output_contract import (
            apply_output_contract,
            resolve_output_contract,
        )

        output_contract = resolve_output_contract(
            data, effective_context, agent_config
        )
        no_tools_constrained = False

        # Issue #188: Continue after budget — shared checkpoint + fresh budget.
        # Prefer rehydrate when a budget_continue snapshot exists; otherwise
        # steer the normal agent path with the same continuation prompt.
        budget_continue_handled = False
        if effective_context.get("continue_after_budget"):
            from motet.core.orchestration.turn.budget_continue import (
                CONTINUE_AFTER_BUDGET_USER_MESSAGE,
                inject_budget_continue_steering,
            )

            continued = continue_after_budget(
                motet,
                history=history,
                stream_key=motet.stream_key,
                max_iterations=int(getattr(agent_config, "max_iterations", 20) or 20),
                max_model_calls=getattr(agent_config, "max_model_calls", None),
                input_text=input_text or CONTINUE_AFTER_BUDGET_USER_MESSAGE,
                model_provider=provider or None,
                model_name=model_name or None,
                model_profile_name=model_profile_name,
                enable_thinking=enable_thinking,
                reasoning_effort=reasoning_effort,
                tool_filter_metadata=discovery_filter_metadata,
            )
            if continued is not None:
                if continued.conversation_history:
                    history = list(continued.conversation_history)
                suspended_response = _maybe_suspended_turn_response(continued)
                if suspended_response is not None:
                    return suspended_response
                motet.stream_event("turn", state="RESPONDING")
                turn_result = continued.payload
                final_response = extract_response_text(turn_result)
                budget_continue_handled = True
            else:
                history = inject_budget_continue_steering(history)

        if not budget_continue_handled:
            # Forced mode + turn_gate. no_tools streams one reply; everything
            # else is run_agent on this worker. Resolved locally — no command
            # hop, and nothing predicts a strategy.
            decision = resolve_turn_mode(
                context=effective_context,
                message=last_user_message(history),
                pending_action=pending.routing_hint,
            )
            if decision.mode == "no_tools":
                turn_result = answer_without_tools(
                    motet,
                    messages=history,
                    reason=decision.no_tools_reason,
                    provider=provider or DEFAULT_MODEL_PROVIDER,
                    model_name=model_name or DEFAULT_MODEL_NAME,
                    output_contract=output_contract,
                )
                no_tools_constrained = output_contract is not None
            else:
                turn_result = run_agent(
                    motet,
                    _agent_data_for_turn(
                        query=input_text or "",
                        history=history,
                        qualified_id=qualified_id,
                        agent_config=agent_config,
                        provider=provider,
                        model_name=model_name,
                        model_profile_name=model_profile_name,
                        enable_thinking=enable_thinking,
                        reasoning_effort=reasoning_effort,
                        resolved_tools=resolved_tools,
                        metadata=metadata,
                        effective_context=effective_context,
                    ),
                )
            suspended_response = _maybe_suspended_turn_response(turn_result)
            if suspended_response is not None:
                return suspended_response
            motet.stream_event("turn", state="RESPONDING")
            final_response = extract_response_text(turn_result)

        if output_contract is not None:
            final_response, turn_result = apply_output_contract(
                motet,
                history=history,
                turn_result=turn_result if isinstance(turn_result, dict) else {},
                contract=output_contract,
                provider=provider or DEFAULT_MODEL_PROVIDER,
                model_name=model_name or DEFAULT_MODEL_NAME,
                final_response=final_response,
                already_constrained=no_tools_constrained,
            )

        # COMPLETING lifecycle + finalize hook.
        motet.stream_event("turn", state="COMPLETING")
        run_finalize_hook(
            motet=motet,
            turn_hooks=turn_hooks,
            finalize_turn=finalize_turn,
            history=history,
            final_response=final_response,
            qualified_id=qualified_id,
            finalize_root_turn=finalize_root_turn,
            finalize_root_agent_id=finalize_root_agent_id,
            reserve_sequence=reserve_sequence,
            pending_action_carry=pending.carry,
            thinking_text=extract_thinking_text(turn_result),
            tool_summaries=extract_tool_summaries(turn_result),
            cost_usd=extract_turn_cost(turn_result),
            spawn_children=extract_spawn_children(turn_result),
        )
        # Optional fail-soft export hooks (e.g. Langfuse generation push).
        run_after_finalize_hooks(
            motet=motet,
            turn_hooks=turn_hooks,
            history=history,
            final_response=final_response,
            qualified_id=qualified_id,
            usage=extract_turn_usage(turn_result),
            cost_usd=extract_turn_cost(turn_result),
            model=resolve_turn_model(
                turn_result, provider=provider, model_name=model_name
            ),
            context=effective_context,
        )

    return complete_agent_turn(
        motet,
        turn_result,
        final_response,
        qualified_id,
        parent_command_id,
        prepared_context_info,
        analysis_metadata,
    )



__all__ = [
    "agent_turn",
    "_agent_data_for_turn",
    # Turn-lifecycle helpers (re-exported from orchestration.py for compatibility)
    "_build_child_metadata",
    "_coerce_reasoning_effort",
    "_collect_generated_media",
    "_inherit_parent_context",
    "_iter_tool_result_dicts",
    "_leading_system_insert_at",
    "_media_type_for_content_type",
    "_resolve_transcript_primary",
    "_suspended_turn_response",
    "_validate_and_enrich_media",
]

# Decorator-based commands auto-register themselves via @motet.command
