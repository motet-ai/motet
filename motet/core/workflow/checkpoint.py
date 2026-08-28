"""
Motet - Workflow Checkpoint Store

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Redis-backed checkpoint store for paused workflow runs.
    When the workflow executor hits a step that needs an external party (client
    handback tool, elicitation, confirmation, or OAuth), it persists graph cursor,
    step results, and pending interactions here, sets WorkflowStatus.PAUSED, and
    returns from the Celery task. resume_workflow later loads the checkpoint,
    validates the resume payload against recorded interaction ids, and continues
    on (possibly) a different worker.

    Owns graph truth; when nested under an agent turn, TurnCheckpoint stores only
    a workflow_run_id pointer. Reuses
    invariants: recorded pending ids, principal re-auth, TTL, non-consuming reads,
    no held workers.

    Run lifecycle (added 2026-08-06): a checkpoint carries ``status`` plus a
    monotonic ``resume_epoch``. ``claim_workflow_run_for_resume`` performs the
    paused → running transition under a distributed lock, so a replayed resume
    payload cannot re-run side-effecting steps a second time; the executor then
    persists progress per completed level and a terminal ``completed`` /
    ``failed`` / ``cancelled`` record. Paused runs are indexed in a per-tenant
    sorted set so operators (and the workflows API) can enumerate what is
    waiting without knowing run ids.

    Operator pause/cancel: ``request_workflow_run_control`` writes
    ``workflow_control:{tenant}:{motet}:{run_id}`` for running runs (honored
    between levels) or immediately marks paused runs ``cancelled``. Operator
    pause uses ``suspend_reason=operator``; resume with ``kind=operator``.

    sticky control also LPUSH-wakes per-waiter cancel lists
    (same hash-tagged ``{waiter}:wake:cancel`` as task cancel) so mid-level
    ``WorkerCommunicator`` waits unwind on workflow-only cancel. Cancel also
    writes the shared ``task:control:{workflow_run_id}`` scope key so honor
    points check one variadic EXISTS. A ``workflow_runs:by_task:{task_id}``
    index supports task cancel → durable workflow cancel+wake bridging.

Dependencies:
    - motet.core.distributed.redis_manager: Centralized Redis operations and
      DistributedLock for the resume claim (paused → running is read-check-write)
    - pydantic: WorkflowCheckpoint model validation/serialization
    - structlog: Structured logging

Usage:
    from motet.core.workflow.checkpoint import (
        WorkflowCheckpoint, WorkflowSuspendReason, PendingInteraction,
        store_workflow_checkpoint, load_workflow_checkpoint,
        claim_workflow_run_for_resume, list_paused_workflow_runs,
    )

    cp = WorkflowCheckpoint(
        workflow_id="demo", tenant_id="t1", principal_id="u1",
        completed_step_ids=["a"], context={"a": {...}},
        suspend_reason=WorkflowSuspendReason.HANDBACK_TOOLS,
        pending_interactions=[PendingInteraction(...)],
    )
    store_workflow_checkpoint(cp)

    # Resume path: claim first so a duplicate payload cannot double-execute.
    claimed = claim_workflow_run_for_resume(
        tenant_id="t1", motet_id="default", workflow_run_id=cp.workflow_run_id
    )

Notes:
    - Reads are non-consuming (retry-safe); the *claim* is what makes resume
      single-shot, so callers must claim before applying a resume payload.
    - Keys are scoped by tenant/motet; principal re-authorization is in resume_workflow.
    - store_workflow_checkpoint raises on failure — a pause without a durable
      checkpoint cannot be resumed. store_workflow_progress is best-effort by
      design: a bookkeeping write must not fail an otherwise healthy run.
    - Interaction index entries exist only while a run is paused; they are dropped
      on claim so an answered tool_call_id cannot resolve to a run again.
"""

from __future__ import annotations

import os
import time
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, cast
from uuid import uuid4

import structlog
from pydantic import BaseModel, ConfigDict, Field, model_validator

from motet.core.checkpoints.redis_store import (
    clear_id_index,
    flatten_nested_blob,
    load_json_blob,
    lookup_id_index,
    scoped_key,
    store_json_blob,
    to_nested_blob,
    write_id_index,
)

logger = structlog.get_logger(__name__)

_SERVICE = "workflow_checkpoint"

WORKFLOW_CHECKPOINT_TTL_SECONDS = int(
    os.getenv("MOTET_WORKFLOW_CHECKPOINT_TTL_SECONDS", str(24 * 3600))
)

WORKFLOW_CHECKPOINT_SCHEMA_VERSION = 1
WORKFLOW_CANCELLED_CODE = "workflow_cancelled"
_BY_TASK_MEMBER_SEP = "\x1f"

_IDENTITY_FIELDS = (
    "motet_id",
    "tenant_id",
    "principal_id",
    "task_id",
    "conversation_id",
    "workflow_id",
    "workflow_name",
)
_RUN_STATE_FIELDS = (
    "completed_step_ids",
    "pending_step_ids",
    "context",
    "step_results",
    "workflow_steps",
    "execution_order",
    "handback_tools",
    "required_inputs",
    "input_parameters",
    "output_field",
    "presentation",
    "use_for",
    "description",
)
_PENDING_FIELDS = (
    "suspend_reason",
    "pending_interactions",
)
_LIFECYCLE_FIELDS = (
    "status",
    "resume_epoch",
    "updated_at",
    "last_error",
    "child_workflow_run_id",
    "blocked_step_id",
    "parent_workflow_run_id",
)

_STORAGE_SECTIONS = (
    (_IDENTITY_FIELDS, "identity"),
    (_RUN_STATE_FIELDS, "run_state"),
    (_PENDING_FIELDS, "pending"),
    (_LIFECYCLE_FIELDS, "lifecycle"),
)

WORKFLOW_RESUME_LOCK_TTL_SECONDS = int(
    os.getenv("MOTET_WORKFLOW_RESUME_LOCK_TTL_SECONDS", "60")
)

WORKFLOW_MAX_NESTING_DEPTH = int(os.getenv("MOTET_WORKFLOW_MAX_NESTING_DEPTH", "5"))


class WorkflowSuspendReason(str, Enum):
    """Why a workflow run paused (orthogonal to step ownership)."""

    NONE = "none"
    HANDBACK_TOOLS = "handback_tools"
    ELICITATION = "elicitation"
    CONFIRMATION = "confirmation"
    OAUTH = "oauth"
    OPERATOR = "operator"  # Operator pause of a running run (no pending interaction)


class WorkflowRunStatus(str, Enum):
    """Lifecycle of a checkpointed workflow run."""

    PAUSED = "paused"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_WORKFLOW_RUN_STATUSES = (
    WorkflowRunStatus.COMPLETED,
    WorkflowRunStatus.FAILED,
    WorkflowRunStatus.CANCELLED,
)


class WorkflowControlAction(str, Enum):
    """Operator control signal for a running workflow run."""

    PAUSE = "pause"
    CANCEL = "cancel"


class WorkflowResumeConflict(RuntimeError):
    """A resume was attempted against a run that is not waiting for one.

    Raised when the stored run is already running (another worker claimed it),
    already terminal (the payload is a replay), or at a different resume epoch.
    Callers should treat this as "the work is done or in flight", not as a
    transient failure to retry — re-running the tail would duplicate side effects.
    """


class WorkflowRunControlConflict(RuntimeError):
    """Operator pause/cancel rejected because the run is already terminal."""


class WorkflowSuspendNotConsumable(RuntimeError):
    """A workflow paused for a reason the calling surface cannot satisfy.

    The agent path can only carry ``handback_tools`` suspends, because those map
    onto tool calls the client already declared. Elicitation, confirmation, and
    OAuth pauses need a surface that can render a form, an approve/reject card,
    or an auth redirect; when no such consumer declared itself, the turn fails
    with this error and the run stays resumable through ``resume_workflow``.
    """


class PendingInteraction(BaseModel):
    """One recorded external interaction the resume payload must satisfy."""

    model_config = ConfigDict(populate_by_name=True)

    interaction_id: str = Field(
        ...,
        description="Stable id (tool_call_id for handback; generated for other kinds).",
    )
    kind: WorkflowSuspendReason = Field(
        default=WorkflowSuspendReason.HANDBACK_TOOLS,
        description="Suspend reason for this interaction.",
    )
    step_id: str = Field(..., description="Workflow step awaiting this interaction.")
    tool_name: Optional[str] = Field(
        default=None,
        description="Client/Motet tool name when kind is handback_tools or confirmation.",
    )
    parameters: Dict[str, Any] = Field(
        default_factory=dict,
        description="Tool parameters or staged action payload.",
    )
    interaction_schema: Optional[Dict[str, Any]] = Field(
        default=None,
        description="JSON schema for elicitation answers.",
        validation_alias="schema",
        serialization_alias="schema",
    )
    prompt: Optional[str] = Field(
        default=None,
        description="Human-readable prompt for elicitation/confirmation.",
    )
    auth_challenge: Optional[Dict[str, Any]] = Field(
        default=None,
        description="OAuth auth_required challenge payload.",
    )


class WorkflowCheckpoint(BaseModel):
    """Graph cursor + pending interactions for a paused workflow run."""

    schema_version: int = Field(default=WORKFLOW_CHECKPOINT_SCHEMA_VERSION)
    workflow_run_id: str = Field(default_factory=lambda: f"wfrun-{uuid4().hex}")
    created_at: float = Field(default_factory=time.time)

    motet_id: str = Field(default="default")
    tenant_id: Optional[str] = None
    principal_id: Optional[str] = None
    task_id: Optional[str] = None
    conversation_id: Optional[str] = None

    workflow_id: str = ""
    workflow_name: str = ""
    description: str = ""
    required_inputs: Optional[List[str]] = None
    input_parameters: Optional[Dict[str, Dict[str, Any]]] = None
    output_field: Optional[str] = None
    presentation: Optional[Dict[str, Any]] = None
    use_for: Optional[List[str]] = None
    handback_tools: Optional[List[Dict[str, Any]]] = None

    # Graph definition snapshot so resume works even if registry changed.
    workflow_steps: List[Dict[str, Any]] = Field(default_factory=list)
    execution_order: List[List[str]] = Field(default_factory=list)

    completed_step_ids: List[str] = Field(default_factory=list)
    pending_step_ids: List[str] = Field(
        default_factory=list,
        description="Steps in the current level awaiting external interaction or Motet run after resume.",
    )
    context: Dict[str, Any] = Field(default_factory=dict)
    step_results: Dict[str, Any] = Field(default_factory=dict)

    suspend_reason: WorkflowSuspendReason = WorkflowSuspendReason.NONE
    pending_interactions: List[PendingInteraction] = Field(default_factory=list)

    status: WorkflowRunStatus = Field(
        default=WorkflowRunStatus.PAUSED,
        description=(
            "Run lifecycle: paused (awaiting resume), running, completed, "
            "failed, cancelled."
        ),
    )
    resume_epoch: int = Field(
        default=0,
        description="Incremented on each successful resume claim; guards replayed payloads.",
    )
    updated_at: float = Field(
        default_factory=time.time,
        description="Last write time — also the paused-run index score.",
    )
    last_error: Optional[str] = Field(
        default=None,
        description="Failure message when status is failed.",
    )
    # Nested workflow stack (issue #189): parent waits on child frame.
    child_workflow_run_id: Optional[str] = Field(
        default=None,
        description="When paused awaiting a nested child, the child's workflow_run_id.",
    )
    blocked_step_id: Optional[str] = Field(
        default=None,
        description="Parent step id blocked on child_workflow_run_id.",
    )
    parent_workflow_run_id: Optional[str] = Field(
        default=None,
        description="When this run is a nested child, the parent's workflow_run_id.",
    )

    model_config = ConfigDict(arbitrary_types_allowed=True)

    @model_validator(mode="before")
    @classmethod
    def _accept_nested_storage(cls, data: Any) -> Any:
        if isinstance(data, dict):
            return _flatten_storage_blob(data)
        return data

    def to_storage_dict(self) -> Dict[str, Any]:
        flat = self.model_dump(mode="json")
        return to_nested_blob(
            flat,
            sections=_STORAGE_SECTIONS,
            id_field="workflow_run_id",
            schema_version=WORKFLOW_CHECKPOINT_SCHEMA_VERSION,
        )

    def is_terminal(self) -> bool:
        """True when the run finished and must not be resumed again."""
        return self.status in TERMINAL_WORKFLOW_RUN_STATUSES

    def interaction_ids(self) -> List[str]:
        """Recorded pending interaction ids (index keys for this run)."""
        return [
            str(item.interaction_id)
            for item in self.pending_interactions
            if item.interaction_id
        ]

    def summary(self) -> Dict[str, Any]:
        """Operator-facing view: what is waiting, on which step, and why.

        Excludes ``context`` / ``step_results`` — those carry full step payloads
        (agent transcripts, research text) and would make a list endpoint huge.
        """
        return {
            "workflow_run_id": self.workflow_run_id,
            "workflow_id": self.workflow_id,
            "workflow_name": self.workflow_name,
            "status": self.status.value
            if isinstance(self.status, WorkflowRunStatus)
            else str(self.status),
            "suspend_reason": self.suspend_reason.value
            if isinstance(self.suspend_reason, WorkflowSuspendReason)
            else str(self.suspend_reason),
            "resume_epoch": self.resume_epoch,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "conversation_id": self.conversation_id,
            "task_id": self.task_id,
            "principal_id": self.principal_id,
            "completed_step_ids": list(self.completed_step_ids),
            "pending_step_ids": list(self.pending_step_ids),
            "last_error": self.last_error,
            "pending_interactions": [
                item.model_dump(mode="json", by_alias=True)
                for item in self.pending_interactions
            ],
            "child_workflow_run_id": self.child_workflow_run_id,
            "blocked_step_id": self.blocked_step_id,
            "parent_workflow_run_id": self.parent_workflow_run_id,
        }

    def pending_tool_calls(self) -> List[Dict[str, Any]]:
        """OpenAI-shaped pending tool_calls for handback_tools suspends."""
        calls: List[Dict[str, Any]] = []
        for item in self.pending_interactions:
            if item.kind != WorkflowSuspendReason.HANDBACK_TOOLS:
                continue
            calls.append(
                {
                    "tool_call_id": item.interaction_id,
                    "tool_name": item.tool_name or "",
                    "parameters": dict(item.parameters or {}),
                }
            )
        return calls


def _flatten_storage_blob(data: Dict[str, Any]) -> Dict[str, Any]:
    return flatten_nested_blob(
        data,
        sections=_STORAGE_SECTIONS,
        id_field="workflow_run_id",
        schema_version=WORKFLOW_CHECKPOINT_SCHEMA_VERSION,
    )


def _checkpoint_key(tenant_id: Optional[str], motet_id: Optional[str], workflow_run_id: str) -> str:
    return scoped_key("workflow_checkpoint", tenant_id, motet_id, workflow_run_id)


def _interaction_index_key(
    tenant_id: Optional[str], motet_id: Optional[str], interaction_id: str
) -> str:
    return scoped_key("workflow_checkpoint:index", tenant_id, motet_id, interaction_id)


def _paused_index_key(tenant_id: Optional[str], motet_id: Optional[str]) -> str:
    return f"workflow_checkpoint:paused:{tenant_id or 'global'}:{motet_id or 'default'}"


def _resume_lock_key(workflow_run_id: str) -> str:
    return f"lock:workflow_resume:{workflow_run_id}"


def _control_key(
    tenant_id: Optional[str], motet_id: Optional[str], workflow_run_id: str
) -> str:
    return scoped_key("workflow_control", tenant_id, motet_id, workflow_run_id)


def _by_task_index_key(task_id: str) -> str:
    return f"workflow_runs:by_task:{task_id}"


def _encode_by_task_member(
    tenant_id: Optional[str], motet_id: Optional[str], workflow_run_id: str
) -> str:
    tenant = (tenant_id or "").strip() or "global"
    motet = (motet_id or "").strip() or "default"
    return f"{tenant}{_BY_TASK_MEMBER_SEP}{motet}{_BY_TASK_MEMBER_SEP}{workflow_run_id}"


def _decode_by_task_member(
    raw: Any,
) -> Optional[Tuple[Optional[str], str, str]]:
    text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
    parts = text.split(_BY_TASK_MEMBER_SEP)
    if len(parts) != 3 or not parts[2]:
        return None
    tenant = None if parts[0] in ("", "global") else parts[0]
    motet = parts[1] or "default"
    return tenant, motet, parts[2]


def _status_value(checkpoint: WorkflowCheckpoint) -> str:
    return (
        checkpoint.status.value
        if isinstance(checkpoint.status, WorkflowRunStatus)
        else str(checkpoint.status)
    )


def store_workflow_checkpoint(checkpoint: WorkflowCheckpoint) -> str:
    """Persist a workflow run checkpoint and reconcile its indexes.

    Index entries and the paused-run set track ``status``: a paused run is
    discoverable by interaction id and enumerable per tenant, while a running or
    terminal run is neither (an answered interaction id must not resolve to a run
    that already consumed it).
    """
    from motet.core.distributed.redis_manager import get_sync_redis_client

    checkpoint.updated_at = time.time()
    is_paused = _status_value(checkpoint) == WorkflowRunStatus.PAUSED.value
    key = _checkpoint_key(
        checkpoint.tenant_id, checkpoint.motet_id, checkpoint.workflow_run_id
    )
    paused_key = _paused_index_key(checkpoint.tenant_id, checkpoint.motet_id)
    store_json_blob(
        _SERVICE,
        key,
        checkpoint.to_storage_dict(),
        WORKFLOW_CHECKPOINT_TTL_SECONDS,
        error_label="workflow_checkpoint",
    )
    try:
        client = get_sync_redis_client(_SERVICE)
        if is_paused:
            for interaction_id in checkpoint.interaction_ids():
                write_id_index(
                    _SERVICE,
                    _interaction_index_key(
                        checkpoint.tenant_id, checkpoint.motet_id, interaction_id
                    ),
                    target_field="workflow_run_id",
                    target_id=checkpoint.workflow_run_id,
                    ttl_seconds=WORKFLOW_CHECKPOINT_TTL_SECONDS,
                )
            client.zadd(
                paused_key, {checkpoint.workflow_run_id: checkpoint.updated_at}
            )
        else:
            client.zrem(paused_key, checkpoint.workflow_run_id)
        _reconcile_by_task_index(client, checkpoint)
    except Exception as e:
        logger.error(
            "workflow_checkpoint_index_reconcile_failed",
            workflow_run_id=checkpoint.workflow_run_id,
            workflow_id=checkpoint.workflow_id,
            status=_status_value(checkpoint),
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
        raise RuntimeError(
            f"failed to reconcile workflow checkpoint indexes: {e}"
        ) from e

    logger.info(
        "workflow_checkpoint_stored",
        workflow_run_id=checkpoint.workflow_run_id,
        workflow_id=checkpoint.workflow_id,
        status=_status_value(checkpoint),
        resume_epoch=checkpoint.resume_epoch,
        suspend_reason=checkpoint.suspend_reason.value
        if isinstance(checkpoint.suspend_reason, WorkflowSuspendReason)
        else checkpoint.suspend_reason,
        pending_count=len(checkpoint.pending_interactions),
        completed_count=len(checkpoint.completed_step_ids),
        ttl_seconds=WORKFLOW_CHECKPOINT_TTL_SECONDS,
    )
    return checkpoint.workflow_run_id


def store_workflow_progress(checkpoint: WorkflowCheckpoint) -> bool:
    """Best-effort progress write for a running (not paused) run.

    Deliberately does not raise: progress records exist so a crashed or retried
    run can skip completed steps, and losing that bookkeeping is strictly better
    than failing a workflow whose steps all succeeded. Pause writes still go
    through ``store_workflow_checkpoint``, which does raise — a pause with no
    durable record is unresumable and must be loud.
    """
    try:
        store_workflow_checkpoint(checkpoint)
        return True
    except Exception as e:
        logger.error(
            "workflow_progress_checkpoint_failed",
            workflow_run_id=checkpoint.workflow_run_id,
            workflow_id=checkpoint.workflow_id,
            status=_status_value(checkpoint),
            completed_count=len(checkpoint.completed_step_ids),
            error=str(e),
            error_type=type(e).__name__,
        )
        return False


def clear_workflow_interaction_index(
    *,
    tenant_id: Optional[str],
    motet_id: Optional[str],
    interaction_ids: List[str],
) -> None:
    """Drop index entries for interactions that have been answered."""
    ids = [str(i).strip() for i in interaction_ids if str(i or "").strip()]
    if not ids:
        return
    clear_id_index(
        _SERVICE,
        [_interaction_index_key(tenant_id, motet_id, i) for i in ids],
        error_label="workflow_interaction_index",
    )


def claim_workflow_run_for_resume(
    *,
    tenant_id: Optional[str],
    motet_id: Optional[str],
    workflow_run_id: str,
    expected_epoch: Optional[int] = None,
) -> Optional[WorkflowCheckpoint]:
    """Transition a paused run to running so exactly one resume proceeds.

    Returns the freshly loaded checkpoint (status ``running``, epoch incremented)
    on success, or ``None`` when no stored record exists — an in-process executor
    call with a hand-built checkpoint is legitimate and stays unguarded. Raises
    ``WorkflowResumeConflict`` when a stored run is already running, terminal, or
    at a different epoch, which is what makes a replayed resume payload safe.

    The read-check-write runs under a DistributedLock: two clients answering the
    same handback simultaneously would otherwise both observe ``paused``.
    """
    from motet.core.distributed.redis_manager import acquire_distributed_lock_sync

    lock = acquire_distributed_lock_sync(
        _SERVICE,
        _resume_lock_key(workflow_run_id),
        ttl_seconds=WORKFLOW_RESUME_LOCK_TTL_SECONDS,
    )
    if not lock:
        raise WorkflowResumeConflict(
            f"workflow run '{workflow_run_id}' is being resumed by another worker"
        )
    try:
        checkpoint = load_workflow_checkpoint(
            tenant_id=tenant_id, motet_id=motet_id, workflow_run_id=workflow_run_id
        )
        if checkpoint is None:
            logger.debug(
                "workflow_resume_claim_no_stored_record",
                workflow_run_id=workflow_run_id,
            )
            return None

        status = _status_value(checkpoint)
        if status != WorkflowRunStatus.PAUSED.value:
            raise WorkflowResumeConflict(
                f"workflow run '{workflow_run_id}' is not awaiting resume "
                f"(status={status}); a duplicate resume payload was rejected to "
                "avoid re-running completed steps"
            )
        if expected_epoch is not None and int(expected_epoch) != int(
            checkpoint.resume_epoch
        ):
            raise WorkflowResumeConflict(
                f"workflow run '{workflow_run_id}' is at resume epoch "
                f"{checkpoint.resume_epoch}, payload targets {expected_epoch}"
            )

        answered_ids = checkpoint.interaction_ids()
        checkpoint.status = WorkflowRunStatus.RUNNING
        checkpoint.resume_epoch = int(checkpoint.resume_epoch) + 1
        store_workflow_checkpoint(checkpoint)
        clear_workflow_interaction_index(
            tenant_id=tenant_id, motet_id=motet_id, interaction_ids=answered_ids
        )
        logger.info(
            "workflow_resume_claimed",
            workflow_run_id=workflow_run_id,
            workflow_id=checkpoint.workflow_id,
            resume_epoch=checkpoint.resume_epoch,
        )
        return checkpoint
    finally:
        try:
            lock.release_sync()
        except Exception as e:
            logger.warning(
                "workflow_resume_lock_release_failed",
                workflow_run_id=workflow_run_id,
                error=str(e),
                error_type=type(e).__name__,
            )


def release_workflow_run_to_paused(checkpoint: WorkflowCheckpoint) -> None:
    """Return a claimed run to ``paused`` after a rejected resume payload.

    A malformed payload (missing observation, wrong decision value) must not
    strand the run: the interactions are still outstanding, so re-publish them
    and let a corrected payload through. The epoch stays incremented, so the
    rejected payload itself cannot be replayed.
    """
    checkpoint.status = WorkflowRunStatus.PAUSED
    try:
        store_workflow_checkpoint(checkpoint)
    except Exception as e:
        logger.error(
            "workflow_resume_release_failed",
            workflow_run_id=checkpoint.workflow_run_id,
            error=str(e),
            error_type=type(e).__name__,
        )


def _reconcile_by_task_index(client: Any, checkpoint: WorkflowCheckpoint) -> None:
    """Maintain ``workflow_runs:by_task:{task_id}`` for ADR-0131 task→workflow bridge."""
    task_id = (checkpoint.task_id or "").strip()
    if not task_id or not checkpoint.workflow_run_id:
        return
    key = _by_task_index_key(task_id)
    member = _encode_by_task_member(
        checkpoint.tenant_id, checkpoint.motet_id, checkpoint.workflow_run_id
    )
    status = _status_value(checkpoint)
    if status in {s.value for s in TERMINAL_WORKFLOW_RUN_STATUSES}:
        client.srem(key, member)
        return
    client.sadd(key, member)
    client.expire(key, WORKFLOW_CHECKPOINT_TTL_SECONDS)


def list_workflow_runs_for_task(task_id: Optional[str]) -> List[Dict[str, str]]:
    """Return ``[{tenant_id, motet_id, workflow_run_id}, ...]`` for a Motet task."""
    tid = (task_id or "").strip()
    if not tid:
        return []
    try:
        from motet.core.distributed.redis_manager import get_sync_redis_client

        client = get_sync_redis_client(_SERVICE)
        members = client.smembers(_by_task_index_key(tid)) or set()
        if not isinstance(members, (set, list, tuple, frozenset)):
            return []
    except Exception as e:
        logger.warning(
            "workflow_by_task_list_failed",
            task_id=tid,
            error=str(e),
            error_type=type(e).__name__,
        )
        return []

    out: List[Dict[str, str]] = []
    for raw in members:
        decoded = _decode_by_task_member(raw)
        if decoded is None:
            continue
        tenant, motet, run_id = decoded
        out.append(
            {
                "tenant_id": tenant or "",
                "motet_id": motet,
                "workflow_run_id": run_id,
            }
        )
    return out


def register_workflow_control_waiter(
    *,
    tenant_id: Optional[str],
    motet_id: Optional[str],
    workflow_run_id: Optional[str],
    waiter_id: str,
) -> None:
    """Register a parked parent so workflow cancel/pause can LPUSH its wake key.

    Thin wrapper around the shared task-control waiter registry (ADR-0131
    ``cancel_scopes``). ``tenant_id`` / ``motet_id`` are unused; kept for
    call-site compatibility.
    """
    del tenant_id, motet_id
    run_id = (workflow_run_id or "").strip()
    wid = (waiter_id or "").strip()
    if not run_id or not wid:
        return
    from motet.core.distributed.task_control import register_command_waiter

    register_command_waiter(run_id, wid)


def unregister_workflow_control_waiter(
    *,
    tenant_id: Optional[str],
    motet_id: Optional[str],
    workflow_run_id: Optional[str],
    waiter_id: str,
) -> None:
    """Drop workflow waiter registration (does not delete shared wake lists)."""
    del tenant_id, motet_id
    run_id = (workflow_run_id or "").strip()
    wid = (waiter_id or "").strip()
    if not run_id or not wid:
        return
    from motet.core.distributed.task_control import unregister_command_waiter

    unregister_command_waiter(run_id, wid)


def _wake_workflow_control_waiters(
    *,
    tenant_id: Optional[str],
    motet_id: Optional[str],
    workflow_run_id: str,
    action: str,
) -> int:
    """LPUSH wake onto each registered waiter's cancel list. Returns count."""
    del tenant_id, motet_id, action
    from motet.core.distributed.task_control import _wake_registered_cancel_waiters

    return _wake_registered_cancel_waiters(workflow_run_id)


def is_workflow_cancelled(
    *,
    tenant_id: Optional[str],
    motet_id: Optional[str],
    workflow_run_id: Optional[str],
) -> bool:
    """Cheap sticky check for workflow cancel (empty run id → False)."""
    run_id = (workflow_run_id or "").strip()
    if not run_id:
        return False
    try:
        from motet.core.distributed.redis_manager import get_sync_redis_client

        client = get_sync_redis_client(_SERVICE)
        key = _control_key(tenant_id, motet_id, run_id)
        if not client.exists(key):
            return False
        payload = peek_workflow_run_control(
            tenant_id=tenant_id, motet_id=motet_id, workflow_run_id=run_id
        )
        if not payload:
            return True
        return str(payload.get("action") or "") == WorkflowControlAction.CANCEL.value
    except Exception as e:
        logger.warning(
            "workflow_control_is_cancelled_failed",
            workflow_run_id=run_id,
            error=str(e),
            error_type=type(e).__name__,
        )
        return False


def peek_workflow_run_control(
    *,
    tenant_id: Optional[str],
    motet_id: Optional[str],
    workflow_run_id: str,
) -> Optional[Dict[str, Any]]:
    """Non-consuming read of an operator pause/cancel signal."""
    if not workflow_run_id:
        return None
    return load_json_blob(
        _SERVICE,
        _control_key(tenant_id, motet_id, workflow_run_id),
        error_label="workflow_control",
    )


def clear_workflow_run_control(
    *,
    tenant_id: Optional[str],
    motet_id: Optional[str],
    workflow_run_id: str,
) -> None:
    """Drop a control signal after the executor honors it (best-effort)."""
    if not workflow_run_id:
        return
    try:
        from motet.core.distributed.redis_manager import get_sync_redis_client

        client = get_sync_redis_client(_SERVICE)
        client.delete(_control_key(tenant_id, motet_id, workflow_run_id))
    except Exception as e:
        logger.warning(
            "workflow_control_clear_failed",
            workflow_run_id=workflow_run_id,
            error=str(e),
            error_type=type(e).__name__,
        )


def _write_workflow_run_control(
    *,
    tenant_id: Optional[str],
    motet_id: Optional[str],
    workflow_run_id: str,
    action: WorkflowControlAction,
    principal_id: Optional[str],
    reason: Optional[str],
) -> Dict[str, Any]:
    """Persist a control signal. Cancel wins over a prior pause request."""
    existing = peek_workflow_run_control(
        tenant_id=tenant_id, motet_id=motet_id, workflow_run_id=workflow_run_id
    )
    if (
        existing
        and str(existing.get("action")) == WorkflowControlAction.CANCEL.value
        and action == WorkflowControlAction.PAUSE
    ):
        return existing

    payload: Dict[str, Any] = {
        "action": action.value,
        "requested_at": time.time(),
        "requested_by": principal_id,
        "reason": reason,
    }
    store_json_blob(
        _SERVICE,
        _control_key(tenant_id, motet_id, workflow_run_id),
        payload,
        WORKFLOW_CHECKPOINT_TTL_SECONDS,
        error_label="workflow_control",
    )
    woken = _wake_workflow_control_waiters(
        tenant_id=tenant_id,
        motet_id=motet_id,
        workflow_run_id=workflow_run_id,
        action=action.value,
    )
    if woken:
        logger.info(
            "workflow_control_waiters_woken",
            workflow_run_id=workflow_run_id,
            action=action.value,
            waiters_woken=woken,
        )
    if action == WorkflowControlAction.CANCEL:
        try:
            from motet.core.distributed.task_control import request_scope_cancel

            request_scope_cancel(
                workflow_run_id,
                reason=reason,
                principal_id=principal_id,
                source="workflow_control",
                tenant_id=tenant_id,
            )
        except Exception as e:
            logger.warning(
                "workflow_control_scope_cancel_failed",
                workflow_run_id=workflow_run_id,
                error=str(e),
                error_type=type(e).__name__,
            )
    return payload


def _mark_run_cancelled(checkpoint: WorkflowCheckpoint) -> WorkflowCheckpoint:
    """Persist terminal cancelled status and drop indexes / control signal."""
    answered_ids = checkpoint.interaction_ids()
    checkpoint.status = WorkflowRunStatus.CANCELLED
    checkpoint.suspend_reason = WorkflowSuspendReason.NONE
    checkpoint.pending_interactions = []
    checkpoint.child_workflow_run_id = None
    checkpoint.blocked_step_id = None
    store_workflow_checkpoint(checkpoint)
    clear_workflow_interaction_index(
        tenant_id=checkpoint.tenant_id,
        motet_id=checkpoint.motet_id,
        interaction_ids=answered_ids,
    )
    clear_workflow_run_control(
        tenant_id=checkpoint.tenant_id,
        motet_id=checkpoint.motet_id,
        workflow_run_id=checkpoint.workflow_run_id,
    )
    return checkpoint


def request_workflow_run_control(
    *,
    tenant_id: Optional[str],
    motet_id: Optional[str],
    workflow_run_id: str,
    action: str,
    principal_id: Optional[str] = None,
    reason: Optional[str] = None,
    _cascade_depth: int = 0,
) -> Dict[str, Any]:
    """Operator pause or cancel for a checkpointed workflow run.

    * **Paused + cancel** — terminal ``cancelled`` immediately (under the resume
      lock so it cannot race a claim).
    * **Running + cancel/pause** — Redis control signal + per-waiter LPUSH wake
      (ADR-0131); the executor honors at the next level boundary, and parked
      ``WorkerCommunicator`` waiters unwind on cancel (cooperative; in-flight
      leaf work may finish its current unit).
    * **Paused + pause** — no-op success (already paused).
    * Nesting: cancel/pause cascades to ``child_workflow_run_id``; cancel of a
      leaf also cancels a parent that is blocked on that child.
    """
    from motet.core.distributed.redis_manager import acquire_distributed_lock_sync

    if _cascade_depth > WORKFLOW_MAX_NESTING_DEPTH + 2:
        raise RuntimeError(
            f"workflow run control cascade exceeded depth for '{workflow_run_id}'"
        )

    action_norm = (action or "").strip().lower()
    try:
        control_action = WorkflowControlAction(action_norm)
    except ValueError as e:
        raise ValueError(
            f"workflow run control action must be 'pause' or 'cancel', got '{action}'"
        ) from e

    if not workflow_run_id:
        raise ValueError("workflow_run_id is required")

    checkpoint = load_workflow_checkpoint(
        tenant_id=tenant_id, motet_id=motet_id, workflow_run_id=workflow_run_id
    )
    if checkpoint is None:
        raise ValueError(
            f"workflow run '{workflow_run_id}' not found or expired"
        )

    status = _status_value(checkpoint)
    if status == WorkflowRunStatus.CANCELLED.value:
        return {
            "status": "cancelled",
            "workflow_run_id": workflow_run_id,
            "action": control_action.value,
            "applied": True,
            "already_terminal": True,
        }
    if status in (
        WorkflowRunStatus.COMPLETED.value,
        WorkflowRunStatus.FAILED.value,
    ):
        raise WorkflowRunControlConflict(
            f"workflow run '{workflow_run_id}' is terminal (status={status}); "
            f"cannot {control_action.value}"
        )

    # Cascade to nested child first so the leaf stops before the parent frame.
    child_id = str(getattr(checkpoint, "child_workflow_run_id", None) or "").strip()
    child_result: Optional[Dict[str, Any]] = None
    if child_id:
        try:
            child_result = request_workflow_run_control(
                tenant_id=tenant_id,
                motet_id=motet_id,
                workflow_run_id=child_id,
                action=control_action.value,
                principal_id=principal_id,
                reason=reason,
                _cascade_depth=_cascade_depth + 1,
            )
        except (ValueError, WorkflowRunControlConflict) as e:
            logger.info(
                "workflow_control_child_cascade_skipped",
                parent_run_id=workflow_run_id,
                child_run_id=child_id,
                error=str(e),
            )

    if control_action == WorkflowControlAction.PAUSE:
        if status == WorkflowRunStatus.PAUSED.value:
            return {
                "status": "paused",
                "workflow_run_id": workflow_run_id,
                "action": "pause",
                "applied": True,
                "already_paused": True,
                "child": child_result,
            }
        # running → cooperative pause signal
        _write_workflow_run_control(
            tenant_id=tenant_id,
            motet_id=motet_id,
            workflow_run_id=workflow_run_id,
            action=WorkflowControlAction.PAUSE,
            principal_id=principal_id,
            reason=reason,
        )
        logger.info(
            "workflow_pause_requested",
            workflow_run_id=workflow_run_id,
            principal_id=principal_id,
        )
        return {
            "status": "pause_requested",
            "workflow_run_id": workflow_run_id,
            "action": "pause",
            "applied": False,
            "child": child_result,
        }

    # CANCEL
    if status == WorkflowRunStatus.PAUSED.value:
        lock = acquire_distributed_lock_sync(
            _SERVICE,
            _resume_lock_key(workflow_run_id),
            ttl_seconds=WORKFLOW_RESUME_LOCK_TTL_SECONDS,
        )
        if not lock:
            raise WorkflowRunControlConflict(
                f"workflow run '{workflow_run_id}' is being resumed; retry cancel"
            )
        try:
            fresh = load_workflow_checkpoint(
                tenant_id=tenant_id,
                motet_id=motet_id,
                workflow_run_id=workflow_run_id,
            )
            if fresh is None:
                raise ValueError(
                    f"workflow run '{workflow_run_id}' not found or expired"
                )
            fresh_status = _status_value(fresh)
            if fresh_status == WorkflowRunStatus.CANCELLED.value:
                result = {
                    "status": "cancelled",
                    "workflow_run_id": workflow_run_id,
                    "action": "cancel",
                    "applied": True,
                    "already_terminal": True,
                    "child": child_result,
                }
            elif fresh_status != WorkflowRunStatus.PAUSED.value:
                # Claimed between our load and lock — fall through to signal.
                _write_workflow_run_control(
                    tenant_id=tenant_id,
                    motet_id=motet_id,
                    workflow_run_id=workflow_run_id,
                    action=WorkflowControlAction.CANCEL,
                    principal_id=principal_id,
                    reason=reason,
                )
                result = {
                    "status": "cancel_requested",
                    "workflow_run_id": workflow_run_id,
                    "action": "cancel",
                    "applied": False,
                    "child": child_result,
                }
            else:
                _mark_run_cancelled(fresh)
                logger.info(
                    "workflow_run_cancelled",
                    workflow_run_id=workflow_run_id,
                    principal_id=principal_id,
                    mode="immediate",
                )
                result = {
                    "status": "cancelled",
                    "workflow_run_id": workflow_run_id,
                    "action": "cancel",
                    "applied": True,
                    "child": child_result,
                }
        finally:
            try:
                lock.release_sync()
            except Exception as e:
                logger.warning(
                    "workflow_control_lock_release_failed",
                    workflow_run_id=workflow_run_id,
                    error=str(e),
                    error_type=type(e).__name__,
                )
    else:
        # running → cooperative cancel signal
        _write_workflow_run_control(
            tenant_id=tenant_id,
            motet_id=motet_id,
            workflow_run_id=workflow_run_id,
            action=WorkflowControlAction.CANCEL,
            principal_id=principal_id,
            reason=reason,
        )
        logger.info(
            "workflow_cancel_requested",
            workflow_run_id=workflow_run_id,
            principal_id=principal_id,
        )
        result = {
            "status": "cancel_requested",
            "workflow_run_id": workflow_run_id,
            "action": "cancel",
            "applied": False,
            "child": child_result,
        }

    # Cancel a parent that is blocked waiting on this child.
    parent_id = str(getattr(checkpoint, "parent_workflow_run_id", None) or "").strip()
    if parent_id and control_action == WorkflowControlAction.CANCEL:
        parent = load_workflow_checkpoint(
            tenant_id=tenant_id, motet_id=motet_id, workflow_run_id=parent_id
        )
        if (
            parent is not None
            and str(parent.child_workflow_run_id or "") == workflow_run_id
            and _status_value(parent) not in (
                WorkflowRunStatus.COMPLETED.value,
                WorkflowRunStatus.FAILED.value,
                WorkflowRunStatus.CANCELLED.value,
            )
        ):
            try:
                result["parent"] = request_workflow_run_control(
                    tenant_id=tenant_id,
                    motet_id=motet_id,
                    workflow_run_id=parent_id,
                    action="cancel",
                    principal_id=principal_id,
                    reason=reason,
                    _cascade_depth=_cascade_depth + 1,
                )
            except (ValueError, WorkflowRunControlConflict) as e:
                logger.info(
                    "workflow_control_parent_cascade_skipped",
                    child_run_id=workflow_run_id,
                    parent_run_id=parent_id,
                    error=str(e),
                )
    return result


def list_paused_workflow_runs(
    *,
    tenant_id: Optional[str],
    motet_id: Optional[str],
    limit: int = 50,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    """Enumerate paused runs for a tenant, newest first.

    Self-healing: members whose checkpoint expired or moved on are pruned from
    the index as they are encountered, so TTL expiry does not leave phantom rows.
    """
    try:
        from motet.core.distributed.redis_manager import get_sync_redis_client

        client = get_sync_redis_client(_SERVICE)
        paused_key = _paused_index_key(tenant_id, motet_id)
        start = max(int(offset), 0)
        stop = start + max(int(limit), 1) - 1
        # redis-py stubs type sync zrevrange as Awaitable in some versions.
        members = cast(Any, client.zrevrange(paused_key, start, stop))
    except Exception as e:
        logger.warning(
            "workflow_paused_index_read_failed",
            tenant_id=tenant_id,
            error=str(e),
            error_type=type(e).__name__,
        )
        return []

    runs: List[Dict[str, Any]] = []
    for member in members or []:
        run_id = member.decode() if isinstance(member, bytes) else str(member)
        checkpoint = load_workflow_checkpoint(
            tenant_id=tenant_id, motet_id=motet_id, workflow_run_id=run_id
        )
        if checkpoint is None or _status_value(checkpoint) != WorkflowRunStatus.PAUSED.value:
            try:
                client.zrem(paused_key, run_id)
            except Exception:
                logger.debug("workflow_paused_index_prune_failed", workflow_run_id=run_id)
            continue
        runs.append(checkpoint.summary())
    return runs


def load_workflow_checkpoint(
    *,
    tenant_id: Optional[str],
    motet_id: Optional[str],
    workflow_run_id: str,
) -> Optional[WorkflowCheckpoint]:
    """Load a checkpoint by run id (non-consuming)."""
    if not workflow_run_id:
        return None
    data = load_json_blob(
        _SERVICE,
        _checkpoint_key(tenant_id, motet_id, workflow_run_id),
        error_label="workflow_checkpoint",
    )
    if not data:
        return None
    try:
        return WorkflowCheckpoint.model_validate(data)
    except Exception as e:
        logger.warning(
            "workflow_checkpoint_load_failed",
            workflow_run_id=workflow_run_id,
            error=str(e),
            error_type=type(e).__name__,
        )
        return None


def find_workflow_run_id_by_interaction(
    *,
    tenant_id: Optional[str],
    motet_id: Optional[str],
    interaction_id: str,
) -> Optional[str]:
    """Resolve a pending interaction id to its workflow_run_id."""
    if not interaction_id:
        return None
    return lookup_id_index(
        _SERVICE,
        _interaction_index_key(tenant_id, motet_id, interaction_id),
        target_field="workflow_run_id",
        error_label="workflow_checkpoint_index",
    )
