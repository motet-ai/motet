"""
Motet - Command Invoker

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Command invocation system for the Motet distributed framework.
    Provides high-level command invocation with routing, coordination, and state management.

Dependencies:
    - asyncio: Asynchronous I/O
    - json: JSON serialization
    - Worker routing and communication
    - Command execution and management
    - State management and persistence

Usage:
    from motet.core.workers.command_invoker import CommandInvoker
    
    # Create invoker
    invoker = CommandInvoker()
    
    # Invoke command
    result = await invoker.invoke(command_type, params)

Notes:
    - Supports high-level command invocation
    - Includes state management and coordination
    - Provides comprehensive routing capabilities
    - Integrates with distributed architecture
"""

import asyncio
import json
import time
import os
from typing import Any, Dict, List, Optional
from datetime import datetime
from uuid import uuid4


from motet.core.commands.distributed import DistributedCommand, DistributedCommandContext
from motet.core.commands.base import CommandStatus
from ..resilience import global_resilient_caller
from ..constants import DEFAULT_REDIS_URL
from . import global_bus
from .routing import WorkerRouter
from .command_executor import CommandExecutor
from .routing.worker_communicator import WorkerCommunicator
from ..distributed import get_readiness_service


class DistributedInvokerNode:
    """
    New distributed command invoker using the consolidated routing architecture.
    
    This completely replaces the legacy routing system with:
    - WorkerRouter for intelligent routing decisions
    - CommandExecutor for clean command lifecycle management
    - WorkerCommunicator for Celery integration
    - No monkey patching or legacy fallbacks
    """
    
    def __init__(self, 
                 node_id: Optional[str] = None,
                 redis_url: Optional[str] = None,
                 max_local_history: int = 100,
                 enable_circuit_breakers: bool = True,
                 default_routing_strategy: str = "least_loaded"):
        
        self.node_id = node_id or f"new-invoker-{uuid4().hex[:8]}"
        self.redis_url = (redis_url or 
                         os.getenv('MOTET_PURE_DISTRIBUTED_INVOKER_REDIS_URL') or 
                         os.getenv('MOTET_REDIS_URL', DEFAULT_REDIS_URL))
        self.max_local_history = max_local_history
        self.enable_circuit_breakers = enable_circuit_breakers
        self.default_routing_strategy = default_routing_strategy
        
        # Configuration from environment
        self.node_ttl = int(os.getenv('MOTET_DISTRIBUTED_INVOKER_NODE_TTL', '300'))
        self.heartbeat_interval = int(os.getenv('MOTET_DISTRIBUTED_INVOKER_HEARTBEAT_INTERVAL', '60'))
        
        # Local state tracking
        self.command_history: List[DistributedCommand] = []
        self.local_stats = {
            'total_commands': 0,
            'successful_commands': 0,
            'failed_commands': 0,
            'distributed_commands': 0,
            'worker_to_worker_commands': 0,
            'routed_commands': 0,
            'total_execution_time_ms': 0.0
        }
        
        # New routing components (initialized in initialize())
        self.readiness_service = None
        self.worker_router: Optional[WorkerRouter] = None
        self.command_executor: Optional[CommandExecutor] = None
        self.worker_communicator: Optional[WorkerCommunicator] = None
        
        # Redis connection
        self._redis_client = None
        self._lock = asyncio.Lock()
        self._execution_stack: set[str] = set()
        
        # Circuit breaker integration
        self.resilient_caller = global_resilient_caller if enable_circuit_breakers else None
        
        # Node health tracking
        self.last_heartbeat = datetime.now()
        self.node_load = 0.0
        
        print(f"🚀 New distributed invoker node created: {self.node_id}")
        
        # Track initialization mode to avoid double init ('none' | 'sync' | 'async')
        self._init_mode: str = 'none'
    
    def initialize(self):
        """Initialize the new distributed invoker with consolidated routing."""
        # Guard against double-initialization
        if self._init_mode != 'none':
            print(f"ℹ️ Invoker already initialized ({self._init_mode}); skipping initialize")
            return

        self._connect_redis()
        
        # Initialize readiness service
        self.readiness_service = get_readiness_service()
        print(f"✅ Readiness service initialized")
        
        # Initialize worker communicator
        self.worker_communicator = WorkerCommunicator(
            default_timeout=60,
            enable_retries=True,
            max_retries=3
        )
        print(f"✅ Worker communicator initialized")
        
        # Initialize worker router with all strategies
        self.worker_router = WorkerRouter(
            readiness_service=self.readiness_service,
            default_strategy=self.default_routing_strategy,
            enable_caching=True,
            cache_ttl_seconds=30
        )
        print(f"✅ Worker router initialized with strategy: {self.default_routing_strategy}")
        
        # Initialize command executor
        self.command_executor = CommandExecutor(
            worker_router=self.worker_router,
            worker_communicator=self.worker_communicator,
            enable_circuit_breaker=self.enable_circuit_breakers,
            enable_metrics=True
        )
        print(f"✅ Command executor initialized")
        
        # Register node with readiness service
        self._register_node()
        
        print(f"🎯 New distributed invoker fully initialized: {self.node_id}")
        self._init_mode = 'sync'
    
    
    
    def execute_command(self, 
                                       command: DistributedCommand,
                                       target_worker_id: Optional[str] = None,
                                       strategy_override: Optional[str] = None) -> Any:
        """
        Main command invocation method using new routing architecture.
        
        This completely bypasses legacy routing and uses the new system.
        """
        # Auto-initialize if not already initialized (lazy initialization)
        if not self.command_executor:
            self.initialize()
        executor = self.command_executor
        if executor is None:
            raise RuntimeError("Command executor not available after initialize")
        
        start_time = datetime.now().timestamp()
        
        try:
            # Update local stats
            self.local_stats['total_commands'] += 1
            self.local_stats['distributed_commands'] += 1
            
            if target_worker_id:
                self.local_stats['routed_commands'] += 1
            
            # Check if this is a worker-to-worker call
            if self._execution_stack:
                self.local_stats['worker_to_worker_commands'] += 1
            
            # Add to execution stack for cycle detection
            command_id = command.command_id
            if command_id in self._execution_stack:
                raise RuntimeError(f"Circular command execution detected: {command_id}")
            
            self._execution_stack.add(command_id)
            
            # Save current command context and set new one for parent tracking
            from .invoker_context import (
                set_current_command_id,
                get_current_command_id,
                set_current_identity_context,
                get_current_identity_context,
                clear_current_identity_context,
                IdentityContext,
            )
            saved_parent_id = get_current_command_id()  # Save parent context
            saved_identity_context = get_current_identity_context()  # Save parent identity context
            set_current_command_id(command_id)  # Set current command as context
            set_current_identity_context(
                IdentityContext(
                    tenant_id=str(command.distributed_context.tenant_id or "").strip(),
                    motet_id=str(command.distributed_context.motet_id or "").strip(),
                    principal_id=str(command.distributed_context.principal_id or "").strip(),
                )
            )
            
            try:
                # Execute using new command executor (sync version)
                result = executor.execute_command(
                    command=command,
                    target_worker_id=target_worker_id,
                    strategy_override=strategy_override
                )
                
                # Process result
                if result['status'] == 'completed':
                    self.local_stats['successful_commands'] += 1
                    command_result = result['result']
                    
                    # Add to command history
                    self._add_to_history(command)
                    
                    # Events are published by process_distributed_command task
                    
                    return command_result
                else:
                    # Command failed
                    self.local_stats['failed_commands'] += 1
                    error_msg = result.get('error', 'Command execution failed')
                    
                    # Add to command history
                    command.status = CommandStatus.FAILED
                    self._add_to_history(command)
                    
                    # Events are published by process_distributed_command task
                    
                    raise RuntimeError(f"Command execution failed: {error_msg}")
                
            finally:
                # Restore parent command context (instead of clearing)
                if saved_parent_id is not None:
                    set_current_command_id(saved_parent_id)
                else:
                    # Only clear if there was no parent
                    from .invoker_context import clear_current_command_id
                    clear_current_command_id()
                if saved_identity_context is not None:
                    set_current_identity_context(saved_identity_context)
                else:
                    clear_current_identity_context()
                
                # Remove from execution stack
                self._execution_stack.discard(command_id)
                
                # Update execution time
                execution_time = (datetime.now().timestamp() - start_time) * 1000
                self.local_stats['total_execution_time_ms'] += execution_time
                
        except Exception as e:
            self.local_stats['failed_commands'] += 1
            command.status = CommandStatus.FAILED
            self._add_to_history(command)
            
            # Events are published by process_distributed_command task
            
            raise
    
    def get_routing_stats(self) -> Dict[str, Any]:
        """Get comprehensive routing statistics from new system."""
        if (
            not self.worker_router
            or not self.command_executor
            or not self.worker_communicator
        ):
            return {"error": "Invoker not initialized"}
        
        # Get stats from new components
        routing_stats = self.worker_router.get_routing_stats()
        execution_stats = self.command_executor.get_execution_stats()
        communication_stats = self.worker_communicator.get_communication_stats()
        
        return {
            "node_id": self.node_id,
            "node_stats": self.local_stats.copy(),
            "routing_stats": routing_stats,
            "execution_stats": execution_stats,
            "communication_stats": communication_stats,
            "command_history_size": len(self.command_history),
            "execution_stack_depth": len(self._execution_stack),
            "last_heartbeat": self.last_heartbeat.isoformat(),
            "node_load": self.node_load
        }
    
    def get_available_workers(self, 
                                  required_capabilities: Optional[List[str]] = None,
                                  tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get available workers using new routing system."""
        if not self.worker_router:
            return []
        
        capabilities_set = set(required_capabilities) if required_capabilities else None
        return self.worker_router.get_available_workers(
            required_capabilities=capabilities_set,
            tenant_id=tenant_id,
            include_readiness_check=True
        )
    
    def route_to_specific_worker(self, 
                                     command: DistributedCommand,
                                     target_worker_id: str,
                                     wait_if_not_ready: bool = True,
                                     timeout_seconds: int = 30) -> Any:
        """Route command to specific worker using new routing system."""
        return self.execute_command(
            command=command,
            target_worker_id=target_worker_id
        )
    
    def get_tenant_routing_info(self, tenant_id: str) -> Dict[str, Any]:
        """Get tenant-specific routing information."""
        if not self.worker_router:
            return {"error": "Router not initialized"}
        
        return self.worker_router.get_tenant_routing_info(tenant_id)
    
    # Legacy compatibility methods (delegate to new system)
    
    def get_worker_stats(self) -> Dict[str, Any]:
        """Legacy compatibility - delegate to new routing stats."""
        return self.get_routing_stats()
    
    def get_local_stats(self) -> Dict[str, Any]:
        """Get local node statistics."""
        return self.local_stats.copy()
    
    def get_command_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Get recent command history."""
        recent_commands = self.command_history[-limit:]
        return [
            {
                "command_id": cmd.command_id,
                "command_type": cmd.get_command_type(),
                "status": cmd.status.value if cmd.status else "unknown",
                "created_at": (
                    cmd.distributed_context.created_at.isoformat()
                    if cmd.distributed_context is not None
                    else None
                ),
                "tenant_id": getattr(cmd, 'tenant_id', None),
                "conversation_id": cmd.distributed_context.conversation_id if cmd.distributed_context else None
            }
            for cmd in recent_commands
        ]
    
    # Private methods
    
    def _connect_redis(self):
        """Connect to Redis for distributed state management."""
        try:
            # Use sync Redis client for sync contexts
            from ..distributed.redis_manager import get_sync_redis_client
            self._redis_client = get_sync_redis_client("new_command_invoker")
            self._redis_client.ping()
            print(f"✅ Connected to Redis via sync client: {self.redis_url}")
        except Exception as e:
            print(f"⚠️ Redis connection failed: {e}")
            self._redis_client = None
    
    
    def _register_node(self):
        """Register this node with the readiness service."""
        if self.readiness_service and self._redis_client:
            try:
                # Register as a command invoker node (sync version)
                self._redis_client.hset(
                    "invoker_nodes",
                    self.node_id,
                    json.dumps({
                        "node_id": self.node_id,
                        "registered_at": datetime.now().isoformat(),
                        "routing_strategy": self.default_routing_strategy,
                        "circuit_breakers_enabled": self.enable_circuit_breakers
                    })
                )
                print(f"✅ Node registered: {self.node_id}")
            except Exception as e:
                print(f"⚠️ Node registration failed: {e}")
    
    
    def _add_to_history(self, command: DistributedCommand):
        """Add command to local history."""
        self.command_history.append(command)
        
        # Limit history size
        if len(self.command_history) > self.max_local_history:
            self.command_history = self.command_history[-self.max_local_history:]
    
    def _publish_command_event(self, 
                                   command: DistributedCommand, 
                                   event_type: str, 
                                   result: Dict[str, Any]):
        """Publish command execution event."""
        print(f"🔍 DistributedInvokerNode: Publishing command_{event_type} event for {command.get_command_type()} command {command.command_id}")
        try:
            # Create the event data that will go in the "data" field
            event_data = {
                "event_type": f"command_{event_type}",
                "node_id": self.node_id,
                "command_id": command.command_id,
                "command_type": command.get_command_type(),
                "tenant_id": getattr(command, 'tenant_id', None),
                "conversation_id": command.distributed_context.conversation_id if command.distributed_context else None,
                "task_id": command.distributed_context.task_id if command.distributed_context else None,
                "timestamp": datetime.now().isoformat(),
                "execution_info": {
                    "routing_info": result.get('routing_info', {}),
                    "execution_time_ms": result.get('execution_time_ms', 0),
                    "worker_id": result.get('routing_info', {}).get('selected_worker_id'),
                    "strategy_used": result.get('routing_info', {}).get('strategy_used')
                }
            }
            
            if event_type == 'error':
                event_data["error"] = result.get('error')
            
            # Wrap in proper event structure for EventBus
            event = {
                "kind": f"command_{event_type}",
                "source": "distributed_command_invoker",
                "data": event_data,
                "timestamp": datetime.now().isoformat(),
                "priority": 5,
                "correlation_id": None,
                "tags": [],
                "metadata": {}
            }
            
            global_bus.publish(event)
            print(f"✅ DistributedInvokerNode: Successfully published command_{event_type} event for {command.get_command_type()}")
            
        except Exception as e:
            print(f"❌ DistributedInvokerNode: Failed to publish command event: {e}")
    
    def schedule_command(self, command: DistributedCommand) -> str:
        """
        Schedule a command for future execution.
        
        This method handles scheduling of commands based on their schedule_type:
        - IMMEDIATE: Execute immediately (same as execute_command)
        - DELAYED: Execute at a specific future time
        - RECURRING: Execute on a recurring schedule
        - CONDITIONAL: Execute when conditions are met
        
        Args:
            command: The command to schedule
            
        Returns:
            str: Schedule ID for tracking and management
        """
        try:
            # Import here to avoid circular dependencies
            from ..orchestration.scheduling import ScheduledCommandManager
            from motet.core.commands.distributed import ScheduleType
            
            # If immediate execution, just execute normally
            if command.distributed_context.schedule_type == ScheduleType.IMMEDIATE:
                result = self.execute_command(command)
                return f"immediate_{command.command_id}"
            
            # Create schedule manager
            schedule_manager = ScheduledCommandManager()
            
            # Schedule the command
            schedule_id = schedule_manager.schedule_command(command)
            
            # Handle different schedule types
            if command.distributed_context.schedule_type == ScheduleType.DELAYED:
                # Schedule with Celery using eta parameter
                self._schedule_delayed_execution(command, schedule_id)
                
            elif command.distributed_context.schedule_type == ScheduleType.RECURRING:
                # Schedule with Celery Beat for recurring execution
                self._schedule_recurring_execution(command, schedule_id)
                
            elif command.distributed_context.schedule_type == ScheduleType.CONDITIONAL:
                # Add to conditional schedule monitoring
                self._register_conditional_schedule(command, schedule_id)
            
            print(f"✅ DistributedInvokerNode: Command scheduled successfully - ID: {schedule_id}")
            return schedule_id
            
        except Exception as e:
            print(f"❌ DistributedInvokerNode: Failed to schedule command: {e}")
            raise RuntimeError(f"Failed to schedule command: {e}") from e
    
    def _schedule_delayed_execution(self, command: DistributedCommand, schedule_id: str) -> None:
        """Schedule a command for delayed execution using Celery eta"""
        try:
            from .celery_app import get_celery_app
            from .schedule_tasks import schedule_distributed_command
            
            celery_app = get_celery_app()
            
            # Prepare schedule data
            schedule_data = {
                "schedule_id": schedule_id,
                "command_data": command.serialize_for_transport()
            }
            
            # Schedule with Celery using eta
            eta = command.distributed_context.scheduled_at
            if not eta:
                raise ValueError("scheduled_at is required for DELAYED execution")
            
            celery_app.send_task(
                'imf.commands.schedule',
                args=[json.dumps(schedule_data)],
                eta=eta
            )
            
            print(f"📅 DistributedInvokerNode: Delayed execution scheduled for {eta}")
            
        except Exception as e:
            print(f"❌ DistributedInvokerNode: Failed to schedule delayed execution: {e}")
            raise
    
    def _schedule_recurring_execution(self, command: DistributedCommand, schedule_id: str) -> None:
        """Schedule a command for recurring execution using Celery Beat"""
        try:
            from .celery_app import get_celery_app
            
            celery_app = get_celery_app()
            
            # Prepare schedule data
            schedule_data = {
                "schedule_id": schedule_id,
                "command_data": command.serialize_for_transport()
            }
            
            # Add to Celery Beat schedule
            # Note: This is a simplified implementation
            # Full cron support will be implemented in Phase 3
            cron_expr = command.distributed_context.cron_expression
            if not cron_expr:
                raise ValueError("cron_expression is required for RECURRING execution")
            
            # For now, we'll use a simple interval-based approach
            # TODO: Implement proper cron parsing in Phase 3
            beat_schedule_key = f"recurring_{schedule_id}"
            
            # This is a placeholder - full implementation will be in Phase 3
            print(f"🔄 DistributedInvokerNode: Recurring execution registered (placeholder)")
            
        except Exception as e:
            print(f"❌ DistributedInvokerNode: Failed to schedule recurring execution: {e}")
            raise
    
    def _register_conditional_schedule(self, command: DistributedCommand, schedule_id: str) -> None:
        """Register a command for conditional execution"""
        try:
            # For now, just log the registration
            # Full conditional execution will be implemented in Phase 4
            condition_expr = command.distributed_context.condition_expression
            check_interval = command.distributed_context.condition_check_interval
            
            print(f"🔍 DistributedInvokerNode: Conditional schedule registered (placeholder)")
            print(f"   Condition: {condition_expr}")
            print(f"   Check interval: {check_interval}s")
            
        except Exception as e:
            print(f"❌ DistributedInvokerNode: Failed to register conditional schedule: {e}")
            raise


class DistributedCommandInvoker:
    """
    New distributed command invoker that manages multiple nodes.
    
    This replaces PureDistributedCommandInvoker with the new routing architecture.
    """
    
    def __init__(self):
        self.primary_node: Optional[DistributedInvokerNode] = None
        self._initialized = False
        self._init_mode: str = 'none'

    def _require_primary_node(self) -> DistributedInvokerNode:
        """Return the initialized primary node (initializes on first use)."""
        if not self._initialized:
            self.initialize()
        node = self.primary_node
        if node is None:
            raise RuntimeError("Primary invoker node not available after initialize")
        return node
    
    def initialize(self):
        """Initialize the command invoker."""
        if self._initialized:
            return
        
        self.primary_node = DistributedInvokerNode()
        self.primary_node.initialize()
        self._initialized = True
        self._init_mode = 'sync'
        
        print("🎯 New distributed command invoker fully initialized")
    
    def execute_command(self, 
                                       command: DistributedCommand,
                                       target_worker_id: Optional[str] = None,
                                       strategy_override: Optional[str] = None) -> Any:
        """Invoke distributed command using new routing system."""
        return self._require_primary_node().execute_command(
            command=command,
            target_worker_id=target_worker_id,
            strategy_override=strategy_override
        )
    
    # Delegate all other methods to primary node
    def get_routing_stats(self) -> Dict[str, Any]:
        return self._require_primary_node().get_routing_stats()
    
    def get_available_workers(self, **kwargs) -> List[Dict[str, Any]]:
        return self._require_primary_node().get_available_workers(**kwargs)
    
    def route_to_specific_worker(self, **kwargs) -> Any:
        return self._require_primary_node().route_to_specific_worker(**kwargs)
    
    def get_tenant_routing_info(self, tenant_id: str) -> Dict[str, Any]:
        return self._require_primary_node().get_tenant_routing_info(tenant_id)
    
    def schedule_command(self, command: DistributedCommand) -> str:
        """Schedule a distributed command for future execution."""
        return self._require_primary_node().schedule_command(command)


# Create new global invoker instance (lazy initialization)
new_global_invoker = DistributedCommandInvoker()

__all__ = [
    "DistributedInvokerNode",
    "DistributedCommandInvoker", 
    "new_global_invoker"
]
