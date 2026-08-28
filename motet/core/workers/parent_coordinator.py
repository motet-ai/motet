"""
Motet - Parent Coordinator

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Worker parent coordinator for the Motet distributed framework.
    Owns parent-process readiness registration, heartbeats, health checks,
    and recovery re-registration after Redis/readiness key loss (#151).

Dependencies:
    - typing: Type hints and annotations
    - psutil: Parent/child process detection
    - Worker readiness service for Redis registration
    - Worker context creation from tasks._create_worker_context

Usage:
    from motet.core.workers.parent_coordinator import (
        initialize_parent_coordination,
        publish_worker_readiness_from_context,
    )

    initialize_parent_coordination("cloud_worker1")
    publish_worker_readiness_from_context(worker_id, worker_context)

Notes:
    - Never marks a worker ready with empty/fallback capabilities (#151)
    - Successful context rebuilds must rewrite Redis capabilities via
      publish_worker_readiness_from_context
    - Integrates with distributed architecture
"""


import os
import sys
import time
import threading
import logging
from typing import Dict, Any, Optional

import psutil

logger = logging.getLogger(__name__)

# Debug gating for noisy worker startup logs.
DEBUG_WORKER_STARTUP = os.getenv("MOTET_DEBUG_WORKER_STARTUP", "false").lower() == "true"


def _startup_log(msg: str) -> None:
    """Log startup breadcrumbs at INFO when enabled, otherwise at DEBUG."""
    if DEBUG_WORKER_STARTUP:
        logger.info(msg)
    else:
        logger.debug(msg)

# Global thread tracking for parent process
_parent_heartbeat_thread: Optional[threading.Thread] = None
_parent_heartbeat_stop_event: Optional[threading.Event] = None
_parent_health_thread: Optional[threading.Thread] = None
_parent_health_stop_event: Optional[threading.Event] = None
_parent_cleanup_thread: Optional[threading.Thread] = None
_parent_cleanup_stop_event: Optional[threading.Event] = None

# Initialization flag
_parent_coordination_initialized = False


def is_celery_parent_process() -> bool:
    """
    Detect if this is the Celery parent process or a child worker.
    
    Parent process characteristics:
    - Has 'celery worker' in command line
    - Has worker child processes (when running)
    - Has specific environment markers
    
    Returns:
        True if parent process, False if child
    """
    try:
        current_process = psutil.Process()
        cmdline = ' '.join(current_process.cmdline())
        
        # Check for celery worker in command line
        if 'celery' not in cmdline or 'worker' not in cmdline:
            return False
        
        # Check if we're executing from main worker module
        # Parent imports tasks module, children execute tasks
        if os.getenv('CELERY_LOADER') and not os.getenv('CELERY_WORKER_PID'):
            # This is parent - set marker for children
            os.environ['CELERY_PARENT_PID'] = str(os.getpid())
            return True
        
        # If CELERY_PARENT_PID is set and doesn't match us, we're a child
        parent_pid = os.getenv('CELERY_PARENT_PID')
        if parent_pid and int(parent_pid) != os.getpid():
            return False
        
        # Check if we have child processes (after fork)
        # Note: During startup, children may not exist yet
        children = current_process.children(recursive=False)
        
        # If we have many children or none (startup), and celery in cmdline, we're parent
        # The key is we're not a worker process (which would have CELERY_WORKER_PID set)
        return True
        
    except Exception as e:
        logger.warning(f"Could not determine parent process status: {e}")
        # Conservative: assume child process
        return False


def is_celery_child_process() -> bool:
    """
    Detect if this is a child worker process.
    
    Returns:
        True if child process, False if parent
    """
    return not is_celery_parent_process()


def get_celery_concurrency_from_args() -> int:
    """
    Parse Celery --concurrency value from command line arguments.
    
    Returns:
        Concurrency value from --concurrency flag, or 20 as default
    """
    try:
        import sys
        for i, arg in enumerate(sys.argv):
            if arg.startswith('--concurrency='):
                return int(arg.split('=')[1])
            elif arg == '--concurrency' and i + 1 < len(sys.argv):
                return int(sys.argv[i + 1])
        
        # Default if not found
        logger.debug("No --concurrency flag found in command line, using default of 20")
        return 20
    except Exception as e:
        logger.warning(f"Failed to parse --concurrency from args: {e}, using default of 20")
        return 20


def _is_usable_worker_context(worker_context: Dict[str, Any]) -> bool:
    """
    Return True when a worker context is safe to advertise as ready.

    Fallback contexts from ``_create_worker_context()`` set ``error`` and
    ``capabilities=[]``. Marking those ready leaves Redis as
    ``state=ready`` + ``capabilities=[]``, which CapabilityFilter rejects
    even after later in-process recovery (#151).
    """
    if not worker_context:
        return False
    if worker_context.get("error"):
        return False
    capabilities = worker_context.get("capabilities") or []
    return len(capabilities) > 0


def _count_mcp_tools(worker_context: Dict[str, Any]) -> int:
    """Count MCP tools currently present on the context tool registry."""
    tool_registry = worker_context.get("tool_registry")
    if not tool_registry:
        return 0
    tools = tool_registry.list_items()
    return len([t for t in tools if str(t).startswith("mcp.")])


def publish_worker_readiness_from_context(
    worker_id: str,
    worker_context: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Register a worker in Redis from an in-process context.

    Always writes a registration entry (so heartbeats have a key). Marks the
    worker ready only when the context is usable (non-empty capabilities and
    not a fallback/error context). Call this after any successful context
    rebuild so Redis capabilities match in-process state (#151).

    Args:
        worker_id: Worker identifier
        worker_context: Context dict from ``_create_worker_context()``

    Returns:
        Dict with registration outcome:
        ``registered``, ``marked_ready``, ``capabilities``, ``tool_count``,
        ``mcp_tool_count``
    """
    from ..distributed.worker_readiness import get_readiness_service

    capabilities = list(worker_context.get("capabilities") or [])
    tool_count = int(worker_context.get("tool_count") or 0)
    mcp_tool_count = _count_mcp_tools(worker_context)
    pool_type = worker_context.get("pool_type", "unknown")
    initial_concurrency = get_celery_concurrency_from_args()
    usable = _is_usable_worker_context(worker_context)

    readiness_service = get_readiness_service()
    readiness_service.register_worker(
        worker_id=worker_id,
        capabilities=capabilities,
        max_concurrency=initial_concurrency,
        pool_type=pool_type,
    )

    marked_ready = False
    if usable:
        readiness_service.mark_worker_ready(
            worker_id=worker_id,
            tool_count=tool_count,
            mcp_tool_count=mcp_tool_count,
            warmup_duration_ms=0,
        )
        marked_ready = True
        logger.info(
            "✅ Worker %s registered and marked ready (capabilities=%s, tools=%s, mcp=%s)",
            worker_id,
            len(capabilities),
            tool_count,
            mcp_tool_count,
        )
    else:
        # Keep STARTING so capability-gated routes do not select this worker.
        logger.warning(
            "Worker %s registered but NOT marked ready: empty/fallback capabilities "
            "(error=%s, capabilities=%s, tool_count=%s). Redis will be rewritten "
            "after a successful context rebuild.",
            worker_id,
            worker_context.get("error"),
            len(capabilities),
            tool_count,
        )

    # ADR-0105 §R3: republish local-inference binding after register_worker,
    # which otherwise drops bindings when recreating the Redis entry.
    local_inference_client = worker_context.get("local_inference_client")
    if local_inference_client is not None:
        try:
            readiness_service.update_worker_local_inference_manager_binding(
                worker_id=worker_id,
                local_inference_manager_id=local_inference_client.manager_id,
            )
        except Exception as bind_err:
            logger.debug(
                "local_inference_manager_binding_republish_failed: %s",
                bind_err,
            )

    return {
        "registered": True,
        "marked_ready": marked_ready,
        "capabilities": capabilities,
        "tool_count": tool_count,
        "mcp_tool_count": mcp_tool_count,
        "pool_type": pool_type,
        "max_concurrency": initial_concurrency,
    }


def initialize_parent_coordination(worker_id: str) -> Dict[str, Any]:
    """
    Initialize parent process coordination + worker registration (ADR-0038 Enhanced).
    
    This should be called ONCE when the Celery parent process starts,
    BEFORE the worker pool is forked.
    
    Parent now owns:
    - Worker registration (NEW)
    - Worker readiness marking (NEW)
    - Coordination threads (heartbeat, health, cleanup)
    
    Args:
        worker_id: The worker identifier (e.g., "worker1", "worker2")
        
    Returns:
        Dict containing initialization status
    """
    global _parent_coordination_initialized
    
    # Only initialize once per parent process
    if _parent_coordination_initialized:
        logger.info(f"Parent coordination already initialized for {worker_id}")
        return {"status": "already_initialized", "worker_id": worker_id}
    
    # Verify we're in parent process
    if not is_celery_parent_process():
        logger.warning("initialize_parent_coordination called from child process - ignoring")
        return {"status": "error", "error": "not_parent_process"}
    
    logger.info(f"🎯 Initializing parent process coordination for {worker_id}")
    
    try:
        # Create worker context (ADR-0069: watcher started inside _create_worker_context for all processes)
        logger.info(f"🔧 Creating worker context in parent process...")
        from .tasks import _create_worker_context
        _startup_log("Calling _create_worker_context()")
        worker_context = _create_worker_context()
        _startup_log("_create_worker_context() returned successfully")
        
        capabilities = worker_context.get("capabilities", [])
        tool_count = worker_context.get("tool_count", 0)
        pool_type = worker_context.get("pool_type", "unknown")  # ADR-0033: Get pool type
        _startup_log(f"Extracted context data: tools={tool_count}, capabilities={len(capabilities)}, pool={pool_type}")

        logger.info(
            f"📝 Publishing worker {worker_id} readiness from parent context..."
        )
        publish_result = publish_worker_readiness_from_context(worker_id, worker_context)
        mcp_tool_count = publish_result["mcp_tool_count"]
        marked_ready = publish_result["marked_ready"]

        logger.info(
            f"{'✅' if marked_ready else '⚠️'} Worker {worker_id} registration complete "
            f"(marked_ready={marked_ready})"
        )
        logger.info(f"   - Tools: {tool_count} ({mcp_tool_count} MCP)")
        logger.info(f"   - Capabilities: {len(capabilities)}")
        logger.info(f"   - Max concurrency: {publish_result['max_concurrency']}")
        
        # Start coordination threads
        _start_parent_heartbeat(worker_id)
        _start_parent_health_check(worker_id)
        _start_parent_cleanup(worker_id)
        
        # Start thread health monitor (only once during initialization)
        _start_thread_health_monitor(worker_id)
        
        _parent_coordination_initialized = True
        
        logger.info(f"✅ Parent process coordination initialized for {worker_id}")
        logger.info(f"   - Heartbeat thread: Running (30s interval, sleep/wake resilient)")
        logger.info(f"   - Health check thread: Running (2s interval)")
        logger.info(f"   - Cleanup thread: Running (60s interval)")
        logger.info(f"   - Thread health monitor: Running (60s check interval)")
        
        return {
            "status": "initialized",
            "worker_id": worker_id,
            "parent_pid": os.getpid(),
            "tool_count": tool_count,
            "mcp_tool_count": mcp_tool_count,
            "capabilities": len(capabilities),
            "marked_ready": marked_ready,
            "threads": {
                "heartbeat": "running",
                "health_check": "running",
                "cleanup": "running"
            }
        }
        
    except Exception as e:
        logger.error(f"Failed to initialize parent coordination: {e}")
        import traceback
        traceback.print_exc()
        
        return {
            "status": "error",
            "error": str(e),
            "worker_id": worker_id
        }


def shutdown_parent_coordination(worker_id: str) -> Dict[str, Any]:
    """
    Shutdown parent process coordination threads.
    
    This should be called when the Celery worker is shutting down.
    
    Args:
        worker_id: The worker identifier
        
    Returns:
        Dict containing shutdown status
    """
    global _parent_coordination_initialized
    
    logger.info(f"🛑 Shutting down parent coordination for {worker_id}")
    
    try:
        # Stop all threads
        _stop_thread_health_monitor(worker_id)
        _stop_parent_heartbeat(worker_id)
        _stop_parent_health_check(worker_id)
        _stop_parent_cleanup(worker_id)
        
        _parent_coordination_initialized = False
        
        logger.info(f"✅ Parent coordination shutdown complete for {worker_id}")
        
        return {
            "status": "shutdown",
            "worker_id": worker_id
        }
        
    except Exception as e:
        logger.error(f"Error during parent coordination shutdown: {e}")
        return {
            "status": "error",
            "error": str(e),
            "worker_id": worker_id
        }


def _re_register_worker(worker_id: str) -> bool:
    """
    Re-register a worker with the readiness service.
    
    This is used when:
    - Worker registration key has expired (after sleep)
    - Too many consecutive heartbeat failures
    
    Args:
        worker_id: The worker ID to re-register
        
    Returns:
        True if re-registration succeeded, False otherwise.
        Returns False when only a fallback/empty context is available so callers
        keep retrying instead of treating empty-ready as success (#151).
    """
    try:
        from .tasks import _create_worker_context, _clear_worker_context_cache

        # Get worker context (ADR-0069: same path for all; watcher adds MCP tools)
        worker_context = _create_worker_context()

        # If we only have a stale fallback cache, clear and retry once so recovery
        # can rewrite real capabilities into Redis (#151).
        if not _is_usable_worker_context(worker_context):
            logger.warning(
                "Re-register saw empty/fallback context for %s; clearing cache and retrying",
                worker_id,
            )
            _clear_worker_context_cache(os.getpid())
            worker_context = _create_worker_context()

        publish_result = publish_worker_readiness_from_context(worker_id, worker_context)
        if not publish_result.get("marked_ready"):
            logger.error(
                "❌ Re-register for %s did not mark ready (empty/fallback capabilities)",
                worker_id,
            )
            return False

        logger.info(f"✅ Worker {worker_id} re-registered successfully")
        return True
        
    except Exception as e:
        logger.error(f"❌ Failed to re-register worker {worker_id}: {e}", exc_info=True)
        return False


def _parent_heartbeat_thread_func(worker_id: str, stop_event: threading.Event):
    """
    Background thread that sends periodic heartbeats from parent process.
    
    Runs in parent process, monitors worker health.
    
    Enhanced with:
    - Redis connection health checks
    - Automatic reconnection on failure
    - Worker re-registration if registration is lost
    - Retry logic with exponential backoff
    
    Args:
        worker_id: The worker ID to send heartbeats for
        stop_event: Threading event to signal when to stop
    """
    from ..distributed.worker_readiness import get_readiness_service
    from ..distributed.redis_manager import get_redis_manager
    
    logger.info(f"🫀 Starting parent heartbeat thread for worker {worker_id}")
    
    consecutive_failures = 0
    max_consecutive_failures = 5
    base_retry_delay = 5.0  # Start with 5 seconds
    max_retry_delay = 60.0   # Cap at 60 seconds
    
    while not stop_event.is_set():
        try:
            # Check Redis connection health before heartbeat
            redis_manager = get_redis_manager()
            if not redis_manager.health_check_sync("worker_readiness"):
                logger.warning(f"⚠️ Redis connection unhealthy for {worker_id}, attempting reconnection...")
                consecutive_failures += 1
                # Wait with exponential backoff before retry
                retry_delay = min(base_retry_delay * (2 ** (consecutive_failures - 1)), max_retry_delay)
                logger.info(f"🔄 Waiting {retry_delay:.1f}s before retry (failure {consecutive_failures}/{max_consecutive_failures})")
                stop_event.wait(retry_delay)
                continue
            
            # Reset failure counter on successful connection check
            if consecutive_failures > 0:
                logger.info(f"✅ Redis connection restored for {worker_id}")
                consecutive_failures = 0
            
            # Send worker readiness heartbeat
            readiness_service = get_readiness_service()
            
            # Check if worker is still registered (fast check before heartbeat)
            worker_info = readiness_service.get_worker_info(worker_id)
            if not worker_info:
                # Worker key is missing - likely expired during sleep
                logger.warning(f"⚠️ Worker {worker_id} registration missing (likely expired), re-registering immediately...")
                if _re_register_worker(worker_id):
                    consecutive_failures = 0  # Reset counter after successful re-registration
                else:
                    consecutive_failures += 1
            
            # Get current active commands from child processes
            current_active = readiness_service.get_worker_active_commands(worker_id)
            
            # Attempt heartbeat
            heartbeat_success = readiness_service.worker_heartbeat(
                worker_id=worker_id,
                active_commands=current_active
            )
            
            if heartbeat_success:
                logger.debug(f"💓 Heartbeat sent for worker {worker_id} (active: {current_active})")
                consecutive_failures = 0  # Reset on success
            else:
                logger.warning(f"⚠️ Heartbeat returned False for {worker_id}")
                consecutive_failures += 1
            
        except Exception as e:
            consecutive_failures += 1
            logger.error(f"❌ Heartbeat failed for worker {worker_id}: {e}", exc_info=True)
            
            # If we've had too many consecutive failures, try to re-register worker
            if consecutive_failures >= max_consecutive_failures:
                logger.warning(f"🔄 Too many heartbeat failures ({consecutive_failures}), attempting worker re-registration...")
                if _re_register_worker(worker_id):
                    consecutive_failures = 0  # Reset counter after successful re-registration
        
        # Calculate wait time - use shorter interval if we've had failures
        if consecutive_failures > 0:
            wait_time = min(base_retry_delay * (2 ** min(consecutive_failures - 1, 3)), 30.0)
        else:
            wait_time = 30.0
        
        # Wait for next heartbeat or until stop event
        stop_event.wait(wait_time)
    
    logger.info(f"🫀 Parent heartbeat thread stopped for worker {worker_id}")


def _parent_health_check_thread_func(worker_id: str, stop_event: threading.Event):
    """
    Background thread that performs comprehensive health checks from parent.
    
    Parent process can directly monitor all child processes using psutil.
    
    Args:
        worker_id: The worker ID to perform health checks for
        stop_event: Threading event to signal when to stop
    """
    logger.info(f"🏥 Starting parent health check thread for worker {worker_id}")
    
    # Get parent PID for monitoring children
    parent_pid = os.getpid()
    
    while not stop_event.is_set():
        try:
            start_time = time.time()
            
            # Get all child worker processes
            parent_process = psutil.Process(parent_pid)
            child_processes = parent_process.children(recursive=False)
            
            # ADR-0033: Detect pool type and get Celery concurrency for accurate monitoring
            from .worker_utils import detect_worker_pool_type
            pool_type = detect_worker_pool_type()
            celery_concurrency = get_celery_concurrency_from_args()
            
            # Perform health checks on each child (or parent for single-process pools)
            from .process_health_monitor import create_process_health_monitor
            health_monitor = create_process_health_monitor(
                worker_id=worker_id,
                pool_type=pool_type,
                celery_concurrency=celery_concurrency
            )
            
            # Comprehensive health check of all children
            process_metrics = health_monitor.perform_comprehensive_health_check()
            
            # Calculate worker utilization
            utilization_summary = health_monitor.calculate_worker_utilization(process_metrics)
            
            # Store utilization data in Redis
            health_monitor.store_utilization_data_sync(utilization_summary, process_metrics)
            
            # Update worker capacity and tool information via WorkerReadinessService.
            try:
                from ..distributed.worker_readiness import get_readiness_service
                from .tasks import _create_worker_context
                readiness_service = get_readiness_service()
                readiness_service.update_worker_capacity(
                    worker_id=worker_id,
                    max_concurrency=utilization_summary.total_processes
                )

                worker_context = _create_worker_context()
                stack = worker_context.get("stack")
                
                if stack and hasattr(stack, 'tool_registry') and stack.tool_registry:
                    tools = stack.tool_registry.list_items()
                    tool_count = len(tools)
                    mcp_tool_count = len([t for t in tools if str(t).startswith('mcp.')])
                    
                    serialized_tools = []
                    for tool_name, tool_info in tools.items():
                        tool_data = {
                            'name': tool_name,
                            'description': getattr(tool_info, 'description', 'No description'),
                            'category': getattr(tool_info, 'category', 'unknown'),
                            'keywords': getattr(tool_info, 'keywords', []),
                            'data_types': getattr(tool_info, 'data_types', []),
                            'priority': getattr(tool_info, 'priority', 0),
                            'cost_class': getattr(tool_info, 'cost_class', None),
                            'is_mcp': tool_name.startswith('mcp.')
                        }
                        serialized_tools.append(tool_data)
                    
                    readiness_service.update_worker_tools(
                        worker_id=worker_id,
                        tools=serialized_tools,
                        tool_count=tool_count,
                        mcp_tool_count=mcp_tool_count
                    )

                    logger.debug(f"🔧 Updated {tool_count} tools for worker {worker_id}")
                    
            except Exception as e:
                logger.warning(f"⚠️ Failed to update worker readiness: {e}")
            
            check_duration_ms = int((time.time() - start_time) * 1000)
            
            logger.debug(f"🏥 Health check completed for worker {worker_id} in {check_duration_ms}ms")
            logger.debug(f"   - Processes: {utilization_summary.total_processes}")
            logger.debug(f"   - Healthy: {utilization_summary.healthy_processes}")
            logger.debug(f"   - Health score: {utilization_summary.overall_health_score:.2f}")
            
        except Exception as e:
            logger.error(f"❌ Health check failed for worker {worker_id}: {e}")
        
        # Wait 2 seconds or until stop event
        stop_event.wait(2.0)
    
    logger.info(f"🏥 Parent health check thread stopped for worker {worker_id}")


def _parent_cleanup_thread_func(worker_id: str, stop_event: threading.Event):
    """
    Background thread that performs cleanup from parent process.
    
    Args:
        worker_id: The worker ID to perform cleanup for
        stop_event: Threading event to signal when to stop
    """
    logger.info(f"🧹 Starting parent cleanup thread for worker {worker_id}")
    
    from ..distributed.worker_readiness import get_readiness_service
    
    while not stop_event.is_set():
        try:
            start_time = time.time()
            
            # Get readiness service
            readiness_service = get_readiness_service()
            
            # Perform cleanup
            readiness_service._cleanup_stale_workers()
            
            # Get stats
            all_workers = readiness_service.get_all_workers()
            
            from ..distributed.redis_manager import get_sync_redis_client
            sync_redis = get_sync_redis_client("worker_readiness_cleanup")
            ready_set_size = sync_redis.scard(readiness_service.READY_WORKERS_SET)
            
            cleanup_duration_ms = int((time.time() - start_time) * 1000)
            
            logger.debug(f"🧹 Cleanup completed for worker {worker_id} in {cleanup_duration_ms}ms")
            logger.debug(f"   - Total workers: {len(all_workers)}")
            logger.debug(f"   - Ready set size: {ready_set_size}")
            
        except Exception as e:
            logger.error(f"❌ Cleanup failed for worker {worker_id}: {e}")
        
        # Wait 60 seconds or until stop event
        stop_event.wait(60.0)
    
    logger.info(f"🧹 Parent cleanup thread stopped for worker {worker_id}")


def _start_parent_heartbeat(worker_id: str):
    """Start parent heartbeat thread with health monitoring."""
    global _parent_heartbeat_thread, _parent_heartbeat_stop_event
    
    if _parent_heartbeat_thread and _parent_heartbeat_thread.is_alive():
        logger.warning("Parent heartbeat thread already running")
        return
    
    _parent_heartbeat_stop_event = threading.Event()
    _parent_heartbeat_thread = threading.Thread(
        target=_parent_heartbeat_thread_func,
        args=(worker_id, _parent_heartbeat_stop_event),
        daemon=True,
        name=f"parent-heartbeat-{worker_id}"
    )
    _parent_heartbeat_thread.start()
    logger.info(f"🫀 Started parent heartbeat thread for {worker_id} (with sleep/wake resilience)")


def _stop_parent_heartbeat(worker_id: str):
    """Stop parent heartbeat thread."""
    global _parent_heartbeat_thread, _parent_heartbeat_stop_event
    
    if _parent_heartbeat_stop_event:
        _parent_heartbeat_stop_event.set()
    
    if _parent_heartbeat_thread and _parent_heartbeat_thread.is_alive():
        _parent_heartbeat_thread.join(timeout=5.0)
    
    logger.info(f"🫀 Stopped parent heartbeat thread for {worker_id}")


def _start_parent_health_check(worker_id: str):
    """Start parent health check thread."""
    global _parent_health_thread, _parent_health_stop_event
    
    if _parent_health_thread and _parent_health_thread.is_alive():
        logger.warning("Parent health check thread already running")
        return
    
    _parent_health_stop_event = threading.Event()
    _parent_health_thread = threading.Thread(
        target=_parent_health_check_thread_func,
        args=(worker_id, _parent_health_stop_event),
        daemon=True,
        name=f"parent-health-{worker_id}"
    )
    _parent_health_thread.start()
    logger.info(f"🏥 Started parent health check thread for {worker_id}")


def _stop_parent_health_check(worker_id: str):
    """Stop parent health check thread."""
    global _parent_health_thread, _parent_health_stop_event
    
    if _parent_health_stop_event:
        _parent_health_stop_event.set()
    
    if _parent_health_thread and _parent_health_thread.is_alive():
        _parent_health_thread.join(timeout=5.0)
    
    logger.info(f"🏥 Stopped parent health check thread for {worker_id}")


def _start_parent_cleanup(worker_id: str):
    """Start parent cleanup thread."""
    global _parent_cleanup_thread, _parent_cleanup_stop_event
    
    if _parent_cleanup_thread and _parent_cleanup_thread.is_alive():
        logger.warning("Parent cleanup thread already running")
        return
    
    _parent_cleanup_stop_event = threading.Event()
    _parent_cleanup_thread = threading.Thread(
        target=_parent_cleanup_thread_func,
        args=(worker_id, _parent_cleanup_stop_event),
        daemon=True,
        name=f"parent-cleanup-{worker_id}"
    )
    _parent_cleanup_thread.start()
    logger.info(f"🧹 Started parent cleanup thread for {worker_id}")


def _stop_parent_cleanup(worker_id: str):
    """Stop parent cleanup thread."""
    global _parent_cleanup_thread, _parent_cleanup_stop_event
    
    if _parent_cleanup_stop_event:
        _parent_cleanup_stop_event.set()
    
    if _parent_cleanup_thread and _parent_cleanup_thread.is_alive():
        _parent_cleanup_thread.join(timeout=5.0)
    
    logger.info(f"🧹 Stopped parent cleanup thread for {worker_id}")


# Thread health monitoring
_thread_health_monitor_thread: Optional[threading.Thread] = None
_thread_health_monitor_stop_event: Optional[threading.Event] = None


def _thread_health_monitor_thread_func(worker_id: str, stop_event: threading.Event):
    """
    Background thread that monitors the health of other coordination threads.
    
    Detects if threads have died and restarts them if needed.
    This provides resilience against sleep/wake cycles and thread failures.
    
    Args:
        worker_id: The worker ID to monitor threads for
        stop_event: Threading event to signal when to stop
    """
    logger.info(f"🔍 Starting thread health monitor for worker {worker_id}")
    
    while not stop_event.is_set():
        try:
            global _parent_heartbeat_thread, _parent_health_thread, _parent_cleanup_thread
            
            # Check heartbeat thread
            if _parent_heartbeat_thread and not _parent_heartbeat_thread.is_alive():
                logger.warning(f"⚠️ Heartbeat thread died for {worker_id}, restarting...")
                _start_parent_heartbeat(worker_id)
            
            # Check health check thread
            if _parent_health_thread and not _parent_health_thread.is_alive():
                logger.warning(f"⚠️ Health check thread died for {worker_id}, restarting...")
                _start_parent_health_check(worker_id)
            
            # Check cleanup thread
            if _parent_cleanup_thread and not _parent_cleanup_thread.is_alive():
                logger.warning(f"⚠️ Cleanup thread died for {worker_id}, restarting...")
                _start_parent_cleanup(worker_id)
            
        except Exception as e:
            logger.error(f"❌ Thread health monitor error for {worker_id}: {e}")
        
        # Check every 60 seconds
        stop_event.wait(60.0)
    
    logger.info(f"🔍 Thread health monitor stopped for worker {worker_id}")


def _start_thread_health_monitor(worker_id: str):
    """Start thread health monitoring thread."""
    global _thread_health_monitor_thread, _thread_health_monitor_stop_event
    
    if _thread_health_monitor_thread and _thread_health_monitor_thread.is_alive():
        logger.warning("Thread health monitor already running")
        return
    
    _thread_health_monitor_stop_event = threading.Event()
    _thread_health_monitor_thread = threading.Thread(
        target=_thread_health_monitor_thread_func,
        args=(worker_id, _thread_health_monitor_stop_event),
        daemon=True,
        name=f"thread-health-monitor-{worker_id}"
    )
    _thread_health_monitor_thread.start()
    logger.info(f"🔍 Started thread health monitor for {worker_id}")


def _stop_thread_health_monitor(worker_id: str):
    """Stop thread health monitoring thread."""
    global _thread_health_monitor_thread, _thread_health_monitor_stop_event
    
    if _thread_health_monitor_stop_event:
        _thread_health_monitor_stop_event.set()
    
    if _thread_health_monitor_thread and _thread_health_monitor_thread.is_alive():
        _thread_health_monitor_thread.join(timeout=5.0)
    
    logger.info(f"🔍 Stopped thread health monitor for {worker_id}")


# Export main functions
__all__ = [
    'is_celery_parent_process',
    'is_celery_child_process',
    'initialize_parent_coordination',
    'shutdown_parent_coordination'
]

