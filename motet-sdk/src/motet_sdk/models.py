"""
Motet SDK - Data models for bundle authors.

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Bundle authors subclass BaseCommandData for command inputs, use
CommandError/CommandMetadata when building or handling responses,
catch CommandExecutionError / GatherExecutionError / ApplyExecutionError from
composition helpers, and use IdentityContext to carry principal
identity.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from motet_sdk.capabilities import WorkerCapability


@dataclass(frozen=True)
class IdentityContext:
    """Immutable principal identity carrier.

    Carries ``tenant_id``, ``motet_id``, and ``principal_id`` through nested
    command composition.  In the runtime, this is replaced by
    ``motet.core.workers.invoker_context.IdentityContext`` via the SDK bridge;
    both are frozen dataclasses with the same three fields so they are
    interchangeable.

    Bundle tools that need identity use ``resolve_current_identity()`` which
    returns an instance of this type.
    """

    tenant_id: str
    motet_id: str
    principal_id: str


class BaseCommandData(BaseModel):
    """
    Base class for command data payloads.

    Subclass this for your command's input model. The runtime may extend
    this with additional fields (e.g. conversation_history, reasoning_context);
    at minimum your subclass can define only the fields your command needs.
    """

    conversation_history: Optional[List[Any]] = Field(
        default=None,
        description="Optional list of prior conversation messages for context.",
    )
    reasoning_task: Optional[Any] = Field(
        default=None,
        description="Optional reasoning task payload (command-dependent).",
    )
    reasoning_context: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Optional structured context for reasoning/execution.",
    )
    metadata: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Optional metadata (e.g. source, trace).",
    )

    model_config = ConfigDict(extra="allow")


class CommandError(BaseModel):
    """Structured error information for command failures."""

    type: str = Field(..., description="Error category (e.g. 'ToolExecutionError')")
    message: str = Field(..., description="User-facing error message")
    details: Dict[str, Any] = Field(default_factory=dict, description="Error context")
    recoverable: bool = Field(default=False, description="Whether the error is recoverable")
    retry_recommended: bool = Field(default=False, description="Whether retry is recommended")


class CommandMetadata(BaseModel):
    """Standard execution metadata for command responses."""

    command_id: str = Field(..., description="Unique command identifier")
    command_type: str = Field(..., description="Type of command executed")
    execution_time_ms: float = Field(..., description="Execution time in milliseconds")
    queue_time_ms: Optional[float] = Field(None, description="Time in queue before execution")
    worker_id: Optional[str] = Field(None, description="ID of worker that executed the command")
    retry_count: int = Field(default=0, description="Number of retry attempts")
    capabilities_used: List[WorkerCapability] = Field(
        default_factory=list,
        description="Worker capabilities used for execution",
    )
    resource_usage: Optional[Dict[str, Any]] = Field(None, description="Resource metrics")
    streaming_enabled: bool = Field(default=False, description="Whether streaming was used")
    stream_key: Optional[str] = Field(None, description="Redis stream key for streamed output")

    model_config = ConfigDict(extra="allow")


class CommandExecutionError(Exception):
    """Raised by ``motet.do()`` when a command fails.

    Catch this in bundle commands. At runtime the SDK bridge replaces this
    stub with ``motet.core.commands.response_models.CommandExecutionError``
    so ``except`` matches what ``do()`` raises (same pattern as IdentityContext).
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

    ``partial_results`` matches a successful join: domain data per child,
    or ``{_error: True, ...}``.
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
