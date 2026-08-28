"""
Motet - Process Health Monitor

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Worker process health monitor for the Motet distributed framework.

Dependencies:
    - typing: Type hints and annotations
    - Base interfaces and implementations

Usage:
    from motet.core.workers.process_health_monitor import ProcessHealthMonitor

Notes:
    - Provides core functionality
    - Integrates with distributed architecture
"""


import os
import time
import signal
import psutil
from typing import Dict, Any, List, Optional, Tuple
from pydantic import BaseModel
# Removed: from concurrent.futures import ThreadPoolExecutor, as_completed
# Now using WorkerPool for pool-aware execution (ADR-0033)
from enum import Enum
import structlog

logger = structlog.get_logger(__name__)


def _read_uss_mb(process: psutil.Process) -> Optional[float]:
    """Return USS (Unique Set Size) in MB for ``process``.

    USS = anonymous + private pages — i.e. what the kernel would actually
    reclaim if the process exited. This is the OOM-relevant figure and is
    additive across sibling processes (unlike RSS, which double-counts
    shared mappings).

    Returns ``None`` if USS is unavailable on this platform / for this
    process (e.g. macOS without elevated privileges, or a kernel without
    ``/proc/<pid>/smaps``). Callers should fall back to RSS in that case
    so the dashboard never goes blank.

    Cost: psutil walks ``/proc/<pid>/smaps`` on Linux. Measured at
    O(0.5–2 ms) for typical worker processes; called at the same cadence
    as the existing health check (default 30 s), so amortized overhead is
    negligible.
    """
    try:
        return process.memory_full_info().uss / 1024 / 1024
    except (psutil.AccessDenied, psutil.NoSuchProcess, AttributeError, NotImplementedError):
        return None
    except Exception:  # pragma: no cover - defensive
        return None


def _read_cgroup_working_set_mb() -> float:
    """Return the worker container's cgroup working set in MB, or 0.0.

    Working set := ``memory.current − inactive_file`` on cgroup v2, or
    ``memory.usage_in_bytes − total_inactive_file`` on cgroup v1. This
    matches what ``docker stats`` displays for the container ("MEM USAGE"
    column) — i.e. the memory the kernel would *not* trivially reclaim
    under pressure.

    Returns ``0.0`` if not running under a Linux cgroup (e.g. local dev
    on macOS, or any non-Linux host). The dashboard treats ``0.0`` as
    "unavailable, fall back to USS sum".
    """
    try:
        with open("/sys/fs/cgroup/memory.current", "r", encoding="utf-8") as fh:
            current_bytes = int(fh.read().strip())
        inactive_file_bytes = 0
        try:
            with open("/sys/fs/cgroup/memory.stat", "r", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("inactive_file "):
                        inactive_file_bytes = int(line.split()[1])
                        break
        except OSError:
            inactive_file_bytes = 0
        working_set = max(0, current_bytes - inactive_file_bytes)
        return working_set / 1024 / 1024
    except (OSError, ValueError):
        pass

    try:
        with open(
            "/sys/fs/cgroup/memory/memory.usage_in_bytes", "r", encoding="utf-8"
        ) as fh:
            usage_bytes = int(fh.read().strip())
        total_inactive_file = 0
        try:
            with open(
                "/sys/fs/cgroup/memory/memory.stat", "r", encoding="utf-8"
            ) as fh:
                for line in fh:
                    if line.startswith("total_inactive_file "):
                        total_inactive_file = int(line.split()[1])
                        break
        except OSError:
            total_inactive_file = 0
        working_set = max(0, usage_bytes - total_inactive_file)
        return working_set / 1024 / 1024
    except (OSError, ValueError):
        return 0.0


class ProcessHealthStatus(Enum):
    """Health status for individual processes."""
    HEALTHY = "healthy"
    WARNING = "warning"
    CRITICAL = "critical"
    STUCK = "stuck"
    UNRESPONSIVE = "unresponsive"
    TERMINATED = "terminated"


class ProcessHealthMetrics(BaseModel):
    """Health metrics for an individual process.

    Memory accounting:
        ``memory_mb`` is **USS** (Unique Set Size) — only the anonymous /
        private pages that would actually be reclaimed by killing this
        process. This matches the cgroup working-set delta for the worker
        process and converges with what ``docker stats`` reports for the
        container. Use this for dashboards, threshold decisions, and
        OOM-proximity reasoning.

        ``memory_rss_mb`` is the legacy RSS reading kept for diagnostics
        and historical comparison. It includes file-backed mappings
        (shared libraries, mmap'd artifact payloads, or other file-backed mappings)
        which are evictable by the kernel under cgroup pressure and
        therefore overstate true OOM exposure. Do **not** use it for
        new threshold logic.

        On platforms where USS is unavailable (e.g. macOS without
        elevated privileges, or a kernel that does not expose
        ``/proc/<pid>/smaps``), ``memory_mb`` falls back to RSS so the
        dashboard never goes blank — see ``_read_uss_mb``.
    """
    pid: int
    cpu_percent: float
    memory_mb: float  # USS — the OOM-relevant figure
    memory_rss_mb: float = 0.0  # RSS — diagnostic only; do not use for thresholds
    memory_percent: float
    num_threads: int
    status: str
    create_time: float
    last_check: float
    response_time_ms: Optional[float] = None
    active_tasks: int = 0
    completed_tasks: int = 0
    failed_tasks: int = 0
    consecutive_failures: int = 0
    health_status: ProcessHealthStatus = ProcessHealthStatus.HEALTHY
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for Redis storage."""
        data = self.model_dump()
        data['health_status'] = self.health_status.value
        return data
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ProcessHealthMetrics':
        """Create from dictionary loaded from Redis."""
        if 'health_status' in data and isinstance(data['health_status'], str):
            data['health_status'] = ProcessHealthStatus(data['health_status'])
        return cls.model_validate(data)


class WorkerUtilizationSummary(BaseModel):
    """Aggregated utilization summary for a worker.

    Memory accounting:
        ``total_memory_mb`` is sum of per-process **USS**. This is the
        accurate "RAM consumed by this worker's processes" figure —
        USS is additive across processes (no double-counting of shared
        library / COW pages), and it tracks the cgroup working set.
        This is the value surfaced in the ops dashboard as
        ``memory_usage_mb`` and the value the auto-termination logic
        compares against.

        ``total_memory_rss_mb`` is sum of RSS, kept for diagnostics. In
        prefork pools this is **inflated** because every child's RSS
        re-counts the same shared library / pre-fork heap pages.

        ``cgroup_memory_working_set_mb`` is read once from the worker
        container's own cgroup (``/sys/fs/cgroup/memory.current`` minus
        file cache on cgroup v2, or ``memory.usage_in_bytes`` minus
        ``total_inactive_file`` on v1). This is the most accurate
        container-level number and matches ``docker stats`` exactly. It
        is ``0.0`` outside Linux containers (e.g. local dev on macOS
        without a Linux kernel cgroup interface).
    """
    worker_id: str
    total_processes: int
    healthy_processes: int
    warning_processes: int
    critical_processes: int
    stuck_processes: int
    avg_cpu_percent: float
    total_memory_mb: float  # sum of USS — the OOM-relevant figure
    total_memory_rss_mb: float = 0.0  # sum of RSS — diagnostic; double-counts shared pages in prefork
    cgroup_memory_working_set_mb: float = 0.0  # container cgroup working set; matches `docker stats`
    avg_memory_percent: float
    total_active_tasks: int
    total_completed_tasks: int
    total_failed_tasks: int
    overall_health_score: float  # 0.0 to 1.0
    last_updated: float
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for Redis storage."""
        return self.model_dump()
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'WorkerUtilizationSummary':
        """Create from dictionary loaded from Redis."""
        return cls.model_validate(data)


class ProcessHealthMonitor:
    """
    Enhanced process-level health monitor for Celery workers.
    
    This class implements the core functionality of ADR-0008 Phase 2:
    - Discovers and monitors all processes in a Celery worker
    - Performs individual health checks with configurable thresholds
    - Terminates stuck or unhealthy processes
    - Aggregates utilization data for routing decisions
    - Integrates with circuit breakers for failure handling
    """
    
    def __init__(self, worker_id: str, pool_type: str = "fork", celery_concurrency: Optional[int] = None):
        self.worker_id = worker_id
        self.pool_type = pool_type  # ADR-0033: fork, gevent, eventlet, threads
        
        # ADR-0033: Get actual Celery concurrency for capacity calculation
        # For fork pool: this equals number of processes
        # For gevent/eventlet/threads: this is greenlets/threads count (e.g., 1000)
        # Must be passed in from command line detection (no env var fallback to avoid inconsistencies)
        self.celery_concurrency = celery_concurrency if celery_concurrency is not None else 20
        
        # Health check thresholds (configurable)
        self.cpu_warning_threshold = 80.0  # 80% CPU
        self.cpu_critical_threshold = 95.0  # 95% CPU
        self.memory_warning_threshold = float(os.getenv("MOTET_MEMORY_WARNING_MB", "1200"))  # 1200MB default
        self.memory_critical_threshold = float(os.getenv("MOTET_MEMORY_CRITICAL_MB", "1800"))  # 1800MB default
        self.response_timeout_ms = 5000  # 5 second response timeout
        self.consecutive_failure_threshold = 3  # 3 consecutive failures = stuck
        
        # Process tracking
        self._process_metrics: Dict[int, ProcessHealthMetrics] = {}
        self._worker_processes: List[int] = []
        
        logger.info("ProcessHealthMonitor initialized", 
                   worker_id=worker_id,
                   pool_type=pool_type,
                   celery_concurrency=self.celery_concurrency,
                   cpu_warning=self.cpu_warning_threshold,
                   memory_warning=self.memory_warning_threshold)
    
    def discover_worker_processes(self) -> List[int]:
        """
        Discover all processes belonging to this Celery worker.
        
        Returns:
            List of process IDs (PIDs) for this worker
        """
        try:
            current_pid = os.getpid()
            current_process = psutil.Process(current_pid)
            
            # Get parent process (should be the main Celery worker process)
            parent = current_process.parent()
            if not parent:
                # We are the parent process
                parent = current_process
            
            # Find all child processes of the parent (don't include parent itself)
            worker_pids = []
            
            try:
                children = parent.children(recursive=False)  # Only direct children, not recursive
                for child in children:
                    # Only include processes that are still running
                    if child.is_running():
                        worker_pids.append(child.pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                # Parent process may have died or we don't have access
                pass
            
            # Filter to only include actual worker pool processes (exclude parent, monitors, etc.)
            celery_worker_pids = []
            for pid in worker_pids:
                try:
                    proc = psutil.Process(pid)
                    cmdline = ' '.join(proc.cmdline())
                    
                    # Only count actual Celery worker pool processes
                    # These typically have patterns like: "celeryd" or "celery worker" in cmdline
                    # and are direct children of the main process
                    # Exclude:
                    # - Processes with "beat" (celery beat scheduler)
                    # - Processes that are just generic python/bash processes
                    # - Monitor/management processes
                    if 'celery' in cmdline.lower() and 'worker' in cmdline.lower():
                        # Exclude beat scheduler
                        if 'beat' not in cmdline.lower():
                            celery_worker_pids.append(pid)
                        
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            
            self._worker_processes = celery_worker_pids
            
            logger.info("Discovered worker processes", 
                       worker_id=self.worker_id,
                       process_count=len(celery_worker_pids),
                       pids=celery_worker_pids,
                       parent_pid=parent.pid)
            
            return celery_worker_pids
            
        except Exception as e:
            logger.error("Failed to discover worker processes", 
                        worker_id=self.worker_id,
                        error=str(e),
                        exc_info=True)
            return []
    
    def check_individual_process_health(self, pid: int) -> ProcessHealthMetrics:
        """
        Perform comprehensive health check on an individual process.
        
        Args:
            pid: Process ID to check
            
        Returns:
            ProcessHealthMetrics with current health status
        """
        try:
            process = psutil.Process(pid)
            
            # Basic process metrics
            cpu_percent = process.cpu_percent(interval=0.1)  # 100ms sample
            memory_info = process.memory_info()
            memory_rss_mb = memory_info.rss / 1024 / 1024
            # ADR-0107 metric-fix: prefer USS for the OOM-relevant figure.
            # Fall back to RSS when USS is unavailable (e.g. macOS without
            # elevated privileges) so the dashboard never goes blank.
            uss_mb = _read_uss_mb(process)
            memory_mb = uss_mb if uss_mb is not None else memory_rss_mb
            memory_percent = process.memory_percent()
            num_threads = process.num_threads()
            status = process.status()
            create_time = process.create_time()
            
            # Get existing metrics for trend analysis
            existing_metrics = self._process_metrics.get(pid)
            
            # Initialize counters from existing metrics or defaults
            active_tasks = existing_metrics.active_tasks if existing_metrics else 0
            completed_tasks = existing_metrics.completed_tasks if existing_metrics else 0
            failed_tasks = existing_metrics.failed_tasks if existing_metrics else 0
            consecutive_failures = existing_metrics.consecutive_failures if existing_metrics else 0
            
            # TODO: Implement task counting via Celery inspection
            # For now, we'll use placeholder values
            
            # Determine health status based on thresholds
            health_status = self._determine_health_status(
                cpu_percent, memory_mb, memory_percent, status, consecutive_failures
            )
            
            # Create metrics object
            metrics = ProcessHealthMetrics(
                pid=pid,
                cpu_percent=cpu_percent,
                memory_mb=memory_mb,
                memory_rss_mb=memory_rss_mb,
                memory_percent=memory_percent,
                num_threads=num_threads,
                status=status,
                create_time=create_time,
                last_check=time.time(),
                active_tasks=active_tasks,
                completed_tasks=completed_tasks,
                failed_tasks=failed_tasks,
                consecutive_failures=consecutive_failures,
                health_status=health_status
            )
            
            # Store metrics for trend analysis
            self._process_metrics[pid] = metrics
            
            logger.debug("Process health check completed", 
                        worker_id=self.worker_id,
                        pid=pid,
                        cpu_percent=cpu_percent,
                        memory_mb=memory_mb,
                        health_status=health_status.value)
            
            return metrics
            
        except psutil.NoSuchProcess:
            # Process no longer exists
            logger.info("Process no longer exists", 
                       worker_id=self.worker_id,
                       pid=pid)
            
            # Remove from tracking
            if pid in self._process_metrics:
                del self._process_metrics[pid]
            
            # Return terminated status
            return ProcessHealthMetrics(
                pid=pid,
                cpu_percent=0.0,
                memory_mb=0.0,
                memory_percent=0.0,
                num_threads=0,
                status="terminated",
                create_time=0.0,
                last_check=time.time(),
                health_status=ProcessHealthStatus.TERMINATED
            )
            
        except Exception as e:
            logger.error("Failed to check process health", 
                        worker_id=self.worker_id,
                        pid=pid,
                        error=str(e),
                        exc_info=True)
            
            # Return critical status for failed health checks
            return ProcessHealthMetrics(
                pid=pid,
                cpu_percent=0.0,
                memory_mb=0.0,
                memory_percent=0.0,
                num_threads=0,
                status="unknown",
                create_time=0.0,
                last_check=time.time(),
                health_status=ProcessHealthStatus.CRITICAL
            )
    
    def _determine_health_status(self, cpu_percent: float, memory_mb: float, 
                                memory_percent: float, status: str, 
                                consecutive_failures: int) -> ProcessHealthStatus:
        """
        Determine health status based on metrics and thresholds.
        
        Args:
            cpu_percent: CPU usage percentage
            memory_mb: Memory usage in MB
            memory_percent: Memory usage percentage
            status: Process status from psutil
            consecutive_failures: Number of consecutive failures
            
        Returns:
            ProcessHealthStatus enum value
        """
        # Check for stuck processes first
        if consecutive_failures >= self.consecutive_failure_threshold:
            return ProcessHealthStatus.STUCK
        
        # Check process status
        if status in ['zombie', 'dead']:
            return ProcessHealthStatus.TERMINATED
        
        if status in ['stopped', 'tracing_stop']:
            return ProcessHealthStatus.UNRESPONSIVE
        
        # Check resource usage
        if (cpu_percent >= self.cpu_critical_threshold or 
            memory_mb >= self.memory_critical_threshold):
            return ProcessHealthStatus.CRITICAL
        
        if (cpu_percent >= self.cpu_warning_threshold or 
            memory_mb >= self.memory_warning_threshold):
            return ProcessHealthStatus.WARNING
        
        return ProcessHealthStatus.HEALTHY
    
    def terminate_stuck_process(self, pid: int, force: bool = False) -> bool:
        """
        Terminate a stuck or unhealthy process.
        
        Args:
            pid: Process ID to terminate
            force: If True, use SIGKILL instead of SIGTERM
            
        Returns:
            True if process was terminated, False otherwise
        """
        try:
            process = psutil.Process(pid)
            
            logger.warning("Terminating stuck process", 
                          worker_id=self.worker_id,
                          pid=pid,
                          force=force,
                          process_status=process.status())
            
            if force:
                # Force kill with SIGKILL
                process.kill()
                signal_used = "SIGKILL"
            else:
                # Graceful termination with SIGTERM
                process.terminate()
                signal_used = "SIGTERM"
            
            # Wait for process to terminate (up to 10 seconds)
            try:
                process.wait(timeout=10)
                terminated = True
            except psutil.TimeoutExpired:
                if not force:
                    # Graceful termination failed, try force kill
                    logger.warning("Graceful termination failed, forcing kill", 
                                  worker_id=self.worker_id,
                                  pid=pid)
                    return self.terminate_stuck_process(pid, force=True)
                else:
                    terminated = False
            
            if terminated:
                logger.info("Process terminated successfully", 
                           worker_id=self.worker_id,
                           pid=pid,
                           signal=signal_used)
                
                # Remove from tracking
                if pid in self._process_metrics:
                    del self._process_metrics[pid]
                
                return True
            else:
                logger.error("Failed to terminate process", 
                            worker_id=self.worker_id,
                            pid=pid,
                            signal=signal_used)
                return False
                
        except psutil.NoSuchProcess:
            # Process already terminated
            logger.info("Process already terminated", 
                       worker_id=self.worker_id,
                       pid=pid)
            
            if pid in self._process_metrics:
                del self._process_metrics[pid]
            
            return True
            
        except Exception as e:
            logger.error("Failed to terminate process", 
                        worker_id=self.worker_id,
                        pid=pid,
                        error=str(e),
                        exc_info=True)
            return False
    
    def perform_comprehensive_health_check(self) -> List[ProcessHealthMetrics]:
        """
        Perform comprehensive health check on all worker processes.
        
        Returns:
            List of ProcessHealthMetrics for all discovered processes
        """
        logger.info("Starting comprehensive health check", 
                   worker_id=self.worker_id)
        
        # Discover current processes
        current_pids = self.discover_worker_processes()
        
        # ADR-0033: For single-process pools (gevent/eventlet/threads), monitor parent process
        if len(current_pids) == 0:
            logger.info("No child processes found - monitoring parent process (single-process pool)", 
                       worker_id=self.worker_id, 
                       parent_pid=os.getpid())
            current_pids = [os.getpid()]
        
        # Check health of each process in parallel (ADR-0033: pool-aware execution)
        from .concurrency_primitives import WorkerExecutor
        
        all_metrics = []
        max_workers = min(len(current_pids), 10) if len(current_pids) > 0 else 1
        with WorkerExecutor(max_workers=max_workers) as executor:
            # Submit all health checks in parallel
            future_to_pid = {
                executor.submit(self.check_individual_process_health, pid): pid 
                for pid in current_pids
            }
            
            # Collect results as they complete
            for future, pid in future_to_pid.items():
                try:
                    metrics = future.result()
                    all_metrics.append(metrics)
                except Exception as e:
                    logger.error("Failed to check process health", 
                               worker_id=self.worker_id,
                               pid=pid,
                               error=str(e))
                    # Create a failed metrics entry
                    now_ts = time.time()
                    all_metrics.append(ProcessHealthMetrics(
                        pid=pid,
                        cpu_percent=0.0,
                        memory_mb=0.0,
                        memory_percent=0.0,
                        num_threads=0,
                        status="unknown",
                        create_time=now_ts,
                        last_check=now_ts,
                        active_tasks=0,
                        completed_tasks=0,
                        failed_tasks=0,
                        consecutive_failures=1,
                        health_status=ProcessHealthStatus.UNRESPONSIVE,
                    ))
        
        # Check if any processes need termination (after parallel collection)
        for metrics in all_metrics:
            if metrics.health_status in [ProcessHealthStatus.STUCK, ProcessHealthStatus.CRITICAL]:
                logger.warning("Unhealthy process detected", 
                              worker_id=self.worker_id,
                              pid=metrics.pid,
                              health_status=metrics.health_status.value,
                              cpu_percent=metrics.cpu_percent,
                              memory_mb=metrics.memory_mb,
                              consecutive_failures=metrics.consecutive_failures)
                
                # Implement termination policy - integrate with WorkerLifecycleService
                if metrics.health_status == ProcessHealthStatus.STUCK:
                    logger.critical("Process is stuck and will be terminated", 
                                   worker_id=self.worker_id,
                                   pid=metrics.pid)
                    
                    # Schedule termination via WorkerLifecycleService
                    try:
                        # Import here to avoid circular dependencies
                        import asyncio
                        from ..distributed.worker_lifecycle import get_lifecycle_service, TerminationReason, TerminationMethod
                        
                        # Create async task to terminate the worker
                        async def terminate_stuck_worker():
                            try:
                                lifecycle_service = get_lifecycle_service()
                                result = lifecycle_service.terminate_worker(
                                    worker_id=self.worker_id,
                                    reason=TerminationReason.STUCK_TASKS,
                                    method=TerminationMethod.GRACEFUL_SHUTDOWN,
                                    timeout_seconds=30
                                )
                                logger.info("Stuck worker termination completed", 
                                          worker_id=self.worker_id,
                                          result=result)
                            except Exception as e:
                                logger.error("Failed to terminate stuck worker", 
                                           worker_id=self.worker_id,
                                           error=str(e))
                        
                        # Schedule the termination (non-blocking)
                        loop = asyncio.get_event_loop()
                        loop.create_task(terminate_stuck_worker())
                        
                    except Exception as e:
                        logger.error("Failed to schedule worker termination", 
                                   worker_id=self.worker_id,
                                   pid=metrics.pid,
                                   error=str(e))
                
                elif metrics.health_status == ProcessHealthStatus.CRITICAL:
                    logger.warning("Process is critical - monitoring for escalation", 
                                 worker_id=self.worker_id,
                                 pid=metrics.pid,
                                 consecutive_failures=metrics.consecutive_failures)
                    
                    # Escalate to termination if critical for too long
                    if metrics.consecutive_failures >= 3:
                        logger.critical("Critical process escalated to termination", 
                                       worker_id=self.worker_id,
                                       pid=metrics.pid)
                        
                        try:
                            import asyncio
                            from ..distributed.worker_lifecycle import get_lifecycle_service, TerminationReason, TerminationMethod
                            
                            async def terminate_critical_worker():
                                try:
                                    lifecycle_service = get_lifecycle_service()
                                    result = lifecycle_service.terminate_worker(
                                        worker_id=self.worker_id,
                                        reason=TerminationReason.HIGH_ERROR_RATE,
                                        method=TerminationMethod.GRACEFUL_SHUTDOWN,
                                        timeout_seconds=60
                                    )
                                    logger.info("Critical worker termination completed", 
                                              worker_id=self.worker_id,
                                              result=result)
                                except Exception as e:
                                    logger.error("Failed to terminate critical worker", 
                                               worker_id=self.worker_id,
                                               error=str(e))
                            
                            # Schedule the termination (non-blocking)
                            loop = asyncio.get_event_loop()
                            loop.create_task(terminate_critical_worker())
                            
                        except Exception as e:
                            logger.error("Failed to schedule critical worker termination", 
                                       worker_id=self.worker_id,
                                       pid=metrics.pid,
                                       error=str(e))
        
        logger.info("Comprehensive health check completed", 
                   worker_id=self.worker_id,
                   total_processes=len(all_metrics),
                   healthy=len([m for m in all_metrics if m.health_status == ProcessHealthStatus.HEALTHY]),
                   warning=len([m for m in all_metrics if m.health_status == ProcessHealthStatus.WARNING]),
                   critical=len([m for m in all_metrics if m.health_status == ProcessHealthStatus.CRITICAL]),
                   stuck=len([m for m in all_metrics if m.health_status == ProcessHealthStatus.STUCK]))
        
        return all_metrics
    
    def calculate_worker_utilization(self, process_metrics: List[ProcessHealthMetrics]) -> WorkerUtilizationSummary:
        """
        Calculate aggregated worker utilization from individual process metrics.
        
        Args:
            process_metrics: List of ProcessHealthMetrics for all processes
            
        Returns:
            WorkerUtilizationSummary with aggregated data
        """
        if not process_metrics:
            return WorkerUtilizationSummary(
                worker_id=self.worker_id,
                total_processes=0,
                healthy_processes=0,
                warning_processes=0,
                critical_processes=0,
                stuck_processes=0,
                avg_cpu_percent=0.0,
                total_memory_mb=0.0,
                total_memory_rss_mb=0.0,
                cgroup_memory_working_set_mb=_read_cgroup_working_set_mb(),
                avg_memory_percent=0.0,
                total_active_tasks=0,
                total_completed_tasks=0,
                total_failed_tasks=0,
                overall_health_score=0.0,
                last_updated=time.time()
            )
        
        # Count processes by health status
        healthy_count = len([m for m in process_metrics if m.health_status == ProcessHealthStatus.HEALTHY])
        warning_count = len([m for m in process_metrics if m.health_status == ProcessHealthStatus.WARNING])
        critical_count = len([m for m in process_metrics if m.health_status == ProcessHealthStatus.CRITICAL])
        stuck_count = len([m for m in process_metrics if m.health_status == ProcessHealthStatus.STUCK])
        
        # Calculate averages and totals
        actual_process_count = len(process_metrics)
        avg_cpu = sum(m.cpu_percent for m in process_metrics) / actual_process_count
        # ADR-0107 metric-fix: total_memory_mb is sum of USS (additive, no
        # double-count of shared pages); total_memory_rss_mb is the legacy
        # sum-of-RSS kept for diagnostics and historical comparison.
        total_memory = sum(m.memory_mb for m in process_metrics)
        total_memory_rss = sum(m.memory_rss_mb for m in process_metrics)
        cgroup_working_set = _read_cgroup_working_set_mb()
        avg_memory_percent = sum(m.memory_percent for m in process_metrics) / actual_process_count
        total_active_tasks = sum(m.active_tasks for m in process_metrics)
        total_completed_tasks = sum(m.completed_tasks for m in process_metrics)
        total_failed_tasks = sum(m.failed_tasks for m in process_metrics)
        
        # ADR-0033: Determine effective concurrency based on pool type
        # Fork pool: concurrency = process count (each process runs 1 task)
        # Gevent/eventlet/threads: concurrency = greenlets/threads (from --concurrency flag)
        if self.pool_type in ["gevent", "eventlet", "threads"]:
            # Single-process pools: use Celery concurrency (greenlets/threads)
            effective_concurrency = self.celery_concurrency
            logger.debug("Using Celery concurrency for single-process pool", 
                        worker_id=self.worker_id,
                        pool_type=self.pool_type,
                        effective_concurrency=effective_concurrency,
                        actual_processes=actual_process_count)
        else:
            # Fork pool: process count = concurrency
            effective_concurrency = actual_process_count
            logger.debug("Using process count for fork pool", 
                        worker_id=self.worker_id,
                        pool_type=self.pool_type,
                        effective_concurrency=effective_concurrency)
        
        # Calculate overall health score (0.0 to 1.0)
        # Healthy processes contribute 1.0, warning 0.7, critical 0.3, stuck 0.0
        health_weights = {
            ProcessHealthStatus.HEALTHY: 1.0,
            ProcessHealthStatus.WARNING: 0.7,
            ProcessHealthStatus.CRITICAL: 0.3,
            ProcessHealthStatus.STUCK: 0.0,
            ProcessHealthStatus.UNRESPONSIVE: 0.1,
            ProcessHealthStatus.TERMINATED: 0.0
        }
        
        total_weight = sum(health_weights.get(m.health_status, 0.0) for m in process_metrics)
        overall_health_score = total_weight / actual_process_count if actual_process_count > 0 else 0.0
        
        return WorkerUtilizationSummary(
            worker_id=self.worker_id,
            total_processes=effective_concurrency,  # ADR-0033: Use effective concurrency (greenlets/threads or processes)
            healthy_processes=healthy_count,
            warning_processes=warning_count,
            critical_processes=critical_count,
            stuck_processes=stuck_count,
            avg_cpu_percent=avg_cpu,
            total_memory_mb=total_memory,
            total_memory_rss_mb=total_memory_rss,
            cgroup_memory_working_set_mb=cgroup_working_set,
            avg_memory_percent=avg_memory_percent,
            total_active_tasks=total_active_tasks,
            total_completed_tasks=total_completed_tasks,
            total_failed_tasks=total_failed_tasks,
            overall_health_score=overall_health_score,
            last_updated=time.time()
        )
    
    def store_utilization_data_sync(self, utilization: WorkerUtilizationSummary, 
                                   process_metrics: Optional[List[ProcessHealthMetrics]] = None) -> None:
        """
        Store worker utilization data in Redis for routing decisions (synchronous version).
        Also updates circuit breaker states based on health metrics.
        
        Args:
            utilization: WorkerUtilizationSummary to store
            process_metrics: Optional list of ProcessHealthMetrics for circuit breaker updates
        """
        try:
            from ..distributed.redis_manager import store_structured_data_sync
            
            # Store utilization data with TTL
            utilization_key = f"worker:utilization:{self.worker_id}"
            
            store_structured_data_sync(
                client_id="process_health_monitoring",
                key=utilization_key,
                data=utilization.to_dict(),
                format_type="hash"
            )
            
            # Update circuit breaker states if process metrics provided
            if process_metrics:
                try:
                    from .process_circuit_breakers import get_circuit_breaker_manager
                    
                    # Update circuit breaker states synchronously (without storing to Redis)
                    manager = get_circuit_breaker_manager()
                    manager.update_from_utilization_summary(utilization, process_metrics)
                    
                    logger.debug("Circuit breaker states updated", 
                                worker_id=self.worker_id,
                                process_count=len(process_metrics))
                    
                except Exception as cb_error:
                    logger.error("Failed to update circuit breaker states", 
                                worker_id=self.worker_id,
                                error=str(cb_error))
            
            logger.debug("Worker utilization data stored", 
                        worker_id=self.worker_id,
                        overall_health_score=utilization.overall_health_score,
                        total_processes=utilization.total_processes)
            
        except Exception as e:
            logger.error("Failed to store utilization data", 
                        worker_id=self.worker_id,
                        error=str(e),
                        exc_info=True)


# Convenience functions for easy access
def create_process_health_monitor(worker_id: str, pool_type: str = "fork", celery_concurrency: Optional[int] = None) -> ProcessHealthMonitor:
    """
    Create a ProcessHealthMonitor instance for the given worker.
    
    Args:
        worker_id: Worker ID
        pool_type: Celery pool type (fork, gevent, eventlet, threads) - ADR-0033
        celery_concurrency: Celery --concurrency value (for single-process pools) - ADR-0033
    
    Returns:
        ProcessHealthMonitor instance
    """
    return ProcessHealthMonitor(worker_id, pool_type=pool_type, celery_concurrency=celery_concurrency)


def perform_worker_health_check_sync(worker_id: str) -> WorkerUtilizationSummary:
    """
    Convenience function to perform a complete health check and return utilization summary (sync version).
    
    Args:
        worker_id: Worker ID to check
        
    Returns:
        WorkerUtilizationSummary with current health data
    """
    monitor = create_process_health_monitor(worker_id)
    
    # Perform comprehensive health check
    process_metrics = monitor.perform_comprehensive_health_check()
    
    # Calculate utilization summary
    utilization = monitor.calculate_worker_utilization(process_metrics)
    
    # Store in Redis for routing and update circuit breakers (sync version)
    monitor.store_utilization_data_sync(utilization, process_metrics)
    
    return utilization


async def perform_worker_health_check(worker_id: str) -> WorkerUtilizationSummary:
    """
    Convenience function to perform a complete health check and return utilization summary (async version).
    
    Args:
        worker_id: Worker ID to check
        
    Returns:
        WorkerUtilizationSummary with current health data
    """
    # Use the sync version since all the underlying operations are sync
    return perform_worker_health_check_sync(worker_id)


# Export main classes and functions
__all__ = [
    'ProcessHealthStatus',
    'ProcessHealthMetrics',
    'WorkerUtilizationSummary',
    'ProcessHealthMonitor',
    'create_process_health_monitor',
    'perform_worker_health_check_sync',
    'perform_worker_health_check'
]
