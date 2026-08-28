"""
Motet - Event Observers

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-04

Description:
    Event observer system for the Motet distributed framework.
    Provides observer pattern implementation with comprehensive event filtering and handling.

Dependencies:
    - abc: Abstract base classes
    - pydantic: Data validation and serialization
    - datetime: Time and date handling
    - enum: Enumeration types

Usage:
    from motet.core.workers.observers import Observer, Event, EventFilter
    
    # Create custom observer
    class MyObserver(Observer):
        async def on_event(self, event: Event):
            # Handle event
            pass

Notes:
    - Provides observer pattern for event handling
    - Includes comprehensive event filtering
    - Supports priority-based event processing
    - Integrates with distributed event bus
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pydantic import BaseModel, Field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Set, Optional, Callable
from collections import defaultdict, deque
from types import SimpleNamespace

import structlog
from ..security.system_principals import (
    SYSTEM_PRINCIPAL_SCHEDULER,
    SYSTEM_TENANT_ID,
    SYSTEM_MOTET_ID,
)

logger = structlog.get_logger(__name__)


class EventPriority(int, Enum):
    LOW = 1
    NORMAL = 5
    HIGH = 10
    CRITICAL = 15


class Event(BaseModel):
    """Enhanced event class with rich metadata that wraps existing dict events"""
    event_type: str
    source: str
    data: Dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    priority: EventPriority = EventPriority.NORMAL
    correlation_id: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    
    @classmethod
    def from_dict(cls, event_dict: Dict[str, Any]) -> 'Event':
        """Convert existing dict event to enhanced event"""
        return cls(
            event_type=event_dict.get("kind", "unknown"),
            source=event_dict.get("source", "unknown"),
            data=event_dict.get("data", event_dict),  # Fallback to full dict
            priority=EventPriority(event_dict.get("priority", EventPriority.NORMAL.value)),
            correlation_id=event_dict.get("correlation_id"),
            tags=event_dict.get("tags", []),
            metadata=event_dict.get("metadata", {})
        )
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert enhanced event back to dict for existing bus"""
        return {
            "kind": self.event_type,
            "source": self.source,
            "data": self.data,
            "timestamp": self.timestamp.isoformat(),
            "priority": self.priority.value,
            "correlation_id": self.correlation_id,
            "tags": self.tags,
            "metadata": self.metadata
        }
    
    def matches_filter(self, event_filter: 'EventFilter') -> bool:
        """Check if event matches the given filter"""
        return event_filter.matches(self)


class EventFilter(BaseModel):
    """Filter for subscribing to specific events"""
    event_types: Set[str] = Field(default_factory=set)
    sources: Set[str] = Field(default_factory=set)
    tags: Set[str] = Field(default_factory=set)
    min_priority: EventPriority = EventPriority.LOW
    max_priority: EventPriority = EventPriority.CRITICAL
    correlation_ids: Set[str] = Field(default_factory=set)
    custom_filter: Optional[Callable[[Event], bool]] = None
    
    def matches(self, event: Event) -> bool:
        """Check if event matches this filter"""
        # Check event types
        if self.event_types and event.event_type not in self.event_types:
            return False
        
        # Check sources
        if self.sources and event.source not in self.sources:
            return False
        
        # Check tags (event must have at least one matching tag)
        if self.tags and not set(event.tags).intersection(self.tags):
            return False
        
        # Check priority range
        if not (self.min_priority <= event.priority <= self.max_priority):
            return False
        
        # Check correlation IDs
        if self.correlation_ids and event.correlation_id not in self.correlation_ids:
            return False
        
        # Apply custom filter
        if self.custom_filter and not self.custom_filter(event):
            return False
        
        return True


class Observer(ABC):
    """Enhanced abstract observer interface"""
    
    def __init__(self, name: str):
        self.name = name
        self.is_active = True
        self.event_count = 0
        self.last_event_time: Optional[datetime] = None
        self.error_count = 0
        self.last_error: Optional[str] = None
    
    @abstractmethod
    def on_event(self, event: Event) -> None:
        """Handle an event notification"""
        pass
    
    @abstractmethod
    def get_event_filter(self) -> EventFilter:
        """Return event filter for subscription"""
        pass
    
    def handle_event_safely(self, event: Event) -> bool:
        """Handle event with error tracking"""
        try:
            self.event_count += 1
            self.last_event_time = datetime.utcnow()
            self.on_event(event)
            return True
        except Exception as e:
            self.error_count += 1
            self.last_error = str(e)
            self._handle_error(event, e)
            return False
    
    def _handle_error(self, event: Event, error: Exception):
        """Handle observer errors"""
        # Log error (could be enhanced with proper logging)
        logger.error("observer_event_handling_failed",
                    observer=self.name,
                    event_type=event.event_type,
                    error=str(error))
    
    def get_stats(self) -> Dict[str, Any]:
        """Get observer statistics"""
        return {
            "name": self.name,
            "is_active": self.is_active,
            "event_count": self.event_count,
            "error_count": self.error_count,
            "last_event_time": self.last_event_time.isoformat() if self.last_event_time else None,
            "last_error": self.last_error
        }


class MemoryModuleObserver(Observer):
    """Observer that reacts to memory-related events"""
    
    def __init__(self, memory_manager=None):
        super().__init__("memory_module_observer")
        self.memory_manager = memory_manager
        self.stored_items = 0
        self.consolidations = 0
        self.cleanups = 0

    def _system_memory_context(self) -> Any:
        """Explicit system identity for observer-driven memory operations."""
        return SimpleNamespace(
            principal_id=SYSTEM_PRINCIPAL_SCHEDULER,
            tenant_id=SYSTEM_TENANT_ID,
            motet_id=SYSTEM_MOTET_ID,
            conversation_id=None,
        )
    
    def on_event(self, event: Event) -> None:
        """Handle memory-related events"""
        if not self.memory_manager:
            return
        
        if event.event_type == "task_completed":
            self._handle_task_completion(event)
        elif event.event_type == "user_feedback_received":
            self._handle_user_feedback(event)
        elif event.event_type == "conversation_ended":
            self._handle_conversation_end(event)
        elif event.event_type == "memory_cleanup_needed":
            self._handle_memory_cleanup(event)
    
    def _handle_task_completion(self, event: Event):
        """Store successful task results in long-term memory"""
        try:
            mm = self.memory_manager
            if mm is None:
                return

            task_id = event.data.get("task_id")
            result = event.data.get("result")
            execution_time = event.data.get("execution_time")
            
            if task_id and result:
                if hasattr(mm, 'store_memory'):
                    mm.store_memory(
                        content=f"Task {task_id} completed successfully: {str(result)[:500]}",
                        type="task_result",
                        tags=["task_completion", f"task:{task_id}"],
                        metadata={
                            "task_id": task_id,
                            "execution_time": execution_time,
                            "success": True
                        },
                        long_term=True,
                        motet_context=self._system_memory_context(),
                    )
                    self.stored_items += 1
        except Exception as e:
            self._handle_error(event, e)
    
    def _handle_user_feedback(self, event: Event):
        """Store user feedback for learning"""
        try:
            mm = self.memory_manager
            if mm is None:
                return

            feedback = event.data.get("feedback")
            task_id = event.data.get("task_id")
            
            if feedback and task_id and hasattr(mm, 'store_memory'):
                mm.store_memory(
                    content=f"User feedback for task {task_id}: {feedback}",
                    type="user_feedback",
                    tags=["feedback", f"task:{task_id}"],
                    metadata={
                        "task_id": task_id,
                        "feedback_type": event.data.get("feedback_type", "general")
                    },
                    long_term=True,
                    motet_context=self._system_memory_context(),
                )
                self.stored_items += 1
        except Exception as e:
            self._handle_error(event, e)
    
    def _handle_conversation_end(self, event: Event):
        """Consolidate conversation memories"""
        try:
            mm = self.memory_manager
            if mm is None:
                return

            conversation_id = event.data.get("conversation_id")
            
            if conversation_id and hasattr(mm, 'consolidate_memories'):
                # Use existing consolidate_memories method with conversation-specific tags
                mm.consolidate_memories(
                    memory_ids=[],  # Empty list means consolidate all memories
                    strategy="conversation_summary"
                )
                self.consolidations += 1
        except Exception as e:
            self._handle_error(event, e)
    
    def _handle_memory_cleanup(self, event: Event):
        """Perform memory cleanup"""
        try:
            mm = self.memory_manager
            if mm is None:
                return

            # Since cleanup_old_memories doesn't exist, we'll implement a simple cleanup
            # by recalling old memories and potentially removing them
            if hasattr(mm, 'recall'):
                # Get old memories that might need cleanup
                old_memories = mm.recall(
                    tags=["old", "cleanup"],
                    limit=100,
                    motet_context=self._system_memory_context(),
                )
                # For now, just log the cleanup attempt
                # In a real implementation, you might want to delete or archive these memories
                logger.debug("memory_cleanup_scan", old_memories_found=len(old_memories))
                self.cleanups += 1
        except Exception as e:
            self._handle_error(event, e)
    
    def get_event_filter(self) -> EventFilter:
        """Return event filter for memory-related events"""
        return EventFilter(
            event_types={
                "task_completed",
                "user_feedback_received", 
                "conversation_ended",
                "memory_cleanup_needed"
            },
            min_priority=EventPriority.LOW
        )
    
    def get_stats(self) -> Dict[str, Any]:
        """Get memory observer statistics"""
        base_stats = super().get_stats()
        base_stats.update({
            "stored_items": self.stored_items,
            "consolidations": self.consolidations,
            "cleanups": self.cleanups
        })
        return base_stats


class PerformanceObserver(Observer):
    """Observer that tracks system performance metrics"""
    
    def __init__(self):
        super().__init__("performance_observer")
        self.response_times: List[float] = []
        self.max_response_times = 1000
        self.slow_operations = 0
        self.resource_alerts = 0
    
    def on_event(self, event: Event) -> None:
        """Handle performance-related events"""
        if event.event_type == "operation_completed":
            self._track_operation_time(event)
        elif event.event_type == "resource_usage_high":
            self._handle_resource_alert(event)
        elif event.event_type == "slow_operation_detected":
            self._handle_slow_operation(event)
        elif event.event_type == "task_completed":
            # Track task completion times
            execution_time = event.data.get("execution_time")
            if execution_time:
                self._track_operation_time(event)
    
    def _track_operation_time(self, event: Event):
        """Track operation response times"""
        try:
            duration = event.data.get("duration_ms") or event.data.get("execution_time")
            if duration is not None:
                self.response_times.append(duration)
                if len(self.response_times) > self.max_response_times:
                    self.response_times = self.response_times[-self.max_response_times:]
                
                # Check for slow operations
                if duration > 5000:  # 5 seconds threshold
                    self.slow_operations += 1
        except Exception as e:
            self._handle_error(event, e)
    
    def _handle_resource_alert(self, event: Event):
        """Handle high resource usage alerts"""
        try:
            self.resource_alerts += 1
            # Could trigger additional monitoring or scaling actions
        except Exception as e:
            self._handle_error(event, e)
    
    def _handle_slow_operation(self, event: Event):
        """Handle slow operation detection"""
        try:
            self.slow_operations += 1
            # Could trigger performance analysis or optimization
        except Exception as e:
            self._handle_error(event, e)
    
    def get_event_filter(self) -> EventFilter:
        """Return event filter for performance events"""
        return EventFilter(
            event_types={
                "operation_completed",
                "resource_usage_high",
                "slow_operation_detected",
                "task_completed"
            },
            min_priority=EventPriority.LOW
        )
    
    def get_average_response_time(self) -> float:
        """Get average response time"""
        return sum(self.response_times) / len(self.response_times) if self.response_times else 0.0
    
    def get_stats(self) -> Dict[str, Any]:
        """Get performance observer statistics"""
        base_stats = super().get_stats()
        base_stats.update({
            "avg_response_time_ms": self.get_average_response_time(),
            "slow_operations": self.slow_operations,
            "resource_alerts": self.resource_alerts,
            "tracked_operations": len(self.response_times)
        })
        return base_stats


class FunctionObserver(Observer):
    """Wrapper to convert function-based observers to class-based observers"""
    
    def __init__(self, name: str, func: Callable[[Dict[str, Any]], None]):
        super().__init__(name)
        self.func = func
    
    def on_event(self, event: Event) -> None:
        """Convert Event object to dict and call the function"""
        try:
            # Call function with dict representation for backward compatibility
            self.func(event.to_dict())
        except Exception as e:
            self._handle_error(event, e)
    
    def get_event_filter(self) -> EventFilter:
        """Allow all events for function-based observers"""
        return EventFilter()  # Empty filter matches all events


# ============================================================================
# DISTRIBUTED OBSERVERS
# ============================================================================

class WorkerStats(BaseModel):
    """Statistics for a single worker."""
    worker_id: str
    last_seen: datetime
    total_commands: int = 0
    successful_commands: int = 0
    failed_commands: int = 0
    active_commands: int = 0
    avg_execution_time_ms: float = 0.0
    capabilities: Set[str] = Field(default_factory=set)
    load_factor: float = 0.0  # 0.0 = idle, 1.0 = fully loaded
    
    @property
    def success_rate(self) -> float:
        """Calculate success rate as percentage."""
        if self.total_commands == 0:
            return 0.0
        return (self.successful_commands / self.total_commands) * 100.0
    
    @property
    def is_healthy(self) -> bool:
        """Determine if worker is healthy based on recent activity and success rate."""
        # Consider worker unhealthy if not seen in last 5 minutes or success rate < 80%
        time_threshold = datetime.utcnow() - timedelta(minutes=5)
        return self.last_seen > time_threshold and self.success_rate >= 80.0


class WorkerObserver(Observer):
    """
    Observer for monitoring worker health, load, and performance in distributed architecture.
    
    Tracks:
    - Worker registration/deregistration
    - Command execution success/failure rates
    - Worker load and performance metrics
    - Worker capability utilization
    """
    
    def __init__(self):
        super().__init__("WorkerObserver")
        
        # Worker tracking
        self.workers: Dict[str, WorkerStats] = {}
        self.worker_history: deque = deque(maxlen=1000)  # Recent worker events
        
        # Performance tracking
        self.total_distributed_commands = 0
        self.total_execution_time_ms = 0.0
        self.command_distribution: Dict[str, int] = defaultdict(int)  # command_type -> count
        
        # Health monitoring
        self.unhealthy_workers: Set[str] = set()
        self.last_health_check = datetime.utcnow()
        
    def get_event_filter(self) -> EventFilter:
        """Filter for worker and command execution events."""
        return EventFilter(
            event_types={
                "command_executed",
                "worker_registered", 
                "worker_deregistered",
                "worker_health_check",
                "task_state_changed"
            },
            sources={"distributed_orchestrator", "celery_worker", "command_invoker"},
            min_priority=EventPriority.LOW,
            max_priority=EventPriority.CRITICAL
        )
    
    def on_event(self, event: Event) -> None:
        """Handle worker and command execution events."""
        
        if event.event_type == "command_executed":
            self._handle_command_executed(event)
            
        elif event.event_type == "worker_registered":
            self._handle_worker_registered(event)
            
        elif event.event_type == "worker_deregistered":
            self._handle_worker_deregistered(event)
            
        elif event.event_type == "worker_health_check":
            self._handle_worker_health_check(event)
            
        elif event.event_type == "task_state_changed":
            self._handle_task_state_changed(event)
    
    def _handle_command_executed(self, event: Event) -> None:
        """Track command execution statistics."""
        data = event.data
        
        # Extract worker information
        worker_id = data.get("worker_id", "unknown")
        command_type = data.get("command_type", "unknown")
        status = data.get("status", "unknown")
        execution_time_ms = data.get("execution_time_ms", 0.0)
        
        # Update global stats
        self.total_distributed_commands += 1
        self.total_execution_time_ms += execution_time_ms
        self.command_distribution[command_type] += 1
        
        # Update or create worker stats
        if worker_id not in self.workers:
            self.workers[worker_id] = WorkerStats(
                worker_id=worker_id,
                last_seen=datetime.utcnow()
            )
        
        worker = self.workers[worker_id]
        worker.last_seen = datetime.utcnow()
        worker.total_commands += 1
        
        if status in ["completed", "success"]:
            worker.successful_commands += 1
        else:
            worker.failed_commands += 1
        
        # Update average execution time (rolling average)
        if worker.total_commands == 1:
            worker.avg_execution_time_ms = execution_time_ms
        else:
            # Exponential moving average with alpha=0.1
            alpha = 0.1
            worker.avg_execution_time_ms = (
                alpha * execution_time_ms + 
                (1 - alpha) * worker.avg_execution_time_ms
            )
        
        # Add to history
        self.worker_history.append({
            "timestamp": datetime.utcnow().isoformat(),
            "event_type": "command_executed",
            "worker_id": worker_id,
            "command_type": command_type,
            "status": status,
            "execution_time_ms": execution_time_ms
        })
        
        # Check worker health
        self._check_worker_health(worker_id)
    
    def _handle_worker_registered(self, event: Event) -> None:
        """Handle worker registration."""
        data = event.data
        worker_id = data.get("worker_id", "unknown")
        capabilities = set(data.get("capabilities", []))
        
        if worker_id not in self.workers:
            self.workers[worker_id] = WorkerStats(
                worker_id=worker_id,
                last_seen=datetime.utcnow(),
                capabilities=capabilities
            )
        else:
            # Update existing worker
            worker = self.workers[worker_id]
            worker.last_seen = datetime.utcnow()
            worker.capabilities.update(capabilities)
        
        # Remove from unhealthy set if present
        self.unhealthy_workers.discard(worker_id)
        
        self.worker_history.append({
            "timestamp": datetime.utcnow().isoformat(),
            "event_type": "worker_registered",
            "worker_id": worker_id,
            "capabilities": list(capabilities)
        })
    
    def _handle_worker_deregistered(self, event: Event) -> None:
        """Handle worker deregistration."""
        data = event.data
        worker_id = data.get("worker_id", "unknown")
        
        # Mark as unhealthy
        self.unhealthy_workers.add(worker_id)
        
        self.worker_history.append({
            "timestamp": datetime.utcnow().isoformat(),
            "event_type": "worker_deregistered", 
            "worker_id": worker_id
        })
    
    def _handle_worker_health_check(self, event: Event) -> None:
        """Handle periodic worker health checks."""
        # Perform health check on all workers
        current_time = datetime.utcnow()
        
        for worker_id, worker in self.workers.items():
            if not worker.is_healthy:
                self.unhealthy_workers.add(worker_id)
            else:
                self.unhealthy_workers.discard(worker_id)
        
        self.last_health_check = current_time
    
    def _handle_task_state_changed(self, event: Event) -> None:
        """Handle task state changes to track active commands per worker."""
        data = event.data
        worker_id = data.get("worker_id")
        new_state = data.get("new_state")
        
        if not worker_id or worker_id not in self.workers:
            return
        
        worker = self.workers[worker_id]
        
        # Track active commands based on state
        if new_state in ["thinking", "responding"]:
            worker.active_commands += 1
        elif new_state in ["completed", "failed"]:
            worker.active_commands = max(0, worker.active_commands - 1)
        
        # Update load factor (simple heuristic: active_commands / max_concurrent)
        max_concurrent = 10  # Could be configurable
        worker.load_factor = min(1.0, worker.active_commands / max_concurrent)
    
    def _check_worker_health(self, worker_id: str) -> None:
        """Check health of a specific worker."""
        if worker_id not in self.workers:
            return
        
        worker = self.workers[worker_id]
        
        if not worker.is_healthy and worker_id not in self.unhealthy_workers:
            self.unhealthy_workers.add(worker_id)
            # Could emit alert event here
            
        elif worker.is_healthy and worker_id in self.unhealthy_workers:
            self.unhealthy_workers.discard(worker_id)
            # Could emit recovery event here
    
    def get_worker_stats(self) -> Dict[str, Any]:
        """Get comprehensive worker statistics."""
        return {
            "total_workers": len(self.workers),
            "healthy_workers": len([w for w in self.workers.values() if w.is_healthy]),
            "unhealthy_workers": len(self.unhealthy_workers),
            "total_distributed_commands": self.total_distributed_commands,
            "avg_execution_time_ms": (
                self.total_execution_time_ms / max(1, self.total_distributed_commands)
            ),
            "command_distribution": dict(self.command_distribution),
            "workers": {
                worker_id: {
                    "last_seen": worker.last_seen.isoformat(),
                    "total_commands": worker.total_commands,
                    "success_rate": worker.success_rate,
                    "active_commands": worker.active_commands,
                    "avg_execution_time_ms": worker.avg_execution_time_ms,
                    "capabilities": list(worker.capabilities),
                    "load_factor": worker.load_factor,
                    "is_healthy": worker.is_healthy
                }
                for worker_id, worker in self.workers.items()
            }
        }
    
    def get_unhealthy_workers(self) -> List[str]:
        """Get list of unhealthy worker IDs."""
        return list(self.unhealthy_workers)
    
    def get_worker_history(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Get recent worker event history."""
        return list(self.worker_history)[-limit:]


class CommandRoutingObserver(Observer):
    """
    Observer for monitoring command routing decisions and load balancing effectiveness.
    
    Tracks:
    - Routing strategy effectiveness
    - Load balancing distribution
    - Worker selection patterns
    - Routing decision latency
    """
    
    def __init__(self):
        super().__init__("CommandRoutingObserver")
        
        # Routing statistics
        self.routing_decisions: deque = deque(maxlen=1000)
        self.strategy_usage: Dict[str, int] = defaultdict(int)
        self.worker_selection_count: Dict[str, int] = defaultdict(int)
        self.capability_routing: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        
        # Performance tracking
        self.total_routing_time_ms = 0.0
        self.total_routing_decisions = 0
    
    def get_event_filter(self) -> EventFilter:
        """Filter for routing and load balancing events."""
        return EventFilter(
            event_types={
                "command_routed",
                "worker_selected", 
                "routing_strategy_applied",
                "load_balancing_decision"
            },
            sources={"command_router", "distributed_orchestrator"},
            min_priority=EventPriority.LOW,
            max_priority=EventPriority.CRITICAL
        )
    
    def on_event(self, event: Event) -> None:
        """Handle routing and load balancing events."""
        
        if event.event_type == "command_routed":
            self._handle_command_routed(event)
            
        elif event.event_type == "worker_selected":
            self._handle_worker_selected(event)
            
        elif event.event_type == "routing_strategy_applied":
            self._handle_routing_strategy_applied(event)
            
        elif event.event_type == "load_balancing_decision":
            self._handle_load_balancing_decision(event)
    
    def _handle_command_routed(self, event: Event) -> None:
        """Track command routing decisions."""
        data = event.data
        
        routing_time_ms = data.get("routing_time_ms", 0.0)
        selected_worker = data.get("selected_worker")
        command_type = data.get("command_type")
        required_capabilities = data.get("required_capabilities", [])
        
        # Update global stats
        self.total_routing_decisions += 1
        self.total_routing_time_ms += routing_time_ms
        
        if selected_worker:
            self.worker_selection_count[selected_worker] += 1
        
        # Track capability-based routing
        for capability in required_capabilities:
            if selected_worker:
                self.capability_routing[capability][selected_worker] += 1
        
        # Add to routing history
        self.routing_decisions.append({
            "timestamp": datetime.utcnow().isoformat(),
            "command_type": command_type,
            "selected_worker": selected_worker,
            "required_capabilities": required_capabilities,
            "routing_time_ms": routing_time_ms
        })
    
    def _handle_worker_selected(self, event: Event) -> None:
        """Track worker selection patterns."""
        # Implementation for worker selection tracking
        pass
    
    def _handle_routing_strategy_applied(self, event: Event) -> None:
        """Track routing strategy usage."""
        data = event.data
        strategy = data.get("strategy", "unknown")
        self.strategy_usage[strategy] += 1
    
    def _handle_load_balancing_decision(self, event: Event) -> None:
        """Track load balancing decisions."""
        # Implementation for load balancing tracking
        pass
    
    def get_routing_stats(self) -> Dict[str, Any]:
        """Get comprehensive routing statistics."""
        return {
            "total_routing_decisions": self.total_routing_decisions,
            "avg_routing_time_ms": (
                self.total_routing_time_ms / max(1, self.total_routing_decisions)
            ),
            "strategy_usage": dict(self.strategy_usage),
            "worker_selection_distribution": dict(self.worker_selection_count),
            "capability_routing_patterns": {
                capability: dict(workers) 
                for capability, workers in self.capability_routing.items()
            }
        }


class DistributedExecutionObserver(Observer):
    """
    Observer for monitoring cross-worker execution telemetry and distributed system health.
    
    Tracks:
    - Inter-worker communication latency
    - Distributed command success patterns
    - System-wide performance metrics
    - Resource utilization across workers
    """
    
    def __init__(self):
        super().__init__("DistributedExecutionObserver")
        
        # Execution tracking
        self.execution_history: deque = deque(maxlen=1000)
        self.latency_by_command_type: Dict[str, List[float]] = defaultdict(list)
        self.success_by_command_type: Dict[str, Dict[str, int]] = defaultdict(lambda: {"success": 0, "failure": 0})
        
        # System health
        self.system_load_samples: deque = deque(maxlen=100)
        self.last_system_check = datetime.utcnow()
    
    def get_event_filter(self) -> EventFilter:
        """Filter for distributed execution events."""
        return EventFilter(
            event_types={
                "command_executed",
                "distributed_command_started",
                "distributed_command_completed",
                "system_health_check"
            },
            sources={"distributed_orchestrator", "celery_worker", "command_invoker"},
            min_priority=EventPriority.NORMAL,
            max_priority=EventPriority.CRITICAL
        )
    
    def on_event(self, event: Event) -> None:
        """Handle distributed execution events."""
        
        if event.event_type in ["command_executed", "distributed_command_completed"]:
            self._handle_command_execution(event)
            
        elif event.event_type == "distributed_command_started":
            self._handle_command_started(event)
            
        elif event.event_type == "system_health_check":
            self._handle_system_health_check(event)
    
    def _handle_command_execution(self, event: Event) -> None:
        """Track distributed command execution metrics."""
        data = event.data
        
        command_type = data.get("command_type", "unknown")
        status = data.get("status", "unknown")
        execution_time_ms = data.get("execution_time_ms", 0.0)
        worker_id = data.get("worker_id")
        
        # Track latency by command type
        self.latency_by_command_type[command_type].append(execution_time_ms)
        
        # Keep only recent latency samples (last 100 per command type)
        if len(self.latency_by_command_type[command_type]) > 100:
            self.latency_by_command_type[command_type] = self.latency_by_command_type[command_type][-100:]
        
        # Track success/failure by command type
        if status in ["completed", "success"]:
            self.success_by_command_type[command_type]["success"] += 1
        else:
            self.success_by_command_type[command_type]["failure"] += 1
        
        # Add to execution history
        self.execution_history.append({
            "timestamp": datetime.utcnow().isoformat(),
            "command_type": command_type,
            "status": status,
            "execution_time_ms": execution_time_ms,
            "worker_id": worker_id
        })
    
    def _handle_command_started(self, event: Event) -> None:
        """Track command start events."""
        # Implementation for command start tracking
        pass
    
    def _handle_system_health_check(self, event: Event) -> None:
        """Track system-wide health metrics."""
        data = event.data
        
        system_load = {
            "timestamp": datetime.utcnow().isoformat(),
            "active_workers": data.get("active_workers", 0),
            "total_active_commands": data.get("total_active_commands", 0),
            "avg_worker_load": data.get("avg_worker_load", 0.0),
            "system_throughput": data.get("system_throughput", 0.0)
        }
        
        self.system_load_samples.append(system_load)
        self.last_system_check = datetime.utcnow()
    
    def get_execution_stats(self) -> Dict[str, Any]:
        """Get comprehensive execution statistics."""
        
        # Calculate latency percentiles by command type
        latency_stats = {}
        for command_type, latencies in self.latency_by_command_type.items():
            if latencies:
                sorted_latencies = sorted(latencies)
                n = len(sorted_latencies)
                latency_stats[command_type] = {
                    "count": n,
                    "avg_ms": sum(latencies) / n,
                    "p50_ms": sorted_latencies[n // 2],
                    "p95_ms": sorted_latencies[int(n * 0.95)] if n > 0 else 0,
                    "p99_ms": sorted_latencies[int(n * 0.99)] if n > 0 else 0,
                    "min_ms": min(latencies),
                    "max_ms": max(latencies)
                }
        
        # Calculate success rates by command type
        success_rates = {}
        for command_type, counts in self.success_by_command_type.items():
            total = counts["success"] + counts["failure"]
            if total > 0:
                success_rates[command_type] = {
                    "total_executions": total,
                    "success_count": counts["success"],
                    "failure_count": counts["failure"],
                    "success_rate": (counts["success"] / total) * 100.0
                }
        
        return {
            "latency_statistics": latency_stats,
            "success_rates": success_rates,
            "recent_system_load": list(self.system_load_samples)[-10:],  # Last 10 samples
            "last_system_check": self.last_system_check.isoformat()
        }


class StreamingObserver(Observer):
    """Observer that can stream events to synchronous generators in real-time
    
    Pool-aware for gevent/eventlet compatibility (ADR-0033).
    Uses WorkerLock and WorkerEvent for safe cross-pool operation.
    """
    
    def __init__(
        self,
        name: str,
        task_id: str,
        event_types: Optional[List[str]] = None,
        stream_until_event: str = "end",
        target_command_type: Optional[str] = None,
    ):
        super().__init__(name)
        self.task_id = task_id
        # More generic event types - accept all events by default, filter by task_id
        self.event_types = event_types  # None means accept all event types
        self.stream_until_event = stream_until_event  # Event type that stops streaming
        self.target_command_type = target_command_type  # Specific command type to wait for
        self._event_queue = deque()
        
        # Use pool-aware concurrency primitives (ADR-0033)
        from .concurrency_primitives import WorkerLock, WorkerEvent
        self._queue_lock = WorkerLock()
        self._new_event = WorkerEvent()
        self._active = True
        
    def get_event_filter(self) -> EventFilter:
        """Return event filter for events with our task ID"""
        return EventFilter(
            event_types=set(self.event_types) if self.event_types else set(),  # Empty set means all event types
            min_priority=EventPriority.LOW,
            custom_filter=lambda event: self._matches_task_id(event)
        )
    
    def _matches_task_id(self, event: Event) -> bool:
        """Check if event matches our task ID"""
        if not self._active:
            return False
            
        # Check for task_id in various locations
        event_task_id = (
            event.data.get("task_id") or 
            event.data.get("trace_id") or
            event.correlation_id
        )
        
        return event_task_id == self.task_id
    
    def on_event(self, event: Event) -> None:
        """Handle incoming events by adding them to the queue"""
        if not self._active:
            return
            
        # Only process events that match our task ID
        if not self._matches_task_id(event):
            return
            
        # Convert to streaming format
        streaming_event = self._convert_to_streaming_format(event)
        if streaming_event:
            self._event_queue.append(streaming_event)
            # Signal that a new event is available
            self._new_event.set()
            logger.debug("streaming_observer_event_queued", event_type=event.event_type, task_id=self.task_id)
    
    def notify(self, event: Event) -> None:
        """Compatibility method for deliver_event task"""
        self.on_event(event)
    
    def _convert_to_streaming_format(self, event: Event) -> Optional[Dict[str, Any]]:
        """Convert Event to streaming format expected by orchestrator"""
        # Use the same transformation format as the previous Redis queue polling approach
        transformed_event = {
            "event": event.event_type,  # Map from event_type to event field
            "data": event.data,
            "source": event.source,
            "timestamp": event.timestamp.isoformat() if event.timestamp else "",
            "correlation_id": event.correlation_id,
            "task_id": (
                event.data.get("task_id") or 
                event.data.get("trace_id") or
                event.correlation_id
            )
        }
        
        # Stop streaming if we get the configured stop event
        if event.event_type == self.stream_until_event:
            # If we have a target command type, also check that it matches
            if self.target_command_type:
                command_type = event.data.get("command_type")
                if command_type == self.target_command_type:
                    logger.debug("streaming_observer_stop_event_matched", event=self.stream_until_event, command_type=command_type)
                    self._active = False
                else:
                    logger.debug("streaming_observer_stop_event_skipped", event=self.stream_until_event, command_type=command_type, target=self.target_command_type)
            else:
                # No specific command type required
                self._active = False
        
        return transformed_event
    
    def stream_events(self, timeout: float = 30.0):
        """Stream events as they arrive via observer notifications (synchronous generator - ADR-0033)
        
        This is now a synchronous generator for pool compatibility.
        Uses WorkerEvent.wait() with timeout for cooperative concurrency.
        """
        import time
        from .concurrency_primitives import worker_sleep
        
        stream_start_time = time.time()
        logger.debug("streaming_observer_started", task_id=self.task_id)
        
        start_time = time.time()
        
        while self._active and (time.time() - start_time) < timeout:
                
            try:
    
                # Check if we were stopped while waiting
                if not self._active:
                    break
                
                # Yield all queued events
                while self._event_queue and self._active:
                    event = self._event_queue.popleft()
                    yield event
                    logger.debug("streaming_observer_event_emitted", event_type=event.get('event', 'unknown'))
                    
                    # Stop streaming if we get the configured stop event
                    if event.get("event") == self.stream_until_event:
                        # If we have a target command type, also check that it matches
                        if self.target_command_type:
                            command_type = event.get("data", {}).get("command_type")
                            if command_type == self.target_command_type:
                                logger.debug("streaming_observer_stream_stop_matched", event=self.stream_until_event, command_type=command_type)
                                self._active = False
                                break
                            else:
                                logger.debug("streaming_observer_stream_stop_skipped", event=self.stream_until_event, command_type=command_type, target=self.target_command_type)
                        else:
                            # No specific command type required
                            self._active = False
                            break

                # If we're no longer active (got stop event), exit immediately
                if not self._active:
                    break

                # Wait for new events or timeout (pool-aware)
                # WorkerEvent.wait() returns True if set, False on timeout
                self._new_event.wait(timeout=0.1)
                self._new_event.clear()
                        
            except Exception as e:
                logger.warning("streaming_observer_error", error=str(e), task_id=self.task_id)
                worker_sleep(0.025)
        
        stream_end_time = time.time()
        stream_duration = stream_end_time - stream_start_time
        logger.debug("streaming_observer_completed", task_id=self.task_id, duration_s=round(stream_duration, 3))
    
    def stop(self):
        """Stop the observer and clean up"""
        self._active = False
        self._new_event.set()  # Wake up any waiting coroutines




__all__ = [
    "Event",
    "EventFilter", 
    "EventPriority",
    "Observer",
    "MemoryModuleObserver",
    "PerformanceObserver",
    "FunctionObserver",
    # Distributed observers
    "WorkerObserver",
    "CommandRoutingObserver", 
    "DistributedExecutionObserver",
    # Streaming observers
    "StreamingObserver"
]
