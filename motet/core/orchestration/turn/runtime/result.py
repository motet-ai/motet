"""
Motet - Turn Runtime Result Types

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    Typed TurnResult and ResumeHandle for. The Turn Runtime is the
    only writer of suspend, budget, and handback state; these models are the
    contract that crosses agent_turn, the loop, and the OpenAI facade so
    optional ``suspended: True`` dict keys stop leaking across packages.

Dependencies:
    - pydantic: TurnResult / ResumeHandle
    - turn.outcome.HandedBackToolCall: typed handback entries

Usage:
    from motet.core.orchestration.turn.runtime.result import (
        TurnResultKind, TurnResult, ResumeHandle,
    )

    if result.kind is TurnResultKind.SUSPENDED:
        return result.checkpoint_id

Notes:
    - Loop iteration still returns a dict (build_loop_result) plus intent
      markers; runtime.materialize_intent turns intents into that dict after
      writing Redis. TurnResult is returned by runtime.start /
      continue_after_budget / resume_turn. Callers branch on ``kind``.
    - Cancel remains an error on the wait path. CANCELLED exists
      so a mapped turn outcome is not confused with a failed iteration.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from motet.core.orchestration.turn.outcome import HandedBackToolCall


class TurnResultKind(str, Enum):
    """Terminal kinds the Turn Runtime may return."""

    COMPLETE = "complete"
    SUSPENDED = "suspended"
    AUTH_REQUIRED = "auth_required"
    BUDGET_STOP = "budget_stop"
    STALLED = "stalled"
    CANCELLED = "cancelled"
    ERROR = "error"


class ResumeHandle(BaseModel):
    """Facade-safe resume lookup: ids only, no TurnCheckpoint."""
    checkpoint_id: str = Field(..., description="Suspension checkpoint id.")
    conversation_id: Optional[str] = Field(
        default=None,
        description="Conversation that owned the suspended turn (rebind target).",
    )

    model_config = ConfigDict(extra="forbid")


class TurnResult(BaseModel):
    """Typed turn outcome. Wire/facade callers should not parse raw loop dicts."""

    kind: TurnResultKind
    stop_reason: Optional[str] = None
    final_response: str = ""
    checkpoint_id: Optional[str] = None
    handed_back_tool_calls: List[HandedBackToolCall] = Field(default_factory=list)
    usage: Optional[Dict[str, Any]] = None
    payload: Dict[str, Any] = Field(
        default_factory=dict,
        description="Loop dict for extract/complete helpers and resume-only keys.",
    )
    conversation_history: List[Any] = Field(
        default_factory=list,
        description="Loop input history after Continue rehydrate; empty on start/resume.",
    )

    model_config = ConfigDict(extra="forbid")


def turn_result_from_loop_payload(payload: Any) -> TurnResult:
    """Classify a loop/runtime dict into TurnResult (best-effort for dual-read)."""
    from motet.core.orchestration.turn.budget_continue import is_budget_stop
    from motet.core.orchestration.turn.outcome import (
        TurnOutcomeKind,
        classify_loop_outcome,
        parse_handed_back_tool_calls,
    )

    if not isinstance(payload, dict):
        return TurnResult(kind=TurnResultKind.COMPLETE, final_response=str(payload or ""))

    if payload.get("cancelled") is True or payload.get("stop_reason") == "cancelled":
        return TurnResult(
            kind=TurnResultKind.CANCELLED,
            stop_reason="cancelled",
            final_response=str(payload.get("final_response") or ""),
            payload=payload,
        )

    outcome = classify_loop_outcome(payload)
    stop = outcome.stop_reason
    kind_map = {
        TurnOutcomeKind.SUSPENDED: TurnResultKind.SUSPENDED,
        TurnOutcomeKind.AUTH_REQUIRED: TurnResultKind.AUTH_REQUIRED,
        TurnOutcomeKind.COMPLETE: TurnResultKind.COMPLETE,
    }
    kind = kind_map[outcome.kind]
    if kind is TurnResultKind.COMPLETE:
        if is_budget_stop(stop):
            kind = TurnResultKind.BUDGET_STOP
        elif stop == "stalled":
            kind = TurnResultKind.STALLED
        elif stop == "error":
            kind = TurnResultKind.ERROR

    return TurnResult(
        kind=kind,
        stop_reason=stop,
        final_response=str(payload.get("final_response") or ""),
        checkpoint_id=outcome.checkpoint_id or (
            str(payload.get("checkpoint_id") or "") or None
        ),
        handed_back_tool_calls=parse_handed_back_tool_calls(
            payload.get("handed_back_tool_calls")
        ),
        usage=payload.get("usage") if isinstance(payload.get("usage"), dict) else None,
        payload=payload,
    )


def coerce_turn_result(result: Any) -> TurnResult:
    """Accept TurnResult or a loop/orchestration dict (tests patch resume_turn)."""
    if isinstance(result, TurnResult):
        return result
    return turn_result_from_loop_payload(result)


__all__ = [
    "ResumeHandle",
    "TurnResult",
    "TurnResultKind",
    "coerce_turn_result",
    "turn_result_from_loop_payload",
]
