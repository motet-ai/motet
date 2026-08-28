"""
Motet - Turn Checkpoints

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Durable state for the lifecycle of a single agent turn, independent of the
    reasoning strategy that produced it. Shared TurnCheckpoint store for:

    - handback suspension (keep remaining budget on resume)
    - Issue #188 budget Continue snapshots (fresh budget on Continue rehydrate)

    Also exports shared Redis/resume helpers (``redis_store``) used by both turn
    and workflow checkpoint stores.

    This package exists to keep turn-lifecycle state out of the reasoning
    strategy packages. The agentic loop writes checkpoints, but the readers
    are `resume_agent_turn` / budget Continue (orchestration) and the
    OpenAI-compatible facade; homing the store here lets callers depend
    downward on a neutral package instead of the facade importing from a
    strategy implementation.

Dependencies:
    - motet.core.distributed.redis_manager: Centralized Redis operations backing
      the checkpoint store (sync variants, called from Celery workers)
    - pydantic: TurnCheckpoint model validation/serialization

Usage:
    from motet.core.checkpoints import (
        TurnCheckpoint,
        store_turn_checkpoint,
        load_turn_checkpoint,
        find_checkpoint_id_by_tool_call,
        resolve_resume_checkpoint,
        classify_turn_ownership,
        TurnOwnership,
    )

    checkpoint = TurnCheckpoint(tenant_id=..., handed_back_tool_calls=[...])
    store_turn_checkpoint(checkpoint)

Notes:
    - Nothing in this package imports `reasoning` or `orchestration`; keep it
      that way so both layers can depend on it without a cycle.
    - Checkpoint reads are non-consuming so resume retries stay idempotent.
    - Redis storage is nested v1 (`schema_version` + identity/loop_state/handback);
      the in-process model stays flat; load accepts nested v1 and flat v0 blobs (#157).
    - Mixed turns (issue #159, execute-at-resume): suspend hands the whole
      turn back; at resume the client covers only externally-owned ids and
      Motet executes its own handed-back calls itself.
    - Distinct from `core.orchestration.turn`, which is the *active* turn
      lifecycle (the commands that run a turn). This is the passive store those
      commands read and write.
"""

from .checkpoint import (
    CHECKPOINT_SCHEMA_VERSION,
    TURN_CHECKPOINT_TTL_SECONDS,
    CheckpointKind,
    TurnCheckpoint,
    find_checkpoint_id_by_tool_call,
    find_latest_checkpoint_for_conversation,
    load_turn_checkpoint,
    resolve_resume_checkpoint,
    store_turn_checkpoint,
)
from .ownership import (
    TurnOwnership,
    call_tool_names,
    classify_turn_ownership,
    split_calls_by_ownership,
)
from .redis_store import (
    assert_checkpoint_principal,
    bind_resume_conversation,
    validate_handback_observations,
)

__all__ = [
    "CHECKPOINT_SCHEMA_VERSION",
    "TURN_CHECKPOINT_TTL_SECONDS",
    "CheckpointKind",
    "TurnCheckpoint",
    "TurnOwnership",
    "assert_checkpoint_principal",
    "bind_resume_conversation",
    "call_tool_names",
    "classify_turn_ownership",
    "split_calls_by_ownership",
    "find_checkpoint_id_by_tool_call",
    "find_latest_checkpoint_for_conversation",
    "load_turn_checkpoint",
    "resolve_resume_checkpoint",
    "store_turn_checkpoint",
    "validate_handback_observations",
]
