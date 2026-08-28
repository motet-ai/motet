"""
Motet - Scheduling Models

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-26

Description:
    Comprehensive scheduling models for the Motet distributed framework.
    Provides data structures for schedule metadata, execution tracking,
    and comprehensive schedule management. Includes status tracking,
    worker targeting, and error handling.

Dependencies:
    - datetime: Time and date handling for scheduling
    - enum: Schedule status enumeration
    - pydantic: Data validation and model definitions
    - typing: Type hints and annotations
    - Distributed command system

Usage:
    from motet.core.orchestration.scheduling.models import (
        ScheduleMetadata, ScheduleStatus, ScheduleFilter
    )
    
    # Create schedule metadata
    schedule = ScheduleMetadata(
        schedule_id="schedule_123",
        command_id="cmd_123",
        command_type="core.agent_turn",
        schedule_type=ScheduleType.ONCE,
        scheduled_at=datetime.now(timezone.utc)
    )
    
    # Check schedule status
    if schedule.status == ScheduleStatus.ACTIVE:
        # Handle active schedule
        pass

Notes:
    - Provides comprehensive schedule metadata and tracking
    - Includes schedule status management and execution tracking
    - Supports worker targeting and affinity management
    - Includes error tracking and retry management
    - Supports various schedule types (once, recurring, condition-based)
    - Integrates with distributed command system
    - Includes comprehensive schedule lifecycle management
"""


from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from motet.core.commands.distributed import ScheduleType


def _utc_now() -> datetime:
    """Return current UTC time as timezone-aware datetime"""
    return datetime.now(timezone.utc)


class ScheduleStatus(str, Enum):
    """Status of a scheduled command"""
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    EXPIRED = "expired"


class ScheduleMetadata(BaseModel):
    """Metadata for a scheduled command execution"""
    
    # Core identifiers
    schedule_id: str
    command_id: str
    command_type: str
    name: Optional[str] = None  # Optional human-readable name for the schedule
    
    # Schedule configuration
    schedule_type: ScheduleType
    created_at: datetime = Field(default_factory=_utc_now)
    scheduled_at: Optional[datetime] = None
    cron_expression: Optional[str] = None
    recurring_until: Optional[datetime] = None
    condition_check_interval: Optional[int] = None
    condition_expression: Optional[str] = None
    
    # Execution tracking
    status: ScheduleStatus = ScheduleStatus.ACTIVE
    execution_count: int = 0
    max_executions: Optional[int] = None
    last_execution_at: Optional[datetime] = None
    next_execution_at: Optional[datetime] = None
    
    # Metadata and context
    created_by: Optional[str] = None  # For audit trails (principal_id)
    tenant_id: Optional[str] = None   # For multi-tenancy
    motet_id: Optional[str] = None   # For multi-motet/environment isolation (ADR-0056)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    
    # Worker targeting (ADR-0025 enhancement)
    target_worker_id: Optional[str] = None  # Force execution on specific worker
    preferred_worker_ids: List[str] = Field(default_factory=list)  # Preferred workers in order
    worker_affinity: Optional[str] = None  # Affinity key for consistent worker selection
    avoid_worker_ids: List[str] = Field(default_factory=list)  # Workers to avoid
    
    # Error tracking
    last_error: Optional[str] = None
    consecutive_failures: int = 0
    max_consecutive_failures: int = 3
    
    # Additional scheduling parameters
    interval_seconds: Optional[int] = None  # For RECURRING schedules
    max_retries: int = 3  # Maximum retry attempts
    timeout_seconds: int = 300  # Command timeout in seconds
    priority: int = 5  # Command priority (1-10, higher is more important)
    
    def is_expired(self) -> bool:
        """Check if the schedule has expired"""
        if self.recurring_until and datetime.now(timezone.utc) > self.recurring_until:
            return True
        if self.max_executions and self.execution_count >= self.max_executions:
            return True
        return False
    
    def should_execute(self) -> bool:
        """Check if the schedule should execute now"""
        if self.status != ScheduleStatus.ACTIVE:
            return False
        if self.is_expired():
            return False
        if self.max_consecutive_failures and self.consecutive_failures >= self.max_consecutive_failures:
            return False
        return True
    
    def increment_execution_count(self, update_last_execution: bool = True) -> None:
        """
        Increment the execution count and optionally update last execution time.
        
        Args:
            update_last_execution: If True, sets last_execution_at to current time.
                                  For RECURRING schedules, this should be False since
                                  last_execution_at is set BEFORE execution to maintain
                                  accurate interval timing.
        """
        self.execution_count += 1
        if update_last_execution:
            self.last_execution_at = datetime.now(timezone.utc)
        self.consecutive_failures = 0  # Reset on successful execution
    
    def record_failure(self, error: str) -> None:
        """Record a failed execution"""
        self.consecutive_failures += 1
        self.last_error = error
        self.last_execution_at = datetime.now(timezone.utc)


class ScheduleExecutionResult(BaseModel):
    """Result of a scheduled command execution"""
    
    schedule_id: str
    execution_id: str
    executed_at: datetime = Field(default_factory=_utc_now)
    success: bool
    result: Optional[Any] = None
    error: Optional[str] = None
    execution_time_ms: Optional[float] = None
    worker_id: Optional[str] = None
    
    # Schedule state after execution
    schedule_status: ScheduleStatus
    next_execution_at: Optional[datetime] = None
    execution_count: int
    consecutive_failures: int


class ScheduleFilter(BaseModel):
    """Filter criteria for listing schedules"""
    
    status: Optional[ScheduleStatus] = None
    schedule_type: Optional[ScheduleType] = None
    tenant_id: Optional[str] = None
    motet_id: Optional[str] = None  # For multi-motet/environment isolation (ADR-0056)
    created_by: Optional[str] = None  # Principal ID who created the schedule
    created_after: Optional[datetime] = None
    created_before: Optional[datetime] = None
    next_execution_after: Optional[datetime] = None
    next_execution_before: Optional[datetime] = None
    limit: int = 100
    offset: int = 0
