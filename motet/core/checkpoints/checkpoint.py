"""
Motet - Turn Suspension Checkpoint Store

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Redis-backed checkpoint store for turn lifecycle snapshots. Two writers share
    the same codec and Redis shape:

    - handback suspend: when the agentic loop encounters a tool call
    owned by an external party, it persists Motet-authoritative loop state,
    hands the tool calls back, and returns ``stop_reason=suspended``.
    ``resume_turn`` loads the checkpoint, validates observations, and re-enters
    the loop with the **same** remaining budget.
    - Issue #188 budget Continue: when the loop stops on ``max_iterations`` /
    ``max_model_calls``, it persists the same snapshot under
    ``checkpoint_kind=budget_continue`` (conversation index). The prior turn
    still finalizes; Continue starts a **new** turn that rehydrates via
    ``LoopStateSnapshot`` with a **fresh** budget policy.

    Redis blob/index helpers live in ``motet.core.checkpoints.redis_store`` and
    are shared with the workflow checkpoint store (issue #149 durability).

    Nested workflow suspends (issue #149): optional ``workflow_run_id`` points at
    the WorkflowCheckpoint that owns graph truth; ``nested_resume_history`` holds
Motet history ending at the unfinished ``workflow_*`` tool call so resume can
    complete that observation after resume_workflow finishes.

    Lives in `core.checkpoints` rather than under a reasoning strategy because the
    checkpoint is turn-lifecycle state, not ReAct logic: the agentic loop writes
    it, but `resume_agent_turn` / budget Continue (orchestration) and the
    OpenAI-compatible facade both read it. A neutral package lets callers depend
    downward instead of the facade reaching into `reasoning`.

    Authority split: the checkpoint owns Motet iteration budget,
    model-call counters (``model_calls_used`` / ``max_model_calls``), usage and
    media accumulators, executed signatures, used-tool names, and model/tool
    settings. Conversation history is recorded for consumers that resume without
    supplying their own (elicitation/OAuth flows); external callers that own the
    wire transcript (the OpenAI facade) may override it on resume. Client
    handbacks are same-iteration: ``remaining_iterations`` is not decremented on
    suspend; ``max_model_calls`` bounds Read↔model loops.

    Storage shape (issue #157): Redis blobs use ``schema_version`` with nested
    ``identity`` / ``loop_state`` / ``handback`` sections. The in-process
    ``TurnCheckpoint`` model keeps a flat constructor/attribute API for call
    sites; load flattens nested v1 and flat v0 blobs; store writes v1.
    ``checkpoint_kind`` and optional ``budget_stop_reason`` are top-level extras.

    Mixed turns (issue #159, execute-at-resume): ``handed_back_tool_calls``
    always records the whole turn so the wire assistant message declares every
    call. At resume the client covers only the externally-owned ids; Motet
    executes its own recorded calls itself (``resume_turn``), discarding any
    client-supplied observations for them.

Dependencies:
    - motet.core.distributed.redis_manager: Centralized Redis operations
      (store/retrieve_structured_data_sync per AGENTS.md requirements)
    - pydantic: TurnCheckpoint model validation/serialization
    - structlog: Structured logging

Usage:
    from motet.core.checkpoints import (
        CheckpointKind,
        TurnCheckpoint, store_turn_checkpoint, load_turn_checkpoint,
        find_checkpoint_id_by_tool_call,
        find_latest_checkpoint_for_conversation,
    )

    checkpoint = TurnCheckpoint(tenant_id=..., principal_id=..., handed_back_tool_calls=[...])
    store_turn_checkpoint(checkpoint)
    cp = load_turn_checkpoint(tenant_id=..., motet_id=..., checkpoint_id=checkpoint.checkpoint_id)

Notes:
    - Reads are non-consuming (idempotent resume retries;). Records
      expire after MOTET_TURN_CHECKPOINT_TTL_SECONDS (default 24h).
    - Keys are scoped by tenant/motet so cross-tenant access is structurally
      impossible; principal re-authorization happens in resume_turn / Continue.
    - store_turn_checkpoint raises on failure: a suspension without a durable
      checkpoint cannot be resumed, so it must fail loudly (no silent loss).
      Budget-continue writers may catch and soft-fail; handback must not.
"""

from __future__ import annotations

import os
import time
from enum import Enum
from typing import Any, Dict, List, Optional, Union
from uuid import uuid4

import structlog
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .redis_store import (
    flatten_nested_blob,
    load_json_blob,
    lookup_id_index,
    scoped_key,
    store_json_blob,
    to_nested_blob,
    write_id_index,
)

logger = structlog.get_logger(__name__)

_SERVICE = "turn_checkpoint"

TURN_CHECKPOINT_TTL_SECONDS = int(
    os.getenv("MOTET_TURN_CHECKPOINT_TTL_SECONDS", str(24 * 3600))
)

# Nested Redis blob version (issue #157). In-process API stays flat.
CHECKPOINT_SCHEMA_VERSION = 1


class CheckpointKind(str, Enum):
    """Why a TurnCheckpoint was written (shared store, two budget policies)."""

    HANDBACK = "handback"
    BUDGET_CONTINUE = "budget_continue"


_IDENTITY_FIELDS = (
    "motet_id",
    "tenant_id",
    "principal_id",
    "task_id",
    "conversation_id",
    "agent_id",
    "parent_agent_id",
)
_HANDBACK_FIELDS = (
    "handed_back_tool_calls",
    "handback_tool_names",
    "handback_tools",
    "workflow_run_id",
    "suspend_reason",
    "nested_workflow_tool_call_id",
    "nested_workflow_tool_name",
    "nested_resume_history",
)
# Loop-state storage keys (agent_id lives under identity; still flat on the model).
_LOOP_STATE_FIELDS = (
    "input",
    "tools",
    "tool_filter_metadata",
    "executed_signatures",
    "stalled_iterations",
    "observation_cache",
    "used_tool_names",
    "max_iterations",
    "remaining_iterations",
    "max_model_calls",
    "model_calls_used",
    "max_cost_usd",
    "max_prompt_tokens",
    "max_tool_time_ms",
    "max_tools",
    "model_provider",
    "model_name",
    "model_profile_name",
    "temperature",
    "enable_thinking",
    "reasoning_effort",
    "enable_prompt_caching",
    "usage_accumulator",
    "media_accumulator",
    "skill_refs",
    "inject_meta_tools",
)

_STORAGE_SECTIONS = (
    (_IDENTITY_FIELDS, "identity"),
    (_LOOP_STATE_FIELDS, "loop_state"),
    (_HANDBACK_FIELDS, "handback"),
)


_CHECKPOINT_EXTRAS = (
    "conversation_history",
    "checkpoint_kind",
    "budget_stop_reason",
)


def _flatten_storage_blob(data: Dict[str, Any]) -> Dict[str, Any]:
    """Accept nested v1 or legacy flat v0 Redis blobs as flat constructor kwargs."""
    return flatten_nested_blob(
        data,
        sections=_STORAGE_SECTIONS,
        id_field="checkpoint_id",
        schema_version=CHECKPOINT_SCHEMA_VERSION,
        extras=_CHECKPOINT_EXTRAS,
    )


class TurnCheckpoint(BaseModel):
    """Motet-authoritative loop state for handback resume or budget Continue.

    Flat in-process fields; Redis storage is nested v1 via ``to_storage_dict``.
    """

    schema_version: int = Field(default=CHECKPOINT_SCHEMA_VERSION)
    checkpoint_id: str = Field(default_factory=lambda: f"suspend-{uuid4().hex}")
    created_at: float = Field(default_factory=time.time)
    # Shared store, two policies (ADR-0127 keep-budget vs #188 fresh-budget).
    # Legacy blobs without the field load as HANDBACK.
    checkpoint_kind: CheckpointKind = Field(default=CheckpointKind.HANDBACK)
    # Populated only for budget_continue snapshots (max_iterations / max_model_calls).
    budget_stop_reason: Optional[str] = None

    # Identity scope (re-authorization on resume / Continue).
    motet_id: str = Field(default="default")
    tenant_id: Optional[str] = None
    principal_id: Optional[str] = None
    task_id: Optional[str] = None
    conversation_id: Optional[str] = None

    # Handback record: the turn's tool calls, in assistant-message order.
    # Each entry: {tool_call_id, tool_name, parameters}.
    handed_back_tool_calls: List[Dict[str, Any]] = Field(default_factory=list)

    # Conversation history as of suspension (ends with the assistant tool_calls
    # message). Serialized Message dicts. Optional: external callers that own
    # the transcript may resume with their own history instead.
    conversation_history: Optional[List[Dict[str, Any]]] = None

    # Loop state to restore (mirrors AgenticLoopData).
    input: str = ""
    tools: Optional[List[Dict[str, Any]]] = None
    tool_filter_metadata: Optional[Dict[str, Any]] = None
    executed_signatures: List[str] = Field(default_factory=list)
    stalled_iterations: int = 0
    observation_cache: Dict[str, Any] = Field(default_factory=dict)
    used_tool_names: List[str] = Field(default_factory=list)
    max_iterations: int = 20
    remaining_iterations: int = 0
    max_model_calls: int = 60
    model_calls_used: int = 0
    max_cost_usd: float = 0.0
    max_prompt_tokens: int = 0
    max_tool_time_ms: int = 0
    max_tools: int = 10
    model_provider: str = "openai"
    model_name: str = "gpt-4.1-mini"
    model_profile_name: Optional[str] = None
    temperature: float = 0.7
    enable_thinking: bool = False
    reasoning_effort: Optional[str] = "medium"
    enable_prompt_caching: Optional[bool] = None
    # Token counters are ints, the ADR-0018 cost_usd running total is a float.
    usage_accumulator: Optional[Dict[str, Any]] = None
    media_accumulator: List[Dict[str, Any]] = Field(default_factory=list)
    skill_refs: Optional[List[Dict[str, Any]]] = None
    handback_tool_names: Optional[List[str]] = None
    # Externally-owned tool schemas (ADR-0125 §5c.1): restored so a resumed
    # turn keeps offering the client's tools and can suspend again.
    handback_tools: Optional[List[Dict[str, Any]]] = None
    # Qualified agent id owning the suspended turn (from command metadata,
    # ADR-0083). Lets resume_turn finalize the completed turn under the right
    # agent so exactly one transcript is written per logical turn (ADR-0127).
    # None when the loop was not agent-owned (OpenAI hosted_tools).
    agent_id: Optional[str] = None
    parent_agent_id: Optional[str] = None
    inject_meta_tools: bool = True

    # Nested workflow suspend (issue #149 / ADR-0127 workflow consumer).
    # Graph truth lives in WorkflowCheckpoint; this is only a pointer + wire shape.
    workflow_run_id: Optional[str] = None
    suspend_reason: Optional[str] = None
    nested_workflow_tool_call_id: Optional[str] = None
    nested_workflow_tool_name: Optional[str] = None
    # Motet history at nested suspend (ends with assistant workflow_* tool_calls).
    nested_resume_history: Optional[List[Dict[str, Any]]] = None

    model_config = ConfigDict(arbitrary_types_allowed=True)

    @field_validator("checkpoint_kind", mode="before")
    @classmethod
    def _coerce_checkpoint_kind(cls, v: Any) -> Any:
        if v is None or v == "":
            return CheckpointKind.HANDBACK
        if isinstance(v, CheckpointKind):
            return v
        return CheckpointKind(str(v))

    @model_validator(mode="before")
    @classmethod
    def _accept_nested_storage(cls, data: Any) -> Any:
        if isinstance(data, dict):
            return _flatten_storage_blob(data)
        return data

    def to_storage_dict(self) -> Dict[str, Any]:
        """Nested Redis blob (schema_version + identity / loop_state / handback)."""
        flat = self.model_dump(mode="json")
        # Enum → value for Redis JSON.
        kind = flat.get("checkpoint_kind")
        if isinstance(kind, CheckpointKind):
            flat["checkpoint_kind"] = kind.value
        return to_nested_blob(
            flat,
            sections=_STORAGE_SECTIONS,
            id_field="checkpoint_id",
            schema_version=CHECKPOINT_SCHEMA_VERSION,
            extras=_CHECKPOINT_EXTRAS,
        )


def _checkpoint_key(tenant_id: Optional[str], motet_id: Optional[str], checkpoint_id: str) -> str:
    return scoped_key("turn_checkpoint", tenant_id, motet_id, checkpoint_id)


def _tool_call_index_key(tenant_id: Optional[str], motet_id: Optional[str], tool_call_id: str) -> str:
    return scoped_key("turn_checkpoint:index", tenant_id, motet_id, tool_call_id)


def _conversation_kind_index_key(
    tenant_id: Optional[str],
    motet_id: Optional[str],
    conversation_id: str,
    kind: Union[CheckpointKind, str],
) -> str:
    kind_value = kind.value if isinstance(kind, CheckpointKind) else str(kind)
    return scoped_key(
        "turn_checkpoint:by_conversation",
        tenant_id,
        motet_id,
        f"{conversation_id}:{kind_value}",
    )


def store_turn_checkpoint(checkpoint: TurnCheckpoint) -> str:
    """
    Persist a turn checkpoint plus index entries.

    Handback: per-tool_call_id indexes (resume handle).
    Budget Continue: conversation+kind index (Continue handle).

    Returns the checkpoint_id. Raises on storage failure — a suspension whose
    checkpoint was lost cannot be resumed, so this must never fail silently.
    Budget-continue callers may catch and degrade to transcript-only Continue.
    """
    key = _checkpoint_key(checkpoint.tenant_id, checkpoint.motet_id, checkpoint.checkpoint_id)
    store_json_blob(
        _SERVICE,
        key,
        checkpoint.to_storage_dict(),
        TURN_CHECKPOINT_TTL_SECONDS,
        error_label="turn_checkpoint",
    )
    for call in checkpoint.handed_back_tool_calls:
        tool_call_id = str(call.get("tool_call_id") or "").strip()
        if not tool_call_id:
            continue
        write_id_index(
            _SERVICE,
            _tool_call_index_key(checkpoint.tenant_id, checkpoint.motet_id, tool_call_id),
            target_field="checkpoint_id",
            target_id=checkpoint.checkpoint_id,
            ttl_seconds=TURN_CHECKPOINT_TTL_SECONDS,
        )

    conversation_id = str(checkpoint.conversation_id or "").strip()
    if (
        conversation_id
        and checkpoint.checkpoint_kind == CheckpointKind.BUDGET_CONTINUE
    ):
        write_id_index(
            _SERVICE,
            _conversation_kind_index_key(
                checkpoint.tenant_id,
                checkpoint.motet_id,
                conversation_id,
                checkpoint.checkpoint_kind,
            ),
            target_field="checkpoint_id",
            target_id=checkpoint.checkpoint_id,
            ttl_seconds=TURN_CHECKPOINT_TTL_SECONDS,
        )

    logger.info(
        "turn_checkpoint_stored",
        checkpoint_id=checkpoint.checkpoint_id,
        checkpoint_kind=getattr(
            checkpoint.checkpoint_kind, "value", checkpoint.checkpoint_kind
        ),
        conversation_id=checkpoint.conversation_id,
        handed_back_count=len(checkpoint.handed_back_tool_calls),
        remaining_iterations=checkpoint.remaining_iterations,
        budget_stop_reason=checkpoint.budget_stop_reason,
        ttl_seconds=TURN_CHECKPOINT_TTL_SECONDS,
    )
    return checkpoint.checkpoint_id


def load_turn_checkpoint(
    *,
    tenant_id: Optional[str],
    motet_id: Optional[str],
    checkpoint_id: str,
) -> Optional[TurnCheckpoint]:
    """
    Load a checkpoint by id (non-consuming; ADR-0127 idempotent resume reads).

    Returns None when the checkpoint does not exist, has expired, or Redis is
    unavailable.
    """
    if not checkpoint_id:
        return None
    data = load_json_blob(
        _SERVICE,
        _checkpoint_key(tenant_id, motet_id, checkpoint_id),
        error_label="turn_checkpoint",
    )
    if not data:
        return None
    try:
        return TurnCheckpoint.model_validate(data)
    except Exception as e:
        logger.warning(
            "turn_checkpoint_load_failed",
            checkpoint_id=checkpoint_id,
            error=str(e),
            error_type=type(e).__name__,
        )
        return None


def find_checkpoint_id_by_tool_call(
    *,
    tenant_id: Optional[str],
    motet_id: Optional[str],
    tool_call_id: str,
) -> Optional[str]:
    """Resolve a handed-back tool_call_id (the resume handle) to its checkpoint_id."""
    if not tool_call_id:
        return None
    return lookup_id_index(
        _SERVICE,
        _tool_call_index_key(tenant_id, motet_id, tool_call_id),
        target_field="checkpoint_id",
        error_label="turn_checkpoint_index",
    )


def find_latest_checkpoint_for_conversation(
    *,
    tenant_id: Optional[str],
    motet_id: Optional[str],
    conversation_id: str,
    kind: Union[CheckpointKind, str] = CheckpointKind.BUDGET_CONTINUE,
) -> Optional[TurnCheckpoint]:
    """
    Load the latest checkpoint indexed for a conversation + kind (issue #188).

    The conversation index is overwritten on each store for that kind, so this
    returns the most recent budget-continue snapshot for Continue rehydrate.
    """
    conversation_id = str(conversation_id or "").strip()
    if not conversation_id:
        return None
    checkpoint_id = lookup_id_index(
        _SERVICE,
        _conversation_kind_index_key(tenant_id, motet_id, conversation_id, kind),
        target_field="checkpoint_id",
        error_label="turn_checkpoint_conversation_index",
    )
    if not checkpoint_id:
        return None
    return load_turn_checkpoint(
        tenant_id=tenant_id,
        motet_id=motet_id,
        checkpoint_id=checkpoint_id,
    )


def resolve_resume_checkpoint(
    *,
    tenant_id: Optional[str],
    motet_id: Optional[str],
    tool_call_ids: List[str],
) -> Optional[TurnCheckpoint]:
    """
    Resolve trailing tool_call_ids to a loaded suspension checkpoint (issue #157).

    Facade resume adapters use this instead of open-coding index lookup + load +
    conversation rebind. Returns None when no index hit or the blob is gone.
    """
    for tool_call_id in tool_call_ids:
        checkpoint_id = find_checkpoint_id_by_tool_call(
            tenant_id=tenant_id,
            motet_id=motet_id,
            tool_call_id=tool_call_id,
        )
        if not checkpoint_id:
            continue
        checkpoint = load_turn_checkpoint(
            tenant_id=tenant_id,
            motet_id=motet_id,
            checkpoint_id=checkpoint_id,
        )
        if checkpoint is not None:
            return checkpoint
    return None
