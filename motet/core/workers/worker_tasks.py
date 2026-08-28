"""
Motet - Worker Tasks

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Worker worker tasks for the Motet distributed framework.

Dependencies:
    - typing: Type hints and annotations
    - Base interfaces and implementations

Usage:
    from motet.core.workers.worker_tasks import WorkerTasks

Notes:
    - Provides core functionality
    - Integrates with distributed architecture
"""


import time
from typing import Dict, Any

from .celery_app import celery_app


# NOTE: All background thread functions moved to parent_coordinator.py (ADR-0038)
# Parent process now owns all coordination threads for stability and simplicity.
# Child processes only execute tasks and handle shutdown.


def _perform_worker_shutdown(worker_id: str, task_id: str, graceful: bool = True, timeout_seconds: int = 30) -> Dict[str, Any]:
    """
    Helper function that performs worker shutdown logic.
    
    NOTE (ADR-0038): Background threads are now owned by the parent process.
    Child processes only need to update their state and exit gracefully.
    
    This can be called from:
    1. The Celery task (worker_shutdown)
    2. Signal handlers (SIGTERM, SIGINT)
    3. Emergency shutdown procedures
    
    Args:
        worker_id: The worker ID to shutdown
        task_id: The task/operation ID for tracking
        graceful: Whether to shutdown gracefully
        timeout_seconds: Timeout for graceful shutdown
        
    Returns:
        Dict containing shutdown status and details
    """
    start_time = time.time()
    
    try:
        print(f"🔄 Initiating {'graceful' if graceful else 'immediate'} shutdown for worker {worker_id}")
        
        # Update worker state to indicate shutdown in progress
        try:
            from ..distributed.worker_readiness import get_readiness_service, WorkerState
            
            # Direct sync call - readiness service is now fully synchronous
            readiness_service = get_readiness_service()
            readiness_service.update_worker_state(worker_id, WorkerState.UNHEALTHY)
                
            print(f"✅ Worker {worker_id} marked as UNHEALTHY for shutdown")
            
        except Exception as e:
            print(f"⚠️ Failed to update worker state during shutdown: {e}")
        
        # Perform shutdown
        if graceful:
            # Graceful shutdown - allow current tasks to complete
            print(f"⏳ Graceful shutdown initiated, waiting up to {timeout_seconds}s for tasks to complete")
            
            # In a real implementation, we would:
            # 1. Stop accepting new tasks
            # 2. Wait for current tasks to complete
            # 3. Clean up resources
            # 4. Exit worker process
            
            # For now, just simulate the process
            time.sleep(min(5, timeout_seconds))  # Simulate cleanup time
            
            print(f"✅ Graceful shutdown completed for worker {worker_id}")
            
        else:
            # Immediate shutdown
            print("⚡ Immediate shutdown initiated")
        
        # NOTE (ADR-0038): Parent process owns background threads, no need to stop them here
        # The parent process will handle cleanup when the worker exits
        
        shutdown_time = int((time.time() - start_time) * 1000)
        
        return {
            "status": "completed",
            "worker_id": worker_id,
            "graceful": graceful,
            "shutdown_time_ms": shutdown_time,
            "task_id": task_id
        }
        
    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "worker_id": worker_id,
            "graceful": graceful,
            "shutdown_time_ms": int((time.time() - start_time) * 1000),
            "task_id": task_id
        }


@celery_app.task(name="imf.worker.shutdown", bind=True)
def worker_shutdown(self, graceful: bool = True, timeout_seconds: int = 30) -> Dict[str, Any]:
    """
    Initiate worker shutdown procedure.
    
    This task delegates to _perform_worker_shutdown helper function
    which can also be called directly from signal handlers or emergency procedures.
    
    Args:
        graceful: Whether to shutdown gracefully
        timeout_seconds: Timeout for graceful shutdown
        
    Returns:
        Dict containing shutdown status and details
    """
    # Get the actual worker ID from the worker context to match what was registered
    try:
        from .tasks import _create_worker_context
        worker_context = _create_worker_context()
        worker_id = worker_context.get("worker_id", self.request.hostname)
    except Exception as e:
        print(f"⚠️ Could not get worker context for shutdown, using Celery hostname: {e}")
        worker_id = self.request.hostname
    
    # Delegate to the helper function
    return _perform_worker_shutdown(
        worker_id=worker_id,
        task_id=self.request.id,
        graceful=graceful,
        timeout_seconds=timeout_seconds
    )
