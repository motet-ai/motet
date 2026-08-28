"""
Motet - Distributed Command System

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-26

Description:
    Enhanced command system for distributed execution across worker pools with
    serialization, routing, and result aggregation. Extends the base Command
    system to support distributed execution with worker capability management.

    Types and mixins live in sibling modules (issue #158):
    ``distributed_types``, ``distributed_serialization``,
    ``distributed_streaming``, ``distributed_responses``.

Dependencies:
    - pydantic: Data validation and serialization
    - msgpack: Efficient binary serialization
    - uuid: Unique identifier generation
    - enum: Worker capability definitions

Usage:
    from motet.core.commands.distributed import DistributedCommand
    
    class MyCommand(DistributedCommand):
        async def execute(self, context):
            return {"result": "success"}

Notes:
    - Supports distributed execution across worker pools
    - Includes worker capability management
    - Provides serialization and routing mechanisms
    - Integrates with command context and status tracking
    - Mechanical mixin split; public import path unchanged
"""

from __future__ import annotations

import importlib
import json
import os
import uuid
from abc import abstractmethod
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Type, Union, cast
import msgpack

import structlog
from pydantic import BaseModel, Field

from motet.core.commands.base import Command, CommandContext, CommandStatus
from motet.core.commands.capabilities import WorkerCapability
from motet.core.commands.distributed_types import (
    DistributionStrategy,
    ScheduleType,
    WorkerAssignment,
    DistributedCommandContext,
)
from motet.core.commands.distributed_serialization import DistributedCommandSerializationMixin
from motet.core.commands.distributed_streaming import DistributedCommandStreamingMixin
from motet.core.commands.distributed_responses import DistributedCommandResponseMixin
# Note: Cannot import EventPriority here due to circular import with eventing.command_invoker
# EventPriority values: LOW=1, NORMAL=5, HIGH=10, CRITICAL=15

logger = structlog.get_logger(__name__)


class DistributedCommand(
    DistributedCommandSerializationMixin,
    DistributedCommandStreamingMixin,
    DistributedCommandResponseMixin,
    Command,
):
    """Simplified command class for distributed execution"""
    
    def __init__(
        self, 
        task_id: str,
        data: Any,  # Command-specific data dataclass
        command_id: Optional[str] = None,  # Optional - will be generated if not provided
        **distributed_kwargs  # Common distributed parameters
    ):
        # Generate command_id if not provided
        if command_id is None:
            command_id = str(uuid.uuid4())
        
        # NOTE: Intentionally no transport-level debug logging here. Command instantiation is
        # high-volume; prefer targeted logging at call sites when debugging.
        
        # Create context with sensible defaults
        context = self._create_context(task_id, **distributed_kwargs)
        super().__init__(command_id, context)
        
        # Store distributed context and command-specific data
        self.distributed_context = context
        self.data = data
        
        # NOTE: Intentionally no transport-level debug logging here (too noisy in production).
        
        # Set pool type preference in context (ADR-0033)
        self.distributed_context.preferred_pool_type = self._get_preferred_pool_type()
        
        # Streaming support (optional - only used by streaming commands)
        # Initialize BEFORE _setup_command_specifics() so it can be enabled
        self.stream_key: Optional[str] = distributed_kwargs.get('stream_key')
        self._stream_enabled: bool = False
        self._stream_event_counter: int = 0  # Track events written to stream (ADR-0029)
        self._stream_ttl: int = 3600  # Default TTL for streams (ADR-0029)
        
        # Setup command specifics (may enable streaming)
        self._setup_command_specifics()

        from motet.core.distributed.task_control import apply_command_cancel_scopes

        apply_command_cancel_scopes(self)
        
        # Distribution state
        self.worker_assignments: List[WorkerAssignment] = []
        self.distribution_started_at: Optional[datetime] = None
        self.distribution_completed_at: Optional[datetime] = None
        
        # Results from distributed execution
        self.worker_results: Dict[str, Any] = {}
        self.aggregated_result: Optional[Any] = None
        
        # Retry state
        self.retry_count: int = 0
        self.last_retry_at: Optional[datetime] = None
        
        # Performance metrics
        self.queue_time_ms: Optional[float] = None
        self.execution_time_ms: Optional[float] = None
        self.network_time_ms: Optional[float] = None
        
        # Worker context (set during execution for helper methods) (ADR-0029)
        self._worker_id: Optional[str] = None


    def _create_context(self, task_id: str, **kwargs) -> DistributedCommandContext:
        """Create context with sensible defaults and automatic parent command ID injection"""
        kwargs = dict(kwargs or {})

        # Enforce immutable identity context when nested under an executing parent command.
        try:
            from motet.core.workers.invoker_context import get_current_identity_context
            parent_identity = get_current_identity_context()
        except (ImportError, Exception):
            parent_identity = None
        if parent_identity:
            for field in ("tenant_id", "motet_id", "principal_id"):
                parent_value = str(getattr(parent_identity, field, "") or "").strip()
                requested_raw = kwargs.get(field)
                requested_value = str(requested_raw).strip() if requested_raw is not None else ""
                # Allow explicit same-value input; reject value hopping.
                if parent_value and requested_value and requested_value != parent_value:
                    raise ValueError(
                        f"{field} override is not allowed in nested command composition "
                        f"(current={parent_value!r}, requested={requested_value!r})"
                    )
                # Backfill from parent when missing to keep propagation deterministic.
                if parent_value and not requested_value:
                    kwargs[field] = parent_value
        
        # Automatically inject parent command ID if not explicitly provided
        # Check if parent_command_id was explicitly passed with a non-None value
        if 'parent_command_id' in kwargs and kwargs.get('parent_command_id') is not None:
            # Use the explicitly provided value
            parent_command_id = kwargs.get('parent_command_id')
        else:
            # Not explicitly provided (or was None) - try to auto-detect from execution context
            try:
                from motet.core.workers.invoker_context import get_current_command_id
                parent_command_id = get_current_command_id()
            except (ImportError, Exception):
                # Fallback if import fails or context not available
                parent_command_id = None
        
        # Automatically inject parent worker ID when invoking from a worker (so child can know if parent ran on same worker)
        if 'parent_worker_id' in kwargs and kwargs.get('parent_worker_id') is not None:
            parent_worker_id = kwargs.get('parent_worker_id')
        else:
            try:
                from motet.core.workers.invoker_context import get_worker_context
                ctx = get_worker_context()
                parent_worker_id = ctx.get('worker_id') if isinstance(ctx, dict) else None
            except (ImportError, Exception):
                parent_worker_id = None
        
        required_capabilities = self._parse_required_capabilities(
            kwargs.get("required_capabilities", [])
        )

        cancel_scopes = kwargs.get("cancel_scopes")
        if cancel_scopes is None:
            try:
                from motet.core.commands.motet_context import get_motet_context

                parent_motet = get_motet_context()
                cancel_scopes = list(getattr(parent_motet, "cancel_scopes", None) or [])
            except Exception:
                cancel_scopes = []

        return DistributedCommandContext(
            task_id=task_id,
            conversation_id=kwargs.get('conversation_id') or '',
            # ADR-0056: tenant_id must be non-empty when encryption-at-rest is enabled.
            # Default to "default" to preserve backward compatibility for callers that omit tenant_id.
            tenant_id=kwargs.get('tenant_id') or 'default',
            principal_id=kwargs.get('principal_id') or '',
            # ADR-0056/stream encryption requires a non-empty motet_id; default to "default"
            # to preserve multi-environment separation without breaking older callers.
            motet_id=kwargs.get('motet_id') or 'default',
            metadata=kwargs.get('metadata', {}),
            trace_id=kwargs.get('trace_id'),
            parent_command_id=parent_command_id,  # Automatically injected
            parent_worker_id=parent_worker_id,    # Automatically injected when on a worker
            cancel_scopes=list(cancel_scopes or []),
            own_cancel_scope=kwargs.get("own_cancel_scope"),
            timeout_seconds=kwargs.get('timeout_seconds', self._get_default_timeout()),
            priority=kwargs.get('priority', self._get_default_priority()),
            max_retries=kwargs.get('max_retries', 3),
            use_redis_storage=kwargs.get('use_redis_storage', True),  # Default to True
            required_capabilities=required_capabilities,
            # Worker targeting fields (ADR-0025)
            target_worker_id=kwargs.get('target_worker_id'),
            preferred_worker_ids=kwargs.get('preferred_worker_ids', []),
            worker_affinity=kwargs.get('worker_affinity'),
            avoid_worker_ids=kwargs.get('avoid_worker_ids', []),
            # Schedule configuration
            schedule_name=kwargs.get('schedule_name')
        )


    @staticmethod
    def _parse_required_capabilities(raw_caps: Any) -> Set[WorkerCapability]:
        """Parse required capabilities from transport/runtime values."""
        parsed: Set[WorkerCapability] = set()
        if not raw_caps:
            return parsed

        if isinstance(raw_caps, (str, WorkerCapability)):
            caps_iter = [raw_caps]
        else:
            try:
                caps_iter = list(raw_caps)
            except Exception:
                caps_iter = []

        for cap in caps_iter:
            if isinstance(cap, WorkerCapability):
                parsed.add(cap)
                continue
            cap_str = str(cap or "").strip()
            if not cap_str:
                continue
            if cap_str in WorkerCapability.__members__:
                parsed.add(WorkerCapability[cap_str])
                continue
            try:
                parsed.add(WorkerCapability(cap_str.lower()))
            except Exception:
                logger.warning("unknown_required_capability_ignored", capability=cap_str)
        return parsed


    def _get_default_timeout(self) -> int:
        """Return default timeout for this command type - override in subclasses"""
        return 60


    def _get_default_priority(self) -> int:
        """Return default priority for this command type - override in subclasses.
        
        Priority values (EventPriority): LOW=1, NORMAL=5, HIGH=10, CRITICAL=15
        """
        return 5  # EventPriority.NORMAL


    def _get_preferred_pool_type(self) -> Optional[str]:
        """
        Return preferred worker pool type for optimal execution (ADR-0033).
        
        This is an OPTIONAL hint for the router - commands work on ALL pool types!
        The router will prefer matching workers when available but always falls back
        to any capable worker. This ensures no command ever fails due to pool type.
        
        Pool types:
        - "high_concurrency": eventlet/gevent/threads pools (50-100+ concurrent I/O ops)
        - "process": fork pool (process isolation, better for CPU-heavy operations)
        - None: No preference (default) - works equally well on any pool
        
        Override in subclasses to express performance preferences:
        - I/O-heavy commands (model inference, memory ops): "high_concurrency"
        - Generic commands: None (no preference)
        
        Returns:
            Optional pool type hint: "high_concurrency", "process", or None
        """
        return None  # Default: no preference, works on any pool


    @classmethod
    def _get_data_class(cls) -> Type[BaseModel]:
        """Return the Pydantic model for this command's payload; subclasses must override."""
        raise NotImplementedError(f"{cls.__qualname__} must implement _get_data_class()")


    def _setup_command_specifics(self):
        """Setup command-specific attributes and capabilities - override in subclasses"""
        pass


    def get_vault_client(self):
        """Get synchronous vault client for secure credential access."""
        if not self.distributed_context.vault_enabled:
            return None
        
        if self.distributed_context.vault_client is None:
            try:
                # ADR-0095: local workers use HttpVaultClient (HTTPS through
                # WireGuard tunnel) when MOTET_VAULT_RESOLVE_URL is set.
                if os.getenv("MOTET_VAULT_RESOLVE_URL", "").strip():
                    from motet.core.edge.http_vault_client import HttpVaultClient

                    self.distributed_context.vault_client = HttpVaultClient()
                else:
                    from motet.core.security.vault_client import get_vault_client

                    self.distributed_context.vault_client = get_vault_client()
            except Exception as e:
                logger.warning(
                    "vault_client_init_failed",
                    command_id=self.command_id,
                    command_type=self.get_command_type(),
                    error=str(e),
                    exc_info=True,
                )
                return None
        
        return self.distributed_context.vault_client


    def execute(self, stack: Dict[str, Any]) -> Any:
        """
        Execute the command in a distributed worker context with automatic result storage.
        
        This method wraps the actual execution with automatic Redis storage for large results.
        
        Args:
            stack: Worker execution context dict (tools, models, etc.); name matches Command.execute.
            
        Returns:
            The result of the command execution (may be a Redis key for large results)
        """
        # Store command metadata for debugging and flow tracking
        try:
            from motet.core.distributed.redis_command_data_manager import get_redis_command_data_manager
            command_data_manager = get_redis_command_data_manager()
            
            # Store initial metadata
            from motet.core.commands.distributed_types import (
                agentic_loop_iteration_metadata_fields,
            )

            command_data_manager.store_command_metadata(
                command_id=self.command_id,
                command_type=self.get_command_type(),
                task_id=self.distributed_context.task_id,
                tenant_id=self.distributed_context.tenant_id,
                motet_id=self.distributed_context.motet_id,
                principal_id=self.distributed_context.principal_id or "",
                conversation_id=self.distributed_context.conversation_id,
                parent_command_id=self.distributed_context.parent_command_id,
                triggered_by="command_execution",
                worker_id=stack.get('worker_id'),
                status="executing",
                executed_at=datetime.utcnow().isoformat(),
                **agentic_loop_iteration_metadata_fields(
                    getattr(self.distributed_context, "metadata", None)
                ),
            )
        except Exception as e:
            logger.warning(
                "command_metadata_store_failed",
                command_id=self.command_id,
                command_type=self.get_command_type(),
                error=str(e),
                exc_info=True,
            )
        
        try:
            # Execute the actual command logic
            result = self._do_execute(stack)
            
            # Update command metadata with completion info
            try:
                from motet.core.distributed.redis_command_data_manager import get_redis_command_data_manager
                command_data_manager = get_redis_command_data_manager()
                
                command_data_manager.update_command_metadata(
                    command_id=self.command_id,
                    tenant_id=self.distributed_context.tenant_id,
                    status="completed",
                    completed_at=datetime.utcnow().isoformat()
                )
            except Exception as e:
                logger.warning(
                    "command_metadata_update_failed",
                    command_id=self.command_id,
                    command_type=self.get_command_type(),
                    error=str(e),
                    exc_info=True,
                )
            
            # Check if result should be stored in Redis
            if self._should_store_result_in_redis(result):
                try:
                    # Store large result in Redis
                    redis_key = self.store_result_in_redis(result)
                    logger.info(
                        "command_result_stored_in_redis",
                        command_id=self.command_id,
                        command_type=self.get_command_type(),
                        redis_key=redis_key,
                    )
                    
                    # Return Redis key instead of full result
                    return {"_redis_result_key": redis_key}
                    
                except Exception as redis_error:
                    logger.warning(
                        "command_result_store_redis_failed_fallback_inline",
                        command_id=self.command_id,
                        command_type=self.get_command_type(),
                        error=str(redis_error),
                        exc_info=True,
                    )
                    # Fall back to inline storage if Redis fails
                    return result
            
            # For debug mode, always store results in Redis for debugging purposes
            try:
                from motet.core.distributed.redis_command_data_manager import get_redis_command_data_manager
                command_data_manager = get_redis_command_data_manager()
                
                # Store result in Redis for debugging (even if it's small)
                result_key = command_data_manager.store_command_result(
                    command_id=self.command_id,
                    result=result,
                    command_type=self.get_command_type(),
                    tenant_id=self.distributed_context.tenant_id,
                    motet_id=self.distributed_context.motet_id
                )
                logger.debug(
                    "command_result_debug_stored_in_redis",
                    command_id=self.command_id,
                    command_type=self.get_command_type(),
                    redis_key=result_key,
                )
                
            except Exception as debug_error:
                logger.warning(
                    "command_result_debug_store_failed",
                    command_id=self.command_id,
                    command_type=self.get_command_type(),
                    error=str(debug_error),
                    exc_info=True,
                )
            
            # Return result inline (small results or Redis storage failed)
            return result
            
        except Exception as e:
            # Update command metadata with error info
            try:
                from motet.core.distributed.redis_command_data_manager import get_redis_command_data_manager
                command_data_manager = get_redis_command_data_manager()
                
                command_data_manager.update_command_metadata(
                    command_id=self.command_id,
                    tenant_id=self.distributed_context.tenant_id,
                    status="failed",
                    completed_at=datetime.utcnow().isoformat(),
                    error=str(e)
                )
            except Exception as e2:
                logger.warning(
                    "command_metadata_update_failed_while_handling_error",
                    command_id=self.command_id,
                    command_type=self.get_command_type(),
                    original_error=str(e),
                    error=str(e2),
                    exc_info=True,
                )
            
            # Re-raise the exception
            raise


    @abstractmethod
    def _do_execute(self, worker_context: Dict[str, Any]) -> Any:
        """
        Implement the actual command execution logic.
        
        This method contains the actual work to be done by workers,
        without dependencies on the main stack object.
        
        Args:
            worker_context: Context provided by the worker (tools, models, etc.)
            
        Returns:
            The result of the command execution
        """
        pass


    def _get_result_summary(self, result: Any) -> str:
        """
        Generate a summary of the command execution result for tracking.
        
        Subclasses can override this to provide more specific summaries.
        
        Args:
            result: The result returned by _execute_impl
            
        Returns:
            A string summary of the result
        """
        command_type = self.get_command_type()
        
        tool_name = getattr(self, "tool_name", None)
        if tool_name:
            return f"Tool {tool_name} executed successfully"
        elif "model" in command_type:
            return f"Model inference completed successfully"
        elif "memory" in command_type:
            return f"Memory operation completed successfully"
        elif "reasoning" in command_type:
            return f"Reasoning operation completed successfully"
        else:
            return f"{command_type} executed successfully"


    @classmethod
    def register_command_type(cls, command_class: Type['DistributedCommand']) -> None:
        """
        Register a command class with the unified CommandTypeRegistry.
        
        This should be called by each command subclass to register itself.
        Uses only the new CommandTypeRegistry (no legacy registry).
        
        Args:
            command_class: The command class to register
        """
        try:
            # Try to get the command type without creating an instance
            # Use a mock data object to avoid abstract method issues
            from motet.core.commands.command_data_classes import BaseCommandData
            mock_data = BaseCommandData()
            temp_instance = command_class.__new__(command_class)
            # Set minimal required attributes to avoid abstract method errors.
            # distributed_context is intentionally unset; cast for the stub only.
            temp_instance.data = mock_data
            temp_instance.distributed_context = cast(Any, None)
            command_type = temp_instance.get_command_type()
            
            # Register with unified CommandTypeRegistry
            from motet.core.commands.command_type_registry import (
                command_type_registry,
                CommandImplementationType,
                first_docstring_line,
            )
            
            # Get data class for this command
            try:
                data_class = command_class._get_data_class()
            except Exception:
                data_class = None
            
            # Extract metadata from command instance if possible
            metadata = {}
            try:
                if hasattr(temp_instance, '_get_default_timeout'):
                    metadata['timeout_seconds'] = temp_instance._get_default_timeout()
                if hasattr(temp_instance, '_get_default_priority'):
                    metadata['priority'] = temp_instance._get_default_priority()
                if hasattr(temp_instance, 'distributed_context') and temp_instance.distributed_context:
                    if hasattr(temp_instance.distributed_context, 'required_capabilities'):
                        metadata['required_capabilities'] = list(temp_instance.distributed_context.required_capabilities or [])
            except Exception:
                pass  # metadata extraction is best-effort for registration
            
            command_type_registry.register_command(
                command_type=command_type,
                implementation=command_class,
                implementation_type=CommandImplementationType.CLASS_BASED,
                data_class=data_class,
                description=first_docstring_line(command_class),
                metadata=metadata,
                version="1.0.0"
            )
            
            logger.debug(
                "command_type_registered",
                command_class=command_class.__name__,
                command_type=command_type,
            )
            
        except Exception as e:
            logger.warning(
                "command_type_registration_failed",
                command_class=command_class.__name__,
                error=str(e),
                exc_info=True,
            )


    @classmethod
    def _ensure_commands_registered(cls) -> None:
        """
        Ensure all command types are registered by importing the command modules.
        
        This is called lazily to avoid circular imports during module initialization.
        """
        # Always import all modules to ensure complete registration
        # Don't skip based on registry length - different processes may register different commands
        #
        # This list is the *only* thing that registers these command types. It
        # used to be backstopped by `orchestration/commands/__init__.py` eagerly
        # importing every sibling; that package is gone, so an unlisted module
        # is an unregistered command, and workers reject it at runtime with
        # "Unknown command type". No unit test catches that - add the import
        # here whenever you add a command module.
        try:
            # Built-in command library
            from motet.core.commands.builtin import agents  # ADR-0068 agent_list
            from motet.core.commands.builtin import artifacts
            from motet.core.commands.builtin import conversation  # ADR-0072 conversation list/get/clear/register
            from motet.core.commands.builtin import conversation_analysis  # replaced intent (ADR-0002)
            from motet.core.commands.builtin import derivation  # ADR-0062 media processing → derived artifacts
            from motet.core.commands.builtin import memory
            from motet.core.commands.builtin import model
            from motet.core.commands.builtin import rag
            from motet.core.commands.builtin import schedule  # ADR-0025
            from motet.core.commands.builtin import test_decorator_command  # ADR-0030 validation
            from motet.core.commands.builtin import tool
            from motet.core.commands.builtin import transform
            from motet.core.commands.builtin import worker_lifecycle
            from motet.core.commands.builtin import workflow
            from motet.core.commands.builtin import sync_user_workflow  # ADR-0129 user.* catalog fan-out
            # Composition primitives (ADR-0023): gather / dispatch / map
            from motet.core.commands import concurrency
            # Orchestration-owned turn lifecycle (issues #146 / #147)
            from motet.core.orchestration import turn  # agent_turn, resume_agent_turn, and the phase commands
            # discovery_commands removed - consolidated into tool
            # mcp_commands removed - MCP tools handled by tool_execution
            
            # Reasoning packages register their command types on import: react
            # (agent_loop remains a Celery command so core.spawn_agents
            # sub-agents run on separate workers; the turn path calls
            # run_agent in-process).
            try:
                importlib.import_module("motet.core.reasoning.react")
            except ImportError:
                logger.warning(
                    "reasoning_strategy_import_failed",
                    strategy="motet.core.reasoning.react",
                    exc_info=True,
                )
            # Bundle deploy / hot-reload commands (ADR-0071). These used to be
            # registered as a side effect of `orchestration.commands.__init__`
            # importing them; now that they live in `core.bundles`, registration
            # has to be requested explicitly or workers reject the command type.
            try:
                from motet.core.bundles import deploy, bundle_reload
            except ImportError:
                pass
        except ImportError as e:
            logger.warning(
                "command_modules_import_failed",
                error=str(e),
                exc_info=True,
            )


    def get_required_capabilities(self) -> Set[WorkerCapability]:
        """Get the capabilities required to execute this command"""
        return self.distributed_context.required_capabilities


    def can_execute_on_worker(self, worker_capabilities: Set[WorkerCapability]) -> bool:
        """Check if this command can execute on a worker with given capabilities"""
        required = self.get_required_capabilities()
        return required.issubset(worker_capabilities)


    def assign_to_worker(self, worker_id: str, worker_type: str, capabilities: Set[WorkerCapability]):
        """Assign this command to a specific worker"""
        assignment = WorkerAssignment(
            worker_id=worker_id,
            worker_type=worker_type,
            capabilities=capabilities
        )
        self.worker_assignments.append(assignment)


    def mark_worker_started(self, worker_id: str):
        """Mark that a worker has started executing this command"""
        for assignment in self.worker_assignments:
            if assignment.worker_id == worker_id:
                assignment.started_at = datetime.utcnow()
                break


    def mark_worker_completed(self, worker_id: str, result: Any):
        """Mark that a worker has completed executing this command"""
        for assignment in self.worker_assignments:
            if assignment.worker_id == worker_id:
                assignment.completed_at = datetime.utcnow()
                assignment.result = result
                break
        
        self.worker_results[worker_id] = result


    def mark_worker_failed(self, worker_id: str, error: Exception):
        """Mark that a worker has failed executing this command"""
        for assignment in self.worker_assignments:
            if assignment.worker_id == worker_id:
                assignment.completed_at = datetime.utcnow()
                assignment.error = str(error)
                break


    def get_worker_assignment(self, worker_id: str) -> Optional[WorkerAssignment]:
        """Get the assignment for a specific worker"""
        for assignment in self.worker_assignments:
            if assignment.worker_id == worker_id:
                return assignment
        return None


    def aggregate_results(self) -> Any:
        """
        Aggregate results from multiple workers based on the aggregation strategy.
        
        Returns:
            The aggregated result
        """
        if not self.worker_results:
            return None
        
        strategy = self.distributed_context.result_aggregation_strategy
        
        if strategy == "first_success":
            # Return the first successful result
            for result in self.worker_results.values():
                if result is not None:
                    return result
            return None
        
        elif strategy == "all_results":
            # Return all results as a list
            return list(self.worker_results.values())
        
        elif strategy == "majority_vote":
            # Return the most common result (for simple values)
            from collections import Counter
            if not self.worker_results:
                return None
            
            # Convert results to strings for comparison
            str_results = [str(r) for r in self.worker_results.values() if r is not None]
            if not str_results:
                return None
            
            counter = Counter(str_results)
            most_common = counter.most_common(1)[0][0]
            
            # Find the original result that matches the most common string
            for result in self.worker_results.values():
                if str(result) == most_common:
                    return result
            
            return None
        
        else:
            # Default to first result
            return next(iter(self.worker_results.values()), None)


    def is_distribution_complete(self) -> bool:
        """Check if distribution to all assigned workers is complete"""
        if not self.worker_assignments:
            return False
        
        for assignment in self.worker_assignments:
            if assignment.completed_at is None:
                return False
        
        return True


    def has_successful_results(self) -> bool:
        """Check if at least one worker has produced a successful result"""
        for assignment in self.worker_assignments:
            if assignment.completed_at and assignment.error is None:
                return True
        return False


    def should_retry(self) -> bool:
        """Check if the command should be retried"""
        if self.retry_count >= self.distributed_context.max_retries:
            return False
        
        if self.has_successful_results():
            return False
        
        return True


    def prepare_for_retry(self):
        """Prepare the command for retry"""
        self.retry_count += 1
        self.last_retry_at = datetime.utcnow()
        
        # Clear previous assignments and results
        self.worker_assignments.clear()
        self.worker_results.clear()
        
        # Reset status
        self.status = CommandStatus.PENDING
        self.error = None



__all__ = [
    'DistributedCommand',
    'DistributedCommandContext',
    'WorkerCapability',
    'DistributionStrategy',
    'WorkerAssignment',
    'ScheduleType',
]
