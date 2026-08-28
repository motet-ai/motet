"""
Motet - Worker Lifecycle

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Worker lifecycle management for the Motet distributed framework.
    Handles health monitoring, start/stop/restart, and termination of workers.
    Start/stop/restart delegate to a pluggable backend: Docker (same
    host) or HTTP (multi-host/PaaS).

Dependencies:
    - typing: Type hints and annotations
    - motet.core.distributed.worker_lifecycle_backends: get_lifecycle_backend
    - Base interfaces and implementations

Usage:
    from motet.core.distributed.worker_lifecycle import (
        WorkerLifecycleService,
        get_lifecycle_service,
        TerminationReason,
        TerminationMethod,
        WorkerHealthMetrics,
    )

Notes:
    - Provides core lifecycle functionality (start, stop, restart, terminate).
    - Readiness state updates (STOPPED/STARTING/RESTARTING) are done here;
      backends only perform the physical action.
"""


import os
import time
import signal
from enum import Enum
from typing import Dict, List, Optional, Any, Set
from pydantic import BaseModel
from datetime import datetime, timedelta

import structlog

from .worker_readiness import get_readiness_service, WorkerState
from .redis_manager import retrieve_structured_data_sync
from motet.core.constants import DEFAULT_REDIS_URL

logger = structlog.get_logger(__name__)


class TerminationReason(Enum):
    """Reasons for worker termination"""
    UNRESPONSIVE = "unresponsive"
    HIGH_ERROR_RATE = "high_error_rate"
    MEMORY_LEAK = "memory_leak"
    STUCK_TASKS = "stuck_tasks"
    MANUAL_REQUEST = "manual_request"
    HEALTH_CHECK_FAILED = "health_check_failed"
    TIMEOUT_EXCEEDED = "timeout_exceeded"


class TerminationMethod(Enum):
    """Methods for terminating workers"""
    GRACEFUL_SHUTDOWN = "graceful_shutdown"  # SIGTERM, allow cleanup
    FORCED_KILL = "forced_kill"              # SIGKILL, immediate termination
    CELERY_REVOKE = "celery_revoke"          # Revoke all tasks and shutdown
    DOCKER_RESTART = "docker_restart"        # Restart Docker container


class WorkerHealthMetrics(BaseModel):
    """Health metrics for a worker"""
    worker_id: str
    last_heartbeat: float
    success_rate: float
    active_tasks: int
    stuck_tasks: int
    memory_usage_mb: float
    cpu_usage_percent: float
    response_time_avg_ms: float
    error_count_last_hour: int
    uptime_seconds: float
    
    def is_healthy(self) -> bool:
        """Determine if worker is healthy based on metrics"""
        now = time.time()
        
        # Check heartbeat (5 minute threshold)
        if now - self.last_heartbeat > 300:
            return False
        
        # Check success rate (80% threshold)
        if self.success_rate < 80.0:
            return False
        
        # Check for stuck tasks (tasks running > 10 minutes)
        if self.stuck_tasks > 0:
            return False
        
        # Check error rate (> 10 errors per hour)
        if self.error_count_last_hour > 10:
            return False
        
        return True
    
    def get_termination_reason(self) -> Optional[TerminationReason]:
        """Get the reason this worker should be terminated, if any"""
        if not self.is_healthy():
            now = time.time()
            
            if now - self.last_heartbeat > 300:
                return TerminationReason.UNRESPONSIVE
            
            if self.success_rate < 80.0:
                return TerminationReason.HIGH_ERROR_RATE
            
            if self.stuck_tasks > 0:
                return TerminationReason.STUCK_TASKS
            
            if self.error_count_last_hour > 10:
                return TerminationReason.HIGH_ERROR_RATE
        
        return None


class WorkerLifecycleService:
    """
    Service for managing worker health monitoring and lifecycle (start/stop/restart/terminate).

    Start/stop/restart delegate to a pluggable backend (ADR-0067). This service
    drives readiness state; the backend performs the physical action (Docker or HTTP).
    """

    def __init__(
        self,
        redis_url: str = DEFAULT_REDIS_URL,
        backend: Optional[Any] = None,
    ):
        self.redis_url = redis_url
        self._termination_history: List[Dict[str, Any]] = []

        if backend is None:
            from .worker_lifecycle_backends import get_lifecycle_backend

            backend = get_lifecycle_backend()
        self._backend = backend

        # Configuration
        self.termination_timeout = 60  # seconds for graceful shutdown
        self.max_termination_history = 100

        # Termination thresholds
        self.max_consecutive_failures = 3
        self.max_stuck_task_duration = 600  # 10 minutes
        self.error_rate_threshold = 10  # errors per hour
        
    def initialize(self):
        """
        Initialize the lifecycle service.
        
        NOTE (ADR-0038): Continuous health monitoring is now handled by parent_coordinator.py.
        This service provides termination coordination and policy enforcement only.
        """
        logger.info("worker_lifecycle_service_initialized", mode="coordination_only")
    
    def shutdown(self):
        """
        Shutdown the lifecycle service.
        
        NOTE (ADR-0038): Health monitoring is handled by parent_coordinator.py; no threads here.
        """
        logger.info("worker_lifecycle_service_shutdown_complete")
    
    def terminate_worker(self, 
                             worker_id: str, 
                             reason: TerminationReason,
                             method: TerminationMethod = TerminationMethod.GRACEFUL_SHUTDOWN,
                             timeout_seconds: int = 60) -> Dict[str, Any]:
        """
        Terminate a specific worker.
        
        Args:
            worker_id: ID of worker to terminate
            reason: Reason for termination
            method: Termination method to use
            timeout_seconds: Timeout for graceful shutdown
            
        Returns:
            Dictionary with termination result
        """
        start_time = time.time()
        
        logger.warning(
            "worker_termination_requested",
            worker_id=worker_id,
            reason=reason.value,
            method=method.value,
        )
        
        try:
            readiness_service = get_readiness_service()
            readiness_service.update_worker_state(worker_id, WorkerState.TERMINATING)

            # Execute termination based on method
            if method == TerminationMethod.GRACEFUL_SHUTDOWN:
                result = self._graceful_shutdown(worker_id, timeout_seconds)
            elif method == TerminationMethod.FORCED_KILL:
                result = self._forced_kill(worker_id)
            elif method == TerminationMethod.CELERY_REVOKE:
                result = self._celery_revoke(worker_id)
            elif method == TerminationMethod.DOCKER_RESTART:
                result = self._docker_restart(worker_id)
            else:
                raise ValueError(f"Unknown termination method: {method}")
            
            # Record termination in history
            termination_record = {
                'timestamp': datetime.now().isoformat(),
                'worker_id': worker_id,
                'reason': reason.value,
                'method': method.value,
                'duration_seconds': time.time() - start_time,
                'success': result.get('success', False),
                'details': result
            }
            
            self._termination_history.append(termination_record)
            
            # Limit history size
            if len(self._termination_history) > self.max_termination_history:
                self._termination_history = self._termination_history[-self.max_termination_history:]
            
            # Remove from readiness service
            readiness_service.remove_worker(worker_id)
            
            logger.info(
                "worker_termination_completed",
                worker_id=worker_id,
                success=bool(result.get("success")),
                duration_seconds=(time.time() - start_time),
            )
            
            return termination_record
            
        except Exception as e:
            error_record = {
                'timestamp': datetime.now().isoformat(),
                'worker_id': worker_id,
                'reason': reason.value,
                'method': method.value,
                'duration_seconds': time.time() - start_time,
                'success': False,
                'error': str(e)
            }
            
            self._termination_history.append(error_record)
            logger.error(
                "worker_termination_failed",
                worker_id=worker_id,
                error=str(e),
                exc_info=True,
            )
            
            return error_record

    def start_worker(self, worker_id: str) -> Dict[str, Any]:
        """
        Start a worker (delegates to backend). Updates readiness to STARTING on success.

        Args:
            worker_id: ID of the worker to start

        Returns:
            Dictionary with start result (success, method, container?, error?)
        """
        try:
            result = self._backend.start_worker(worker_id)
            if result.get("success"):
                readiness_service = get_readiness_service()
                existing = readiness_service.get_worker_info(worker_id)
                if existing:
                    readiness_service.update_worker_state(
                        worker_id, WorkerState.STARTING
                    )
                else:
                    readiness_service.register_worker(worker_id, capabilities=[])
            return result
        except Exception as e:
            return {"success": False, "error": str(e)}

    def stop_worker(self, worker_id: str, timeout_seconds: int = 30) -> Dict[str, Any]:
        """
        Stop a worker (delegates to backend). Updates readiness to STOPPED first.

        Args:
            worker_id: ID of the worker to stop
            timeout_seconds: Timeout in seconds before force stop

        Returns:
            Dictionary with stop result
        """
        try:
            readiness_service = get_readiness_service()
            readiness_service.update_worker_state(worker_id, WorkerState.STOPPED)
            return self._backend.stop_worker(worker_id, timeout_seconds)
        except Exception as e:
            return {"success": False, "error": str(e)}

    def restart_worker(self, worker_id: str) -> Dict[str, Any]:
        """
        Restart a worker (delegates to backend). Updates readiness RESTARTING then STARTING.

        Args:
            worker_id: ID of the worker to restart

        Returns:
            Dictionary with restart result
        """
        try:
            readiness_service = get_readiness_service()
            readiness_service.update_worker_state(worker_id, WorkerState.RESTARTING)
            result = self._backend.restart_worker(worker_id)
            if result.get("success"):
                readiness_service.update_worker_state(
                    worker_id, WorkerState.STARTING
                )
            return result
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def get_worker_health_metrics(self, worker_id: str) -> Optional[WorkerHealthMetrics]:
        """Get comprehensive health metrics for a worker"""
        try:
            # Get basic info from readiness service
            readiness_service = get_readiness_service()
            worker_info = readiness_service.get_worker_info(worker_id)
            
            if not worker_info:
                return None
            
            # Get additional metrics from Redis
            redis_metrics = self._get_redis_worker_metrics(worker_id)
            
            # Combine metrics
            return WorkerHealthMetrics(
                worker_id=worker_id,
                last_heartbeat=worker_info.last_heartbeat,
                success_rate=self._calculate_success_rate(worker_id),
                active_tasks=worker_info.active_commands,
                stuck_tasks=self._count_stuck_tasks(worker_id),
                memory_usage_mb=redis_metrics.get('memory_usage_mb', 0),
                cpu_usage_percent=redis_metrics.get('cpu_usage_percent', 0),
                response_time_avg_ms=redis_metrics.get('response_time_avg_ms', 0),
                error_count_last_hour=self._count_recent_errors(worker_id),
                uptime_seconds=time.time() - worker_info.startup_time if worker_info.startup_time > 0 else 0
            )
            
        except Exception as e:
            logger.warning(
                "worker_health_metrics_failed",
                worker_id=worker_id,
                error=str(e),
                exc_info=True,
            )
            return None
    
    def get_unhealthy_workers(self) -> List[str]:
        """Get list of workers that should be terminated.

        Excludes workers in STOPPED, TERMINATING, or RESTARTING state so we
        do not auto-terminate lifecycle-managed workers.
        """
        readiness_service = get_readiness_service()
        all_workers = readiness_service.get_all_workers()
        excluded_states = {
            WorkerState.STOPPED,
            WorkerState.TERMINATING,
            WorkerState.RESTARTING,
        }

        unhealthy_workers = []
        for worker_id, worker_info in all_workers.items():
            if worker_info.state in excluded_states:
                continue
            metrics = self.get_worker_health_metrics(worker_id)
            if metrics and not metrics.is_healthy():
                unhealthy_workers.append(worker_id)
        return unhealthy_workers
    
    def auto_terminate_unhealthy_workers(self) -> List[Dict[str, Any]]:
        """Automatically terminate all unhealthy workers"""
        unhealthy_workers = self.get_unhealthy_workers()
        termination_results = []
        
        for worker_id in unhealthy_workers:
            metrics = self.get_worker_health_metrics(worker_id)
            if metrics:
                reason = metrics.get_termination_reason()
                if reason:
                    # Choose termination method based on reason
                    if reason in [TerminationReason.UNRESPONSIVE, TerminationReason.STUCK_TASKS]:
                        method = TerminationMethod.FORCED_KILL
                    else:
                        method = TerminationMethod.GRACEFUL_SHUTDOWN
                    
                    result = self.terminate_worker(worker_id, reason, method)
                    termination_results.append(result)
        
        return termination_results
    
    def get_termination_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Get recent termination history"""
        return self._termination_history[-limit:]
    
    # Private methods for different termination strategies
    
    def _graceful_shutdown(self, worker_id: str, timeout_seconds: int) -> Dict[str, Any]:
        """Attempt graceful shutdown of worker"""
        try:
            # Send SIGTERM to worker process
            pid = self._get_worker_pid(worker_id)
            if pid:
                import os
                os.kill(pid, signal.SIGTERM)
                
                # Wait for graceful shutdown
                for _ in range(timeout_seconds):
                    if not self._is_worker_alive(worker_id):
                        return {'success': True, 'method': 'graceful_shutdown'}
                    time.sleep(1)
                
                # If still alive, force kill
                logger.warning("worker_shutdown_not_graceful_forcing_kill", worker_id=worker_id)
                return self._forced_kill(worker_id)
            else:
                return {'success': False, 'error': 'Could not find worker PID'}
                
        except Exception as e:
            return {'success': False, 'error': str(e)}
    
    def _forced_kill(self, worker_id: str) -> Dict[str, Any]:
        """Force kill worker process"""
        try:
            pid = self._get_worker_pid(worker_id)
            if pid:
                import os
                os.kill(pid, signal.SIGKILL)
                
                # Verify termination
                time.sleep(2)
                if not self._is_worker_alive(worker_id):
                    return {'success': True, 'method': 'forced_kill'}
                else:
                    return {'success': False, 'error': 'Worker still alive after SIGKILL'}
            else:
                return {'success': False, 'error': 'Could not find worker PID'}
                
        except Exception as e:
            return {'success': False, 'error': str(e)}
    
    def _celery_revoke(self, worker_id: str) -> Dict[str, Any]:
        """Revoke all tasks and shutdown worker via Celery"""
        try:
            # This would require Celery app access
            # For now, return placeholder
            return {'success': False, 'error': 'Celery revoke not implemented yet'}
            
        except Exception as e:
            return {'success': False, 'error': str(e)}
    
    def _docker_restart(self, worker_id: str) -> Dict[str, Any]:
        """Restart worker via backend (used by terminate_worker for DOCKER_RESTART)."""
        readiness_service = get_readiness_service()
        readiness_service.update_worker_state(worker_id, WorkerState.RESTARTING)
        result = self._backend.restart_worker(worker_id)
        if result.get("success"):
            readiness_service.update_worker_state(worker_id, WorkerState.STARTING)
        return result
    
    def _get_worker_pid(self, worker_id: str) -> Optional[int]:
        """Get process ID for worker from Redis worker registry."""
        try:
            worker_data = retrieve_structured_data_sync(
                "worker_lifecycle",
                f"worker:registration:{worker_id}",
                format_type="json_string"
            )
            
            if worker_data:
                return worker_data.get('worker_pid')
        except Exception as e:
            logger.warning("worker_pid_lookup_failed", worker_id=worker_id, error=str(e), exc_info=True)
        
        return None
    
    def _is_worker_alive(self, worker_id: str) -> bool:
        """Check if worker process is still alive"""
        pid = self._get_worker_pid(worker_id)
        if pid:
            try:
                import os
                os.kill(pid, 0)  # Signal 0 just checks if process exists
                return True
            except OSError:
                return False
        return False
    
    def _get_redis_worker_metrics(self, worker_id: str) -> Dict[str, Any]:
        """Get worker metrics directly from Redis worker registry using UnifiedRedisManager."""
        try:
            # Use UnifiedRedisManager for consistent data access
            worker_data = retrieve_structured_data_sync(
                "worker_lifecycle", 
                f"worker:registration:{worker_id}", 
                format_type="hash"
            )
            
            # Also check utilization data for real-time health metrics
            utilization_data = retrieve_structured_data_sync(
                "worker_lifecycle",
                f"worker:utilization:{worker_id}",
                format_type="hash"
            )
            
            # Start with worker registration data
            metrics = {
                'memory_usage_mb': worker_data.get('memory_usage_mb', 0) if worker_data else 0,
                'cpu_usage_percent': worker_data.get('cpu_usage_percent', 0) if worker_data else 0,
                'active_commands': worker_data.get('active_commands', 0) if worker_data else 0,
                'last_heartbeat': worker_data.get('last_heartbeat', 0) if worker_data else 0,
                'state': worker_data.get('state', 'unknown') if worker_data else 'unknown',
                'warmup_completed': worker_data.get('warmup_completed', False) if worker_data else False,
                'response_time_avg_ms': 0  # Would need to calculate from task history
            }
            
            # Override with real-time utilization data if available
            if utilization_data:
                # ADR-0107 metric-fix: prefer the cgroup working set (matches
                # `docker stats` exactly) when available; otherwise fall back
                # to the sum-of-USS, which is the OOM-relevant per-process
                # accounting (no shared-page double counting). The legacy
                # sum-of-RSS is intentionally NOT preferred — it counted
                # mmap'd library / model file pages that the kernel evicts
                # under pressure, inflating the dashboard reading vs. true
                # container memory consumption.
                cgroup_ws = float(utilization_data.get('cgroup_memory_working_set_mb', 0) or 0)
                uss_sum = float(utilization_data.get('total_memory_mb', 0) or 0)
                metrics['memory_usage_mb'] = cgroup_ws if cgroup_ws > 0 else uss_sum
                metrics['cpu_usage_percent'] = float(utilization_data.get('avg_cpu_percent', 0))
                metrics['active_commands'] = int(utilization_data.get('total_active_tasks', 0))
                metrics['overall_health_score'] = float(utilization_data.get('overall_health_score', 0))
                metrics['total_processes'] = int(utilization_data.get('total_processes', 0))
                metrics['healthy_processes'] = int(utilization_data.get('healthy_processes', 0))
                
            return metrics
            
        except Exception as e:
            logger.warning("worker_redis_metrics_failed", worker_id=worker_id, error=str(e), exc_info=True)
        
        return {}
    
    def _calculate_success_rate(self, worker_id: str) -> float:
        """Calculate success rate for worker"""
        # This would integrate with task history/metrics
        # For now, return placeholder
        return 95.0
    
    def _count_stuck_tasks(self, worker_id: str) -> int:
        """Count tasks that have been running too long"""
        # This would check Celery active tasks
        # For now, return placeholder
        return 0
    
    def _count_recent_errors(self, worker_id: str) -> int:
        """Count errors in the last hour"""
        # This would check error logs/metrics
        # For now, return placeholder
        return 0


# Global service instance
_lifecycle_service: Optional[WorkerLifecycleService] = None


def get_lifecycle_service() -> WorkerLifecycleService:
    """Get or create the global worker lifecycle service"""
    global _lifecycle_service
    
    if _lifecycle_service is None:
        import os
        redis_url = os.getenv('MOTET_REDIS_URL', DEFAULT_REDIS_URL)
        _lifecycle_service = WorkerLifecycleService(redis_url)
        _lifecycle_service.initialize()
    
    return _lifecycle_service
