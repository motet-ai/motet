"""
Motet - Worker Context Initialization

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Worker context initialization with pool-aware strategy.
    Unified initialization module that works across all Celery pool types.
    After a successful rebuild, rewrites Redis readiness capabilities so
    recovery from empty/fallback registration is visible to routing (#151).

Dependencies:
    - structlog: Structured logging
    - typing: Type hints and annotations
    - Celery worker pool management
    - parent_coordinator.publish_worker_readiness_from_context for Redis rewrite

Usage:
    from motet.core.workers.worker_initialization import initialize_worker_context
    
    # Initialize worker context
    context = initialize_worker_context("fork")

Notes:
    - Supports all Celery pool types (fork, threads, eventlet, gevent)
    - Solves worker_process_init signal issues on non-fork pools
    - Provides unified worker context initialization
    - Clears stale fallback context cache before official init
    - Rewrites Redis capabilities after successful context creation (#151)
    - Integrates with distributed architecture

    See:
"""

from typing import Dict, Any, Literal
import structlog
import asyncio
import asyncio_gevent

logger = structlog.get_logger(__name__)


def initialize_worker_context(pool_type: Literal["fork", "threads", "eventlet", "gevent"]) -> Dict[str, Any]:
    """
    Initialize worker context for all pool types (ADR-0033).
    
    This function works for:
    - Fork pool: Called from worker_process_init in each child
    - Threads/Eventlet/Gevent: Called from worker_ready in parent
    
    Components initialized:
    1. EventObserverManager consumer (pool-aware spawning)
    2. Worker context (tools, distributed invoker, capabilities)
    3. MCP tool registry
    4. Connection pools
    
    Args:
        pool_type: One of 'fork', 'threads', 'eventlet', 'gevent'
    
    Returns:
        Dict with initialization status and worker context:
        {
            'status': 'success' or 'error',
            'worker_id': str,
            'pool_type': str,
            'context': dict (worker context) or None,
            'error': str (if status is 'error')
        }
    
    Example:
        >>> result = initialize_worker_context(pool_type="eventlet")
        >>> if result['status'] == 'success':
        ...     print(f"Worker {result['worker_id']} initialized")
    """
    import os
    from .worker_utils import get_worker_id
    
    pid = os.getpid()
    worker_id = get_worker_id()
    
    logger.info(
        "Worker context initialization started",
        pool_type=pool_type,
        pid=pid,
        worker_id=worker_id
    )
    
    # Fail fast for unsupported pool types
    if pool_type == "eventlet":
        logger.error(
            "Eventlet pool is not supported at this time",
            pool_type=pool_type,
            worker_id=worker_id,
            pid=pid
        )
        raise RuntimeError("Eventlet pool is not supported at this time")

    # Set up asyncio-gevent bridge if using gevent pool
    # This must happen ONCE per worker process, AFTER gevent monkey patching
    if pool_type == "gevent":
        try:
            # Set process-wide event loop policy to use gevent
            # This allows asyncio coroutines to run on gevent's event loop
            asyncio.set_event_loop_policy(asyncio_gevent.EventLoopPolicy())
            
            logger.info(
                "Asyncio-gevent event loop policy configured",
                worker_id=worker_id,
                pid=pid
            )
        except ImportError as e:
            logger.warning(
                "asyncio-gevent not available, asyncio operations may fail",
                worker_id=worker_id,
                pid=pid,
                error=str(e)
            )
        except Exception as e:
            logger.error(
                "Failed to set asyncio-gevent event loop policy",
                worker_id=worker_id,
                pid=pid,
                error=str(e),
                exc_info=True
            )
    
    try:
        # Step 1: Start EventObserverManager consumer with pool-appropriate method
        _start_event_observer_consumer(pool_type, pid)
        
        # Step 2: Create worker context (tools, invoker, capabilities, etc.)
        # Clear any stale cached context first — background threads (e.g. MCP manager startup)
        # may have called _create_worker_context() early and cached a fallback with 0 tools.
        # The official initialize_worker_context() call must start fresh.
        from .tasks import _create_worker_context, _clear_worker_context_cache
        _clear_worker_context_cache(pid)
        worker_context = _create_worker_context()
        
        # Step 3: Load bundles installed to the plugin root at startup (ADR-0071)
        try:
            from motet.core.bundles.bundle_reload import load_bundles_on_startup
            loaded_count = load_bundles_on_startup()
            logger.info("Bundle startup catch-up complete", count=loaded_count, worker_id=worker_id)
        except Exception as e:
            # Non-fatal: worker can still function without pre-installed bundles
            logger.warning(
                "Failed to load bundles on startup",
                error=str(e),
                worker_id=worker_id,
                exc_info=True,
            )

        # Step 3b: Hydrate durable user.* workflows from Redis + ensure sync command is registered
        try:
            from motet.core.commands.builtin import sync_user_workflow as _sync_user_workflow  # noqa: F401
            from motet.core.workflow.user_catalog import load_user_workflows_into_registry

            user_wf_count = load_user_workflows_into_registry()
            logger.info(
                "User workflow startup hydrate complete",
                count=user_wf_count,
                worker_id=worker_id,
            )
        except Exception as e:
            logger.warning(
                "Failed to load user workflows on startup",
                error=str(e),
                worker_id=worker_id,
                exc_info=True,
            )
        
        # Issue #151: parent coordination may have registered empty/fallback
        # capabilities after a transient FD/Redis timeout. Rewrite Redis from
        # this successful rebuild so CapabilityFilter sees real capabilities
        # (including tool_execution) without requiring a worker restart.
        readiness_publish: Dict[str, Any] = {
            "registered": False,
            "marked_ready": False,
        }
        try:
            from .parent_coordinator import publish_worker_readiness_from_context

            readiness_publish = publish_worker_readiness_from_context(
                worker_id, worker_context
            )
            logger.info(
                "Worker readiness rewritten after context init",
                worker_id=worker_id,
                marked_ready=readiness_publish.get("marked_ready"),
                capabilities_count=len(readiness_publish.get("capabilities") or []),
                tool_count=readiness_publish.get("tool_count", 0),
            )
        except Exception as readiness_err:
            # Context init succeeded; Redis rewrite is best-effort so the
            # worker can still serve once heartbeats/re-register recover.
            logger.warning(
                "Failed to rewrite Redis readiness after context init",
                worker_id=worker_id,
                error=str(readiness_err),
                error_type=type(readiness_err).__name__,
                exc_info=True,
            )

        logger.info(
            "Worker context initialized successfully",
            pool_type=pool_type,
            worker_id=worker_id,
            tool_count=worker_context.get('tool_count', 0),
            capabilities=len(worker_context.get('capabilities', [])),
            readiness_marked_ready=readiness_publish.get("marked_ready"),
            pid=pid
        )
        
        return {
            'status': 'success',
            'worker_id': worker_id,
            'pool_type': pool_type,
            'context': worker_context,
            'readiness_marked_ready': readiness_publish.get("marked_ready"),
        }
        
    except Exception as e:
        logger.error(
            "Worker context initialization failed",
            pool_type=pool_type,
            worker_id=worker_id,
            pid=pid,
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True
        )
        
        return {
            'status': 'error',
            'worker_id': worker_id,
            'pool_type': pool_type,
            'context': None,
            'error': str(e)
        }


def _start_event_observer_consumer(pool_type: str, pid: int) -> None:
    """
    Start EventObserverManager consumer using pool-appropriate method (ADR-0033).
    
    Different pool types require different spawning strategies:
    - Eventlet: Use eventlet.spawn() for green thread (cooperative)
    - Gevent: Use gevent.spawn() for greenlet (cooperative)
    - Fork/Threads: Use threading.Thread() for native thread
    
    Args:
        pool_type: Worker pool type ('fork', 'threads', 'eventlet', 'gevent')
        pid: Process ID for logging
    
    Raises:
        ImportError: If eventlet/gevent not available when required
        Exception: If consumer start fails
    """
    from .event_observer_manager import start_event_observers
    import asyncio
    
    logger.info(
        "Starting EventObserver consumer",
        pool_type=pool_type,
        pid=pid
    )
    
    if pool_type == "eventlet":
        # Eventlet: Use eventlet.spawn for green thread
        try:
            import eventlet
        except ImportError as e:
            raise ImportError(
                "Eventlet pool requires 'eventlet' package. "
                "Install with: pip install celery[eventlet]"
            ) from e
        
        def run_consumer_eventlet():
            """Run consumer in eventlet-compatible way"""
            try:
                logger.debug("EventObserver consumer starting (eventlet)", pid=pid)
                # Use eventlet-patched asyncio
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(start_event_observers())
                # Keep loop running for continuous event consumption
                loop.run_forever()
            except Exception as e:
                logger.error(
                    "EventObserver consumer error",
                    pool_type="eventlet",
                    error=str(e),
                    exc_info=True
                )
        
        eventlet.spawn(run_consumer_eventlet)
        logger.info("EventObserver consumer started (eventlet greenlet)", pid=pid)
        
    elif pool_type == "gevent":
        # Gevent: Use gevent.spawn for greenlet
        try:
            import gevent
        except ImportError as e:
            raise ImportError(
                "Gevent pool requires 'gevent' package. "
                "Install with: pip install celery[gevent]"
            ) from e
        
        def run_consumer_gevent():
            """Run consumer in gevent-compatible way"""
            try:
                logger.debug("EventObserver consumer starting (gevent)", pid=pid)
                loop = asyncio_gevent.EventLoop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(start_event_observers())
                # Keep loop running for continuous event consumption
                loop.run_forever()
            except Exception as e:
                logger.error(
                    "EventObserver consumer error",
                    pool_type="gevent",
                    error=str(e),
                    exc_info=True
                )
        
        gevent.spawn(run_consumer_gevent)
        logger.info("EventObserver consumer started (gevent greenlet)", pid=pid)
        
    else:
        # Fork/Threads: Use native thread
        import threading
        
        def run_consumer_thread():
            """Run consumer in native thread"""
            try:
                logger.debug("EventObserver consumer starting (thread)", pid=pid)
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(start_event_observers())
                # Keep loop running for continuous event consumption
                loop.run_forever()
            except Exception as e:
                logger.error(
                    "EventObserver consumer error",
                    pool_type=pool_type,
                    error=str(e),
                    exc_info=True
                )
        
        consumer_thread = threading.Thread(
            target=run_consumer_thread,
            daemon=True,
            name=f"EventConsumer-{pid}"
        )
        consumer_thread.start()
        logger.info("EventObserver consumer started (native thread)", pid=pid, pool_type=pool_type)


__all__ = [
    'initialize_worker_context',
]

