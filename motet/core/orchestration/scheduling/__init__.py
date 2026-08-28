"""
Motet - Scheduling System

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Scheduling system for the Motet distributed framework.
    Provides comprehensive scheduling capabilities for distributed commands.

Dependencies:
    - Scheduled command management
    - Cron expression parsing and validation
    - Storage and persistence
    - Execution tracking and monitoring

Usage:
    from motet.core.orchestration.scheduling import ScheduledCommandManager
    
    # Create schedule manager
    manager = ScheduledCommandManager()
    
    # Schedule a command
    await manager.schedule_command(command, schedule_time)

Notes:
    - Supports delayed and recurring execution
    - Includes cron expression support
    - Provides execution tracking
    - Integrates with distributed architecture
"""

from .manager import ScheduledCommandManager
from .models import ScheduleMetadata, ScheduleStatus, ScheduleFilter, ScheduleExecutionResult
from .storage import ScheduleStorage
from .cron_utils import (
    validate_cron_expression,
    get_next_execution_from_cron,
    get_previous_execution_from_cron,
    describe_cron_expression,
    is_time_for_cron_execution,
    CronExpressionError
)

__all__ = [
    "ScheduledCommandManager",
    "ScheduleMetadata", 
    "ScheduleStatus",
    "ScheduleFilter",
    "ScheduleExecutionResult",
    "ScheduleStorage",
    "validate_cron_expression",
    "get_next_execution_from_cron",
    "get_previous_execution_from_cron",
    "describe_cron_expression",
    "is_time_for_cron_execution",
    "CronExpressionError"
]
