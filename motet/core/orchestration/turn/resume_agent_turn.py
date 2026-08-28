"""
Motet - Orchestration-Owned Resume Agent Turn

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Orchestration-owned resume entry for suspended agent turns (GitHub issue #147).
    Calls Turn Runtime ``resume_turn`` for checkpoint load, observation
    validation, and loop re-entry, then applies the same TurnOutcome gate +
    finalize + complete_agent_turn path as a normal ``agent_turn``.

    Mixed-turn finalize (issue #160): consumes ``motet_owned_tool_observations`` from
    the resume_turn ``TurnResult.payload`` and appends those Motet-owned role=tool
    messages after ``build_resume_history`` so the stored transcript matches the
    live loop.

Dependencies:
    - resume_turn / ResumeTurnData: Turn Runtime re-entry
    - build_resume_history / load_turn_checkpoint: Transcript rebuild for finalize
    - turn.outcome: TurnOutcome classifier + suspended/auth gates
    - turn.complete: complete_agent_turn terminal path
    - finalize_turn: Transcript write on completed resumes

Usage:
    from motet.core.orchestration.turn import resume_agent_turn
    from motet.core.orchestration.turn.resume_agent_turn import ResumeAgentTurnData

    result = motet.do(resume_agent_turn, data=ResumeAgentTurnData(
        checkpoint_id="suspend-abc",
        observations=[{"tool_call_id": "call_1", "content": "72°F"}],
    ))

Notes:
    - resume_turn returns ``TurnResult``; this command branches on ``kind``
      then still runs the TurnOutcome gate on ``.payload`` so the public
      command shape (``suspended`` / ``outcome`` / ``stop_reason``) stays
      stable for the facade.
    - resume_turn is a private in-process function, not a Celery
      command. Direct callers against an agent-owned checkpoint still log a
      warning that they skip orchestration finalize.
    - The transcript is rebuilt here from the checkpoint (loads are
      non-consuming) using the same builder the loop ran on, so the resume
      response stays small instead of carrying every message back.
    - auth_required stores conversation history with update_memory=False: it has
      no resume handle, so skipping finalize outright would drop the user's
      question and the authorization prompt before they return from the OAuth
      round trip.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Optional

import structlog
from pydantic import Field

from motet import motet
from motet.core.workers.observers import EventPriority
from motet.core.commands.base_command_data import BaseCommandData
from motet.core.commands.capabilities import WorkerCapability

if TYPE_CHECKING:
    from motet.core.types import Message

logger = structlog.get_logger(__name__)


class ResumeAgentTurnData(BaseCommandData):
    """Orchestration resume input: same handles as resume_turn plus turn context."""

    checkpoint_id: Optional[str] = Field(
        default=None,
        description="Suspension checkpoint id (logical turn id). Prefer when known.",
    )
    tool_call_id: Optional[str] = Field(
        default=None,
        description=(
            "Alternate resume handle: a handed-back tool_call_id indexed to the checkpoint."
        ),
    )
    observations: List[Dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Tool observations covering EVERY handed-back tool_call_id. "
            "Each entry: {tool_call_id, content} (optional name)."
        ),
    )
    conversation_history: Optional[List[Any]] = Field(
        default=None,
        description=(
            "Optional caller-owned history ending with the assistant tool_calls "
            "message (OpenAI facade). When omitted, checkpoint history is restored."
        ),
    )


def get_motet_context() -> Any:
    """Resolve MotetContext through ``turn.phases``.

    Indirect for the same reason as in ``agent_turn.py``: ``phases`` owns the
    single binding, so one patch there covers the whole turn path.
    """
    from motet.core.orchestration.turn import phases

    return phases.get_motet_context()


@motet.command(
    description="Resume a suspended agent turn after elicitation, approval, or handback, then finalize and complete the turn under orchestration.",
    timeout_seconds=600,
    priority=EventPriority.HIGH,
    required_capabilities=[WorkerCapability.REASONING, WorkerCapability.TOOL_EXECUTION],
    streaming_enabled=True,
)
def resume_agent_turn(data: ResumeAgentTurnData) -> Dict[str, Any]:
    """
    Resume a suspended agent turn with orchestration-owned finalize/complete.

    1. Re-enter the loop via Turn Runtime ``resume_turn``.
    2. Branch on ``TurnResult.kind``; classify ``.payload`` with ``TurnOutcome``.
    3. Suspended / auth_required → shared outcome gate (no finalize).
    4. Complete / escalation → finalize_turn (when configured) + complete_agent_turn.
    """
    from motet.core.orchestration.turn.runtime import (
        ResumeTurnData,
        resume_turn,
    )
    from motet.core.orchestration.turn.runtime.result import (
        TurnResultKind,
        coerce_turn_result,
    )
    from motet.core.orchestration.turn.complete import complete_agent_turn, extract_response_text
    from motet.core.orchestration.turn.outcome import apply_turn_outcome_gate, classify_loop_outcome

    motet = get_motet_context()
    parent_command_id = getattr(
        getattr(motet, "distributed_context", None), "parent_command_id", None
    )
    # A resume usually arrives on a fresh task, and the terminal `end` below has
    # nowhere to go unless the stream exists (agent_turn does the same).
    motet.ensure_stream(ttl_seconds=3600)

    turn_result = coerce_turn_result(
        resume_turn(
            ResumeTurnData(
                checkpoint_id=data.checkpoint_id,
                tool_call_id=data.tool_call_id,
                observations=list(data.observations or []),
                conversation_history=data.conversation_history,
                orchestrated=True,
            ),
        )
    )
    loop_result = turn_result.payload
    if not isinstance(loop_result, dict):
        loop_result = {"final_response": str(loop_result or ""), "stop_reason": "stop"}

    # Motet-owned handback tool results executed at resume (GitHub issue #160).
    raw_motet_owned = loop_result.get("motet_owned_tool_observations")
    motet_owned_tool_observations: Optional[List[Dict[str, Any]]] = (
        raw_motet_owned if isinstance(raw_motet_owned, list) else None
    )

    # Checkpoints are non-consuming, so the turn's identity and message list are
    # still readable here instead of being echoed on the loop result.
    checkpoint = _load_resume_checkpoint(motet, data, loop_result)
    qualified_id = _resolve_resume_agent_id(motet, loop_result, checkpoint)
    analysis_metadata: Dict[str, Any] = {}
    prepared_context_info: Dict[str, Any] = {}

    def _persist_history_only(assistant_response: str) -> None:
        _finalize_resume(
            motet,
            qualified_id=qualified_id,
            history=_resume_history(
                data, checkpoint, motet_owned_tool_observations=motet_owned_tool_observations
            ),
            final_response=assistant_response,
            parent_command_id=parent_command_id,
            update_memory=False,
        )

    outcome = classify_loop_outcome(loop_result)
    if turn_result.kind in (TurnResultKind.SUSPENDED, TurnResultKind.AUTH_REQUIRED):
        gated = apply_turn_outcome_gate(
            motet,
            outcome,
            loop_result,
            qualified_id,
            parent_command_id,
            analysis_metadata,
            prepared_context_info,
            _persist_history_only,
        )
        if gated is not None:
            # Preserve resume-only fields for facade / cost attribution.
            for key in ("resumed_from_checkpoint", "usage_this_request", "conversation_id"):
                if key in loop_result:
                    gated[key] = loop_result[key]
            gated["outcome"] = outcome.kind.value
            return gated

    final_response = extract_response_text(loop_result)
    motet.stream_event("turn", state="RESPONDING")
    motet.stream_event("turn", state="COMPLETING")

    if outcome.should_finalize:
        _finalize_resume(
            motet,
            qualified_id=qualified_id,
            history=_resume_history(
                data, checkpoint, motet_owned_tool_observations=motet_owned_tool_observations
            ),
            final_response=final_response,
            parent_command_id=parent_command_id,
            update_memory=True,
        )

    response = complete_agent_turn(
        motet,
        loop_result,
        final_response,
        qualified_id,
        parent_command_id,
        prepared_context_info,
        analysis_metadata,
    )
    for key in ("resumed_from_checkpoint", "usage_this_request", "conversation_id"):
        if key in loop_result:
            response[key] = loop_result[key]
    response["outcome"] = outcome.kind.value
    return response


def _load_resume_checkpoint(
    motet: Any,
    data: ResumeAgentTurnData,
    loop_result: Dict[str, Any],
) -> Optional[Any]:
    """Re-read the checkpoint the loop resumed from (loads are non-consuming)."""
    from motet.core.checkpoints import load_turn_checkpoint

    checkpoint_id = str(
        loop_result.get("resumed_from_checkpoint") or data.checkpoint_id or ""
    ).strip()
    if not checkpoint_id:
        return None
    try:
        return load_turn_checkpoint(
            tenant_id=getattr(motet, "tenant_id", None),
            motet_id=getattr(motet, "motet_id", None) or "default",
            checkpoint_id=checkpoint_id,
        )
    except Exception as e:
        logger.warning(
            "resume_agent_turn_checkpoint_reload_failed",
            checkpoint_id=checkpoint_id,
            error=str(e),
            error_type=type(e).__name__,
        )
        return None


def _resolve_resume_agent_id(
    motet: Any,
    loop_result: Dict[str, Any],
    checkpoint: Optional[Any] = None,
) -> str:
    """Best-effort agent id for finalize/complete after resume."""
    from motet.core.agents import resolve_agent_id

    raw = (
        loop_result.get("agent_id")
        or (checkpoint is not None and getattr(checkpoint, "agent_id", None))
        or (getattr(motet, "distributed_context", None) and
            (getattr(motet.distributed_context, "metadata", None) or {}).get("agent_id"))
    )
    if isinstance(raw, str) and raw.strip():
        try:
            return resolve_agent_id(raw.strip())
        except Exception as e:
            logger.debug(
                "resume_agent_turn_agent_resolve_failed",
                raw=raw,
                error=str(e),
            )
            return raw.strip()
    # No recorded agent: the turn suspended outside agent_turn and its consumer
    # owns the lifecycle, so finalize is skipped (same rule as agent_turn).
    return "unknown"


def _resume_history(
    data: ResumeAgentTurnData,
    checkpoint: Optional[Any],
    motet_owned_tool_observations: Optional[List[Dict[str, Any]]] = None,
) -> List["Message"]:
    """Rebuild the message list the loop ran on, for finalize.

    Uses the same builder as resume_turn so the finalized transcript matches
    what the model saw, including the appended observations.

    Motet-owned handback tool results (GitHub issue #160): resume_turn executes
    Motet-owned calls at resume and ships them as ``motet_owned_tool_observations``.
    They are appended here after the client observations so the finalized
    transcript is complete.
    """
    from motet.core.types import Message

    history: List["Message"]

    if checkpoint is not None:
        try:
            from motet.core.orchestration.turn.runtime import (
                build_resume_history,
            )

            history = build_resume_history(
                checkpoint, data.conversation_history, list(data.observations or [])
            )
        except Exception as e:
            logger.warning(
                "resume_agent_turn_history_rebuild_failed",
                error=str(e),
                error_type=type(e).__name__,
            )
            history = []
    else:
        history = []

    if not history:
        for msg in data.conversation_history or []:
            if isinstance(msg, Message):
                history.append(msg)
            elif isinstance(msg, dict):
                try:
                    history.append(Message.model_validate(msg))
                except Exception:
                    continue

    # Append Motet-owned handback tool results executed at resume (issue #160).
    # build_resume_history skips those ids (no client observation); resume_turn
    # ships them separately so finalize can complete the transcript.
    if motet_owned_tool_observations:
        for obs in motet_owned_tool_observations:
            if not isinstance(obs, dict):
                continue
            history.append(
                Message(
                    role="tool",
                    tool_call_id=str(obs.get("tool_call_id") or ""),
                    name=str(obs.get("name") or ""),
                    content=str(obs.get("content") or ""),
                )
            )

    return history


def _finalize_resume(
    motet: Any,
    *,
    qualified_id: str,
    history: List[Any],
    final_response: str,
    parent_command_id: Optional[str],
    update_memory: bool,
) -> None:
    """Run the owning agent's finalize_turn hook (same policy as agent_turn).

    ``update_memory`` is False for outcomes that store the exchange without
    completing the turn (auth_required), so nothing is learned from an answer
    the agent never produced.
    """
    from motet.core.agents import get_agent_registry, resolve_agent_id
    from motet.core.commands.command_data_classes import FinalizeTurnData
    from motet.core.orchestration.turn.phases import finalize_turn
    from motet.core.orchestration.turn.agent_turn import _resolve_transcript_primary

    if not qualified_id or qualified_id == "unknown":
        logger.debug("resume_agent_turn_finalize_skipped_no_agent")
        return

    try:
        agent_config = get_agent_registry().get(qualified_id)
        turn_hooks = getattr(agent_config, "turn_hooks", None) if agent_config else None
        fin_hook = getattr(turn_hooks, "finalize", None) if turn_hooks else None
        from motet.core.orchestration.turn.hook_resolve import resolve_hook_implementation

        if resolve_hook_implementation(fin_hook, slot="finalize") is None:
            logger.debug(
                "resume_agent_turn_finalize_skipped_hook_disabled",
                agent_id=qualified_id,
            )
            return

        cmd_metadata = (
            getattr(getattr(motet, "distributed_context", None), "metadata", None) or {}
        )
        finalize_root_agent_id, finalize_root_turn = _resolve_transcript_primary(
            cmd_metadata, qualified_id, parent_command_id, resolve_agent_id,
        )

        fin_data, fin_error = motet.maybe(
            finalize_turn,
            data=FinalizeTurnData(
                messages=history,
                assistant_response=final_response,
                agent_id=qualified_id,
                store_conversation=True,
                update_memory=update_memory,
                root_turn=finalize_root_turn,
                root_agent_id=finalize_root_agent_id,
            ),
        )
        if fin_error:
            logger.warning(
                "resume_agent_turn_finalize_failed",
                agent_id=qualified_id,
                update_memory=update_memory,
                error=fin_error,
            )
        else:
            logger.debug(
                "resume_agent_turn_finalize_complete",
                agent_id=qualified_id,
                update_memory=update_memory,
                result=fin_data,
            )
    except Exception as e:
        logger.error(
            "resume_agent_turn_finalize_error",
            agent_id=qualified_id,
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )


def _register_resume_agent_turn_data() -> None:
    from motet.core.commands.command_data_registry import command_data_registry

    command_data_registry.register("core.resume_agent_turn", ResumeAgentTurnData)


_register_resume_agent_turn_data()

__all__ = ["resume_agent_turn", "ResumeAgentTurnData"]
