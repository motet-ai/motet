"""
Motet - Budget-Stop Continue Contract

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Shared constants and helpers for GitHub issue #188: explicit Continue after
    an agent turn stops on ``max_iterations`` or ``max_model_calls``.

    Budget stops finalize the turn. Continue is a **new** turn with a fresh
    budget (not resume, which keeps the same counters). Continuity
    uses the same TurnCheckpoint / LoopStateSnapshot machinery as handback
    suspend, with ``checkpoint_kind=budget_continue`` and
    ``LoopStateSnapshot.with_fresh_budget`` as the budget policy.

    Clients detect a budget stop via ``stop_reason`` on the Motet SSE ``end``
    event / turn response (and ``X-Motet-Stop-Reason`` on non-streaming
    OpenAI-compat replies), then send Continue — either freeform chat or the
    typed ``CONTINUE_AFTER_BUDGET_USER_MESSAGE`` / ``continue_after_budget``
    chat flag.

Dependencies:
    - motet.core.checkpoints: TurnCheckpoint load + principal assert
    - motet.core.reasoning.react.loop_state_snapshot: rehydrate + fresh budget
    - motet.core.types.Message: steering injection into history

Usage:
    from motet.core.orchestration.turn.budget_continue import (
        BUDGET_STOP_REASONS,
        CONTINUE_AFTER_BUDGET_USER_MESSAGE,
        is_budget_stop,
        budget_continue_tip,
        inject_budget_continue_steering,
        try_build_budget_continue_loop_data,
    )

    if is_budget_stop(stop_reason):
        tip = budget_continue_tip(stop_reason)

Notes:
    - ``stalled`` uses a similar "please continue" prose path but is not a
      budget stop for Continue affordance purposes (issue #188 scope).
    - Do not auto-extend remaining_iterations in-loop; Continue is consent to
      spend another configured budget chunk as a new turn.
    - Missing checkpoint degrades to transcript + steering only.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

import structlog

from motet.core.types import Message

logger = structlog.get_logger(__name__)

BUDGET_STOP_REASONS = frozenset({"max_iterations", "max_model_calls"})

BUDGET_STOP_FALLBACK_MESSAGE = (
    "Turn budget exhausted. Please continue to keep working on this task."
)

# Typed user message for structured Continue (chat API + Chat Explorer button).
# Fresh max_iterations / max_model_calls apply because this starts a new turn.
CONTINUE_AFTER_BUDGET_USER_MESSAGE = "Continue working on this task."

# OpenAI-compat response header (non-streaming). Streaming Motet chat uses the
# SSE ``end.stop_reason`` field instead; OpenAI SSE cannot revise response headers.
STOP_REASON_HEADER = "X-Motet-Stop-Reason"

CONTINUE_STEERING_SYSTEM_MESSAGE = (
    "This is a continuation of the previous agent turn after a hard turn budget "
    "stop ({stop_reason}). Resume unfinished work from the latest tool "
    "observations and assistant progress in this conversation. Do not treat "
    "this as a new task. Prefer existing tool results and open plans; do not "
    "re-discover tools or re-open the same pages unless the prior observations "
    "are missing or stale."
)


def is_budget_stop(stop_reason: Optional[str]) -> bool:
    """Return True when *stop_reason* is a hard iteration/model-call budget stop."""
    return bool(stop_reason) and str(stop_reason) in BUDGET_STOP_REASONS


def budget_continue_tip(stop_reason: Optional[str]) -> str:
    """Short markdown tip appended before the session banner on budget stops."""
    reason = str(stop_reason or "budget")
    return (
        f"\n\n_Turn budget exhausted (`{reason}`). "
        f'Send "{CONTINUE_AFTER_BUDGET_USER_MESSAGE}" for another turn budget._'
    )


def inject_budget_continue_steering(
    history: Sequence[Any],
    *,
    stop_reason: Optional[str] = None,
) -> List[Message]:
    """
    Insert a continuation system note before the trailing user Continue message.

    Always applied on ``continue_after_budget`` so transcript-only fallbacks
    still get stronger carryover than the bare typed user line.
    """
    reason = str(stop_reason or "max_iterations")
    if reason not in BUDGET_STOP_REASONS:
        reason = "max_iterations"
    steering = Message(
        role="system",
        content=CONTINUE_STEERING_SYSTEM_MESSAGE.format(stop_reason=reason),
    )
    msgs: List[Message] = []
    for item in history:
        if isinstance(item, Message):
            msgs.append(item)
        elif isinstance(item, dict):
            msgs.append(Message.model_validate(item))
        else:
            msgs.append(item)  # type: ignore[arg-type]

    if not msgs:
        return [steering]

    # Prefer placing steering immediately before the last user message.
    insert_at = len(msgs)
    for i in range(len(msgs) - 1, -1, -1):
        if getattr(msgs[i], "role", None) == "user":
            insert_at = i
            break
    return msgs[:insert_at] + [steering] + msgs[insert_at:]


def _resolve_fresh_max_model_calls(
    *,
    configured: Optional[int],
    max_iterations: int,
    checkpoint_max: Optional[int],
) -> int:
    if configured is not None:
        return max(int(configured), 1)
    if checkpoint_max:
        return max(int(checkpoint_max), 1)
    return max(int(max_iterations) * 3, 30)


def try_build_budget_continue_loop_data(
    motet: Any,
    *,
    history: Sequence[Any],
    stream_key: str,
    max_iterations: int,
    max_model_calls: Optional[int] = None,
    input_text: Optional[str] = None,
) -> Optional[Any]:
    """
    Load the conversation's budget_continue checkpoint and build AgenticLoopData.

    Returns None when no snapshot exists (caller falls back to normal agent path
    with steering only). Rehydrate uses LoopStateSnapshot; budget policy is
    ``with_fresh_budget`` (reset counters). Handback resume must not call this.
    """
    from motet.core.checkpoints import (
        CheckpointKind,
        assert_checkpoint_principal,
        find_latest_checkpoint_for_conversation,
    )
    from motet.core.reasoning.react.loop_state_snapshot import LoopStateSnapshot

    conversation_id = str(getattr(motet, "conversation_id", None) or "").strip()
    if not conversation_id:
        return None

    checkpoint = find_latest_checkpoint_for_conversation(
        tenant_id=getattr(motet, "tenant_id", None),
        motet_id=getattr(motet, "motet_id", None) or "default",
        conversation_id=conversation_id,
        kind=CheckpointKind.BUDGET_CONTINUE,
    )
    if checkpoint is None:
        logger.info(
            "budget_continue_checkpoint_miss",
            conversation_id=conversation_id,
        )
        return None

    assert_checkpoint_principal(
        checkpoint.principal_id,
        getattr(motet, "principal_id", None),
        resource_label="budget_continue_checkpoint",
        resource_id=checkpoint.checkpoint_id,
    )

    fresh_max_iterations = max(int(max_iterations or checkpoint.max_iterations or 20), 1)
    fresh_max_model_calls = _resolve_fresh_max_model_calls(
        configured=max_model_calls,
        max_iterations=fresh_max_iterations,
        checkpoint_max=checkpoint.max_model_calls,
    )

    steered = inject_budget_continue_steering(
        history,
        stop_reason=checkpoint.budget_stop_reason,
    )

    loop_data = (
        LoopStateSnapshot.from_checkpoint(checkpoint)
        .with_fresh_budget(
            max_iterations=fresh_max_iterations,
            max_model_calls=fresh_max_model_calls,
        )
        .to_loop_data(
            conversation_history=steered,
            stream_key=stream_key,
            input=input_text or CONTINUE_AFTER_BUDGET_USER_MESSAGE,
        )
    )

    logger.info(
        "budget_continue_rehydrated",
        checkpoint_id=checkpoint.checkpoint_id,
        conversation_id=conversation_id,
        stop_reason=checkpoint.budget_stop_reason,
        max_iterations=loop_data.max_iterations,
        remaining_iterations=loop_data.remaining_iterations,
        max_model_calls=loop_data.max_model_calls,
        model_calls_used=loop_data.model_calls_used,
        used_tool_count=len(loop_data.used_tool_names or []),
    )
    return loop_data


__all__ = [
    "BUDGET_STOP_REASONS",
    "BUDGET_STOP_FALLBACK_MESSAGE",
    "CONTINUE_AFTER_BUDGET_USER_MESSAGE",
    "CONTINUE_STEERING_SYSTEM_MESSAGE",
    "STOP_REASON_HEADER",
    "is_budget_stop",
    "budget_continue_tip",
    "inject_budget_continue_steering",
    "try_build_budget_continue_loop_data",
]
