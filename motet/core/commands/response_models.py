"""
Motet - Distributed Command Response Models

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    Comprehensive response models for distributed commands in the Motet distributed framework.
    Provides standardized response structures including command results, error handling,
    metadata tracking, and streaming support. Includes performance metrics, worker
    information, and comprehensive observability data.

Dependencies:
    - pydantic: Data validation and model definitions
    - datetime: Timestamp and time-based operations
    - typing: Type hints and annotations
    - Worker capability system
    - Response and error handling

Usage:
    from motet.core.commands.response_models import (
        CommandError, CommandMetadata, BaseCommandResponse,
        child_command_envelope, parse_command_envelope,
        strip_transport_envelope,
    )

    envelope = parse_command_envelope(payload)
    data = envelope.data
    
    # Create command result
    result = CommandResult(
        success=True,
        data={"result": "success"},
        metadata=CommandMetadata(
            command_id="cmd_123",
            command_type="tool_execution",
            execution_time_ms=150
        )
    )
    
    # Handle errors
    error = CommandError(
        type="ToolExecutionError",
        message="Tool execution failed",
        recoverable=True
    )

Notes:
    - Provides standardized response structures for all distributed commands
    - Includes comprehensive error handling with recoverability indicators
    - Supports performance metrics and execution metadata
    - Includes worker information and pool type tracking
    - Supports streaming responses and progress tracking
    - Integrates with distributed worker routing and capability management
    - Supports comprehensive observability and monitoring
"""


from typing import Any, Dict, List, Literal, Optional
from datetime import datetime
from pydantic import BaseModel, Field, model_validator, ConfigDict, ValidationError

from .capabilities import WorkerCapability


class CommandError(BaseModel):
    """
    Structured error information for command failures.
    
    Provides consistent error reporting across all commands with:
    - Error type classification
    - User-friendly error messages
    - Command-specific error details
    - Recoverability indicators for retry logic
    """
    
    type: str = Field(
        ...,
        description="Exception class name or error category (e.g., 'ToolExecutionError', 'TimeoutError')"
    )
    message: str = Field(
        ...,
        description="User-friendly error message explaining what went wrong"
    )
    details: Dict[str, Any] = Field(
        default_factory=dict,
        description="Command-specific error context (e.g., tool_name, parameters, stack trace)"
    )
    recoverable: bool = Field(
        default=False,
        description="Whether the error is recoverable (e.g., network timeout vs validation error)"
    )
    retry_recommended: bool = Field(
        default=False,
        description="Whether the caller should retry the operation"
    )
    
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "type": "ToolExecutionError",
                "message": "Web scraping tool timeout after 30 seconds",
                "details": {
                    "tool_name": "web_scraper",
                    "url": "https://example.com",
                    "timeout_seconds": 30
                },
                "recoverable": True,
                "retry_recommended": True
            }
        }
    )


class CommandMetadata(BaseModel):
    """
    Standard execution metadata for all commands.
    
    Provides observability data including:
    - Command identification
    - Execution timing and performance
    - Worker information (including pool type)
    - Retry tracking
    - Resource usage
    - Streaming metadata
    """
    
    # Core identification
    command_id: str = Field(..., description="Unique command identifier")
    command_type: str = Field(..., description="Type of command executed")
    
    # Performance metrics
    execution_time_ms: float = Field(
        ...,
        description="Total execution time in milliseconds (time in _do_execute)"
    )
    queue_time_ms: Optional[float] = Field(
        None,
        description="Time spent in Celery queue before execution"
    )
    network_time_ms: Optional[float] = Field(
        None,
        description="Network overhead for distributed operations"
    )
    
    # Worker information
    worker_id: Optional[str] = Field(
        None,
        description="ID of worker that executed the command"
    )
    pool_type: Optional[str] = Field(
        None,
        description="Worker pool type: 'eventlet', 'gevent', 'threads', or 'fork'"
    )
    
    # Retry tracking
    retry_count: int = Field(
        default=0,
        description="Number of retry attempts for this command"
    )
    
    # Capabilities and resources
    capabilities_used: List[WorkerCapability] = Field(
        default_factory=list,
        description="Worker capabilities required/used for execution"
    )
    resource_usage: Optional[Dict[str, Any]] = Field(
        None,
        description="Resource usage metrics (memory, tokens, API calls, etc.)"
    )
    data_size_bytes: Optional[int] = Field(
        None,
        description="Size of response data in bytes (useful for large results)"
    )
    
    # Streaming metadata (ADR-0028 integration)
    streaming_enabled: bool = Field(
        default=False,
        description="Whether streaming was used for this command"
    )
    stream_key: Optional[str] = Field(
        None,
        description="Redis stream key where events were written (historical reference after completion)"
    )
    stream_event_count: Optional[int] = Field(
        None,
        description="Total number of events written to stream during execution"
    )
    stream_ttl_seconds: Optional[int] = Field(
        None,
        description="TTL (time-to-live) in seconds for the stream data"
    )
    stream_completed_at: Optional[datetime] = Field(
        None,
        description="Timestamp when streaming completed"
    )
    
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "command_id": "cmd-abc-123",
                "command_type": "tool_execution",
                "execution_time_ms": 1234.5,
                "queue_time_ms": 45.2,
                "worker_id": "worker-1",
                "retry_count": 0,
                "capabilities_used": ["tool_execution"],
                "resource_usage": {
                    "api_calls": 2,
                    "tokens_used": 150
                },
                "data_size_bytes": 2048,
                "streaming_enabled": False
            }
        }
    )


class CommandExecutionError(Exception):
    """Raised by ``motet.do()`` when a command fails (ADR-0133).

    Bundle authors catch this via ``from motet_sdk import CommandExecutionError``.
    The SDK bridge replaces the SDK stub with this class so ``except`` matches.
    """

    def __init__(
        self,
        error_type: str,
        message: str,
        details: Dict[str, Any],
        recoverable: bool,
        command_type: str,
        command_id: str,
    ):
        self.error_type = error_type
        self.message = message
        self.details = details
        self.recoverable = recoverable
        self.command_type = command_type
        self.command_id = command_id
        super().__init__(f"{command_type} failed: {message}")

    def __repr__(self) -> str:
        return (
            f"CommandExecutionError("
            f"error_type={self.error_type!r}, "
            f"message={self.message!r}, "
            f"command_type={self.command_type!r}, "
            f"command_id={self.command_id!r}, "
            f"recoverable={self.recoverable}"
            f")"
        )


class GatherExecutionError(CommandExecutionError):
    """Raised by ``motet.join()`` when parallel execution fails.

    ``partial_results`` is the same unwrapped list a successful join would
    have returned: domain data per child, or ``{_error: True, ...}``.
    """

    def __init__(
        self,
        error_type: str,
        message: str,
        details: Dict[str, Any],
        recoverable: bool,
        command_type: str,
        command_id: str,
        partial_results: List[Any],
    ):
        super().__init__(
            error_type, message, details, recoverable, command_type, command_id
        )
        self.partial_results = partial_results

    def __repr__(self) -> str:
        return (
            f"GatherExecutionError("
            f"error_type={self.error_type!r}, "
            f"message={self.message!r}, "
            f"command_type={self.command_type!r}, "
            f"command_id={self.command_id!r}, "
            f"partial_results_count={len(self.partial_results)}"
            f")"
        )


class ApplyExecutionError(CommandExecutionError):
    """Raised by ``motet.apply()`` when every batch item fails."""

    def __init__(
        self,
        error_type: str,
        message: str,
        details: Dict[str, Any],
        recoverable: bool,
        command_type: str,
        command_id: str,
        total_inputs: int,
        successful: int,
        failed: int,
    ):
        super().__init__(
            error_type, message, details, recoverable, command_type, command_id
        )
        self.total_inputs = total_inputs
        self.successful = successful
        self.failed = failed

    def __repr__(self) -> str:
        return (
            f"ApplyExecutionError("
            f"error_type={self.error_type!r}, "
            f"message={self.message!r}, "
            f"command_type={self.command_type!r}, "
            f"total_inputs={self.total_inputs}, "
            f"successful={self.successful}, "
            f"failed={self.failed}"
            f")"
        )


class BaseCommandResponse(BaseModel):
    """
    Standard response envelope for all DistributedCommand implementations.
    
    Provides consistent structure for command responses with:
    - Status indication (success, error, partial_success)
    - Command-specific result data
    - Structured error information
    - Execution metadata for observability
    - Non-fatal warnings
    
    This is the foundation of  Command Response Standardization.
    """
    
    status: Literal["success", "error", "partial_success"] = Field(
        ...,
        description="Execution status: 'success' (all succeeded), 'error' (all failed), 'partial_success' (some succeeded)"
    )
    
    data: Any = Field(
        None,
        description="Command-specific result data (structure varies by command type)"
    )
    
    error: Optional[CommandError] = Field(
        None,
        description="Structured error information (present only if status is 'error' or 'partial_success')"
    )
    
    metadata: CommandMetadata = Field(
        ...,
        description="Standard execution metadata for observability and tracking"
    )
    
    warnings: List[str] = Field(
        default_factory=list,
        description="Non-fatal issues encountered during execution"
    )
    
    @model_validator(mode='after')
    def validate_error_consistency(self) -> 'BaseCommandResponse':
        """
        Ensure error field is consistent with status.
        
        Rules:
        - status='error' MUST have error field
        - status='success' MUST NOT have error field
        - status='partial_success' MUST have error field (for failed sub-operations)
        """
        if self.status == "error" and self.error is None:
            raise ValueError("Status 'error' requires error field to be set")
        
        if self.status == "success" and self.error is not None:
            raise ValueError("Status 'success' cannot have error field")
        
        if self.status == "partial_success" and self.error is None:
            raise ValueError("Status 'partial_success' requires error field (for failed sub-operations)")
        
        return self
    
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "description": "Successful command execution",
                    "value": {
                        "status": "success",
                        "data": {
                            "result": "Operation completed successfully",
                            "items_processed": 10
                        },
                        "error": None,
                        "metadata": {
                            "command_id": "cmd-123",
                            "command_type": "tool_execution",
                            "execution_time_ms": 1500.0,
                            "worker_id": "worker-1",
                            "retry_count": 0,
                            "capabilities_used": ["tool_execution"]
                        },
                        "warnings": []
                    }
                },
                {
                    "description": "Failed command execution",
                    "value": {
                        "status": "error",
                        "data": None,
                        "error": {
                            "type": "TimeoutError",
                            "message": "Operation timed out after 30 seconds",
                            "details": {"timeout_seconds": 30},
                            "recoverable": True,
                            "retry_recommended": True
                        },
                        "metadata": {
                            "command_id": "cmd-456",
                            "command_type": "tool_execution",
                            "execution_time_ms": 30000.0,
                            "worker_id": "worker-2",
                            "retry_count": 2,
                            "capabilities_used": ["tool_execution"]
                        },
                        "warnings": []
                    }
                },
                {
                    "description": "Partial success (workflow/batch operations)",
                    "value": {
                        "status": "partial_success",
                        "data": {
                            "successful_operations": 8,
                            "failed_operations": 2,
                            "results": ["..."]
                        },
                        "error": {
                            "type": "PartialFailureError",
                            "message": "2 of 10 operations failed",
                            "details": {
                                "failed_indices": [3, 7],
                                "failure_reasons": ["timeout", "invalid_input"]
                            },
                            "recoverable": True,
                            "retry_recommended": False
                        },
                        "metadata": {
                            "command_id": "cmd-789",
                            "command_type": "workflow_execution",
                            "execution_time_ms": 5000.0,
                            "worker_id": "worker-3",
                            "retry_count": 0,
                            "capabilities_used": ["workflow_execution"]
                        },
                        "warnings": ["Some operations took longer than expected"]
                    }
                }
            ]
        }
    )


ADR0029_STATUSES = frozenset({"success", "error", "partial_success"})


def parse_command_envelope(value: Any) -> BaseCommandResponse:
    """Rehydrate a Redis/JSON command result as ``BaseCommandResponse``.

    Validation is the envelope check. Domain payloads such as workflow
    ``{status: "completed", step_results: ...}`` fail (wrong status enum
    and/or missing ``CommandMetadata``) and must not be treated as lifecycle.
    """
    return BaseCommandResponse.model_validate(value)


def child_command_envelope(
    *,
    command_id: str,
    command_type: str,
    data: Any = None,
    error: Optional[Dict[str, Any]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    warnings: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Build a gather/map child that ``parse_command_envelope`` accepts.

    ``motet.join`` / ``motet.apply`` unwrap each ``results[]`` item with
    ``BaseCommandResponse.model_validate``. Children must be full envelopes
    (required ``metadata``), not slim ``{status, data}`` dicts.
    """
    meta_raw = dict(metadata) if isinstance(metadata, dict) else {}
    meta_raw.setdefault("command_id", command_id)
    meta_raw.setdefault("command_type", command_type)
    if meta_raw.get("execution_time_ms") is None:
        meta_raw["execution_time_ms"] = 0.0
    try:
        meta = CommandMetadata.model_validate(meta_raw)
    except ValidationError:
        meta = CommandMetadata(
            command_id=command_id,
            command_type=command_type,
            execution_time_ms=float(meta_raw.get("execution_time_ms") or 0.0),
        )

    if error:
        err_raw = dict(error) if isinstance(error, dict) else {}
        err_raw.setdefault("type", "UnknownError")
        err_raw.setdefault("message", "Command failed")
        err_raw.setdefault("details", {})
        try:
            err = CommandError.model_validate(err_raw)
        except ValidationError:
            err = CommandError(
                type=str(err_raw.get("type") or "UnknownError"),
                message=str(err_raw.get("message") or "Command failed"),
                details=err_raw.get("details") if isinstance(err_raw.get("details"), dict) else {},
            )
        envelope = BaseCommandResponse(
            status="error",
            data=data,
            error=err,
            metadata=meta,
            warnings=list(warnings or []),
        )
    else:
        envelope = BaseCommandResponse(
            status="success",
            data=data,
            error=None,
            metadata=meta,
            warnings=list(warnings or []),
        )
    return envelope.model_dump(mode="json")


def strip_transport_envelope(value: Any) -> Any:
    """Peel invoker/worker ``{status: completed, result: ...}`` layers.

    Stops when the current dict is not a transport envelope (no ``result``
    sibling of ``status: completed``). Workflow domain
    ``{status: completed, step_results: ...}`` is left intact.
    """
    current = value
    for _ in range(4):
        if not isinstance(current, dict):
            return current
        if current.get("status") == "completed" and "result" in current:
            current = current["result"]
            continue
        return current
    return current

