"""
Motet - Turn Runtime Persist

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Checkpoint-write half of Turn Runtime. The agentic loop returns
    intents; this module writes TurnCheckpoint Redis records, owns the loop-head
    cancel check, and resolves facade resume handles without exposing checkpoint
    models to openai_compat. Hosted_tools mixed-turn handbacks go through
    ``materialize_intent`` like agent mode.

Dependencies:
    - motet.core.checkpoints: TurnCheckpoint store and ownership classifier
    - loop_results.build_loop_result: terminal loop dict shape
    - LoopStateSnapshot: codec between AgenticLoopData and checkpoint fields
    - task_control.is_cancelled: loop-head cancel

Usage:
    from motet.core.orchestration.turn.runtime import (
        materialize_intent, resolve_resume, raise_if_turn_cancelled,
    )
    from motet.core.reasoning.react.loop_intents import is_turn_intent

    if is_turn_intent(result):
        result = materialize_intent(motet, loop_data, result)

Notes:
    - store_turn_checkpoint may only be called from this module (allowlist test).
    - Import via the runtime package, not this file.
    - Nested sub-agents (``AgenticLoopData.parent_agent_id`` set) skip the
      budget Continue checkpoint. They are not user turns, and writing one
      blocked the child command after the loop had already stopped.
    - A rail-stop finalize write-up is carried as ``finalized`` on the
      budget-stop intent so spawn_agents can treat findings as an answer
      while ``stop_reason`` stays the rail (parent Continue still applies).
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence
from uuid import uuid4

import structlog

from motet.core.orchestration.turn.runtime.result import ResumeHandle
from motet.core.reasoning.react.loop_intents import (
    INTENT_BUDGET_STOP,
    INTENT_HANDBACK,
    INTENT_NESTED_WORKFLOW,
    TURN_INTENT_KEY,
)

logger = structlog.get_logger(__name__)


def _as_scope_id(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _cancel_scopes_for_turn(motet: Any) -> list:
    raw = getattr(motet, "cancel_scopes", None)
    scopes: Optional[Sequence[str]]
    if isinstance(raw, (list, tuple)):
        scopes = raw
    else:
        scopes = None
    from motet.core.distributed.task_control import append_cancel_scope

    return append_cancel_scope(scopes, _as_scope_id(getattr(motet, "task_id", None)))


def _command_type(motet: Any) -> str:
    explicit = _as_scope_id(getattr(motet, "command_type", None))
    if explicit:
        return explicit
    cmd = getattr(motet, "_command", None)
    getter = getattr(cmd, "get_command_type", None) if cmd is not None else None
    if callable(getter):
        try:
            ct = getter()
        except Exception:
            ct = None
        if isinstance(ct, str) and ct.strip():
            return ct.strip()
    return "core.agent_loop"


def raise_if_turn_cancelled(motet: Any) -> None:
    """Stop before the next iteration if any inherited cancel scope is sticky."""
    from motet.core.commands.response_models import CommandExecutionError
    from motet.core.distributed.task_control import TASK_CANCELLED_CODE, is_cancelled

    scopes = _cancel_scopes_for_turn(motet)
    if not scopes or not is_cancelled(scopes):
        return

    task_id = _as_scope_id(getattr(motet, "task_id", None))
    command_type = _command_type(motet)
    command_id = _as_scope_id(getattr(motet, "command_id", None))
    logger.info(
        "agentic_loop_cancelled_before_iteration",
        task_id=task_id or None,
        command_id=command_id or None,
    )
    raise CommandExecutionError(
        error_type="TaskCancelled",
        message="Task cancelled",
        details={"code": TASK_CANCELLED_CODE, "task_id": task_id},
        recoverable=False,
        command_type=command_type,
        command_id=command_id,
    )


def _metadata_agent_id(motet: Any) -> Optional[str]:
    try:
        command = getattr(motet, "_command", None)
        metadata = getattr(getattr(command, "distributed_context", None), "metadata", None) or {}
        agent_id = metadata.get("agent_id")
        return str(agent_id) if agent_id else None
    except Exception:
        return None


def _owning_agent_id(motet: Any, data: Any) -> Optional[str]:
    if not bool(getattr(data, "inject_meta_tools", True)):
        return None
    carried = (getattr(data, "agent_id", None) or "").strip() or None
    return carried or _metadata_agent_id(motet)


def _is_nested_subagent(data: Any) -> bool:
    """True when this loop was started by another agent (``parent_agent_id``).

    Nested runs are not user turns. A Continue checkpoint on the parent's
    conversation would steal the conversation-kind index, and the Redis
    write sat on the child command after the loop had already stopped —
    join then waited on a result that never arrived.
    """
    parent = getattr(data, "parent_agent_id", None)
    return bool(str(parent).strip()) if parent is not None else False


def _handback_tool_names(data: Any) -> set:
    from motet.core.types import tool_schema_name

    names = set(getattr(data, "handback_tool_names", None) or [])
    for schema in getattr(data, "handback_tools", None) or []:
        name = tool_schema_name(schema)
        if name:
            names.add(name)
    return names


def materialize_intent(motet: Any, data: Any, intent: Dict[str, Any]) -> Dict[str, Any]:
    """Write the checkpoint for a loop intent and return the terminal loop dict."""
    kind = str(intent.get(TURN_INTENT_KEY) or "")
    if kind == INTENT_HANDBACK:
        return _materialize_handback(motet, data, intent)
    if kind == INTENT_NESTED_WORKFLOW:
        return _materialize_nested_workflow(motet, data, intent)
    if kind == INTENT_BUDGET_STOP:
        return _materialize_budget_stop(motet, data, intent)
    raise ValueError(f"unknown turn intent kind: {kind!r}")


def _materialize_handback(motet: Any, data: Any, intent: Dict[str, Any]) -> Dict[str, Any]:
    from motet.core.checkpoints import (
        CheckpointKind,
        TurnCheckpoint,
        TurnOwnership,
        call_tool_names,
        classify_turn_ownership,
        split_calls_by_ownership,
        store_turn_checkpoint,
    )
    from motet.core.reasoning.react.loop_results import build_loop_result
    from motet.core.reasoning.react.loop_state_snapshot import LoopStateSnapshot

    unique_tool_calls: List[Dict[str, Any]] = list(intent.get("unique_tool_calls") or [])
    content = str(intent.get("content") or "")
    iterations_used = int(intent.get("iterations_used") or 0)
    accumulated_usage = dict(intent.get("accumulated_usage") or {})
    accumulated_media = list(intent.get("accumulated_media") or [])

    handback_names = _handback_tool_names(data)
    motet_owned, externally_owned = split_calls_by_ownership(
        unique_tool_calls, external_names=handback_names
    )
    if (
        classify_turn_ownership(
            call_tool_names(unique_tool_calls),
            external_names=handback_names,
        )
        is not TurnOwnership.HANDBACK_ALL
    ):
        raise ValueError("handback intent without HANDBACK_ALL ownership")

    handed_back = [
        {
            "tool_call_id": tc.get("tool_call_id"),
            "tool_name": tc.get("tool_name"),
            "parameters": tc.get("parameters") or {},
        }
        for tc in unique_tool_calls
    ]
    this_turn_names = [str(tc.get("tool_name") or "") for tc in unique_tool_calls]
    owning_agent_id = _owning_agent_id(motet, data)
    loop_fields = LoopStateSnapshot.from_loop_data(
        data,
        executed_signatures=list(data.executed_signatures or []),
        used_tool_names=list(set(data.used_tool_names) | set(this_turn_names)),
        usage_accumulator=dict(accumulated_usage),
        media_accumulator=list(accumulated_media),
        agent_id=owning_agent_id,
    ).to_checkpoint_loop_fields()

    checkpoint = TurnCheckpoint(
        checkpoint_kind=CheckpointKind.HANDBACK,
        motet_id=getattr(motet, "motet_id", None) or "default",
        tenant_id=getattr(motet, "tenant_id", None),
        principal_id=getattr(motet, "principal_id", None),
        task_id=getattr(motet, "task_id", None),
        conversation_id=getattr(motet, "conversation_id", None),
        handed_back_tool_calls=handed_back,
        conversation_history=[
            m.model_dump(mode="json") for m in data.conversation_history
        ],
        **loop_fields,
    )
    store_turn_checkpoint(checkpoint)

    logger.info(
        "agentic_loop_suspended",
        checkpoint_id=checkpoint.checkpoint_id,
        checkpoint_kind=CheckpointKind.HANDBACK.value,
        handed_back=[c["tool_name"] for c in handed_back],
        externally_owned=[str(tc.get("tool_name") or "") for tc in externally_owned],
        motet_owned=[str(tc.get("tool_name") or "") for tc in motet_owned],
        remaining_iterations=checkpoint.remaining_iterations,
    )
    motet.stream_event(
        "agentic_loop_suspended",
        checkpoint_id=checkpoint.checkpoint_id,
        tool_count=len(handed_back),
        stream_key=data.stream_key,
    )

    result = build_loop_result(
        content or "", [], iterations_used, "suspended", accumulated_usage,
        media=accumulated_media,
    )
    result["suspended"] = True
    result["checkpoint_id"] = checkpoint.checkpoint_id
    result["handed_back_tool_calls"] = handed_back
    return result


def _materialize_nested_workflow(motet: Any, data: Any, intent: Dict[str, Any]) -> Dict[str, Any]:
    from motet.core.checkpoints import CheckpointKind, TurnCheckpoint, store_turn_checkpoint
    from motet.core.reasoning.react.loop_results import build_loop_result
    from motet.core.reasoning.react.loop_state_snapshot import LoopStateSnapshot
    from motet.core.models.adapters.tool_call_codec import inbound_tool_call_request
    from motet.core.types import Message
    from motet.core.workflow.checkpoint import WorkflowSuspendNotConsumable

    nested: Dict[str, Any] = dict(intent.get("nested") or {})
    content = str(intent.get("content") or "")
    iterations_used = int(intent.get("iterations_used") or 0)
    accumulated_usage = dict(intent.get("accumulated_usage") or {})
    accumulated_media = list(intent.get("accumulated_media") or [])

    suspend_reason = str(nested.get("suspend_reason") or "").strip()
    pending = list(nested.get("pending_tool_calls") or [])
    if suspend_reason and suspend_reason != "handback_tools":
        raise WorkflowSuspendNotConsumable(
            f"Nested workflow suspended for '{suspend_reason}' which has no "
            f"agent-path consumer; resume via resume_workflow / "
            f"POST /api/v1/workflows/runs/{{id}}/resume "
            f"(workflow_run_id={nested.get('workflow_run_id')})"
        )
    if not pending:
        raise WorkflowSuspendNotConsumable(
            "Nested workflow suspended without handback tool_calls; "
            f"suspend_reason={suspend_reason or 'unknown'}; "
            f"resume via resume_workflow "
            f"(workflow_run_id={nested.get('workflow_run_id')})"
        )

    owning_agent_id = _owning_agent_id(motet, data)
    loop_fields = LoopStateSnapshot.from_loop_data(
        data,
        executed_signatures=list(data.executed_signatures or []),
        used_tool_names=list(data.used_tool_names or []),
        usage_accumulator=dict(accumulated_usage),
        media_accumulator=list(accumulated_media),
        agent_id=owning_agent_id,
    ).to_checkpoint_loop_fields()

    nested_resume_history = [
        m.model_dump(mode="json") for m in data.conversation_history
    ]
    synthetic_history = list(nested_resume_history)
    synthetic_history.append(
        Message(
            role="assistant",
            content=content or "",
            tool_calls_canonical=[
                inbound_tool_call_request(
                    call_id=str(c.get("tool_call_id") or ""),
                    tool_name=str(c.get("tool_name") or ""),
                    arguments_json=json.dumps(c.get("parameters") or {}),
                )
                for c in pending
                if c.get("tool_call_id")
            ],
        ).model_dump(mode="json")
    )

    handback_names = list(_handback_tool_names(data))
    for c in pending:
        name = str(c.get("tool_name") or "").strip()
        if name and name not in handback_names:
            handback_names.append(name)

    checkpoint = TurnCheckpoint(
        checkpoint_kind=CheckpointKind.HANDBACK,
        motet_id=getattr(motet, "motet_id", None) or "default",
        tenant_id=getattr(motet, "tenant_id", None),
        principal_id=getattr(motet, "principal_id", None),
        task_id=getattr(motet, "task_id", None),
        conversation_id=getattr(motet, "conversation_id", None),
        handed_back_tool_calls=pending,
        conversation_history=synthetic_history,
        nested_resume_history=nested_resume_history,
        workflow_run_id=nested.get("workflow_run_id"),
        suspend_reason=nested.get("suspend_reason"),
        nested_workflow_tool_call_id=nested.get("nested_workflow_tool_call_id"),
        nested_workflow_tool_name=nested.get("nested_workflow_tool_name"),
        handback_tool_names=handback_names or None,
        handback_tools=list(data.handback_tools or []) or None,
        **loop_fields,
    )
    store_turn_checkpoint(checkpoint)

    logger.info(
        "agentic_loop_nested_workflow_suspended",
        checkpoint_id=checkpoint.checkpoint_id,
        checkpoint_kind=CheckpointKind.HANDBACK.value,
        workflow_run_id=checkpoint.workflow_run_id,
        suspend_reason=checkpoint.suspend_reason,
        handed_back=[c.get("tool_name") for c in pending],
    )
    pending_interactions = list(nested.get("pending_interactions") or [])
    motet.stream_event(
        "agentic_loop_suspended",
        checkpoint_id=checkpoint.checkpoint_id,
        workflow_run_id=checkpoint.workflow_run_id,
        tool_count=len(pending),
        suspend_reason=checkpoint.suspend_reason,
        pending_interactions=pending_interactions,
        stream_key=data.stream_key,
    )

    result = build_loop_result(
        content or "", [], iterations_used, "suspended", accumulated_usage,
        media=accumulated_media,
    )
    result["suspended"] = True
    result["checkpoint_id"] = checkpoint.checkpoint_id
    result["handed_back_tool_calls"] = pending
    result["workflow_run_id"] = checkpoint.workflow_run_id
    result["suspend_reason"] = checkpoint.suspend_reason
    result["pending_interactions"] = pending_interactions
    return result


def _materialize_budget_stop(motet: Any, data: Any, intent: Dict[str, Any]) -> Dict[str, Any]:
    from motet.core.reasoning.react.loop_results import build_loop_result

    message = str(intent.get("message") or "")
    stop_reason = str(intent.get("stop_reason") or "max_iterations")
    iterations_used = int(intent.get("iterations_used") or 0)
    accumulated_usage = dict(intent.get("accumulated_usage") or {})
    accumulated_media = list(intent.get("accumulated_media") or [])
    if _is_nested_subagent(data):
        logger.info(
            "budget_stop_skip_continue_checkpoint",
            agent_id=getattr(data, "agent_id", None),
            parent_agent_id=getattr(data, "parent_agent_id", None),
            stop_reason=stop_reason,
            reason="nested_subagent",
        )
        return build_loop_result(
            message,
            [],
            iterations_used,
            stop_reason,
            accumulated_usage,
            media=accumulated_media,
            finalized=bool(intent.get("finalized")),
        )
    checkpoint_id = persist_budget_continue_checkpoint(
        motet,
        data,
        stop_reason=stop_reason,
        accumulated_usage=accumulated_usage,
        accumulated_media=accumulated_media,
    )
    result = build_loop_result(
        message,
        [],
        iterations_used,
        stop_reason,
        accumulated_usage,
        media=accumulated_media,
        finalized=bool(intent.get("finalized")),
    )
    if checkpoint_id:
        result["budget_continue_checkpoint_id"] = checkpoint_id
    return result


def persist_budget_continue_checkpoint(
    motet: Any,
    data: Any,
    *,
    stop_reason: str,
    accumulated_usage: Dict[str, Any],
    accumulated_media: List[Dict[str, Any]],
) -> Optional[str]:
    """Soft-persist loop state for issue #188 Continue. Store failures degrade."""
    from motet.core.checkpoints import CheckpointKind, TurnCheckpoint, store_turn_checkpoint
    from motet.core.reasoning.react.loop_state_snapshot import LoopStateSnapshot

    try:
        owning_agent_id = _owning_agent_id(motet, data)
        loop_fields = LoopStateSnapshot.from_loop_data(
            data,
            executed_signatures=list(data.executed_signatures or []),
            used_tool_names=list(data.used_tool_names or []),
            usage_accumulator=dict(accumulated_usage),
            media_accumulator=list(accumulated_media),
            agent_id=owning_agent_id,
        ).to_checkpoint_loop_fields()

        checkpoint = TurnCheckpoint(
            checkpoint_id=f"budget-{uuid4().hex}",
            checkpoint_kind=CheckpointKind.BUDGET_CONTINUE,
            budget_stop_reason=str(stop_reason),
            motet_id=getattr(motet, "motet_id", None) or "default",
            tenant_id=getattr(motet, "tenant_id", None),
            principal_id=getattr(motet, "principal_id", None),
            task_id=getattr(motet, "task_id", None),
            conversation_id=getattr(motet, "conversation_id", None),
            handed_back_tool_calls=[],
            conversation_history=[
                m.model_dump(mode="json") for m in data.conversation_history
            ],
            **loop_fields,
        )
        store_turn_checkpoint(checkpoint)
        logger.info(
            "budget_continue_checkpoint_stored",
            checkpoint_id=checkpoint.checkpoint_id,
            stop_reason=stop_reason,
            conversation_id=checkpoint.conversation_id,
            used_tool_names=list(checkpoint.used_tool_names or [])[:20],
        )
        return checkpoint.checkpoint_id
    except Exception as e:
        logger.warning(
            "budget_continue_checkpoint_store_failed",
            stop_reason=stop_reason,
            conversation_id=getattr(motet, "conversation_id", None),
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
        return None


def resolve_resume(
    *,
    tenant_id: Optional[str],
    motet_id: Optional[str],
    tool_call_ids: List[str],
) -> Optional[ResumeHandle]:
    """Load a resume handle for trailing tool_call_ids. No checkpoint object."""
    from motet.core.checkpoints import resolve_resume_checkpoint

    checkpoint = resolve_resume_checkpoint(
        tenant_id=tenant_id,
        motet_id=motet_id,
        tool_call_ids=tool_call_ids,
    )
    if checkpoint is None:
        return None
    return ResumeHandle(
        checkpoint_id=checkpoint.checkpoint_id,
        conversation_id=checkpoint.conversation_id,
    )


__all__ = [
    "materialize_intent",
    "persist_budget_continue_checkpoint",
    "raise_if_turn_cancelled",
    "resolve_resume",
]
