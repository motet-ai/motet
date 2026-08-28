"""
Motet - Agent Turn Hook Helpers

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-26

Description:
    Turn-hook helpers for `agent_turn`. Owns pre-reasoning hooks
    (conversation analysis, pending-action context, context_inject, skills
    catalog/activation, memory_reset, context_prepare), post-reasoning
    finalize / history-persist helpers, and fail-soft ``after_finalize``
    export hooks. Every slot resolves through the command registry. Forced
    mode and the turn gate live in ``turn/gate.py``.

Dependencies:
    - motet.core.types.Message: Canonical message model
    - motet.core.agents.prompt_policy: Protected system-prefix restore after prepare
    - motet.core.orchestration.turn.prepare: Message/tool-schema helpers
    - motet.core.orchestration.turn.hook_resolve: registry lookup and payload instantiate
    - motet.core.orchestration.turn.hook_models: declared hook payloads
    - motet.core.skills.assembly: Progressive skill catalog / activation
    - Phase commands passed in by the caller so patches on turn.phases still
      apply for Motet defaults

Usage:
    from motet.core.orchestration.turn.hooks import (
        run_pre_reasoning_hooks,
        resolve_analysis_routing,
        run_finalize_hook,
        persist_history_only,
    )

    prep = run_pre_reasoning_hooks(...)
    run_finalize_hook(...)

Notes:
    - Mutates ``history`` and ``effective_context`` in place. Returns updated
      ``resolved_tools`` when skill pins append schemas.
    - An unregistered hook name warns and skips except finalize, which falls
      back to core.finalize_turn.
    - Mode resolution lives in ``turn/gate.py`` (``resolve_turn_mode``).
    - Conversation analysis inherits the turn's provider/model unless
      MOTET_ANALYSIS_MODEL is set (optional MOTET_ANALYSIS_PROVIDER).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import structlog

from motet.core.agents.prompt_policy import ensure_protected_system_prefix
from motet.core.commands.command_data_classes import (
    FinalizeTurnData,
    MemoryResetData,
    PrepareContextData,
)
from motet.core.orchestration.turn.hook_models import (
    ConversationAnalysisResult,
    TurnAfterFinalizeData,
    TurnContextHookData,
    TurnContextHookResult,
    analysis_as_dict,
    parse_analysis_result,
)
from motet.core.orchestration.turn.hook_resolve import (
    HookPayloadError,
    MOTE_DEFAULT_HOOK_COMMANDS,
    instantiate_hook_data,
    resolve_hook_implementation,
)
from motet.core.orchestration.turn.prepare import (
    ensure_tool_schema,
    to_turn_messages,
)
from motet.core.types import Message

logger = structlog.get_logger(__name__)


def _leading_system_insert_at(history: Sequence[Message]) -> int:
    """Index after consecutive leading ``role=system`` messages (0 if none)."""
    insert_at = 0
    for msg in history or []:
        if getattr(msg, "role", None) != "system":
            break
        insert_at += 1
    return insert_at


@dataclass
class PreReasoningHooksResult:
    """Outputs from pre-reasoning turn hooks (history/context mutated in place)."""

    analysis_metadata: Dict[str, Any] = field(default_factory=dict)
    prepared_context_info: Dict[str, Any] = field(default_factory=dict)
    turn_skill_ref_payload: List[Dict[str, Any]] = field(default_factory=list)
    skill_catalog_ref_objs: List[Any] = field(default_factory=list)
    skill_catalog_messages: List[Message] = field(default_factory=list)
    explicit_skill_messages: List[Message] = field(default_factory=list)
    # May be a new list when skill pins append schemas (same as prior rebinding).
    resolved_tools: Optional[List[Any]] = None


def _blank_to_none(value: Optional[str]) -> Optional[str]:
    """Treat empty / whitespace-only strings as unset."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def resolve_analysis_routing(
    *,
    turn_provider: Optional[str] = None,
    turn_model: Optional[str] = None,
    analysis_provider: Optional[str] = None,
    analysis_model: Optional[str] = None,
    config: Any = None,
) -> tuple[Optional[str], Optional[str]]:
    """Pick provider/model for conversation analysis.

    Precedence: explicit command pins, then an operator cheap pin
    (``MOTET_ANALYSIS_MODEL``, optional ``MOTET_ANALYSIS_PROVIDER``),
    then the turn's model. A model pin without a provider keeps the
    turn's provider so a cheaper sibling stays on the same vendor.
    ``Config.analysis_model`` defaults to ``None`` so the old unused
    ``gpt-4o-mini`` default cannot silently split vendors.
    """
    pinned_model = _blank_to_none(analysis_model)
    if pinned_model:
        return (
            _blank_to_none(analysis_provider) or _blank_to_none(turn_provider),
            pinned_model,
        )

    if config is None:
        from motet.core.config import Config

        config = Config()
    cfg_model = _blank_to_none(getattr(config, "analysis_model", None))
    if cfg_model:
        cfg_provider = _blank_to_none(getattr(config, "analysis_provider", None))
        return (cfg_provider or _blank_to_none(turn_provider), cfg_model)

    return (_blank_to_none(turn_provider), _blank_to_none(turn_model))


def _slot_implementation(
    name: Optional[str],
    *,
    slot: str,
    injected: Any = None,
) -> Optional[Any]:
    """Registry lookup, preferring the caller-injected Motet default."""
    impl = resolve_hook_implementation(name, slot=slot)
    if injected is not None and name in MOTE_DEFAULT_HOOK_COMMANDS:
        return injected
    return impl


def _apply_context_inject_result(
    hook_name: str,
    hook_result: Any,
    history: List[Message],
    effective_context: Dict[str, Any],
) -> None:
    """Merge an additive context_inject result into history and context."""
    if hook_result is None:
        return
    if not isinstance(hook_result, dict):
        try:
            parsed = TurnContextHookResult.model_validate(hook_result)
            hook_result = parsed.model_dump()
        except Exception:
            logger.warning(
                "agent_turn_context_inject_hook_invalid_result",
                hook=hook_name,
                result_type=type(hook_result).__name__,
            )
            return
    hook_system_messages = hook_result.get("system_messages")
    if not hook_system_messages and hook_result.get("system_prompt"):
        hook_system_messages = [hook_result.get("system_prompt")]

    if isinstance(hook_system_messages, list):
        insert_at = _leading_system_insert_at(history)
        for msg in hook_system_messages:
            if isinstance(msg, dict):
                content = str(msg.get("content", "")).strip()
            else:
                content = str(msg).strip()
            if not content:
                continue
            history.insert(
                insert_at,
                Message(
                    role="system",
                    content=content,
                    metadata={"source": hook_name, "cache_volatile": True},
                ),
            )
            insert_at += 1

    hook_context = hook_result.get("context_patch") or hook_result.get("context")
    if isinstance(hook_context, dict):
        for key, value in hook_context.items():
            effective_context.setdefault(key, value)


def run_pre_reasoning_hooks(
    *,
    motet: Any,
    turn_hooks: Any,
    history: List[Message],
    pending: Any,
    effective_context: Dict[str, Any],
    agent_config: Any,
    input_text: Optional[str],
    resolved_tools: Optional[List[Any]],
    schema_exporter: Any,
    protected_system_prefix: Sequence[Message],
    conversation_analysis: Any,
    ConversationAnalysisData: Any,
    memory_reset: Any,
    prepare_context: Any,
    extract_analysis_metadata: Any,
    build_pending_action_system_message: Any,
    model_provider: Optional[str] = None,
    model_name: Optional[str] = None,
) -> PreReasoningHooksResult:
    """
    Run pre-reasoning turn hooks and related context injection.

    Order: conversation_analysis → pending-action system message →
    context_inject → skills → memory_reset → context_prepare → skill
    system-message insert.

    Mutates ``history`` and ``effective_context`` in place. Returns an updated
    ``resolved_tools`` list when skill pins append schemas (may be a new list).
    """
    analysis: Optional[ConversationAnalysisResult] = None
    analysis_metadata: Dict[str, Any] = {}
    prepared_context_info: Dict[str, Any] = {}
    turn_skill_ref_payload: List[Dict[str, Any]] = []
    skill_catalog_messages: List[Message] = []
    skill_catalog_ref_objs: List[Any] = []
    explicit_skill_messages: List[Message] = []
    explicit_skill_ref_objs: List[Any] = []

    # Hook: conversation_analysis
    conv_hook = getattr(turn_hooks, "conversation_analysis", None) if turn_hooks else None
    conv_impl = _slot_implementation(
        conv_hook, slot="conversation_analysis", injected=conversation_analysis
    )
    if conv_impl is not None:
        analysis_provider, analysis_model = resolve_analysis_routing(
            turn_provider=model_provider,
            turn_model=model_name,
        )
        analysis_kwargs: Dict[str, Any] = {
            "messages": history,
            "pending_action": pending.routing_hint,
        }
        if analysis_model:
            analysis_kwargs["analysis_model"] = analysis_model
        if analysis_provider:
            analysis_kwargs["analysis_provider"] = analysis_provider
        try:
            if conv_hook in MOTE_DEFAULT_HOOK_COMMANDS:
                hook_data = ConversationAnalysisData(**analysis_kwargs)
            else:
                hook_data = instantiate_hook_data(
                    str(conv_hook),
                    ConversationAnalysisData(**analysis_kwargs),
                )
        except HookPayloadError as exc:
            logger.error(
                "agent_turn_conversation_analysis_payload_mismatch",
                hook=conv_hook,
                error=str(exc),
            )
            hook_data = None
        except Exception as exc:
            if conv_hook in MOTE_DEFAULT_HOOK_COMMANDS:
                hook_data = ConversationAnalysisData(**analysis_kwargs)
            else:
                logger.error(
                    "agent_turn_conversation_analysis_payload_mismatch",
                    hook=conv_hook,
                    error=str(exc),
                )
                hook_data = None
        if hook_data is not None:
            analysis_data, analysis_error = motet.maybe(conv_impl, data=hook_data)
            if analysis_data:
                analysis = parse_analysis_result(analysis_data)
                if extract_analysis_metadata is not None:
                    scraped = extract_analysis_metadata(analysis_data)
                    analysis_metadata = scraped if isinstance(scraped, dict) else analysis_as_dict(analysis)
                else:
                    analysis_metadata = analysis_as_dict(analysis)
                if analysis is not None:
                    extras = {
                        key: value
                        for key, value in analysis_metadata.items()
                        if key not in analysis.model_fields
                    }
                    if extras:
                        analysis = analysis.model_copy(update=extras)
                dump_payload = (
                    analysis_data
                    if isinstance(analysis_data, dict)
                    else analysis_as_dict(analysis)
                )
                motet.stream_event("conversation_analyzed", data=json.dumps(dump_payload, default=str))
            elif analysis_error:
                logger.warning("agent_turn_conversation_analysis_failed", error=analysis_error)

    # ADR-0121: make the pending action visible to the loop. The marker
    # never enters model input via metadata (renderers use content/parts),
    # so an explicit system message carries the proposal — with a
    # re-confirm instruction when stale. Injected after analysis so the
    # extra system message does not shift routing thresholds.
    if pending.marker is not None and pending.status is not None:
        pending_context = build_pending_action_system_message(
            pending.marker, pending.status, pending.reply or "other"
        )
        insert_at = _leading_system_insert_at(history)
        history.insert(
            insert_at,
            Message(
                role="system",
                content=pending_context,
                # cache_volatile: per-turn content — provider adapters keep it out
                # of the cached stable system prefix (ADR-0124).
                metadata={"source": "pending_action", "cache_volatile": True},
            ),
        )

    # Hook: context_inject (additive; declared TurnContextHookData)
    context_hooks = getattr(turn_hooks, "context_inject", None) if turn_hooks else None
    if context_hooks:
        declared_payload = TurnContextHookData(
            messages=list(history),
            context=dict(effective_context),
            analysis=analysis,
        )
        for hook_name in context_hooks:
            impl = _slot_implementation(hook_name, slot="context_inject")
            if impl is None:
                continue
            try:
                hook_data = instantiate_hook_data(str(hook_name), declared_payload)
            except HookPayloadError as exc:
                logger.error(
                    "agent_turn_context_inject_payload_mismatch",
                    hook=hook_name,
                    error=str(exc),
                )
                continue
            hook_result, hook_error = motet.maybe(impl, data=hook_data)
            if hook_error:
                logger.warning(
                    "agent_turn_context_inject_hook_failed",
                    hook=hook_name,
                    error=hook_error,
                )
                continue
            try:
                _apply_context_inject_result(
                    hook_name, hook_result, history, effective_context
                )
            except Exception as e:
                logger.warning(
                    "agent_turn_context_inject_hook_exception",
                    hook=hook_name,
                    error=str(e),
                )

    # ADR-0073 progressive disclosure: disclose catalog, activate full bodies only on explicit user request.
    skill_allowlist = getattr(agent_config, "skill_ids", None) or []
    skill_mode = str(getattr(agent_config, "skill_mode", "allowlist") or "allowlist").strip().lower()
    skill_discovery = skill_mode == "discovery"
    skill_max_per_turn = int(getattr(agent_config, "skill_max_per_turn", 3) or 3)
    if skill_allowlist or skill_discovery:
        from pathlib import Path

        from motet.core.skills.assembly import (
            activate_explicit_skills_for_turn,
            build_skill_catalog_for_turn,
        )
        from motet.core.skills.filesystem import refresh_filesystem_skills

        project_root_hint = (
            effective_context.get("project_root")
            or effective_context.get("workspace_path")
            or effective_context.get("cwd")
        )
        try:
            refresh_filesystem_skills(
                project_root=Path(project_root_hint).expanduser() if isinstance(project_root_hint, str) and project_root_hint else None
            )
        except Exception as e:
            logger.warning("agent_turn_filesystem_skill_refresh_failed", error=str(e))

        skill_catalog_messages, skill_catalog_ref_objs, _skill_candidates = build_skill_catalog_for_turn(
            skill_allowlist,
            discovery_mode=skill_discovery,
            bundle_version_by_id=None,
        )
        explicit_skill_messages, explicit_skill_ref_objs = activate_explicit_skills_for_turn(
            input_text or "",
            skill_allowlist,
            discovery_mode=skill_discovery,
            max_skills=skill_max_per_turn,
            bundle_version_by_id=None,
        )
        if skill_catalog_messages:
            resolved_tools = ensure_tool_schema(
                resolved_tools, "core.activate_skill", schema_exporter
            )
        if explicit_skill_messages:
            resolved_tools = ensure_tool_schema(
                resolved_tools, "core.workspace_shell_exec", schema_exporter
            )
        turn_skill_ref_payload = [
            r.model_dump(mode="json", exclude_none=True) for r in explicit_skill_ref_objs
        ]

    # Hook: memory_reset
    reset_hook = getattr(turn_hooks, "memory_reset", None) if turn_hooks else None
    reset_impl = _slot_implementation(
        reset_hook, slot="memory_reset", injected=memory_reset
    )
    if reset_impl is not None:
        reset_payload = MemoryResetData(
            reset_working_memory=True,
            reset_conversation_memory=False,
        )
        try:
            reset_data = (
                reset_payload
                if reset_hook in MOTE_DEFAULT_HOOK_COMMANDS
                else instantiate_hook_data(str(reset_hook), reset_payload)
            )
        except HookPayloadError as exc:
            logger.error(
                "agent_turn_memory_reset_payload_mismatch",
                hook=reset_hook,
                error=str(exc),
            )
            reset_data = None
        if reset_data is not None:
            motet.dispatch([(reset_impl, reset_data)])

    # Hook: context_prepare
    prep_hook = getattr(turn_hooks, "context_prepare", None) if turn_hooks else None
    prep_impl = _slot_implementation(
        prep_hook, slot="context_prepare", injected=prepare_context
    )
    if prep_impl is not None:
        prepare_payload = PrepareContextData(
            messages=history,
            context=effective_context,
            analysis_metadata=analysis if analysis is not None else analysis_metadata or None,
        )
        try:
            prepare_data = (
                prepare_payload
                if prep_hook in MOTE_DEFAULT_HOOK_COMMANDS
                else instantiate_hook_data(str(prep_hook), prepare_payload)
            )
        except HookPayloadError as exc:
            logger.error(
                "agent_turn_context_prepare_payload_mismatch",
                hook=prep_hook,
                error=str(exc),
            )
            prepare_data = None
        if prepare_data is not None:
            prepared_data, prepared_error = motet.maybe(prep_impl, data=prepare_data)
            if prepared_data and isinstance(prepared_data.get("prepared_messages"), list):
                raw_context_info = prepared_data.get("context_info")
                if isinstance(raw_context_info, dict):
                    prepared_context_info = raw_context_info
                next_history = to_turn_messages(prepared_data.get("prepared_messages", []))
                if next_history:
                    history[:] = ensure_protected_system_prefix(
                        next_history, protected_system_prefix
                    )
            elif prepared_error:
                logger.warning("agent_turn_context_prepare_failed", error=prepared_error)

    skill_context_messages = skill_catalog_messages + explicit_skill_messages
    if skill_context_messages:
        insert_at = _leading_system_insert_at(history)
        for sm in skill_context_messages:
            history.insert(insert_at, sm)
            insert_at += 1

    return PreReasoningHooksResult(
        analysis_metadata=analysis_metadata,
        prepared_context_info=prepared_context_info,
        turn_skill_ref_payload=turn_skill_ref_payload,
        skill_catalog_ref_objs=skill_catalog_ref_objs,
        skill_catalog_messages=skill_catalog_messages,
        explicit_skill_messages=explicit_skill_messages,
        resolved_tools=resolved_tools,
    )


def persist_history_only(
    *,
    motet: Any,
    turn_hooks: Any,
    finalize_turn: Any,
    history: List[Message],
    assistant_response: str,
    qualified_id: str,
    finalize_root_turn: bool,
    finalize_root_agent_id: str,
    reserve_sequence: int,
    pending_action_carry: Any,
) -> None:
    """Store the exchange for an outcome that will never reach finalize.

    Same transcript identity as the completed path, but no memory update:
    the turn did not produce an answer worth learning from.
    """
    fin_hook = getattr(turn_hooks, "finalize", None) if turn_hooks else None
    fin_impl = _slot_implementation(fin_hook, slot="finalize", injected=finalize_turn)
    if fin_impl is None:
        return
    persist_payload = FinalizeTurnData(
        messages=history,
        assistant_response=assistant_response,
        agent_id=qualified_id,
        store_conversation=True,
        update_memory=False,
        root_turn=finalize_root_turn,
        root_agent_id=finalize_root_agent_id,
        transcript_sequence=reserve_sequence,
        pending_action_carry=pending_action_carry,
    )
    try:
        persist_data = (
            persist_payload
            if fin_hook in MOTE_DEFAULT_HOOK_COMMANDS or not fin_hook
            else instantiate_hook_data(str(fin_hook), persist_payload)
        )
    except HookPayloadError as exc:
        logger.error(
            "agent_turn_history_persist_payload_mismatch",
            hook=fin_hook,
            error=str(exc),
        )
        persist_data = persist_payload
        fin_impl = finalize_turn
    _, persist_error = motet.maybe(fin_impl, data=persist_data)
    if persist_error:
        logger.warning(
            "agent_turn_history_persist_failed",
            agent_id=qualified_id,
            error=persist_error,
        )


def run_finalize_hook(
    *,
    motet: Any,
    turn_hooks: Any,
    finalize_turn: Any,
    history: List[Message],
    final_response: str,
    qualified_id: str,
    finalize_root_turn: bool,
    finalize_root_agent_id: str,
    reserve_sequence: int,
    pending_action_carry: Any,
) -> None:
    """COMPLETING-phase finalize hook (store conversation + update memory)."""
    fin_hook = getattr(turn_hooks, "finalize", None) if turn_hooks else None
    fin_impl = _slot_implementation(fin_hook, slot="finalize", injected=finalize_turn)
    if fin_impl is None:
        return
    finalize_payload = FinalizeTurnData(
        messages=history,
        assistant_response=final_response,
        agent_id=qualified_id,
        store_conversation=True,
        update_memory=True,
        root_turn=finalize_root_turn,
        root_agent_id=finalize_root_agent_id,
        transcript_sequence=reserve_sequence,
        pending_action_carry=pending_action_carry,
    )
    try:
        finalize_data = (
            finalize_payload
            if fin_hook in MOTE_DEFAULT_HOOK_COMMANDS or not fin_hook
            else instantiate_hook_data(str(fin_hook), finalize_payload)
        )
    except HookPayloadError as exc:
        logger.error(
            "agent_turn_finalize_payload_mismatch",
            hook=fin_hook,
            error=str(exc),
        )
        finalize_data = finalize_payload
        fin_impl = finalize_turn
    fin_data, fin_error = motet.maybe(fin_impl, data=finalize_data)
    if fin_error:
        logger.warning("agent_turn_finalize_failed", error=fin_error)
    else:
        logger.debug("agent_turn_finalize_complete", result=fin_data)


def _message_to_dict(msg: Any) -> Dict[str, Any]:
    """Best-effort Message / dict → plain dict for after_finalize payloads."""
    if isinstance(msg, dict):
        return {
            "role": msg.get("role"),
            "content": msg.get("content"),
        }
    role = getattr(msg, "role", None)
    content = getattr(msg, "content", None)
    return {"role": role, "content": content}


def run_after_finalize_hooks(
    *,
    motet: Any,
    turn_hooks: Any,
    history: Sequence[Message],
    final_response: str,
    qualified_id: str,
    usage: Optional[Dict[str, Any]] = None,
    cost_usd: Optional[float] = None,
    model: Optional[str] = None,
    context: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Fail-soft post-finalize hooks (``turn_hooks.after_finalize``).

    Runs after ``finalize`` on completed turns. Intended for optional
    export/observability (bundle commands). Failures never abort the turn.
    A payload mismatch is a configuration bug and is logged as an error.
    """
    hooks = getattr(turn_hooks, "after_finalize", None) if turn_hooks else None
    if not hooks:
        return

    message_dicts = [_message_to_dict(m) for m in (history or [])]
    declared_payload = TurnAfterFinalizeData(
        messages=message_dicts,
        assistant_response=final_response,
        agent_id=qualified_id,
        usage=usage,
        cost_usd=cost_usd,
        model=model,
        context=dict(context or {}),
    )
    for hook_name in hooks:
        impl = _slot_implementation(hook_name, slot="after_finalize")
        if impl is None:
            continue
        try:
            hook_data = instantiate_hook_data(str(hook_name), declared_payload)
        except HookPayloadError as exc:
            logger.error(
                "agent_turn_after_finalize_payload_mismatch",
                hook=hook_name,
                error=str(exc),
            )
            continue
        _result, hook_error = motet.maybe(impl, data=hook_data)
        if hook_error:
            logger.warning(
                "agent_turn_after_finalize_hook_failed",
                hook=hook_name,
                error=hook_error,
            )
        else:
            logger.debug("agent_turn_after_finalize_hook_complete", hook=hook_name)


__all__ = [
    "PreReasoningHooksResult",
    "_leading_system_insert_at",
    "persist_history_only",
    "resolve_analysis_routing",
    "run_after_finalize_hooks",
    "run_finalize_hook",
    "run_pre_reasoning_hooks",
]
