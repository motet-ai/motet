"""
Motet - Process Circuit Breakers

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Worker process circuit breakers for the Motet distributed framework.

Dependencies:
    - typing: Type hints and annotations
    - Base interfaces and implementations

Usage:
    from motet.core.workers.process_circuit_breakers import ProcessCircuitBreakers

Notes:
    - Provides core functionality
    - Integrates with distributed architecture
"""


import time
from typing import Dict, Any, List, Optional, Tuple
from pydantic import BaseModel
from enum import Enum
import structlog

from ..resilience.breaker import CircuitState, CircuitBreakerOpenException
from .process_health_monitor import ProcessHealthStatus, ProcessHealthMetrics, WorkerUtilizationSummary

logger = structlog.get_logger(__name__)


class CircuitBreakerLevel(Enum):
    """Circuit breaker hierarchy levels."""
    PROCESS = "process"
    WORKER = "worker"
    COMMAND = "command"


class ProcessCircuitBreakerConfig(BaseModel):
    """Configuration for process-level circuit breakers."""
    failure_threshold: int = 5  # Number of failures to open circuit
    success_threshold: int = 3  # Number of successes to close circuit
    timeout_seconds: int = 60  # Time to wait before trying half-open
    failure_rate_threshold: float = 0.6  # 60% failure rate threshold
    monitor_window_seconds: int = 300  # 5 minute monitoring window
    
    # Health-based thresholds
    cpu_critical_threshold: float = 95.0  # 95% CPU usage
    memory_critical_threshold: float = 500.0  # 500MB memory usage
    consecutive_health_failures: int = 3  # Health check failures to open circuit


class CircuitBreakerState(BaseModel):
    """Current state of a circuit breaker."""
    level: CircuitBreakerLevel
    identifier: str  # PID for process, worker_id for worker, command_type for command
    state: str  # CircuitState value
    failure_count: int = 0
    success_count: int = 0
    last_failure_time: Optional[float] = None
    last_success_time: Optional[float] = None
    opened_at: Optional[float] = None
    last_attempt_time: Optional[float] = None
    total_calls: int = 0
    failure_rate: float = 0.0
    health_failure_count: int = 0  # Consecutive health check failures
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for Redis storage."""
        data = self.model_dump()
        data['level'] = self.level.value
        return data
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'CircuitBreakerState':
        """Create from dictionary loaded from Redis."""
        if 'level' in data and isinstance(data['level'], str):
            data['level'] = CircuitBreakerLevel(data['level'])
        return cls.model_validate(data)


class ProcessCircuitBreaker:
    """
    Circuit breaker for individual processes with health monitoring integration.
    
    This class implements process-level circuit breaking with:
    - Traditional failure-based circuit breaking
    - Health monitoring integration (CPU, memory, responsiveness)
    - Automatic state transitions based on process health
    - Integration with the multi-level circuit breaker hierarchy
    """
    
    def __init__(self, pid: int, config: Optional[ProcessCircuitBreakerConfig] = None):
        self.pid = pid
        self.config = config or ProcessCircuitBreakerConfig()
        self.identifier = f"process_{pid}"
        
        # Initialize circuit breaker state
        self._state = CircuitBreakerState(
            level=CircuitBreakerLevel.PROCESS,
            identifier=self.identifier,
            state=CircuitState.CLOSED
        )
        
        # Failure tracking window
        self._failure_window: List[float] = []
        self._success_window: List[float] = []
        
        logger.info("ProcessCircuitBreaker initialized", 
                   pid=pid,
                   identifier=self.identifier,
                   failure_threshold=self.config.failure_threshold)
    
    def record_success(self) -> None:
        """Record a successful operation."""
        current_time = time.time()
        
        self._state.success_count += 1
        self._state.total_calls += 1
        self._state.last_success_time = current_time
        self._state.last_attempt_time = current_time
        self._state.health_failure_count = 0  # Reset health failures on success
        
        # Add to success window
        self._success_window.append(current_time)
        self._cleanup_windows(current_time)
        
        # Update failure rate
        self._update_failure_rate()
        
        # Check for state transition to CLOSED
        if self._state.state == CircuitState.HALF_OPEN:
            if self._state.success_count >= self.config.success_threshold:
                self._transition_to_closed()
        
        logger.debug("Process circuit breaker success recorded", 
                    pid=self.pid,
                    state=self._state.state,
                    success_count=self._state.success_count)
    
    def record_failure(self, error: Optional[Exception] = None) -> None:
        """Record a failed operation."""
        current_time = time.time()
        
        self._state.failure_count += 1
        self._state.total_calls += 1
        self._state.last_failure_time = current_time
        self._state.last_attempt_time = current_time
        
        # Add to failure window
        self._failure_window.append(current_time)
        self._cleanup_windows(current_time)
        
        # Update failure rate
        self._update_failure_rate()
        
        # Check for state transition to OPEN
        if self._state.state == CircuitState.CLOSED:
            if (self._state.failure_count >= self.config.failure_threshold or
                self._state.failure_rate >= self.config.failure_rate_threshold):
                self._transition_to_open()
        elif self._state.state == CircuitState.HALF_OPEN:
            # Any failure in half-open state opens the circuit
            self._transition_to_open()
        
        logger.warning("Process circuit breaker failure recorded", 
                      pid=self.pid,
                      state=self._state.state,
                      failure_count=self._state.failure_count,
                      failure_rate=self._state.failure_rate,
                      error=str(error) if error else None)
    
    def record_health_failure(self, health_metrics: ProcessHealthMetrics) -> None:
        """Record a health check failure and potentially open circuit."""
        self._state.health_failure_count += 1
        
        # Check if we should open circuit based on health
        should_open = False
        reasons = []
        
        # Check consecutive health failures
        if self._state.health_failure_count >= self.config.consecutive_health_failures:
            should_open = True
            reasons.append(f"consecutive_health_failures={self._state.health_failure_count}")
        
        # Check critical resource usage
        if health_metrics.cpu_percent >= self.config.cpu_critical_threshold:
            should_open = True
            reasons.append(f"cpu_critical={health_metrics.cpu_percent}%")
        
        if health_metrics.memory_mb >= self.config.memory_critical_threshold:
            should_open = True
            reasons.append(f"memory_critical={health_metrics.memory_mb}MB")
        
        # Check for stuck process
        if health_metrics.health_status == ProcessHealthStatus.STUCK:
            should_open = True
            reasons.append("process_stuck")
        
        if should_open and self._state.state != CircuitState.OPEN:
            logger.critical("Opening process circuit breaker due to health issues", 
                           pid=self.pid,
                           reasons=reasons,
                           health_status=health_metrics.health_status.value,
                           cpu_percent=health_metrics.cpu_percent,
                           memory_mb=health_metrics.memory_mb)
            self._transition_to_open()
    
    def record_health_success(self, health_metrics: ProcessHealthMetrics) -> None:
        """Record a successful health check."""
        self._state.health_failure_count = 0
        
        # If process is healthy and circuit is open due to health issues,
        # consider transitioning to half-open
        if (self._state.state == CircuitState.OPEN and 
            health_metrics.health_status == ProcessHealthStatus.HEALTHY):
            
            # Check if timeout has passed
            if (self._state.opened_at and 
                time.time() - self._state.opened_at >= self.config.timeout_seconds):
                self._transition_to_half_open()
    
    def can_execute(self) -> bool:
        """Check if operations can be executed through this circuit breaker."""
        current_time = time.time()
        
        if self._state.state == CircuitState.CLOSED:
            return True
        elif self._state.state == CircuitState.HALF_OPEN:
            return True
        elif self._state.state == CircuitState.OPEN:
            # Check if timeout has passed
            if (self._state.opened_at and 
                current_time - self._state.opened_at >= self.config.timeout_seconds):
                self._transition_to_half_open()
                return True
            return False
        
        return False
    
    def get_state(self) -> CircuitBreakerState:
        """Get current circuit breaker state."""
        return self._state
    
    def _transition_to_closed(self) -> None:
        """Transition circuit breaker to CLOSED state."""
        self._state.state = CircuitState.CLOSED
        self._state.failure_count = 0
        self._state.success_count = 0
        self._state.opened_at = None
        self._state.health_failure_count = 0
        
        logger.info("Process circuit breaker transitioned to CLOSED", 
                   pid=self.pid)
    
    def _transition_to_open(self) -> None:
        """Transition circuit breaker to OPEN state."""
        self._state.state = CircuitState.OPEN
        self._state.opened_at = time.time()
        
        logger.warning("Process circuit breaker transitioned to OPEN", 
                      pid=self.pid,
                      failure_count=self._state.failure_count,
                      failure_rate=self._state.failure_rate)
    
    def _transition_to_half_open(self) -> None:
        """Transition circuit breaker to HALF_OPEN state."""
        self._state.state = CircuitState.HALF_OPEN
        self._state.success_count = 0
        
        logger.info("Process circuit breaker transitioned to HALF_OPEN", 
                   pid=self.pid)
    
    def _cleanup_windows(self, current_time: float) -> None:
        """Clean up old entries from failure and success windows."""
        cutoff_time = current_time - self.config.monitor_window_seconds
        
        # Clean failure window
        self._failure_window = [t for t in self._failure_window if t > cutoff_time]
        
        # Clean success window
        self._success_window = [t for t in self._success_window if t > cutoff_time]
    
    def _update_failure_rate(self) -> None:
        """Update the current failure rate."""
        total_recent_calls = len(self._failure_window) + len(self._success_window)
        
        if total_recent_calls > 0:
            self._state.failure_rate = len(self._failure_window) / total_recent_calls
        else:
            self._state.failure_rate = 0.0


class WorkerCircuitBreaker:
    """
    Worker-level circuit breaker that aggregates process-level circuit breaker states.
    
    This class provides:
    - Aggregated circuit breaking based on process health
    - Worker-level availability scoring
    - Integration with routing decisions
    - Cascading protection from unhealthy processes
    """
    
    def __init__(self, worker_id: str, config: Optional[ProcessCircuitBreakerConfig] = None):
        self.worker_id = worker_id
        self.config = config or ProcessCircuitBreakerConfig()
        self.identifier = f"worker_{worker_id}"
        
        # Process circuit breakers
        self._process_breakers: Dict[int, ProcessCircuitBreaker] = {}
        
        # Worker-level state
        self._state = CircuitBreakerState(
            level=CircuitBreakerLevel.WORKER,
            identifier=self.identifier,
            state=CircuitState.CLOSED
        )
        
        logger.info("WorkerCircuitBreaker initialized", 
                   worker_id=worker_id,
                   identifier=self.identifier)
    
    def get_or_create_process_breaker(self, pid: int) -> ProcessCircuitBreaker:
        """Get or create a process circuit breaker."""
        if pid not in self._process_breakers:
            self._process_breakers[pid] = ProcessCircuitBreaker(pid, self.config)
        return self._process_breakers[pid]
    
    def update_from_health_metrics(self, process_metrics: List[ProcessHealthMetrics]) -> None:
        """Update circuit breaker states based on health metrics."""
        for metrics in process_metrics:
            process_breaker = self.get_or_create_process_breaker(metrics.pid)
            
            if metrics.health_status == ProcessHealthStatus.HEALTHY:
                process_breaker.record_health_success(metrics)
            else:
                process_breaker.record_health_failure(metrics)
        
        # Update worker-level state based on process states
        self._update_worker_state()
    
    def calculate_availability_score(self) -> float:
        """
        Calculate worker availability score based on process circuit breaker states.
        
        Returns:
            Float between 0.0 and 1.0 representing availability
        """
        if not self._process_breakers:
            return 1.0  # No processes tracked yet
        
        # Count processes by circuit breaker state
        closed_count = 0
        half_open_count = 0
        open_count = 0
        
        for breaker in self._process_breakers.values():
            state = breaker.get_state().state
            if state == CircuitState.CLOSED:
                closed_count += 1
            elif state == CircuitState.HALF_OPEN:
                half_open_count += 1
            elif state == CircuitState.OPEN:
                open_count += 1
        
        total_processes = len(self._process_breakers)
        
        # Calculate availability score
        # Closed processes contribute 1.0, half-open 0.5, open 0.0
        availability_score = (
            (closed_count * 1.0 + half_open_count * 0.5 + open_count * 0.0) / 
            total_processes
        )
        
        return availability_score
    
    def get_process_states(self) -> Dict[int, CircuitBreakerState]:
        """Get all process circuit breaker states."""
        return {pid: breaker.get_state() for pid, breaker in self._process_breakers.items()}
    
    def get_worker_state(self) -> CircuitBreakerState:
        """Get worker-level circuit breaker state."""
        return self._state
    
    def can_accept_requests(self) -> bool:
        """Check if worker can accept new requests."""
        availability_score = self.calculate_availability_score()
        
        # Worker is available if at least 30% of processes are healthy
        return availability_score >= 0.3
    
    def _update_worker_state(self) -> None:
        """Update worker-level circuit breaker state based on process states."""
        availability_score = self.calculate_availability_score()
        
        # Determine worker state based on availability
        if availability_score >= 0.7:
            # Most processes healthy
            if self._state.state != CircuitState.CLOSED:
                self._state.state = CircuitState.CLOSED
                logger.info("Worker circuit breaker transitioned to CLOSED", 
                           worker_id=self.worker_id,
                           availability_score=availability_score)
        elif availability_score >= 0.3:
            # Some processes healthy
            if self._state.state != CircuitState.HALF_OPEN:
                self._state.state = CircuitState.HALF_OPEN
                logger.info("Worker circuit breaker transitioned to HALF_OPEN", 
                           worker_id=self.worker_id,
                           availability_score=availability_score)
        else:
            # Most processes unhealthy
            if self._state.state != CircuitState.OPEN:
                self._state.state = CircuitState.OPEN
                self._state.opened_at = time.time()
                logger.warning("Worker circuit breaker transitioned to OPEN", 
                              worker_id=self.worker_id,
                              availability_score=availability_score)


class MultiLevelCircuitBreakerManager:
    """
    Manager for multi-level circuit breaker hierarchy.
    
    This class coordinates:
    - Process-level circuit breakers
    - Worker-level circuit breakers
    - Integration with existing command-level circuit breakers
    - Cascading failure protection
    - Redis storage for routing decisions
    """
    
    def __init__(self):
        self._worker_breakers: Dict[str, WorkerCircuitBreaker] = {}
        self.config = ProcessCircuitBreakerConfig()
        
        logger.info("MultiLevelCircuitBreakerManager initialized")
    
    def get_or_create_worker_breaker(self, worker_id: str) -> WorkerCircuitBreaker:
        """Get or create a worker circuit breaker."""
        if worker_id not in self._worker_breakers:
            self._worker_breakers[worker_id] = WorkerCircuitBreaker(worker_id, self.config)
        return self._worker_breakers[worker_id]
    
    def update_from_utilization_summary(self, utilization: WorkerUtilizationSummary, 
                                      process_metrics: List[ProcessHealthMetrics]) -> None:
        """Update circuit breaker states from worker utilization summary."""
        worker_breaker = self.get_or_create_worker_breaker(utilization.worker_id)
        worker_breaker.update_from_health_metrics(process_metrics)
        
        logger.debug("Circuit breaker states updated from health metrics", 
                    worker_id=utilization.worker_id,
                    availability_score=worker_breaker.calculate_availability_score(),
                    worker_state=worker_breaker.get_worker_state().state)
    
    async def store_circuit_breaker_states(self, worker_id: str) -> None:
        """Store circuit breaker states in Redis for routing decisions."""
        try:
            from ..distributed.redis_manager import store_structured_data
            
            worker_breaker = self._worker_breakers.get(worker_id)
            if not worker_breaker:
                return
            
            # Prepare circuit breaker data
            circuit_breaker_data = {
                "worker_id": worker_id,
                "worker_state": worker_breaker.get_worker_state().to_dict(),
                "availability_score": worker_breaker.calculate_availability_score(),
                "can_accept_requests": worker_breaker.can_accept_requests(),
                "process_states": {
                    str(pid): state.to_dict() 
                    for pid, state in worker_breaker.get_process_states().items()
                },
                "last_updated": time.time()
            }
            
            # Store in Redis
            circuit_breaker_key = f"circuit_breaker_states:{worker_id}"
            
            await store_structured_data(
                client_id="circuit_breaker_monitoring",
                key=circuit_breaker_key,
                data=circuit_breaker_data,
                format_type="hash"
            )
            
            logger.debug("Circuit breaker states stored", 
                        worker_id=worker_id,
                        availability_score=circuit_breaker_data["availability_score"])
            
        except Exception as e:
            logger.error("Failed to store circuit breaker states", 
                        worker_id=worker_id,
                        error=str(e),
                        exc_info=True)
    
    def get_worker_availability_scores(self) -> Dict[str, float]:
        """Get availability scores for all workers."""
        return {
            worker_id: breaker.calculate_availability_score()
            for worker_id, breaker in self._worker_breakers.items()
        }
    
    def get_system_health_summary(self) -> Dict[str, Any]:
        """Get overall system health summary."""
        total_workers = len(self._worker_breakers)
        if total_workers == 0:
            return {
                "total_workers": 0,
                "healthy_workers": 0,
                "degraded_workers": 0,
                "unhealthy_workers": 0,
                "overall_availability": 1.0
            }
        
        healthy_workers = 0
        degraded_workers = 0
        unhealthy_workers = 0
        total_availability = 0.0
        
        for breaker in self._worker_breakers.values():
            availability = breaker.calculate_availability_score()
            total_availability += availability
            
            if availability >= 0.7:
                healthy_workers += 1
            elif availability >= 0.3:
                degraded_workers += 1
            else:
                unhealthy_workers += 1
        
        return {
            "total_workers": total_workers,
            "healthy_workers": healthy_workers,
            "degraded_workers": degraded_workers,
            "unhealthy_workers": unhealthy_workers,
            "overall_availability": total_availability / total_workers
        }


# Global circuit breaker manager instance
_circuit_breaker_manager: Optional[MultiLevelCircuitBreakerManager] = None


def get_circuit_breaker_manager() -> MultiLevelCircuitBreakerManager:
    """Get the global circuit breaker manager instance."""
    global _circuit_breaker_manager
    if _circuit_breaker_manager is None:
        _circuit_breaker_manager = MultiLevelCircuitBreakerManager()
    return _circuit_breaker_manager


# Convenience functions
async def update_circuit_breakers_from_health(utilization: WorkerUtilizationSummary, 
                                            process_metrics: List[ProcessHealthMetrics]) -> None:
    """Update circuit breaker states from health monitoring data."""
    manager = get_circuit_breaker_manager()
    manager.update_from_utilization_summary(utilization, process_metrics)
    await manager.store_circuit_breaker_states(utilization.worker_id)


def get_worker_availability_score(worker_id: str) -> float:
    """Get availability score for a specific worker."""
    manager = get_circuit_breaker_manager()
    worker_breaker = manager._worker_breakers.get(worker_id)
    if worker_breaker:
        return worker_breaker.calculate_availability_score()
    return 1.0  # Default to available if not tracked


# Export main classes and functions
__all__ = [
    'CircuitBreakerLevel',
    'ProcessCircuitBreakerConfig',
    'CircuitBreakerState',
    'ProcessCircuitBreaker',
    'WorkerCircuitBreaker',
    'MultiLevelCircuitBreakerManager',
    'get_circuit_breaker_manager',
    'update_circuit_breakers_from_health',
    'get_worker_availability_score'
]
