"""
Motet - DistributedCommandResponseMixin

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    success/error response helpers for DistributedCommand (issue #158).

Usage:
    Mixed into DistributedCommand; not used standalone.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Union

import structlog

from motet.core.commands.distributed_types import DistributedCommandContext
from motet.core.commands.response_models import (
    BaseCommandResponse,
    CommandError,
    CommandMetadata,
)

logger = structlog.get_logger(__name__)


class DistributedCommandResponseMixin:
    """Mixin extracted from DistributedCommand (issue #158)."""

    # Host state initialized by DistributedCommand.__init__ / Command (for type checkers)
    command_id: str
    distributed_context: DistributedCommandContext
    retry_count: int
    queue_time_ms: Optional[float]
    network_time_ms: Optional[float]
    stream_key: Optional[str]
    _stream_enabled: bool
    _stream_ttl: int
    _worker_id: Optional[str]

    if TYPE_CHECKING:
        def get_command_type(self) -> str: ...

        def _estimate_data_size(self, data: Any) -> int: ...

        def _get_stream_event_count(self) -> int: ...

    def _get_worker_id(self) -> Optional[str]:
        """
        Worker ID for ADR-0029 metadata (tracing / attribution).

        Resolution order:
        1. Value set on the command during ``_do_execute`` (e.g. gather/dispatch/map).
        2. Celery ``get_worker_context()`` dict (decorated commands and normal tasks).
        3. ``get_worker_id()`` from env/hostname (always defined in worker processes).

        Returns:
            Optional[str]: Worker id when resolvable; rarely None only if resolution fails.
        """
        if self._worker_id:
            return self._worker_id
        try:
            from motet.core.workers.invoker_context import get_worker_context

            ctx = get_worker_context()
            if isinstance(ctx, dict):
                wid = ctx.get("worker_id")
                if wid:
                    return str(wid)
        except Exception:
            pass
        try:
            from motet.core.workers.worker_utils import get_worker_id

            return get_worker_id()
        except Exception:
            return None

    def _command_metadata(
        self,
        execution_time_ms: float,
        resource_usage: Optional[Dict[str, Any]] = None,
        data: Any = None,
    ) -> CommandMetadata:
        """Build ADR-0029 metadata for wrap-time ``BaseCommandResponse``."""
        streaming = bool(self._stream_enabled)
        return CommandMetadata(
            command_id=self.command_id,
            command_type=self.get_command_type(),
            execution_time_ms=execution_time_ms,
            worker_id=self._get_worker_id(),
            retry_count=self.retry_count,
            capabilities_used=list(self.distributed_context.required_capabilities),
            resource_usage=resource_usage,
            data_size_bytes=self._estimate_data_size(data) if isinstance(data, dict) else None,
            queue_time_ms=self.queue_time_ms,
            network_time_ms=self.network_time_ms,
            streaming_enabled=streaming,
            stream_key=self.stream_key if streaming else None,
            stream_event_count=self._get_stream_event_count() if streaming else None,
            stream_ttl_seconds=self._stream_ttl if streaming else None,
            stream_completed_at=datetime.now() if streaming else None,
        )

    def _dump_envelope(self, envelope: BaseCommandResponse) -> Dict[str, Any]:
        return envelope.model_dump(mode="json")


    def _is_recoverable_error(self, error: Exception) -> bool:
        """
        Determine if an error is recoverable (retry recommended).
        
        Recoverable errors include:
        - Network timeouts and connection errors
        - Temporary resource unavailability
        - Rate limiting errors
        - Transient service failures
        
        Non-recoverable errors include:
        - Validation errors and invalid input
        - Permission denied and authentication failures
        - Resource not found errors
        - Programming errors and assertion failures
        
        Args:
            error: The exception to classify
            
        Returns:
            bool: True if error is recoverable, False otherwise
        """
        # Check for specific recoverable error types
        recoverable_types = (
            TimeoutError,
            ConnectionError,
            ConnectionRefusedError,
            ConnectionResetError,
            # Add more as needed
        )
        
        if isinstance(error, recoverable_types):
            return True
        
        # Check error message for common recoverable patterns
        error_msg = str(error).lower()
        recoverable_patterns = [
            'timeout',
            'timed out',
            'temporarily unavailable',
            'try again',
            'rate limit',
            'too many requests',
            'service unavailable',
            'connection refused',
            'connection reset',
        ]
        
        return any(pattern in error_msg for pattern in recoverable_patterns)


    def _extract_error_details(self, error: Exception) -> Dict[str, Any]:
        """
        Extract command-specific error details.
        
        Provides context about the error including command information
        and any error-specific attributes. Subclasses can override this
        to add custom error details.
        
        Args:
            error: The exception to extract details from
            
        Returns:
            Dict[str, Any]: Error details dictionary
        """
        details = {
            "command_type": self.get_command_type(),
            "command_id": self.command_id,
            "task_id": self.distributed_context.task_id,
        }
        
        # Add error attributes if available (filter out private attributes)
        if hasattr(error, '__dict__'):
            error_attrs = {
                k: v for k, v in error.__dict__.items() 
                if not k.startswith('_')
            }
            details.update(error_attrs)
        
        return details


    def _create_success_response(
        self,
        data: Any,
        execution_time_ms: float,
        warnings: Optional[List[str]] = None,
        resource_usage: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Create a standard success response (ADR-0029 compliant).
        
        Automatically includes:
        - Standard status field ("success")
        - Command-specific result data
        - Execution metadata (timing, worker, capabilities)
        - Streaming metadata (if streaming was enabled)
        - Optional warnings for non-fatal issues
        
        Args:
            data: Command-specific result data
            execution_time_ms: Total execution time in milliseconds
            warnings: Optional list of non-fatal warnings
            resource_usage: Optional resource usage metrics
            
        Returns:
            Dict[str, Any]: ``BaseCommandResponse`` dumped for Redis/JSON
        """
        return self._dump_envelope(
            BaseCommandResponse(
                status="success",
                data=data,
                metadata=self._command_metadata(
                    execution_time_ms, resource_usage=resource_usage, data=data
                ),
                warnings=warnings or [],
            )
        )


    def _create_error_response(
        self,
        error: Exception,
        execution_time_ms: float,
        recoverable: Optional[bool] = None,
        details: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Create a standard error response (ADR-0029 compliant).
        
        Automatically includes:
        - Standard status field ("error")
        - Structured error information
        - Execution metadata
        - Retry recommendations
        
        Args:
            error: The exception that occurred
            execution_time_ms: Total execution time in milliseconds
            recoverable: Whether error is recoverable (auto-detected if None)
            details: Optional custom error details (auto-extracted if None)
            
        Returns:
            Dict[str, Any]: Standard error response envelope
        """
        # Auto-detect recoverability if not specified
        if recoverable is None:
            recoverable = self._is_recoverable_error(error)
        
        # Auto-extract details if not specified
        if details is None:
            details = self._extract_error_details(error)
        
        # Determine if retry is recommended
        retry_recommended = (
            recoverable and 
            self.retry_count < self.distributed_context.max_retries
        )
        
        return self._dump_envelope(
            BaseCommandResponse(
                status="error",
                data=None,
                error=CommandError(
                    type=type(error).__name__,
                    message=str(error),
                    details=details,
                    recoverable=recoverable,
                    retry_recommended=retry_recommended,
                ),
                metadata=self._command_metadata(execution_time_ms),
                warnings=[],
            )
        )


    def _create_partial_success_response(
        self,
        data: Any,
        error: Union[Exception, Dict[str, Any]],
        execution_time_ms: float,
        warnings: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """
        Create a standard partial success response (ADR-0029 compliant).
        
        Used for commands that execute multiple operations where some succeed
        and some fail (e.g., workflows, plans, batch operations).
        
        Args:
            data: Partial result data (successful operations)
            error: Error information for failed operations (Exception or dict)
            execution_time_ms: Total execution time in milliseconds
            warnings: Optional list of non-fatal warnings
            
        Returns:
            Dict[str, Any]: Standard partial success response envelope
        """
        # Convert exception to error dict if needed
        if isinstance(error, Exception):
            error_dict = {
                "type": type(error).__name__,
                "message": str(error),
                "details": self._extract_error_details(error),
                "recoverable": self._is_recoverable_error(error),
                "retry_recommended": False  # Usually don't retry partial successes
            }
        else:
            error_dict = error
        
        return self._dump_envelope(
            BaseCommandResponse(
                status="partial_success",
                data=data,
                error=CommandError.model_validate(error_dict),
                metadata=self._command_metadata(execution_time_ms, data=data),
                warnings=warnings or [],
            )
        )


