"""
Motet - Async Helpers

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Async Helpers for the Motet distributed framework.

Dependencies:
    - typing: Type hints and annotations
    - Base interfaces and implementations

Usage:
    from motet.core.utils.async_helpers import AsyncHelpers

Notes:
    - Provides core functionality
    - Integrates with distributed architecture
"""


import asyncio
import concurrent.futures
import sys
from typing import Any, Callable, Coroutine, cast


def _is_gevent_or_eventlet() -> bool:
    """
    Detect if we're running in a gevent or eventlet worker (ADR-0033).
    
    Returns:
        True if gevent or eventlet is active, False otherwise
    """
    # Check for gevent
    if 'gevent' in sys.modules:
        try:
            import gevent
            monkey = getattr(gevent, "monkey", None)
            is_patched = getattr(monkey, "is_module_patched", None) if monkey is not None else None
            if callable(is_patched) and is_patched("socket"):
                return True
        except ImportError:
            pass
    
    # Check for eventlet
    if 'eventlet' in sys.modules:
        try:
            import eventlet
            # Check if eventlet monkey patching is active
            if hasattr(eventlet, 'patcher') and eventlet.patcher.is_monkey_patched('socket'):
                return True
        except ImportError:
            pass
    
    return False


def run_async_safe(coro: Coroutine[Any, Any, Any]) -> Any:
    """
    Safely run an async coroutine in both sync and async contexts.
    
    ADR-0033: Pool-aware async execution:
    - Fork/threads pools: Run via asyncio directly (new loop if needed)
    - Gevent pools: Use asyncio-gevent bridge (async_to_sync) on existing loop
    - Eventlet pools: Use thread isolation to avoid loop conflicts
    
    This function handles the async execution appropriately based on the worker pool type:
    
    - Gevent: Bridges using asyncio_gevent.async_to_sync on current loop
    - Eventlet: Runs in a separate OS thread (avoids loop conflicts)
    - Fork/threads: Uses asyncio loop directly or a clean thread loop if one is running
    
    This is essential for distributed commands that need to work in Celery workers
    (which are sync) but also need to call async functions.
    
    Args:
        coro: The coroutine to execute
        
    Returns:
        The result of the coroutine execution
        
    Example:
        >>> async def my_async_function():
        ...     return "Hello from async!"
        >>> 
        >>> # Works in fork/threads pools
        >>> result = run_async_safe(my_async_function())
        >>> print(result)  # "Hello from async!"
        >>> 
        >>> # Also works in gevent/eventlet pools
        >>> result = run_async_safe(my_async_function())
        >>> print(result)  # "Hello from async!"
    """
    # Gevent: prefer asyncio-gevent bridge (async_to_sync) to run on existing loop
    if 'gevent' in sys.modules:
        try:
            import asyncio_gevent  # type: ignore

            async def _runner() -> Any:
                return await coro

            run_sync = asyncio_gevent.async_to_sync(coroutine=_runner)
            return cast(Callable[[], Any], run_sync)()
        except Exception as e:
            import structlog
            logger = structlog.get_logger(__name__)
            logger.error(
                "Failed to use asyncio_gevent.async_to_sync",
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True
            )
            raise
    
    # Eventlet: use thread isolation to avoid loop conflicts
    if 'eventlet' in sys.modules:
        def run_in_thread_eventlet():
            new_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(new_loop)
            try:
                return new_loop.run_until_complete(coro)
            finally:
                new_loop.close()
                asyncio.set_event_loop(None)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(run_in_thread_eventlet).result()
    
    # Fork/threads pools - use standard asyncio handling
    try:
        # Try to get the current event loop
        loop = asyncio.get_running_loop()
        # If we're here, we're in an event loop; create a new event loop in a separate thread
        def run_in_thread():
            new_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(new_loop)
            try:
                return new_loop.run_until_complete(coro)
            finally:
                new_loop.close()
                asyncio.set_event_loop(None)  # Clean up thread-local event loop
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(run_in_thread)
            return future.result()
            
    except RuntimeError:
        # No event loop is running, so we can create a new one
        # Use the low-level asyncio.run_until_complete to avoid recursion
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()
            asyncio.set_event_loop(None)  # Clean up


# Backward compatibility alias
_run_async_safe = run_async_safe
