"""
Motet - Base Command System

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Base command system for the Motet distributed framework.
    Provides abstract base classes, command context, and status definitions.

Dependencies:
    - abc: Abstract base classes
    - datetime: Time and date handling
    - enum: Enumeration types
    - typing: Type hints and annotations

Usage:
    from motet.core.commands.base import Command, CommandContext, CommandStatus
    
    # Create custom command
    class MyCommand(Command):
        async def execute(self, context: CommandContext):
            return {"result": "success"}

Notes:
    - Provides abstract base classes for commands
    - Includes command context and status management
    - Supports command lifecycle and execution
    - Integrates with distributed architecture
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class CommandStatus(str, Enum):
    PENDING = "pending"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNDONE = "undone"


class CommandContext(BaseModel):
    """Context information for command execution"""
    task_id: str
    conversation_id: str = ""
    tenant_id: str = ""
    principal_id: str = ""
    motet_id: str = "default"  # Environment/motet identifier for multi-environment deployments
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Command(ABC):
    """Abstract base class for all commands"""

    def __init__(self, command_id: str, context: CommandContext):
        self.command_id = command_id
        self.context = context
        self.status = CommandStatus.PENDING
        self.result: Optional[Any] = None
        self.error: Optional[Exception] = None
        self.executed_at: Optional[datetime] = None
        self.completed_at: Optional[datetime] = None
        self.undo_data: Optional[Dict[str, Any]] = None
        # Optional resilience metadata; invoker may use these
        self.service_name: Optional[str] = None
        self.breaker_profile: Optional[str] = None
        self.failure_threshold: Optional[int] = None
        self.reset_timeout_seconds: Optional[int] = None
        self.success_threshold: Optional[int] = None
        self.monitor_window_seconds: Optional[int] = None
        self.failure_rate_threshold: Optional[float] = None

    @abstractmethod
    async def execute(self, stack) -> Any:
        """Execute the command"""
        pass

    @abstractmethod
    async def undo(self, stack) -> Any:
        """Undo the command (if possible)"""
        pass

    @abstractmethod
    def can_undo(self) -> bool:
        """Check if command can be undone"""
        pass

    @abstractmethod
    def get_command_type(self) -> str:
        """Get the command type for logging"""
        pass

    def to_dict(self) -> Dict[str, Any]:
        """Serialize command for logging/storage"""
        return {
            "command_id": self.command_id,
            "command_type": self.get_command_type(),
            "status": self.status.value,
            "task_id": self.context.task_id,
            "conversation_id": self.context.conversation_id,
            "tenant_id": self.context.tenant_id,
            "principal_id": self.context.principal_id,
            "motet_id": self.context.motet_id,
            "created_at": self.context.created_at.isoformat(),
            "executed_at": self.executed_at.isoformat() if self.executed_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "has_error": self.error is not None,
            "error_message": str(self.error) if self.error else None,
            "can_undo": self.can_undo(),
            "metadata": self.context.metadata,
        }

    async def _execute_with_tracking(self, stack) -> Any:
        """Execute command with status tracking"""
        self.status = CommandStatus.EXECUTING
        self.executed_at = datetime.utcnow()

        try:
            result = await self.execute(stack)
            self.result = result
            self.status = CommandStatus.COMPLETED
            self.completed_at = datetime.utcnow()
            return result
        except Exception as e:
            self.error = e
            self.status = CommandStatus.FAILED
            self.completed_at = datetime.utcnow()
            raise


__all__ = ["Command", "CommandContext", "CommandStatus"]


