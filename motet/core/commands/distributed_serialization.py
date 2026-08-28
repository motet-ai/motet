"""
Motet - DistributedCommandSerializationMixin

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-19

Description:
    Serialization / Redis transport mixin for DistributedCommand (issue #158).
    Extracted mechanically from distributed.py with no behavior change.

Usage:
    class DistributedCommand(DistributedCommandSerializationMixin, Command):
        ...

"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set, Type, Union, cast

import msgpack
import structlog
from pydantic import BaseModel

from motet.core.commands.base import Command, CommandStatus
from motet.core.commands.capabilities import WorkerCapability
from motet.core.commands.distributed_types import (
    DistributedCommandContext,
    DistributionStrategy,
    WorkerAssignment,
)

if TYPE_CHECKING:
    from motet.core.commands.distributed import DistributedCommand

logger = structlog.get_logger(__name__)


class DistributedCommandSerializationMixin:
    """Mixin extracted from DistributedCommand (issue #158)."""

    # Host state initialized by DistributedCommand.__init__ / Command (for type checkers)
    command_id: str
    data: Any
    status: CommandStatus
    distributed_context: DistributedCommandContext
    worker_assignments: List[WorkerAssignment]
    retry_count: int
    last_retry_at: Optional[datetime]
    queue_time_ms: Optional[float]
    execution_time_ms: Optional[float]
    network_time_ms: Optional[float]

    if TYPE_CHECKING:
        def get_command_type(self) -> str: ...

        def __init__(
            self,
            task_id: str,
            data: Any,
            command_id: Optional[str] = None,
            **distributed_kwargs: Any,
        ) -> None: ...

        @classmethod
        def _ensure_commands_registered(cls) -> None: ...

        @classmethod
        def _get_data_class(cls) -> Type[BaseModel]: ...

        @staticmethod
        def _parse_required_capabilities(raw_caps: Any) -> Set[WorkerCapability]: ...

    def serialize_for_transport(self) -> str:
        """
        Serialize command for transport via Celery (synchronous version).
        
        Returns:
            JSON string representation of the command
        """
        # Option A transport format: envelope + payload (no legacy fallback).
        # The envelope contains execution context (routing/identity/tracing) and MUST be authoritative.
        # The payload contains only the command's Pydantic data model.
        envelope: Dict[str, Any] = {
            "command_id": self.command_id,
            "command_type": self.get_command_type(),
            "task_id": self.distributed_context.task_id,
            "conversation_id": getattr(self.distributed_context, "conversation_id", "") or "",
            "tenant_id": getattr(self.distributed_context, "tenant_id", "") or "",
            "principal_id": getattr(self.distributed_context, "principal_id", "") or "",
            # ADR-0056/stream encryption requires a non-empty motet_id; default to "default"
            "motet_id": getattr(self.distributed_context, "motet_id", "") or "default",
            "parent_command_id": getattr(self.distributed_context, "parent_command_id", None),
            "parent_worker_id": getattr(self.distributed_context, "parent_worker_id", None),
            "cancel_scopes": list(getattr(self.distributed_context, "cancel_scopes", None) or []),
            "own_cancel_scope": getattr(self.distributed_context, "own_cancel_scope", None),
            "metadata": getattr(self.distributed_context, "metadata", {}) or {},
            "trace_id": getattr(self.distributed_context, "trace_id", None),
            "parent_span_id": getattr(self.distributed_context, "parent_span_id", None),
            "timeout_seconds": getattr(self.distributed_context, "timeout_seconds", None),
            "priority": getattr(self.distributed_context, "priority", None),
            "max_retries": getattr(self.distributed_context, "max_retries", None),
            "retry_backoff_seconds": getattr(self.distributed_context, "retry_backoff_seconds", None),
            "circuit_breaker_enabled": getattr(self.distributed_context, "circuit_breaker_enabled", None),
            "distributed_trace_enabled": getattr(self.distributed_context, "distributed_trace_enabled", None),
            "tenant_isolation_required": getattr(self.distributed_context, "tenant_isolation_required", None),
            "worker_security_level": getattr(self.distributed_context, "worker_security_level", None),
            "result_aggregation_strategy": getattr(self.distributed_context, "result_aggregation_strategy", None),
            "partial_results_allowed": getattr(self.distributed_context, "partial_results_allowed", None),
            "target_worker_id": getattr(self.distributed_context, "target_worker_id", None),
            "preferred_worker_ids": getattr(self.distributed_context, "preferred_worker_ids", []) or [],
            "worker_affinity": getattr(self.distributed_context, "worker_affinity", None),
            "avoid_worker_ids": getattr(self.distributed_context, "avoid_worker_ids", []) or [],
            "schedule_name": getattr(self.distributed_context, "schedule_name", None),
            "required_capabilities": [
                cap.value for cap in getattr(self.distributed_context, "required_capabilities", set())
            ],
            "created_at": getattr(self.distributed_context, "created_at", None),
            "status": self.status.value if hasattr(self.status, "value") else str(self.status),
        }
        transport: Dict[str, Any] = {"envelope": envelope}
        
        # NOTE: No debug logging here; transport serialization happens for every command and is noisy.
        
        
        # Get command-specific data and determine if it should be stored in Redis
        command_specific_data = self._get_command_specific_data()
        
        
        # Check if data is large enough to warrant Redis storage
        if self._should_use_redis_storage(command_specific_data):
            # Store large data in Redis and include reference in transport payload
            redis_key = self._store_command_data_in_redis(command_specific_data)
            envelope["_redis_data_key"] = redis_key
            envelope["_data_size"] = self._estimate_data_size(command_specific_data)
        else:
            # Include data directly in transport payload for small data
            transport["payload"] = command_specific_data
        
        return json.dumps(transport, default=self._json_serializer)


    def _get_command_specific_data(self) -> Dict[str, Any]:
        """
        Serialize command data using Pydantic (ADR-0017 Phase 3 - Simplified).
        
        All command data classes are now Pydantic models, so we use
        model_dump(mode='json') for automatic, correct serialization.
        
        Returns:
            Dict[str, Any]: Command-specific data to include in serialization
        """
        # Use Pydantic's JSON mode for proper serialization of complex types
        return self.data.model_dump(mode='json')


    def _should_use_redis_storage(self, data: Dict[str, Any]) -> bool:
        """
        Determine if data should be stored in Redis based on size and complexity.
        
        Args:
            data: Command-specific data to evaluate
            
        Returns:
            bool: True if data should be stored in Redis
        """
        # Check if Redis storage is disabled for this command
        if not self.distributed_context.use_redis_storage:
            return False
        
        # ALWAYS store in Redis when debug mode is enabled (for debugging and consistency)
        # This ensures all command inputs are available in debug API, even for small commands
        import os
        debug_mode = os.getenv("MOTET_DEBUG_MODE", "false").lower() == "true"
        if debug_mode:
            return True
        
        # Get configuration
        from motet.core.config import Config
        config = Config()
        
        # If threshold is 0, always use Redis (when enabled)
        if config.redis_command_size_threshold_bytes == 0:
            return True
        
        # Use Redis storage if data is larger than threshold or contains complex objects
        estimated_size = self._estimate_data_size(data)
        return estimated_size > config.redis_command_size_threshold_bytes or self._contains_complex_objects(data)


    def _estimate_data_size(self, data: Dict[str, Any]) -> int:
        """
        Estimate the serialized size of data in bytes.
        
        Args:
            data: Data to estimate size for
            
        Returns:
            int: Estimated size in bytes
        """
        import json
        try:
            serialized = json.dumps(data, default=self._json_serializer)
            return len(serialized.encode('utf-8'))
        except (TypeError, ValueError):
            # Fallback estimation for non-serializable data
            return len(str(data).encode('utf-8'))


    def _contains_complex_objects(self, data: Dict[str, Any]) -> bool:
        """
        Check if data contains complex objects that benefit from MsgPack serialization.
        
        Args:
            data: Data to check
            
        Returns:
            bool: True if data contains complex objects
        """
        from motet.core.config import Config
        config = Config()
        
        for value in data.values():
            if isinstance(value, (list, dict)) and len(str(value)) > config.redis_command_complex_object_threshold:
                return True
            elif hasattr(value, '__dict__') and not isinstance(value, (str, int, float, bool)):
                return True
        return False


    def _should_store_result_in_redis(self, result: Any) -> bool:
        """
        Determine if a command result should be stored in Redis based on size and complexity.
        
        Args:
            result: Command execution result to evaluate
            
        Returns:
            bool: True if result should be stored in Redis
        """
        from motet.core.config import Config
        config = Config()
        
        # Check if Redis storage is disabled
        if not self.distributed_context.use_redis_storage:
            return False
        
        # If threshold is 0, always use Redis
        if config.redis_command_size_threshold_bytes == 0:
            return True
        
        # Estimate result size
        estimated_size = self._estimate_result_size(result)
        
        # Use Redis if result is larger than threshold or contains complex objects
        return estimated_size > config.redis_command_size_threshold_bytes or self._result_contains_complex_objects(result)


    def _estimate_result_size(self, result: Any) -> int:
        """
        Estimate the size of a result in bytes.
        
        Args:
            result: Result to estimate size for
            
        Returns:
            int: Estimated size in bytes
        """
        try:
            # Convert to string and estimate size
            result_str = str(result)
            return len(result_str.encode('utf-8'))
        except Exception:
            # Fallback: estimate based on object type
            if isinstance(result, (list, dict)):
                return len(str(result)) * 2  # Rough estimate
            else:
                return len(str(result))


    def _result_contains_complex_objects(self, result: Any) -> bool:
        """
        Check if a result contains complex objects that benefit from MsgPack serialization.
        
        Args:
            result: Result to check
            
        Returns:
            bool: True if result contains complex objects
        """
        from motet.core.config import Config
        config = Config()
        
        # Check if result is a complex structure
        if isinstance(result, (list, dict)):
            result_str = str(result)
            if len(result_str) > config.redis_command_complex_object_threshold:
                return True
        
        # Check for complex objects with attributes
        if hasattr(result, '__dict__') and not isinstance(result, (str, int, float, bool)):
            return True
        
        return False


    def _store_command_data_in_redis(self, data: Dict[str, Any]) -> str:
        """
        Store command data in Redis using RedisCommandDataManager (synchronous version).
        
        Args:
            data: Command-specific data to store
            
        Returns:
            str: Redis key where data was stored
        """
        from motet.core.distributed import get_redis_command_data_manager
        
        # Get Redis command data manager
        redis_manager = get_redis_command_data_manager()
        
        # Create a CommandData instance for this command type
        command_data: Any = self._create_command_data_instance(data)
        
        # Convert to dict for storage
        # Handle both BaseCommandData instances and dicts
        if hasattr(command_data, 'to_dict'):
            data_to_store = command_data.to_dict()
        elif hasattr(command_data, 'model_dump'):
            data_to_store = command_data.model_dump()
        elif isinstance(command_data, dict):
            data_to_store = command_data
        else:
            raise TypeError(f"Unexpected command_data type: {type(command_data)}")
        
        # Store in Redis with TTL based on command timeout (sync version)
        # Pass tenant_id for encryption at rest (ADR-0056 Phase 1B)
        redis_key = redis_manager.store_command_data(
            command_id=self.command_id,
            data=data_to_store,
            command_timeout_seconds=self.distributed_context.timeout_seconds,
            command_type=self.get_command_type(),
            tenant_id=self.distributed_context.tenant_id if self.distributed_context.tenant_id else None,
            motet_id=self.distributed_context.motet_id if self.distributed_context.motet_id else None
        )
        
        return redis_key


    def _create_command_data_instance(self, data: Dict[str, Any]):
        """
        Create a CommandData instance from serialized data.
        
        Args:
            data: Serialized command data (dict or BaseCommandData instance)
            
        Returns:
            BaseCommandData: CommandData instance
        """
        from motet.core.commands.command_data_classes import get_command_data_class, create_command_data
        from motet.core.commands.base_command_data import BaseCommandData
        
        # If data is already a BaseCommandData instance, return it directly
        if isinstance(data, BaseCommandData):
            return data
        
        # Get the appropriate CommandData class for this command type
        command_type = self.get_command_type()
        data_class = get_command_data_class(command_type)
        
        if data_class:
            # IMPORTANT:
            # This method is used when *storing* command data in Redis, where `data` was produced by
            # `self.data.model_dump(...)`. Filtering out "DistributedCommandContext-like" keys is unsafe
            # because command payload schemas can legitimately include fields with the same names
            # (e.g., CreateArtifactData includes `conversation_id` and uses `metadata` for artifact metadata).
            #
            # Safer approach: keep only fields that are defined on the CommandData model.
            allowed_fields = set(getattr(data_class, "model_fields", {}).keys())
            filtered_data = {k: v for k, v in data.items() if k in allowed_fields}
            
            # NOTE: Removed noisy prints for model_inference deserialization.
            
            # Use Pydantic validation for command data creation
            command_data = data_class.model_validate(filtered_data)
            return command_data
        else:
            # No registered data class found (e.g. hot-loaded bundle commands whose data class
            # is only available on workers, not in the API process).  Returning the raw dict
            # preserves ALL fields (including bundle-specific ones like `name`, `shout`, etc.)
            # so that Redis storage and subsequent worker deserialization receive the full payload.
            return data


    def _json_serializer(self, obj):
        """
        Custom JSON serializer for types not handled by standard JSON (ADR-0017 Phase 3).
        
        Simplified for Pydantic-first architecture. Pydantic's mode='json' handles
        most serialization, so this only handles edge cases.
        
        Args:
            obj: Object to serialize
            
        Returns:
            Serializable representation of the object
            
        Raises:
            TypeError: If object is not JSON serializable
        """
        # Pydantic models - use JSON mode for proper serialization
        if hasattr(obj, 'model_dump'):
            return obj.model_dump(mode='json')
        
        # Datetime objects
        if isinstance(obj, datetime):
            return obj.isoformat()
        
        # Enum objects
        if isinstance(obj, Enum):
            return obj.value
        
        # Sets (convert to lists for JSON)
        if isinstance(obj, set):
            return list(obj)
        
        # Bytes - base64 encode for JSON transport (binary data)
        if isinstance(obj, bytes):
            import base64
            return base64.b64encode(obj).decode('utf-8')
        
        # Let JSON encoder handle everything else or raise error
        # This provides clearer error messages than silent fallbacks
        raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


    @classmethod
    def _get_common_fields(cls) -> Set[str]:
        """Return common fields that are not part of the config"""
        return {
            'command_id', 'command_type', 'task_id', 'conversation_id', 
            'principal_id', 'tenant_id', 'motet_id', 'created_at', 'status',
            'timeout_seconds', 'priority', 'max_retries', 'trace_id'
        }


    @classmethod
    def _extract_distributed_params(cls, data: Dict[str, Any]) -> Dict[str, Any]:
        """Extract distributed parameters from serialized data"""
        return {
            'conversation_id': data.get('conversation_id', ''),
            'tenant_id': data.get('tenant_id', 'default'),
            'principal_id': data.get('principal_id', ''),
            'motet_id': data.get('motet_id', 'default'),
            'trace_id': data.get('trace_id'),
            'timeout_seconds': data.get('timeout_seconds', 60),
            'priority': data.get('priority', 5),
            'max_retries': data.get('max_retries', 3)
        }


    def to_dict(self) -> Dict[str, Any]:
        """
        Override base to_dict() to include command-specific data (ADR-0017 Phase 3).
        
        Returns:
            Dictionary with all command fields including data
        """
        # Get base command fields (Command.to_dict; mixins are not subclasses of Command)
        base_dict = Command.to_dict(cast(Command, self))
        
        # Add command-specific data using Pydantic serialization
        if hasattr(self, 'data') and self.data is not None:
            command_data = self._get_command_specific_data()
            base_dict.update(command_data)
        
        return base_dict


    def to_serializable_dict(self) -> Dict[str, Any]:
        """
        Convert command to a dictionary that can be serialized for network transport.
        
        Returns:
            Dictionary representation suitable for msgpack serialization
        """
        # Option A transport format: envelope + payload (no legacy fallback).
        # Keep execution state fields at top-level for retry/status tracking.
        base_dict: Dict[str, Any] = {
            "command_id": self.command_id,
            "command_type": self.get_command_type(),
            "task_id": self.distributed_context.task_id,
            "status": self.status.value if hasattr(self.status, "value") else str(self.status),
            "created_at": getattr(self.distributed_context, "created_at", None),
            "envelope": {
                "command_id": self.command_id,
                "command_type": self.get_command_type(),
                "task_id": self.distributed_context.task_id,
                "conversation_id": getattr(self.distributed_context, "conversation_id", "") or "",
                "tenant_id": getattr(self.distributed_context, "tenant_id", "") or "",
                "principal_id": getattr(self.distributed_context, "principal_id", "") or "",
                "motet_id": getattr(self.distributed_context, "motet_id", "") or "default",
                "parent_command_id": getattr(self.distributed_context, "parent_command_id", None),
                "parent_worker_id": getattr(self.distributed_context, "parent_worker_id", None),
                "cancel_scopes": list(getattr(self.distributed_context, "cancel_scopes", None) or []),
                "own_cancel_scope": getattr(self.distributed_context, "own_cancel_scope", None),
                "metadata": getattr(self.distributed_context, "metadata", {}) or {},
                "trace_id": getattr(self.distributed_context, "trace_id", None),
                "parent_span_id": getattr(self.distributed_context, "parent_span_id", None),
                "timeout_seconds": getattr(self.distributed_context, "timeout_seconds", None),
                "priority": getattr(self.distributed_context, "priority", None),
                "max_retries": getattr(self.distributed_context, "max_retries", None),
                "target_worker_id": getattr(self.distributed_context, "target_worker_id", None),
                "preferred_worker_ids": getattr(self.distributed_context, "preferred_worker_ids", []) or [],
                "worker_affinity": getattr(self.distributed_context, "worker_affinity", None),
                "avoid_worker_ids": getattr(self.distributed_context, "avoid_worker_ids", []) or [],
                "schedule_name": getattr(self.distributed_context, "schedule_name", None),
                "required_capabilities": [
                    cap.value for cap in getattr(self.distributed_context, "required_capabilities", set())
                ],
                "created_at": getattr(self.distributed_context, "created_at", None),
                "status": self.status.value if hasattr(self.status, "value") else str(self.status),
            },
            "payload": self._get_command_specific_data() if hasattr(self, "data") and self.data is not None else {},
        }
        
        # Add distributed-specific fields
        base_dict.update({
            "distributed_context": {
                "required_capabilities": [cap.value for cap in self.distributed_context.required_capabilities],
                "distribution_strategy": self.distributed_context.distribution_strategy.value,
                "max_workers": self.distributed_context.max_workers,
                "timeout_seconds": self.distributed_context.timeout_seconds,
                "preferred_worker_ids": self.distributed_context.preferred_worker_ids,
                "priority": self.distributed_context.priority,
                "target_worker_id": self.distributed_context.target_worker_id,
                "max_retries": self.distributed_context.max_retries,
                "retry_backoff_seconds": self.distributed_context.retry_backoff_seconds,
                "circuit_breaker_enabled": self.distributed_context.circuit_breaker_enabled,
                "trace_id": self.distributed_context.trace_id,
                "parent_span_id": self.distributed_context.parent_span_id,
                "distributed_trace_enabled": self.distributed_context.distributed_trace_enabled,
                "tenant_isolation_required": self.distributed_context.tenant_isolation_required,
                "worker_security_level": self.distributed_context.worker_security_level,
                "result_aggregation_strategy": self.distributed_context.result_aggregation_strategy,
                "partial_results_allowed": self.distributed_context.partial_results_allowed,
            },
            "worker_assignments": [
                {
                    "worker_id": assignment.worker_id,
                    "worker_type": assignment.worker_type,
                    "capabilities": [cap.value for cap in assignment.capabilities],
                    "assigned_at": assignment.assigned_at.isoformat(),
                    "started_at": assignment.started_at.isoformat() if assignment.started_at else None,
                    "completed_at": assignment.completed_at.isoformat() if assignment.completed_at else None,
                    "error": assignment.error,
                }
                for assignment in self.worker_assignments
            ],
            "retry_count": self.retry_count,
            "last_retry_at": self.last_retry_at.isoformat() if self.last_retry_at else None,
            "queue_time_ms": self.queue_time_ms,
            "execution_time_ms": self.execution_time_ms,
            "network_time_ms": self.network_time_ms,
        })
        
        return base_dict


    def serialize(self) -> bytes:
        """
        Serialize the command for network transport using msgpack.
        
        Returns:
            Serialized command as bytes
        """
        data = self.to_serializable_dict()
        return cast(bytes, msgpack.packb(data, use_bin_type=True))


    @classmethod
    def deserialize(cls, data: bytes) -> DistributedCommand:
        """
        Deserialize a command from msgpack bytes (ADR-0017 Phase 3 - Simplified).
        
        Uses the same deserialization pattern as deserialize_from_transport()
        to leverage Pydantic-based data reconstruction.
        
        Args:
            data: Serialized command bytes
            
        Returns:
            Deserialized DistributedCommand instance
        """
        unpacked = msgpack.unpackb(data, raw=False)
        
        envelope = unpacked.get("envelope")
        if not isinstance(envelope, dict):
            raise ValueError("Missing envelope in deserialized msgpack data")
        command_type = envelope.get("command_type")
        if not command_type:
            raise ValueError("Missing command_type in deserialized msgpack data envelope")
        
        # Ensure all command types are registered
        cls._ensure_commands_registered()
        
        # Look up the command class in the new CommandTypeRegistry
        from motet.core.commands.command_type_registry import command_type_registry
        registration = command_type_registry.get(command_type)
        if not registration:
            available_types = command_type_registry.get_command_types()
            raise ValueError(f"Unknown command type: {command_type}. Available types: {', '.join(available_types)}")
        
        # Get the command class from the registration and use its deserialization method
        # This works for both class-based and decorator-based commands since DecoratedCommand
        # inherits from DistributedCommand and has the same deserialization interface
        impl: Any = registration.implementation
        command = impl._deserialize_from_data(unpacked)
        
        # Restore state
        command.status = CommandStatus(unpacked["status"])
        command.retry_count = unpacked.get("retry_count", 0)
        
        if unpacked.get("last_retry_at"):
            command.last_retry_at = datetime.fromisoformat(unpacked["last_retry_at"])
        
        command.queue_time_ms = unpacked.get("queue_time_ms")
        command.execution_time_ms = unpacked.get("execution_time_ms")
        command.network_time_ms = unpacked.get("network_time_ms")
        
        return command


    @classmethod
    def _retrieve_command_data_from_redis(cls, redis_key: str, tenant_id: Optional[str] = None, motet_id: Optional[str] = None):
        """
        Retrieve command data from Redis using RedisCommandDataManager.
        
        Args:
            redis_key: Redis key where command data is stored
            tenant_id: Optional tenant ID for decryption (extracted from encrypted blob if not provided)
            motet_id: Optional motet ID for decryption (used in AAD, falls back to metadata)
            
        Returns:
            BaseCommandData: Retrieved command data or None if not found
        """
        from motet.core.distributed import get_redis_command_data_manager
        
        try:
            # Get Redis command data manager
            redis_manager = get_redis_command_data_manager()
            
            # Use the sync version for Celery workers
            # tenant_id will be extracted from encrypted blob if not provided (handled in retrieve_command_data)
            command_data = redis_manager.retrieve_command_data(redis_key, tenant_id=tenant_id, motet_id=motet_id)
            
            return command_data
            
        except Exception as e:
            import structlog
            logger = structlog.get_logger(__name__)
            logger.error(
                "Failed to retrieve command data from Redis (sync)",
                redis_key=redis_key,
                tenant_id=tenant_id,
                motet_id=motet_id,
                error=str(e),
                exc_info=True
            )
            return None


    @classmethod
    def _deserialize_command_data(cls, data: Dict[str, Any]):
        """
        Generic deserialization for command data using Pydantic (ADR-0017 Phase 3).
        
        This method leverages the data class registry and Pydantic's model_validate()
        to automatically deserialize command-specific data without custom logic.
        
        Override _prepare_data_for_deserialization() for custom preprocessing needs.
        
        Args:
            data: Dictionary containing command data
            
        Returns:
            BaseCommandData: Deserialized command data instance
        """
        data_class = cls._get_data_class()
        prepared_data = cls._prepare_data_for_deserialization(data)

        # Warning-mode visibility for silently dropped payload keys: pydantic ignores
        # unknown fields, so a misnamed key (e.g. `message` instead of `messages`)
        # validates as an empty one and fails far from the source. Schedule creation
        # hard-rejects via validate_command_data(); every other entry point at least
        # logs here. Precursor to a strict extra="forbid" migration.
        from motet.core.commands.base_command_data import unknown_command_data_keys
        dropped_keys = unknown_command_data_keys(data_class, prepared_data)
        if dropped_keys:
            logger.warning(
                "command_data_unknown_fields_dropped",
                data_class=data_class.__name__,
                command_class=cls.__name__,
                dropped_keys=dropped_keys,
            )

        return data_class.model_validate(prepared_data)


    @classmethod
    def _prepare_data_for_deserialization(cls, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Prepare data dict for Pydantic validation (ADR-0017 Phase 3).
        
        Override in subclasses for custom preprocessing (e.g., legacy field migrations).
        Default: pass through unchanged - Pydantic validators handle Message conversion.
        
        Args:
            data: Raw dictionary data
            
        Returns:
            Prepared dictionary ready for Pydantic validation
        """
        return data


    @classmethod
    def _extract_distributed_kwargs(cls, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Extract common distributed command parameters from data dict (ADR-0017 Phase 3).
        
        This centralizes the extraction of standard distributed parameters,
        eliminating duplication across command implementations.
        
        Args:
            data: Dictionary containing serialized command data
            
        Returns:
            Dictionary of distributed kwargs for command constructor
        """
        # Under Option A transport, this method is passed the transport envelope dict.
        return {
            "conversation_id": data.get("conversation_id", "") or "",
            "tenant_id": data.get("tenant_id", "") or "",
            "principal_id": data.get("principal_id", "") or "",
            # ADR-0056/stream encryption requires a non-empty motet_id; default to "default"
            "motet_id": data.get("motet_id", "default") or "default",
            "metadata": data.get("metadata", {}) or {},
            "trace_id": data.get("trace_id"),
            "parent_span_id": data.get("parent_span_id"),
            "parent_command_id": data.get("parent_command_id"),
            "parent_worker_id": data.get("parent_worker_id"),
            "cancel_scopes": data.get("cancel_scopes") or [],
            "own_cancel_scope": data.get("own_cancel_scope"),
            "timeout_seconds": data.get("timeout_seconds", 60),
            "priority": data.get("priority", 5),  # EventPriority.NORMAL
            "max_retries": data.get("max_retries", 3),
            "retry_backoff_seconds": data.get("retry_backoff_seconds", 1.0),
            "circuit_breaker_enabled": data.get("circuit_breaker_enabled", True),
            "distributed_trace_enabled": data.get("distributed_trace_enabled", True),
            "tenant_isolation_required": data.get("tenant_isolation_required", True),
            "worker_security_level": data.get("worker_security_level", "standard"),
            "result_aggregation_strategy": data.get("result_aggregation_strategy", "first_success"),
            "partial_results_allowed": data.get("partial_results_allowed", False),
            # Worker targeting fields (ADR-0025)
            "target_worker_id": data.get("target_worker_id"),
            "preferred_worker_ids": data.get("preferred_worker_ids", []) or [],
            "worker_affinity": data.get("worker_affinity"),
            "avoid_worker_ids": data.get("avoid_worker_ids", []) or [],
            "schedule_name": data.get("schedule_name"),
            "required_capabilities": data.get("required_capabilities", []) or [],
        }


    @classmethod
    def _deserialize_from_data(cls, data: Dict[str, Any]) -> DistributedCommand:
        """
        Deserialize command from dictionary data (ADR-0017 Phase 3 - Simplified).
        
        Standard implementation that works for most commands. Commands only need
        to override this if they have truly unique deserialization needs.
        
        For most commands, simply implementing _get_data_class() is sufficient.
        
        Args:
            data: Dictionary containing command data
            
        Returns:
            DistributedCommand: Deserialized command instance
        """
        envelope = data.get("envelope")
        payload = data.get("payload")
        if not isinstance(envelope, dict):
            raise ValueError("Option A transport requires 'envelope' dict for deserialization")
        if payload is None:
            raise ValueError("Option A transport requires 'payload' for deserialization")
        if not isinstance(payload, dict):
            raise ValueError("Option A transport requires 'payload' to be a dict")

        # NOTE: No debug logging here; command deserialization is high-volume and noisy.

        # Deserialize command-specific payload using Pydantic (auto-handles Message conversion)
        command_data = cls._deserialize_command_data(payload)

        # Extract standard distributed kwargs from envelope
        distributed_kwargs = cls._extract_distributed_kwargs(envelope)

        # NOTE: No debug logging here.

        # Create command instance
        command = cls(
            task_id=envelope.get("task_id", ""),
            data=command_data,
            command_id=envelope.get("command_id"),
            **distributed_kwargs,
        )
        serialized_caps = cls._parse_required_capabilities(
            envelope.get("required_capabilities", []) or []
        )
        if serialized_caps:
            command.distributed_context.required_capabilities = serialized_caps
        return cast("DistributedCommand", command)


    @classmethod
    def _deserialize_from_data_legacy(cls, data: Dict[str, Any]) -> DistributedCommand:
        """
        Legacy deserialization method for backward compatibility.
        
        This is the old implementation before ADR-0017 Phase 3.
        Kept for reference during migration.
        
        Args:
            data: Dictionary containing command data
            
        Returns:
            DistributedCommand: Deserialized command instance
        """
        # This is a base implementation - specific command types should override
        # to properly reconstruct their specific attributes
        
        # Reconstruct context
        # Handle metadata field - ensure it's always a dict, never None
        metadata = data.get("metadata")
        if metadata is None:
            metadata = {}
        
        context = DistributedCommandContext(
            task_id=data.get("task_id", ""),
            conversation_id=data.get("conversation_id", ""),
            tenant_id=data.get("tenant_id", ""),
            principal_id=data.get("principal_id", ""),
            # ADR-0056/stream encryption requires a non-empty motet_id; default to "default"
            motet_id=data.get("motet_id", "default") or "default",
            metadata=metadata,
            required_capabilities=set(),
            distribution_strategy=DistributionStrategy.SINGLE_WORKER,
            max_workers=1,
            timeout_seconds=data.get("timeout_seconds", 60),
            preferred_worker_ids=[],
            priority=data.get("priority", 5),  # EventPriority.NORMAL
            target_worker_id=data.get("target_worker_id"),
            max_retries=data.get("max_retries", 3),
            retry_backoff_seconds=data.get("retry_backoff_seconds", 1.0),
            circuit_breaker_enabled=data.get("circuit_breaker_enabled", True),
            trace_id=data.get("trace_id", str(uuid.uuid4())),
            parent_span_id=data.get("parent_span_id"),
            parent_command_id=data.get("parent_command_id"),
            parent_worker_id=data.get("parent_worker_id"),
            cancel_scopes=data.get("cancel_scopes") or [],
            own_cancel_scope=data.get("own_cancel_scope"),
            distributed_trace_enabled=data.get("distributed_trace_enabled", True),
            tenant_isolation_required=data.get("tenant_isolation_required", True),
            worker_security_level=data.get("worker_security_level", "standard"),
            result_aggregation_strategy=data.get("result_aggregation_strategy", "first_success"),
            partial_results_allowed=data.get("partial_results_allowed", False),
            schedule_name=data.get("schedule_name")  # Include schedule name during deserialization
        )
        
        # Create command instance (legacy Command(command_id, context) signature)
        command = cast(Any, cls)(data.get("command_id", ""), context)
        
        # Restore state
        if data.get("status"):
            command.status = CommandStatus(data["status"])
        command.retry_count = data.get("retry_count", 0)
        
        if data.get("last_retry_at"):
            command.last_retry_at = datetime.fromisoformat(data["last_retry_at"])
        
        command.queue_time_ms = data.get("queue_time_ms")
        command.execution_time_ms = data.get("execution_time_ms")
        command.network_time_ms = data.get("network_time_ms")
        
        return command


    def store_result_in_redis(self, result: Any) -> str:
        """
        Store command result in Redis using RedisCommandDataManager.
        
        Args:
            result: Command execution result to store
            
        Returns:
            str: Redis key where result was stored
        """
        from motet.core.distributed import get_redis_command_data_manager
        
        # Get Redis command data manager
        redis_manager = get_redis_command_data_manager()
        
        # Store result in Redis with TTL based on command timeout
        redis_key = redis_manager.store_command_result(
            command_id=self.command_id,
            result=result,
            command_timeout_seconds=self.distributed_context.timeout_seconds,
            command_type=self.get_command_type(),
            tenant_id=self.distributed_context.tenant_id,
            motet_id=self.distributed_context.motet_id
        )
        
        return redis_key


    def retrieve_result_from_redis(self) -> Any:
        """
        Retrieve command result from Redis using RedisCommandDataManager.
        
        Returns:
            Any: Retrieved command result or None if not found
        """
        from motet.core.distributed import get_redis_command_data_manager
        
        try:
            # Get Redis command data manager
            redis_manager = get_redis_command_data_manager()
            
            # Retrieve result from Redis
            result = redis_manager.retrieve_command_result(
                self.command_id,
                tenant_id=self.distributed_context.tenant_id,
                motet_id=self.distributed_context.motet_id
            )
            
            return result
            
        except Exception as e:
            import structlog
            logger = structlog.get_logger(__name__)
            logger.error(
                "Failed to retrieve command result from Redis",
                command_id=self.command_id,
                error=str(e),
                exc_info=True
            )
            return None


    def get_full_result_from_task_result(self, task_result: Dict[str, Any]) -> Any:
        """
        Retrieve the full command result from a task result, handling Redis storage.
        
        This method can be used to get the complete result regardless of whether 
        it's stored inline or in Redis.
        
        Args:
            task_result: The task result dictionary from Celery
            
        Returns:
            The full command result
        """
        if not task_result or task_result.get("status") != "completed":
            return task_result
        
        result = task_result.get("result")
        if not result:
            return task_result
        
        # Check if result is stored in Redis
        if isinstance(result, dict) and "_redis_result_key" in result:
            redis_key = result["_redis_result_key"]
            try:
                # Retrieve result from Redis using the existing method
                full_result = self.retrieve_result_from_redis_key(redis_key)
                
                # Return the task result with the full result
                return {
                    **task_result,
                    "result": full_result,
                    "result_retrieved_from_redis": True
                }
                
            except Exception as e:
                import structlog
                logger = structlog.get_logger(__name__)
                logger.error(
                    "Failed to retrieve result from Redis key",
                    redis_key=redis_key,
                    error=str(e),
                    exc_info=True
                )
                # Return the original result with error info
                return {
                    **task_result,
                    "result_retrieval_error": str(e)
                }
        
        # Result is stored inline, return as-is
        return task_result


    def retrieve_result_from_redis_key(self, redis_key: str) -> Any:
        """
        Retrieve command result from Redis using a specific Redis key.
        
        Args:
            redis_key: The Redis key to retrieve the result from
            
        Returns:
            Any: Retrieved command result or None if not found
        """
        from motet.core.distributed import get_redis_command_data_manager
        
        try:
            # Get Redis command data manager
            redis_manager = get_redis_command_data_manager()
            
            # Retrieve result from Redis using the key
            result = redis_manager.retrieve_command_result(
                redis_key,
                tenant_id=self.distributed_context.tenant_id,
                motet_id=self.distributed_context.motet_id
            )
            
            return result
            
        except Exception as e:
            import structlog
            logger = structlog.get_logger(__name__)
            logger.error(
                "Failed to retrieve command result from Redis key",
                redis_key=redis_key,
                error=str(e),
                exc_info=True
            )
            return None


    @classmethod
    def deserialize_from_transport(cls, transport_data: str) -> DistributedCommand:
        """
        Deserialize a command from transport JSON with Redis-based data retrieval.
        
        Args:
            transport_data: JSON string from transport layer
            
        Returns:
            Deserialized DistributedCommand instance
        """
        import json
        
        transport_obj = json.loads(transport_data)
        envelope = transport_obj.get("envelope")
        if not isinstance(envelope, dict):
            raise ValueError("Option A transport requires top-level 'envelope' dict")

        command_type = envelope.get("command_type")
        if not command_type:
            raise ValueError("Option A transport requires envelope.command_type")

        payload: Dict[str, Any]
        redis_data_key = envelope.get("_redis_data_key")
        if redis_data_key:
            tenant_id = envelope.get("tenant_id")
            motet_id = envelope.get("motet_id")
            payload_data = cls._retrieve_command_data_from_redis(redis_data_key, tenant_id=tenant_id, motet_id=motet_id)
            if not isinstance(payload_data, dict):
                import structlog
                logger = structlog.get_logger(__name__)
                logger.error(
                    "Failed to retrieve command payload from Redis (sync) - cannot proceed without command data",
                    redis_key=redis_data_key,
                    command_id=envelope.get("command_id"),
                    command_type=command_type,
                )
                raise ValueError(
                    f"Command payload not available for {command_type} command {envelope.get('command_id', 'unknown')}. "
                    f"Redis key: {redis_data_key}"
                )
            payload = payload_data
        else:
            payload_obj = transport_obj.get("payload")
            if not isinstance(payload_obj, dict):
                raise ValueError("Option A transport requires top-level 'payload' dict when no redis payload ref present")
            payload = payload_obj

        structured = {"envelope": envelope, "payload": payload}
        
        # Ensure all command types are registered
        cls._ensure_commands_registered()
        
        # Look up the command class in the new CommandTypeRegistry
        from motet.core.commands.command_type_registry import command_type_registry
        registration = command_type_registry.get(command_type)
        if not registration:
            available_types = command_type_registry.get_command_types()
            raise ValueError(f"Unknown command type: {command_type}. Available types: {', '.join(available_types)}")
        
        # Get the command class from the registration and use its deserialization method
        # This works for both class-based and decorator-based commands since DecoratedCommand
        # inherits from DistributedCommand and has the same deserialization interface
        impl: Any = registration.implementation
        return impl._deserialize_from_data(structured)


    @classmethod
    def rehydrate_command_result(cls, task_result: Dict[str, Any]) -> Dict[str, Any]:
        """
        Rehydrate command result from Redis if it's stored there.
        
        This class method can be used by any consumer of command results
        to automatically retrieve the full result from Redis if needed.
        
        Args:
            task_result: The task result dictionary from Celery
            
        Returns:
            The task result with the full result data (rehydrated from Redis if needed)
        """
        # ADR-0029: Accept both "success" (event format) and "completed" (legacy Celery format)
        status = task_result.get("status") if task_result else None
        if not task_result or status not in ("completed", "success"):
            return task_result
        
        result = task_result.get("result")
        if not result:
            return task_result
        
        if not (isinstance(result, dict) and result.get("_redis_result_key")):
            return task_result

        try:
            from motet.core.distributed import get_redis_command_data_manager

            tenant_id = task_result.get("tenant_id") or task_result.get(
                "data", {}
            ).get("tenant_id")
            motet_id = task_result.get("motet_id") or task_result.get(
                "data", {}
            ).get("motet_id")
            return get_redis_command_data_manager().hydrate_wait_outcome_envelope(
                task_result,
                tenant_id=tenant_id,
                motet_id=motet_id,
            )
        except Exception as e:
            redis_key = result.get("_redis_result_key")
            logger.error(
                "Failed to retrieve result from Redis",
                redis_key=redis_key,
                error=str(e),
                exc_info=True,
            )
            return {**task_result, "result_retrieval_error": str(e)}


