"""
Motet - Turn Outcome Gate

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    Typed turn-outcome classifier and suspended-handback models for the
    reasoning ↔ orchestration boundary (GitHub issue #147).
    Classifies loop payloads into complete / suspended / auth_required outcomes
    with explicit finalize + stream rules so agent_turn and resume_agent_turn
    share one gate. auth_required does not finalize.

Dependencies:
    - pydantic: HandedBackToolCall / TurnOutcome models
    - structlog: Suspended-turn observability

Usage:
    from motet.core.orchestration.turn.outcome import (
        TurnOutcomeKind,
        classify_loop_outcome,
        apply_turn_outcome_gate,
    )

    outcome = classify_loop_outcome(turn_result)
    if not outcome.should_finalize:
        gated = apply_turn_outcome_gate(
            motet, outcome, turn_result, qualified_id, parent_command_id,
            analysis_metadata, prepared_context_info,
        )
        if gated is not None:
            return gated

Notes:
    - HandedBackToolCall is the typed form of checkpoint/wire handback entries;
      Redis checkpoints still store plain dicts (model_dump on write).
    - should_finalize is False for suspended and auth_required; True for
      complete. Loop results do not carry stop_reason="escalation".
    - auth_required additionally sets history_only_finalize: it has no resume
      handle, so its history must be written now or never. Suspended does not,
      because its resume writes the single transcript for the logical turn.
    - Gate responses carry stop_reason as well as the typed suspended/outcome
      keys: the OpenAI facade branches on it to decide handback vs. text, so
      dropping it silently turns a re-suspension into an empty completion.
    - _suspended_turn_response remains as a thin wrapper for test/import
      compatibility and delegates here.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Callable, Dict, List, Optional

import structlog
from pydantic import BaseModel, ConfigDict, Field

logger = structlog.get_logger(__name__)


class TurnOutcomeKind(str, Enum):
    """Post-reasoning turn outcomes with distinct finalize/stream rules."""

    COMPLETE = "complete"
    SUSPENDED = "suspended"
    AUTH_REQUIRED = "auth_required"


class HandedBackToolCall(BaseModel):
    """One externally-owned tool call handed back on suspension."""

    tool_call_id: str = Field(..., description="Provider/tool call id used as resume handle.")
    tool_name: str = Field(..., description="Wire/canonical tool name requested by the model.")
    parameters: Dict[str, Any] = Field(
        default_factory=dict,
        description="Arguments the model supplied for this call.",
    )

    model_config = ConfigDict(extra="allow")


class TurnOutcome(BaseModel):
    """
    Classified loop result for orchestration finalize/stream decisions.

    ``should_finalize`` is the single policy bit shared by agent_turn and
    resume_agent_turn: incomplete outcomes (suspended, auth_required) must not
    write transcripts or emit the normal complete terminal.
    """

    kind: TurnOutcomeKind
    should_finalize: bool = Field(
        ...,
        description="True when finalize / complete_agent_turn may run.",
    )
    history_only_finalize: bool = Field(
        default=False,
        description=(
            "Write conversation history without a memory update. Set for outcomes "
            "that never complete and have no resume to write the transcript later."
        ),
    )
    checkpoint_id: Optional[str] = None
    handed_back_tool_calls: List[HandedBackToolCall] = Field(default_factory=list)
    stop_reason: Optional[str] = None

    model_config = ConfigDict(extra="forbid")


def parse_handed_back_tool_calls(raw: Any) -> List[HandedBackToolCall]:
    """Coerce checkpoint/wire handback lists into typed HandedBackToolCall models."""
    if not isinstance(raw, list):
        return []
    parsed: List[HandedBackToolCall] = []
    for entry in raw:
        if isinstance(entry, HandedBackToolCall):
            parsed.append(entry)
            continue
        if not isinstance(entry, dict):
            continue
        call_id = str(entry.get("tool_call_id") or "").strip()
        name = str(entry.get("tool_name") or "").strip()
        if not call_id or not name:
            continue
        params = entry.get("parameters")
        parsed.append(
            HandedBackToolCall(
                tool_call_id=call_id,
                tool_name=name,
                parameters=params if isinstance(params, dict) else {},
            )
        )
    return parsed


def classify_loop_outcome(payload: Any) -> TurnOutcome:
    """
    Classify an agentic_loop / resume_turn payload into a TurnOutcome.

    Precedence: suspended → auth_required → complete.
    """
    if not isinstance(payload, dict):
        return TurnOutcome(kind=TurnOutcomeKind.COMPLETE, should_finalize=True)

    stop_reason = payload.get("stop_reason")
    stop = str(stop_reason) if stop_reason is not None else None

    if stop == "suspended" or payload.get("suspended") is True:
        return TurnOutcome(
            kind=TurnOutcomeKind.SUSPENDED,
            should_finalize=False,
            checkpoint_id=str(payload.get("checkpoint_id") or "") or None,
            handed_back_tool_calls=parse_handed_back_tool_calls(
                payload.get("handed_back_tool_calls")
            ),
            stop_reason="suspended",
        )

    if stop == "auth_required" or payload.get("auth_required") is True:
        # Unlike suspension, an auth-blocked turn writes no checkpoint and has no
        # resume handle: the user authorizes out of band and starts a new turn.
        # Skipping finalize entirely would drop both their question and the
        # authorization prompt, so history is persisted without a memory update.
        return TurnOutcome(
            kind=TurnOutcomeKind.AUTH_REQUIRED,
            should_finalize=False,
            history_only_finalize=True,
            stop_reason="auth_required",
        )

    return TurnOutcome(
        kind=TurnOutcomeKind.COMPLETE,
        should_finalize=True,
        stop_reason=stop,
    )


def apply_turn_outcome_gate(
    motet: Any,
    outcome: TurnOutcome,
    payload: Any,
    qualified_id: str,
    parent_command_id: Optional[str],
    analysis_metadata: Dict[str, Any],
    prepared_context_info: Dict[str, Any],
    persist_history: Optional[Callable[[str], None]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Apply non-finalize outcome gates (suspended / auth_required).

    Returns a turn response dict when the gate handles the outcome, or None
    when the caller should continue with finalize + complete_agent_turn.

    ``persist_history`` is invoked with the assistant text for outcomes whose
    ``history_only_finalize`` is set. The gate owns *when* history is written;
    the caller owns *how*, since only it has the turn's messages and sequence.
    """
    if outcome.kind == TurnOutcomeKind.SUSPENDED:
        return _emit_suspended_response(
            motet,
            payload if isinstance(payload, dict) else {},
            outcome,
            qualified_id,
            parent_command_id,
            analysis_metadata,
            prepared_context_info,
        )
    if outcome.kind == TurnOutcomeKind.AUTH_REQUIRED:
        return _emit_auth_required_response(
            motet,
            payload if isinstance(payload, dict) else {},
            outcome,
            qualified_id,
            parent_command_id,
            analysis_metadata,
            prepared_context_info,
            persist_history,
        )
    return None


def _emit_suspended_response(
    motet: Any,
    payload: Dict[str, Any],
    outcome: TurnOutcome,
    qualified_id: str,
    parent_command_id: Optional[str],
    analysis_metadata: Dict[str, Any],
    prepared_context_info: Dict[str, Any],
) -> Dict[str, Any]:
    """ADR-0127 suspended terminal: no finalize; emit suspended stream event."""
    checkpoint_id = outcome.checkpoint_id or str(payload.get("checkpoint_id") or "")
    handed_back = [
        call.model_dump(mode="json") for call in outcome.handed_back_tool_calls
    ]
    if not handed_back:
        handed_back = payload.get("handed_back_tool_calls") or []
    content = str(payload.get("final_response") or "")
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else None
    suspend_fields: Dict[str, Any] = {
        "agent_id": qualified_id,
        "checkpoint_id": checkpoint_id,
        "handed_back_tool_calls": handed_back,
        "content": content,
    }
    if usage is not None:
        suspend_fields["usage"] = usage
    logger.info(
        "agent_turn_suspended",
        agent_id=qualified_id,
        checkpoint_id=checkpoint_id,
        handed_back_count=len(handed_back),
    )
    if parent_command_id:
        motet.stream_event("agent_turn_suspended", **suspend_fields)
    else:
        motet.stream_event("suspended", **suspend_fields)
    return {
        "agent_id": qualified_id,
        "final_response": content,
        "media": payload.get("media") or [],
        "result": payload,
        "analysis_metadata": analysis_metadata,
        "context_info": prepared_context_info,
        "artifact_rag_citations": prepared_context_info.get("artifact_rag_citations", []),
        "usage": usage,
        "suspended": True,
        "stop_reason": "suspended",
        "checkpoint_id": checkpoint_id,
        "handed_back_tool_calls": handed_back,
        "outcome": outcome.kind.value,
    }


def _emit_auth_required_response(
    motet: Any,
    payload: Dict[str, Any],
    outcome: TurnOutcome,
    qualified_id: str,
    parent_command_id: Optional[str],
    analysis_metadata: Dict[str, Any],
    prepared_context_info: Dict[str, Any],
    persist_history: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """
    Auth-required terminal without a completed-turn finalize (issue #147).

    The loop already streamed ``auth_required`` for the UI; this gate suppresses
    the memory update and emits a turn-level terminal that preserves auth fields.
    Conversation history is still written so the exchange survives the round trip
    the user makes to the authorization endpoint.
    """
    content = str(payload.get("final_response") or "")
    if outcome.history_only_finalize and persist_history is not None:
        try:
            persist_history(content)
        except Exception as e:
            logger.error(
                "auth_required_history_persist_failed",
                agent_id=qualified_id,
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True,
            )
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else None
    auth_fields: Dict[str, Any] = {
        "agent_id": qualified_id,
        "content": content,
        "stop_reason": "auth_required",
        "auth_required": True,
        "service_id": payload.get("service_id"),
        "display_name": payload.get("display_name"),
        "authorization_endpoint": payload.get("authorization_endpoint"),
        "required_scopes": payload.get("required_scopes") or [],
    }
    if usage is not None:
        auth_fields["usage"] = usage
    logger.info(
        "agent_turn_auth_required",
        agent_id=qualified_id,
        service_id=payload.get("service_id"),
    )
    # Nested turns stay non-terminal; root emits end so chat UIs settle.
    if parent_command_id:
        motet.stream_event("agent_turn_auth_required", **auth_fields)
    else:
        motet.stream_event("end", **auth_fields)
    return {
        "agent_id": qualified_id,
        "final_response": content,
        "media": payload.get("media") or [],
        "result": payload,
        "analysis_metadata": analysis_metadata,
        "context_info": prepared_context_info,
        "artifact_rag_citations": prepared_context_info.get("artifact_rag_citations", []),
        "usage": usage,
        "auth_required": True,
        "stop_reason": "auth_required",
        "service_id": payload.get("service_id"),
        "display_name": payload.get("display_name"),
        "authorization_endpoint": payload.get("authorization_endpoint"),
        "required_scopes": payload.get("required_scopes") or [],
        "outcome": TurnOutcomeKind.AUTH_REQUIRED.value,
    }


__all__ = [
    "TurnOutcomeKind",
    "HandedBackToolCall",
    "TurnOutcome",
    "parse_handed_back_tool_calls",
    "classify_loop_outcome",
    "apply_turn_outcome_gate",
]
