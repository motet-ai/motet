"""
Motet - Turn Runtime Resume

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    Resume half of Turn Runtime. Lives in ``turn/runtime/resume.py``
    next to persist.py so checkpoint writes and re-entry stay one owner.
    ``resume_agent_turn`` is the public Celery command; this module is the
    private in-process primitive it calls.

    Loads the checkpoint, re-authorizes the principal, validates observations
    against the recorded handback, rebinds ``conversation_id``, executes
Motet-owned mixed-turn calls (issue #159 / #160), and re-enters the loop
    via ``run_agentic_loop`` with restored ``LoopStateSnapshot`` state.

    Nested workflow suspends (issue #149): when the checkpoint carries
    ``workflow_run_id``, observations are forwarded to ``resume_workflow`` first;
    on workflow completion the nested ``workflow_*`` tool observation is appended
    and the loop re-enters. Re-suspension is a Turn Runtime intent (materialized
    here so Redis is written).

    Mixed turns (issue #159, execute-at-resume): suspend hands the whole turn
    back (the wire assistant message declares every call, so caller-supplied
    transcripts stay provider-valid), but the client covers only the
    externally-owned ids; observations for Motet-owned ids are discarded with
    a warning (frameworks that answer every call keep working), and Motet
    executes those calls here — after the client's observations land and
    before the loop's next model call.

    Finalize completeness (issue #160): Motet-owned role=tool results appended
    at resume are also shipped on the result as ``motet_owned_tool_observations`` so
    orchestration finalize can rebuild a complete transcript (build_resume_history
    skips Motet-owned ids because the client has no observations for them).

    History authority: callers that own the wire transcript (the OpenAI facade,
    §5c.1) may supply conversation_history; otherwise the checkpointed
    history is used. Either way the history must end with the assistant
    tool_calls message that was handed back.

    State derived from the transcript follows the transcript's owner. Duplicate-
    detection signatures assert "this call ran and its result is above", which is
    only true of the transcript they came from — a client that summarizes drops
    old tool results, so a carried-over signature outlives its observation and
    makes the loop refuse to re-fetch data the model can no longer see (it
    answers "adjust parameters before calling again", which is what drives
    read-window thrashing). When the caller supplies history, signatures are
    re-derived from it once this resume's observations are appended; only Motet's
    own restored history keeps the checkpointed set. Budget counters (iterations,
    model calls, usage) are Motet's own facts and are always restored from the
    checkpoint.

Dependencies:
    - get_motet_context: Caller-supplied MotetContext (in-process,)
    - motet.core.checkpoints: Checkpoint load + tool_call_id index lookup
    - LoopStateSnapshot, run_agentic_loop: Loop re-entry with restored state
    - runtime.materialize_intent: nested re-suspend checkpoint write
    - Message: Canonical message type for observation appends

Usage:
    from motet.core.orchestration.turn.runtime import resume_turn, ResumeTurnData
    from motet.core.orchestration.turn.runtime.result import TurnResultKind

    result = resume_turn(ResumeTurnData(
        checkpoint_id="suspend-abc123",     # or tool_call_id="call_1" (resume handle)
        observations=[{"tool_call_id": "call_1", "content": "72°F and sunny"}],
        orchestrated=True,
    ))
    if result.kind is TurnResultKind.SUSPENDED:
        ...

Notes:
    - Checkpoint reads are non-consuming: retrying a failed resume is safe
      until the checkpoint TTL expires (idempotency,).
    - Observations must cover ALL handed-back tool_call_ids — strict providers
      (Anthropic) reject an assistant tool_calls message without a result for
      every call; partial resumes would wedge the transcript.
    - Returns ``TurnResult``. Resume-only keys (``resumed_from_checkpoint``,
      ``agent_id``, rebound ``conversation_id``, ``usage_this_request``,
      ``motet_owned_tool_observations``) live on ``.payload``. History is not
      echoed: resume_agent_turn rebuilds it from the checkpoint through
      ``build_resume_history``.
    - Conversation identity: before re-entering the loop, motet.conversation_id
      is rebound to checkpoint.conversation_id so prompt_cache_key
      and cost attribution stay on the suspend conversation when the HTTP
      resume request minted a fresh id.
    - Nested re-suspend lazy-imports persist.materialize_intent so this
      module does not import the runtime package at module scope (cycle).
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import structlog
from pydantic import Field

from motet.core.commands.base_command_data import BaseCommandData
from motet.core.commands.decorator import get_motet_context
from motet.core.distributed.tenant_keys import task_response_stream_for
from motet.core.types import Message
from motet.core.reasoning.react.loop_driver import run_agentic_loop, stamp_agentic_loop_iteration
from motet.core.reasoning.react.loop_execution import derive_executed_signatures
from motet.core.reasoning.react.loop_state_snapshot import LoopStateSnapshot
from motet.core.checkpoints import (
    TurnCheckpoint,
    find_checkpoint_id_by_tool_call,
    load_turn_checkpoint,
)
from motet.core.orchestration.turn.runtime.result import TurnResult, coerce_turn_result

logger = structlog.get_logger(__name__)


class ResumeTurnData(BaseCommandData):
    """Input for resume_turn: resume handle plus tool observations."""

    checkpoint_id: Optional[str] = Field(
        default=None,
        description="Checkpoint id returned in the suspended result. Either this or tool_call_id is required.",
    )
    tool_call_id: Optional[str] = Field(
        default=None,
        description=(
            "Any handed-back tool_call_id (the resume handle, ). Resolved to the "
            "checkpoint via the index; used when the caller only kept the wire-level ids."
        ),
    )
    observations: List[Dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Tool results from the external owner, one per handed-back call: "
            "{tool_call_id, content} (content coerced to string; dicts are JSON-encoded). "
            "Must cover every handed-back tool_call_id; unknown ids are rejected."
        ),
    )
    orchestrated: bool = Field(
        default=False,
        description=(
            "Set by resume_agent_turn to signal that orchestration owns finalize "
            "and completion for this resume. Direct callers leave it False and "
            "are warned when the checkpoint belongs to an agent_turn."
        ),
    )
    # conversation_history (inherited from BaseCommandData): optional external
    # override for callers that own the wire transcript (ADR-0125 §5c.1). When
    # omitted, the checkpointed history is restored.


def _observation_content(obs: Dict[str, Any]) -> str:
    """Coerce an observation's content/result to the string appended as role=tool."""
    content = obs.get("content", obs.get("result"))
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    try:
        return json.dumps(content)
    except (TypeError, ValueError):
        return str(content)


def _bind_resume_conversation(motet: Any, checkpoint: TurnCheckpoint) -> Optional[str]:
    """
    Rebind the resume request to the checkpoint's conversation_id.

    Facade clients often omit ``previous_response_id`` / ``X-Motet-Conversation-Id``
    on the tool-result POST, so Motet mints a fresh ``openai-{uuid}``. Using that
    id for ``prompt_cache_key`` (ADR-0124) and cost attribution breaks OpenAI
    cache affinity with the suspend call that seeded the prefix. The checkpoint
    records the conversation that owned the suspended turn — bind back to it
    before re-entering ``agentic_loop`` so child model calls inherit it via
    ``motet.do`` context propagation.
    """
    from motet.core.checkpoints.redis_store import bind_resume_conversation

    return bind_resume_conversation(
        motet,
        checkpoint.conversation_id,
        log_context={"checkpoint_id": checkpoint.checkpoint_id},
    )


def _motet_owned_handback_ids(checkpoint: TurnCheckpoint) -> set:
    """
    Handed-back tool_call_ids Motet executes itself at resume (issue #159).

    Ownership is re-derived from the checkpoint's externally-owned names
    (``handback_tool_names`` plus ``handback_tools`` schema names): a
    handed-back call whose tool is not externally owned is Motet's to execute.
    Empty for pure-client turns, so those resume exactly as before.
    """
    from motet.core.checkpoints import split_calls_by_ownership

    external_names = {
        str(n).strip()
        for n in (checkpoint.handback_tool_names or [])
        if str(n).strip()
    }
    for schema in checkpoint.handback_tools or []:
        name = str((schema or {}).get("name") or "").strip()
        if name:
            external_names.add(name)
    if not external_names:
        return set()
    motet_owned, _ = split_calls_by_ownership(
        checkpoint.handed_back_tool_calls, external_names=external_names
    )
    return {
        str(c.get("tool_call_id") or "") for c in motet_owned if c.get("tool_call_id")
    }


def _validate_observations(
    checkpoint: TurnCheckpoint,
    observations: List[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """
    Validate observations against the recorded handback (ADR-0127 security).

    Every supplied tool_call_id must be in the recorded handback (forged
    observations rejected) and every handed-back call must have exactly one
    observation (strict providers reject partial tool results).

    Execute-at-resume (issue #159): Motet-owned handed-back ids are
    excluded from the client's coverage requirement — Motet executes those.
    A client observation for one is discarded with a warning, not rejected:
    stock agent frameworks answer every call in ``tool_calls`` (the classic
    fake-error dance), and Motet's own execution is authoritative regardless
    of what the client claims, so there is no injection risk in ignoring it.

    Returns observations keyed by tool_call_id.
    """
    from motet.core.checkpoints.redis_store import validate_handback_observations

    recorded_ids = {
        str(c.get("tool_call_id") or "")
        for c in checkpoint.handed_back_tool_calls
        if c.get("tool_call_id")
    }
    motet_owned_ids = _motet_owned_handback_ids(checkpoint)
    try:
        return validate_handback_observations(
            recorded_ids,
            observations,
            exclude_ids=motet_owned_ids,
            error_prefix="resume_turn",
        )
    except ValueError as e:
        # Preserve checkpoint id in forged-id messages for operators.
        msg = str(e)
        if "unknown tool_call_id" in msg:
            raise ValueError(
                f"{msg}; not part of the recorded handback for checkpoint "
                f"'{checkpoint.checkpoint_id}'"
            ) from e
        raise


def _execute_motet_owned_calls_at_resume(
    motet: Any,
    loop_data: Any,
    checkpoint: TurnCheckpoint,
) -> List[Dict[str, str]]:
    """
    Execute Motet-owned handed-back calls before loop re-entry (issue #159).

    Appends their ``role=tool`` observations to ``loop_data.conversation_history``
    after the client's observations. Any Motet-owned call left without an
    observation (execution error, auth_required, workflow prep failure) gets a
    synthetic error observation: strict providers reject an assistant
    tool_calls message with uncovered calls, and at this point — the client has
    already answered its subset — falling back to classic whole-turn handback
    is no longer possible.

    Returns the Motet-owned ``role=tool`` observations appended during this call
    (issue #160) so the orchestration finalize path can rebuild a complete
    transcript without re-scanning the full conversation history.
    """
    motet_owned_ids = _motet_owned_handback_ids(checkpoint)
    if not motet_owned_ids:
        return []
    stamp_agentic_loop_iteration(motet, loop_data.current_iteration)
    motet_calls = [
        c
        for c in checkpoint.handed_back_tool_calls
        if str(c.get("tool_call_id") or "") in motet_owned_ids
    ]

    from motet.core.reasoning.react.loop_execution import execute_tools_and_append_results

    hist_before = len(loop_data.conversation_history)
    timings = {"embedding_ms": 0.0, "llm_ms": 0.0, "tool_execution_ms": 0.0}
    try:
        exec_result = execute_tools_and_append_results(
            motet_calls,
            [],
            loop_data,
            motet,
            1,
            0,
            dict(checkpoint.usage_accumulator or {}),
            timings,
        )
        if exec_result.auth_response is not None or exec_result.early_return is not None:
            # POC open question: auth flows have no clean surface mid-resume;
            # uncovered calls fall through to the error-observation backstop.
            logger.warning(
                "resume_turn_motet_execution_interrupted",
                checkpoint_id=checkpoint.checkpoint_id,
                reason="auth_or_early_return",
                motet_owned=[str(c.get("tool_name") or "") for c in motet_calls],
            )
    except Exception as e:
        logger.error(
            "resume_turn_motet_execution_failed",
            checkpoint_id=checkpoint.checkpoint_id,
            motet_owned=[str(c.get("tool_name") or "") for c in motet_calls],
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )

    observed_ids = {
        str(getattr(msg, "tool_call_id", "") or "")
        for msg in loop_data.conversation_history[hist_before:]
        if getattr(msg, "role", None) == "tool"
    }
    for call in motet_calls:
        call_id = str(call.get("tool_call_id") or "")
        if call_id in observed_ids:
            continue
        loop_data.conversation_history.append(
            Message(
                role="tool",
                tool_call_id=call_id,
                name=str(call.get("tool_name") or ""),
                content="Tool execution failed during resume; no result available.",
            )
        )
        logger.warning(
            "resume_turn_motet_observation_backstopped",
            checkpoint_id=checkpoint.checkpoint_id,
            tool_call_id=call_id,
            tool_name=str(call.get("tool_name") or ""),
        )

    logger.info(
        "resume_turn_motet_calls_executed",
        checkpoint_id=checkpoint.checkpoint_id,
        motet_executed=[str(c.get("tool_name") or "") for c in motet_calls],
    )

    # Only messages appended by this execute-at-resume pass (issue #160).
    motet_owned_tool_observations: List[Dict[str, str]] = []
    for msg in loop_data.conversation_history[hist_before:]:
        role = getattr(msg, "role", None)
        tc_id = str(getattr(msg, "tool_call_id", "") or "")
        if role == "tool" and tc_id in motet_owned_ids:
            motet_owned_tool_observations.append({
                "role": "tool",
                "tool_call_id": tc_id,
                "name": str(getattr(msg, "name", "") or ""),
                "content": str(getattr(msg, "content", "") or ""),
            })
    return motet_owned_tool_observations


def resume_turn(data: ResumeTurnData) -> TurnResult:
    """
    Resume a suspended agent turn from its checkpoint (ADR-0127).

    Loads the checkpoint (by checkpoint_id or handed-back tool_call_id),
    re-authorizes the caller, validates the observations against the recorded
    handback, appends role=tool messages, and re-enters the loop via
    ``run_agentic_loop`` with the restored state.
    """
    motet = get_motet_context()

    tenant_id = getattr(motet, "tenant_id", None)
    motet_id = getattr(motet, "motet_id", None) or "default"

    checkpoint_id = (data.checkpoint_id or "").strip()
    if not checkpoint_id and data.tool_call_id:
        checkpoint_id = find_checkpoint_id_by_tool_call(
            tenant_id=tenant_id,
            motet_id=motet_id,
            tool_call_id=str(data.tool_call_id).strip(),
        ) or ""
    if not checkpoint_id:
        raise ValueError(
            "resume_turn: no checkpoint found; supply checkpoint_id or a "
            "handed-back tool_call_id (checkpoints expire after their TTL)"
        )

    checkpoint = load_turn_checkpoint(
        tenant_id=tenant_id, motet_id=motet_id, checkpoint_id=checkpoint_id
    )
    if checkpoint is None:
        raise ValueError(
            f"resume_turn: checkpoint '{checkpoint_id}' not found or expired"
        )

    # Re-authorization (ADR-0127): the tenant/motet scope is enforced by key
    # scoping; the principal must match the one that suspended the turn.
    from motet.core.checkpoints.redis_store import assert_checkpoint_principal

    assert_checkpoint_principal(
        checkpoint.principal_id,
        getattr(motet, "principal_id", None),
        resource_label="resume_turn",
        resource_id=checkpoint_id,
    )

    # ADR-0124 / ADR-0127: keep prompt_cache_key + cost under the suspend
    # conversation even when the HTTP resume request minted a fresh id.
    bound_conversation_id = _bind_resume_conversation(motet, checkpoint)

    if checkpoint.agent_id and not data.orchestrated:
        # Loop re-entry only. The transcript for an agent-owned turn is written
        # by resume_agent_turn, so a direct call completes the turn with no
        # record of it (core.resume_agent_turn is the entry point).
        logger.warning(
            "resume_turn_direct_call_skips_finalize",
            checkpoint_id=checkpoint_id,
            agent_id=checkpoint.agent_id,
            remedy="dispatch core.resume_agent_turn instead",
        )

    observations_by_id = _validate_observations(checkpoint, data.observations)
    caller_owns_history = bool(data.conversation_history)

    # Nested workflow resume (issue #149): apply observations to the workflow
    # graph before re-entering the agentic loop.
    if checkpoint.workflow_run_id:
        return _resume_nested_workflow_turn(
            motet=motet,
            data=data,
            checkpoint=checkpoint,
            checkpoint_id=checkpoint_id,
            observations_by_id=observations_by_id,
            bound_conversation_id=bound_conversation_id,
        )

    history = build_resume_history(checkpoint, data.conversation_history, data.observations)

    logger.info(
        "resume_turn_resuming",
        checkpoint_id=checkpoint_id,
        conversation_id=checkpoint.conversation_id,
        observation_count=len(observations_by_id),
        remaining_iterations=checkpoint.remaining_iterations,
    )
    motet.stream_event(
        "turn_resumed",
        checkpoint_id=checkpoint_id,
        observation_count=len(observations_by_id),
        stream_key=task_response_stream_for(motet),
    )

    resume_max_model_calls = int(checkpoint.max_model_calls or 0) or max(
        int(checkpoint.max_iterations or 20) * 3, 30
    )
    resume_model_calls_used = max(int(checkpoint.model_calls_used or 0), 0)

    loop_data = LoopStateSnapshot.from_checkpoint(
        checkpoint,
        executed_signatures=list(checkpoint.executed_signatures),
        max_model_calls=resume_max_model_calls,
        model_calls_used=resume_model_calls_used,
        used_tool_names=list(checkpoint.used_tool_names),
        media_accumulator=list(checkpoint.media_accumulator),
    ).to_loop_data(
        conversation_history=history,
        stream_key=task_response_stream_for(motet),
    )

    # Execute-at-resume (issue #159): Motet's own handed-back calls run here,
    # after the client's observations landed and before signature derivation so
    # they count as executed below. No-op for pure-client turns. Returns the
    # Motet-owned role=tool observations for finalize (issue #160).
    motet_owned_tool_observations = _execute_motet_owned_calls_at_resume(
        motet, loop_data, checkpoint
    )

    # Duplicate-detection signatures follow whichever history won. A signature is
    # a claim about the transcript ("this call ran and its result is above"), so
    # keeping Motet's copy beside a caller-owned transcript leaves two sources of
    # truth for one fact: the caller prunes old tool results when it summarizes,
    # and the surviving signature then makes the loop refuse to re-fetch data the
    # model can no longer see. Derived after all observations are appended (the
    # caller's and Motet's own), so the calls just executed count as executed.
    if caller_owns_history:
        executed_signatures = derive_executed_signatures(loop_data.conversation_history)
        dropped = len(set(checkpoint.executed_signatures) - set(executed_signatures))
        if dropped:
            logger.info(
                "resume_turn_signatures_rederived",
                checkpoint_id=checkpoint_id,
                conversation_id=checkpoint.conversation_id,
                checkpointed=len(checkpoint.executed_signatures),
                derived=len(executed_signatures),
                dropped=dropped,
                reason="supplied transcript no longer carries these observations",
            )
        loop_data.executed_signatures = executed_signatures

    result = run_agentic_loop(motet, loop_data)
    if isinstance(result, dict):
        result["resumed_from_checkpoint"] = checkpoint_id
        # The loop's `usage` is the whole logical turn: the accumulator is seeded
        # from the checkpoint, so on the Nth handback it is the sum of every model
        # call since the turn began. Motet needs that total (budgets, cost, the
        # next checkpoint), but a wire client reading `usage` as "what this API
        # call cost" is being lied to at N-times scale — Cursor took the inflated
        # totals as context fullness and summarized the transcript on every turn,
        # which is what drove the re-read loops. The delta against the checkpoint
        # baseline is what this request actually consumed.
        result["usage_this_request"] = _usage_since(
            checkpoint.usage_accumulator, result.get("usage")
        )
        if motet_owned_tool_observations:
            # Ship Motet-owned handback tool results for orchestration finalize
            # (issue #160). Full history is not echoed; resume_agent_turn rebuilds
            # via build_resume_history then appends these observations.
            result["motet_owned_tool_observations"] = motet_owned_tool_observations
        if bound_conversation_id:
            result["conversation_id"] = bound_conversation_id
        # Surface identity for orchestration-owned resume finalize
        # (resume_agent_turn / TurnOutcome gate — issue #147).
        if checkpoint.agent_id:
            result.setdefault("agent_id", checkpoint.agent_id)
    return coerce_turn_result(result)


def _resume_nested_workflow_turn(
    *,
    motet: Any,
    data: ResumeTurnData,
    checkpoint: TurnCheckpoint,
    checkpoint_id: str,
    observations_by_id: Dict[str, Dict[str, Any]],
    bound_conversation_id: Optional[str],
) -> TurnResult:
    """Forward client observations into resume_workflow, then continue the loop."""
    from motet.core.commands.builtin.workflow import resume_workflow
    from motet.core.commands.command_data_classes import ResumeWorkflowData
    from motet.core.reasoning.react.loop_observations import format_workflow_steps

    kind = (checkpoint.suspend_reason or "handback_tools").strip() or "handback_tools"
    if kind != "handback_tools":
        raise ValueError(
            f"resume_turn: nested workflow suspend_reason '{kind}' is not "
            f"agent-path consumable; resume via resume_workflow / HTTP with the "
            f"correct tagged payload (workflow_run_id={checkpoint.workflow_run_id})"
        )
    observations = [
        {
            "tool_call_id": obs_id,
            "content": obs.get("content", obs.get("result", "")),
        }
        for obs_id, obs in observations_by_id.items()
    ]

    logger.info(
        "resume_turn_nested_workflow",
        checkpoint_id=checkpoint_id,
        workflow_run_id=checkpoint.workflow_run_id,
        kind=kind,
        observation_count=len(observations),
    )
    motet.stream_event(
        "turn_resumed",
        checkpoint_id=checkpoint_id,
        workflow_run_id=checkpoint.workflow_run_id,
        observation_count=len(observations),
        stream_key=task_response_stream_for(motet),
    )

    wf_result = motet.do(
        resume_workflow,
        data=ResumeWorkflowData(
            workflow_run_id=str(checkpoint.workflow_run_id),
            kind=kind,
            observations=observations,
        ),
    )

    # Workflow paused again — create a new turn checkpoint (facade re-handback).
    if isinstance(wf_result, dict) and (
        wf_result.get("suspended") or wf_result.get("status") == "suspended"
    ):
        from motet.core.reasoning.react.agentic_loop import _suspend_for_nested_workflow
        from motet.core.orchestration.turn.runtime.persist import materialize_intent
        from motet.core.reasoning.react.loop_intents import is_turn_intent

        resume_max_model_calls = int(checkpoint.max_model_calls or 0) or max(
            int(checkpoint.max_iterations or 20) * 3, 30
        )
        loop_data = LoopStateSnapshot.from_checkpoint(
            checkpoint,
            executed_signatures=list(checkpoint.executed_signatures),
            max_model_calls=resume_max_model_calls,
            model_calls_used=max(int(checkpoint.model_calls_used or 0), 0),
            used_tool_names=list(checkpoint.used_tool_names),
            media_accumulator=list(checkpoint.media_accumulator),
        ).to_loop_data(
            conversation_history=[
                Message.model_validate(m) if isinstance(m, dict) else m
                for m in (checkpoint.nested_resume_history or checkpoint.conversation_history or [])
            ],
            stream_key=task_response_stream_for(motet),
        )
        nested_payload = {
            "nested_workflow_suspend": True,
            "workflow_run_id": wf_result.get("workflow_run_id") or checkpoint.workflow_run_id,
            "suspend_reason": wf_result.get("suspend_reason") or kind,
            "pending_tool_calls": list(wf_result.get("pending_tool_calls") or []),
            "pending_interactions": list(wf_result.get("pending_interactions") or []),
            "nested_workflow_tool_call_id": checkpoint.nested_workflow_tool_call_id,
            "nested_workflow_tool_name": checkpoint.nested_workflow_tool_name,
        }
        result = _suspend_for_nested_workflow(
            motet,
            loop_data,
            nested_payload,
            "",
            max(int(checkpoint.max_iterations or 20) - int(checkpoint.remaining_iterations or 0), 1),
            dict(checkpoint.usage_accumulator or {}),
            list(checkpoint.media_accumulator or []),
        )
        if is_turn_intent(result):
            result = materialize_intent(motet, loop_data, result)
        if isinstance(result, dict):
            result["resumed_from_checkpoint"] = checkpoint_id
            if bound_conversation_id:
                result["conversation_id"] = bound_conversation_id
            if checkpoint.agent_id:
                result.setdefault("agent_id", checkpoint.agent_id)
        return coerce_turn_result(result)

    # Workflow completed: append workflow_* tool observation and re-enter loop.
    from motet.core.types import Message as CanonicalMessage

    history_dicts = list(checkpoint.nested_resume_history or [])
    history: List[Message] = []
    for item in history_dicts:
        if isinstance(item, Message):
            history.append(item)
        else:
            history.append(CanonicalMessage.model_validate(item))

    wf_content = format_workflow_steps(wf_result) if isinstance(wf_result, dict) else str(wf_result)
    if checkpoint.nested_workflow_tool_call_id:
        history.append(
            CanonicalMessage(
                role="tool",
                tool_call_id=checkpoint.nested_workflow_tool_call_id,
                name=checkpoint.nested_workflow_tool_name or "workflow",
                content=wf_content,
            )
        )

    resume_max_model_calls = int(checkpoint.max_model_calls or 0) or max(
        int(checkpoint.max_iterations or 20) * 3, 30
    )
    loop_data = LoopStateSnapshot.from_checkpoint(
        checkpoint,
        executed_signatures=list(checkpoint.executed_signatures),
        max_model_calls=resume_max_model_calls,
        model_calls_used=max(int(checkpoint.model_calls_used or 0), 0),
        used_tool_names=list(checkpoint.used_tool_names),
        media_accumulator=list(checkpoint.media_accumulator),
    ).to_loop_data(
        conversation_history=history,
        stream_key=task_response_stream_for(motet),
    )

    result = run_agentic_loop(motet, loop_data)
    if isinstance(result, dict):
        result["resumed_from_checkpoint"] = checkpoint_id
        result["usage_this_request"] = _usage_since(
            checkpoint.usage_accumulator, result.get("usage")
        )
        if bound_conversation_id:
            result["conversation_id"] = bound_conversation_id
        if checkpoint.agent_id:
            result.setdefault("agent_id", checkpoint.agent_id)
    return coerce_turn_result(result)


def build_resume_history(
    checkpoint: TurnCheckpoint,
    caller_history: Optional[List[Any]],
    observations: List[Dict[str, Any]],
) -> List[Message]:
    """
    Rebuild the message list a resumed turn re-enters the loop with.

    History authority (ADR-0127): a caller-supplied transcript wins; otherwise
    the checkpointed history is restored. Either way it ends with the assistant
    tool_calls message, so observations append directly after it in the recorded
    handback order.

    Shared with the orchestration-owned resume (``resume_agent_turn``) so the
    transcript it finalizes is byte-identical to the one the loop saw, without
    shipping the whole history back on the command response.

    Execute-at-resume (issue #159): Motet-owned handed-back ids have no
    client observation and are skipped here; their observations are appended
    by ``_execute_motet_owned_calls_at_resume`` before loop re-entry. The
    orchestration finalize path (``resume_agent_turn`` → ``_resume_history``)
    appends them separately via the ``motet_owned_tool_observations`` field shipped
    on the result (GitHub issue #160), so the finalized transcript is complete.
    """
    observations_by_id = _validate_observations(checkpoint, observations)
    motet_owned_ids = _motet_owned_handback_ids(checkpoint)
    if caller_history:
        history: List[Message] = [
            m if isinstance(m, Message) else Message.model_validate(m)
            for m in caller_history
        ]
    else:
        history = [
            Message.model_validate(m) for m in (checkpoint.conversation_history or [])
        ]
    if not history:
        raise ValueError(
            f"resume_turn: checkpoint '{checkpoint.checkpoint_id}' has no conversation "
            "history and none was supplied"
        )

    for call in checkpoint.handed_back_tool_calls:
        call_id = str(call.get("tool_call_id") or "")
        if call_id in motet_owned_ids:
            continue
        obs = observations_by_id[call_id]
        history.append(
            Message(
                role="tool",
                tool_call_id=call_id,
                name=str(call.get("tool_name") or ""),
                content=_observation_content(obs),
            )
        )
    return history


def _usage_since(
    baseline: Optional[Dict[str, Any]],
    total: Any,
) -> Optional[Dict[str, int]]:
    """Usage consumed by this invocation: the turn accumulator minus its value
    at resume entry.

    Clamped at zero per key: a checkpoint written by an older build can carry
    keys the current accumulator lacks, and a negative token count is worse
    than a slightly generous one.
    """
    if not isinstance(total, dict):
        return None
    base = baseline or {}
    return {
        key: max(int(value or 0) - int(base.get(key) or 0), 0)
        for key, value in total.items()
        if isinstance(value, (int, float))
    }


__all__ = ["ResumeTurnData", "build_resume_history", "resume_turn"]
