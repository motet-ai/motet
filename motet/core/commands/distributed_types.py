"""
Motet - Distributed Command Types

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Shared types for the distributed command system (issue #158). Extracted from
    distributed.py with no behavior change.

Dependencies:
    - pydantic / enum: Model and strategy definitions
    - motet.core.commands.base: CommandContext
    - motet.core.commands.capabilities: WorkerCapability

Usage:
    from motet.core.commands.distributed_types import (
        DistributionStrategy, ScheduleType, WorkerAssignment, DistributedCommandContext,
        AGENTIC_LOOP_ITERATION_META_KEY, parse_agentic_loop_iteration,
    )
    # Prefer re-export via distributed for existing call sites.

Notes:
    - ScheduleType is part of the public surface even though older __all__ omitted it.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Set

from pydantic import BaseModel, Field

from motet.core.commands.base import CommandContext
from motet.core.commands.capabilities import WorkerCapability


class DistributionStrategy(str, Enum):
    """Strategies for distributing commands"""
    SINGLE_WORKER = "single_worker"  # Execute on one worker
    PARALLEL_FANOUT = "parallel_fanout"  # Execute on multiple workers in parallel
    SEQUENTIAL_CHAIN = "sequential_chain"  # Execute on workers in sequence
    MAP_REDUCE = "map_reduce"  # Map to multiple workers, reduce results
    BROADCAST = "broadcast"  # Send to all capable workers


class ScheduleType(str, Enum):
    """Types of scheduling supported"""
    IMMEDIATE = "immediate"           # Execute now (current behavior)
    DELAYED = "delayed"              # Execute at specific datetime
    RECURRING = "recurring"          # Execute on schedule (cron-like)
    CONDITIONAL = "conditional"      # Execute when condition is met


class WorkerAssignment(BaseModel):
    """Assignment of a command to a specific worker"""
    worker_id: str
    worker_type: str
    capabilities: Set[WorkerCapability]
    assigned_at: datetime = Field(default_factory=datetime.utcnow)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    result: Optional[Any] = None
    error: Optional[str] = None


class DistributedCommandContext(CommandContext):
    """Enhanced context for distributed command execution"""
    
    # Distribution settings
    required_capabilities: Set[WorkerCapability] = Field(default_factory=set)
    distribution_strategy: DistributionStrategy = DistributionStrategy.SINGLE_WORKER
    max_workers: int = 1
    timeout_seconds: int = 60
    
    # Routing and priority
    preferred_worker_ids: List[str] = Field(default_factory=list)
    priority: int = 5  # EventPriority.NORMAL
    preferred_pool_type: Optional[str] = None  # ADR-0033: "high_concurrency", "process", or None
    target_worker_id: Optional[str] = None  # Force execution on specific worker
    worker_affinity: Optional[str] = None  # Affinity key for consistent worker selection
    avoid_worker_ids: List[str] = Field(default_factory=list)  # Workers to avoid
    
    # Retry and resilience
    max_retries: int = 3
    retry_backoff_seconds: float = 1.0
    circuit_breaker_enabled: bool = True
    
    # Tracing and observability
    trace_id: Optional[str] = None
    parent_span_id: Optional[str] = None
    parent_command_id: Optional[str] = None  # Automatically injected by invoker
    parent_worker_id: Optional[str] = None   # Worker that executed the parent (injected when invoking from a worker)
    distributed_trace_enabled: bool = True
    # ADR-0131 amendment: opt-in cancel scopes checked upward (task_id, root
    # command_id, workflow_run_id). Not full ancestry — nested leaves inherit.
    cancel_scopes: List[str] = Field(default_factory=list)
    own_cancel_scope: Optional[str] = None
    
    # Security and isolation
    tenant_isolation_required: bool = True
    worker_security_level: str = "standard"  # standard, high, isolated
    
    # Result handling
    result_aggregation_strategy: str = "first_success"  # first_success, all_results, majority_vote
    partial_results_allowed: bool = False
    
    # Redis storage control
    use_redis_storage: bool = True  # True = use Redis for large data, False = always inline
    
    # Vault integration for secure credential access
    vault_enabled: bool = True  # Enable vault integration for credential access
    vault_client: Optional[Any] = None  # Vault client instance (lazy-loaded)
    
    # Scheduling configuration (ADR-0025)
    schedule_type: ScheduleType = ScheduleType.IMMEDIATE
    schedule_name: Optional[str] = None              # Optional human-readable name for the schedule
    scheduled_at: Optional[datetime] = None          # For DELAYED execution
    cron_expression: Optional[str] = None            # For RECURRING execution
    interval_seconds: Optional[int] = None           # For RECURRING execution (alternative to cron)
    recurring_until: Optional[datetime] = None       # End date for recurring
    condition_check_interval: Optional[int] = None   # Seconds for CONDITIONAL
    condition_expression: Optional[str] = None       # Python expression for CONDITIONAL
    
    # Schedule metadata
    schedule_id: Optional[str] = None                # Unique schedule identifier
    original_command_id: Optional[str] = None        # Link to original command
    execution_count: int = 0                         # Track recurring executions
    max_executions: Optional[int] = None             # Limit for recurring


# Stamped on child commands of the in-process agentic loop (ADR-0132) so task
# flow can group model/tool/workflow work by round without a command per iteration.
AGENTIC_LOOP_ITERATION_META_KEY = "agentic_loop_iteration"


def parse_agentic_loop_iteration(value: Any) -> Optional[int]:
    """Return a 1-based iteration number, or None if missing/invalid."""
    if value is None or isinstance(value, bool):
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n >= 1 else None


def agentic_loop_iteration_metadata_fields(metadata: Any) -> Dict[str, int]:
    """Kwargs to merge into cmd:meta when the parent stamped a loop round."""
    if not isinstance(metadata, dict):
        return {}
    n = parse_agentic_loop_iteration(metadata.get(AGENTIC_LOOP_ITERATION_META_KEY))
    if n is None:
        return {}
    return {AGENTIC_LOOP_ITERATION_META_KEY: n}


__all__ = [
    "DistributionStrategy",
    "ScheduleType",
    "WorkerAssignment",
    "DistributedCommandContext",
    "AGENTIC_LOOP_ITERATION_META_KEY",
    "parse_agentic_loop_iteration",
    "agentic_loop_iteration_metadata_fields",
]
