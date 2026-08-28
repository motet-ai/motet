"""
Motet - Concurrency Primitives

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Unified concurrency primitives for multi-pool Celery workers.
    Provides a single, clean abstraction layer that automatically adapts to the current
    worker pool type (fork, threads, gevent). Developers write code once using
    these primitives, and it works correctly on supported pool types.

    Eventlet is not supported.

Dependencies:
    - threading: Standard threading primitives (fork, threads pools)
    - gevent: Green thread support (gevent pool)
    - asyncio-gevent: Async/await bridge for gevent workers

Usage:
    from motet.core.workers.concurrency_primitives import WorkerLock, worker_sleep
    
    # Synchronous code - works on fork, threads, and gevent pools
    lock = WorkerLock()
    with lock:
        # Critical section
        worker_sleep(0.1)  # Cooperative yielding
    
    # Async code - use run_async_safe from async_helpers
    from motet.core.utils.async_helpers import run_async_safe
    
    async def async_operation():
        # Your async code here
        pass
    
    result = run_async_safe(async_operation())

Notes:
    - Pool-agnostic development: Write once, run anywhere (fork/threads/gevent)
    - Automatic adaptation to current worker pool type
    - External library compatibility with subprocess-based tools
    - Drop-in replacement for standard threading API
    - For async operations: Use run_async_safe from async_helpers module
    - Eventlet pool is NOT supported and will raise RuntimeError

    Key Design Principles:
    1. **Pool-Agnostic Development**: Write once, run anywhere (fork/threads/gevent)
    2. **Automatic Adaptation**: Primitives detect pool type at runtime
    3. **External Library Compatibility**: Subprocess-based tools work correctly
    4. **Zero Knowledge Required**: Developers don't need to understand pool internals
    5. **Drop-In Replacement**: Compatible with threading API
    6. **Async Integration**: Use run_async_safe for async operations (gevent-compatible)

    Primitives Provided:
    - WorkerLock: Mutual exclusion (threading.Lock or gevent.lock.Semaphore)
    - WorkerRLock: Reentrant locks (threading.RLock or gevent.lock.RLock)
    - WorkerEvent: Cross-thread/greenlet signaling
    - WorkerSemaphore: Resource pooling with bounded concurrency
    - WorkerThread: Spawn threads or greenlets
    - WorkerExecutor: Pool-aware concurrent execution
    - WorkerLocal: Thread-local/greenlet-local storage
    - worker_sleep(): Cooperative yielding with pool awareness

    For Async Operations:
    - Use run_async_safe() from motet.core.utils.async_helpers
    - Automatically handles asyncio-gevent bridge on gevent pools
    - Works seamlessly with Playwright, httpx, and other async libraries

    Usage Examples:
    ```python
    # Synchronous operations (fork/threads/gevent)
    from motet.core.workers.concurrency_primitives import WorkerLock, WorkerLocal, worker_sleep

    lock = WorkerLock()
    with lock:
        # Critical section
        data = shared_resource.read()
        worker_sleep(0.1)  # Yields cooperatively on gevent
        shared_resource.write(data)

    # Worker-local storage (per-thread/greenlet)
    worker_local = WorkerLocal()
    worker_local.request_id = "req-123"
    worker_local.db_connection = create_db_connection()
    try:
        process_request()
    finally:
        worker_local.clear()  # Clean up

    # Async operations (all pools, gevent-compatible)
    from motet.core.utils.async_helpers import run_async_safe

    async def fetch_data():
        async with httpx.AsyncClient() as client:
            response = await client.get("https://example.com")
            return response.json()

    result = run_async_safe(fetch_data())  # Works on all pool types!
    ```

    Architecture Decisions:
    - High-Concurrency Worker Support (Phase 0: Primitives)
    - Design: Hybrid approach with pool detection and conditional primitives
    - Green threads: use gevent (eventlet is not supported)

    References:
    - threading module (stdlib) - fork/threads pools
    - gevent.lock, gevent.event - gevent pool
    - asyncio-gevent - async/await bridge for gevent workers
"""

import importlib
import sys
import time
from abc import ABC, abstractmethod
from contextlib import contextmanager
from typing import Any, Callable, Dict, Optional, Protocol, Union, cast

import structlog

logger = structlog.get_logger(__name__)


# ============================================================================
# Pool Type Detection
# ============================================================================

def detect_worker_pool_type() -> str:
    """
    Detect the current worker pool type based on loaded modules.
    
    This function checks sys.modules to determine which concurrency model
    is active. It's called lazily by primitives when they're first created.
    
    Detection Logic:
    1. Check for eventlet.patcher - DEPRECATED (raises RuntimeError)
    2. Check for gevent.monkey - most reliable gevent detection
    3. Check for os.fork capability - fork pool
    4. Fallback to threads pool
    
    Returns:
        str: "gevent", "fork", or "threads"
        
    Raises:
        RuntimeError: If eventlet pool is detected (not supported)
        
    Note:
        This is called automatically by primitives. Most code should use
        get_current_pool_type() instead for caching.
    """
    # Check for eventlet - FAIL FAST (not supported)
    # Use importlib to avoid static import of optional eventlet (not in deps; type checker would fail).
    if 'eventlet.patcher' in sys.modules:
        try:
            eventlet_patcher = importlib.import_module('eventlet.patcher')
            if eventlet_patcher.is_monkey_patched('socket'):
                logger.error("Eventlet pool detected but not supported")
                raise RuntimeError(
                    "Eventlet pool is not supported at this time. "
                    "Please use gevent pool for green threads, or fork/threads pools. "
                    "See worker_initialization.py for details."
                )
        except ImportError:
            pass
    
    # Check for gevent (most reliable: check if monkey patched)
    if 'gevent.monkey' in sys.modules:
        try:
            import gevent.monkey
            if gevent.monkey.is_module_patched('socket'):
                return "gevent"
        except Exception:
            pass  # best-effort pool detection; gevent check optional

    # Check for fork capability (Unix-like systems)
    try:
        import os
        if hasattr(os, 'fork'):
            # On Unix, if not patched, we're likely using fork pool
            # This is Celery's default on Unix systems
            return "fork"
    except Exception:
        pass  # best-effort pool detection; fork check optional

    # Fallback to threads
    return "threads"


# Cache the detected pool type to avoid repeated detection
_cached_pool_type: Optional[str] = None


def get_current_pool_type() -> str:
    """
    Get the current worker pool type (cached).
    
    This is the main function that primitives use internally. It caches
    the result after first detection for performance.
    
    Returns:
        str: "gevent", "fork", or "threads"
    """
    global _cached_pool_type
    if _cached_pool_type is None:
        _cached_pool_type = detect_worker_pool_type()
        logger.debug("Detected worker pool type", pool_type=_cached_pool_type)
    return _cached_pool_type


# ============================================================================
# Lock Protocol (for type hinting)
# ============================================================================

class LockProtocol(Protocol):
    """Protocol for lock-like objects."""
    
    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        """Acquire the lock."""
        ...
    
    def release(self) -> None:
        """Release the lock."""
        ...
    
    def __enter__(self) -> 'LockProtocol':
        """Context manager entry."""
        ...
    
    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Context manager exit."""
        ...


# ============================================================================
# Event Protocol (for type hinting)
# ============================================================================

class EventProtocol(Protocol):
    """Protocol for event-like objects."""
    
    def set(self) -> None:
        """Set the event."""
        ...
    
    def clear(self) -> None:
        """Clear the event."""
        ...
    
    def wait(self, timeout: Optional[float] = None) -> bool:
        """Wait for the event."""
        ...
    
    def is_set(self) -> bool:
        """Check if event is set."""
        ...


# ============================================================================
# WorkerLock: Pool-Aware Mutual Exclusion
# ============================================================================

class WorkerLock:
    """
    Pool-aware mutual exclusion lock.
    
    This class provides a unified locking interface that automatically uses
    the appropriate locking primitive based on the detected pool type:
    - Fork pool: threading.Lock (processes isolated, threads safe)
    - Threads pool: threading.Lock (standard thread synchronization)
    - Eventlet pool: eventlet.semaphore.Semaphore (cooperative, no OS threads)
    - Gevent pool: gevent.lock.Semaphore (cooperative, no OS threads)
    
    Usage:
        ```python
        lock = WorkerLock()
        
        # Explicit acquire/release
        lock.acquire()
        try:
            # Critical section
            pass
        finally:
            lock.release()
        
        # Context manager (recommended)
        with lock:
            # Critical section
            pass
        ```
    
    Why This Works:
    - Eventlet/Gevent pools: Uses cooperative semaphores (greenlet-safe)
    - Fork/Threads pools: Uses OS-level thread locks (thread-safe)
    - Automatically adapts: No code changes needed
    
    Thread Safety:
    - Fork pool: Each process has isolated memory (no shared state issues)
    - Threads pool: Uses real OS threads (standard threading.Lock)
    - Eventlet pool: Uses greenlets (cooperative, no preemption)
    - Gevent pool: Uses greenlets (cooperative, no preemption)
    """
    
    def __init__(self):
        """Initialize lock based on current pool type."""
        pool_type = get_current_pool_type()
        
        if pool_type == "gevent":
            # Use gevent's cooperative lock
            try:
                import gevent.lock
                self._lock = gevent.lock.Semaphore(1)
            except ImportError:
                logger.warning("gevent not available, falling back to threading.Lock")
                import threading
                self._lock = threading.Lock()
        
        else:  # fork or threads
            # Use standard threading lock
            import threading
            self._lock = threading.Lock()
    
    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        """
        Acquire the lock.
        
        Args:
            blocking: If True, block until lock is available
            timeout: Maximum time to wait (-1 = infinite)
            
        Returns:
            bool: True if lock acquired, False otherwise
        """
        return self._lock.acquire(blocking=blocking, timeout=timeout)
    
    def release(self) -> None:
        """Release the lock."""
        self._lock.release()
    
    def __enter__(self) -> 'WorkerLock':
        """Context manager entry."""
        self._lock.__enter__()
        return self
    
    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Context manager exit."""
        self._lock.__exit__(exc_type, exc_val, exc_tb)
    
    def locked(self) -> bool:
        """
        Check if lock is currently held.
        
        Returns:
            bool: True if lock is held, False otherwise
        """
        if hasattr(self._lock, 'locked'):
            return self._lock.locked()
        # For threading.Lock, try to acquire and immediately release
        acquired = self._lock.acquire(blocking=False)
        if acquired:
            self._lock.release()
            return False
        return True


# ============================================================================
# WorkerRLock: Pool-Aware Reentrant Lock
# ============================================================================

class WorkerRLock:
    """
    Pool-aware reentrant lock (recursive lock).
    
    A reentrant lock can be acquired multiple times by the same thread/greenlet
    without deadlocking. This is useful for recursive operations or when the
    same code path may acquire a lock multiple times.
    
    Usage:
        ```python
        lock = WorkerRLock()
        
        def recursive_operation(depth):
            with lock:
                if depth > 0:
                    recursive_operation(depth - 1)
        
        recursive_operation(5)  # Works without deadlock
        ```
    
    Implementation Notes:
    - Fork/Threads: Uses threading.RLock
    - Eventlet: Uses eventlet.semaphore.BoundedSemaphore
    - Gevent: Uses gevent.lock.RLock
    """
    
    def __init__(self):
        """Initialize reentrant lock based on current pool type."""
        pool_type = get_current_pool_type()
        
        if pool_type == "gevent":
            try:
                import gevent.lock
                self._lock = gevent.lock.RLock()
            except ImportError:
                import threading
                self._lock = threading.RLock()
        
        else:  # fork or threads
            import threading
            self._lock = threading.RLock()
    
    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        """Acquire the lock."""
        return self._lock.acquire(blocking=blocking, timeout=timeout)
    
    def release(self) -> None:
        """Release the lock."""
        self._lock.release()
    
    def __enter__(self) -> 'WorkerRLock':
        """Context manager entry."""
        self._lock.__enter__()
        return self
    
    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Context manager exit."""
        self._lock.__exit__(exc_type, exc_val, exc_tb)


# ============================================================================
# WorkerEvent: Pool-Aware Event Signaling
# ============================================================================

class WorkerEvent:
    """
    Pool-aware event for cross-thread/greenlet signaling.
    
    Events are used for coordination between concurrent operations. One
    operation can wait for an event, while another sets it when ready.
    
    Usage:
        ```python
        event = WorkerEvent()
        
        def waiter():
            event.wait()  # Blocks until set
            print("Event triggered!")
        
        def trigger():
            worker_sleep(1.0)
            event.set()  # Unblocks waiter
        
        # Both work on all pool types
        ```
    
    Pool Adaptation:
    - Fork/Threads: Uses threading.Event (OS-level signaling)
    - Eventlet: Uses eventlet.event.Event (greenlet signaling)
    - Gevent: Uses gevent.event.Event (greenlet signaling)
    """
    
    def __init__(self):
        """Initialize event based on current pool type."""
        pool_type = get_current_pool_type()
        
        if pool_type == "gevent":
            try:
                import gevent.event
                self._event = gevent.event.Event()
            except ImportError:
                import threading
                self._event = threading.Event()
        
        else:  # fork or threads
            import threading
            self._event = threading.Event()
    
    def set(self) -> None:
        """Set the event, unblocking all waiters."""
        self._event.set()
    
    def clear(self) -> None:
        """Clear the event."""
        self._event.clear()
    
    def wait(self, timeout: Optional[float] = None) -> bool:
        """
        Wait for the event to be set.
        
        Args:
            timeout: Maximum time to wait (None = infinite)
            
        Returns:
            bool: True if event was set, False if timeout
        """
        return self._event.wait(timeout=timeout)
    
    def is_set(self) -> bool:
        """
        Check if event is currently set.
        
        Returns:
            bool: True if set, False otherwise
        """
        return self._event.is_set()


# ============================================================================
# WorkerSemaphore: Pool-Aware Resource Pooling
# ============================================================================

class WorkerSemaphore:
    """
    Pool-aware semaphore for resource pooling and bounded concurrency.
    
    Semaphores limit the number of concurrent operations accessing a shared
    resource. Useful for connection pools, rate limiting, etc.
    
    Usage:
        ```python
        # Limit to 5 concurrent database connections
        db_semaphore = WorkerSemaphore(5)
        
        def query_database():
            with db_semaphore:
                # Only 5 operations can be here simultaneously
                result = db.query("SELECT ...")
                return result
        ```
    
    Pool Adaptation:
    - Fork/Threads: Uses threading.Semaphore
    - Eventlet: Uses eventlet.semaphore.Semaphore
    - Gevent: Uses gevent.lock.Semaphore
    """
    
    def __init__(self, value: int = 1):
        """
        Initialize semaphore with given value.
        
        Args:
            value: Initial semaphore count (default 1)
        """
        pool_type = get_current_pool_type()
        
        if pool_type == "gevent":
            try:
                import gevent.lock
                self._semaphore = gevent.lock.Semaphore(value)
            except ImportError:
                import threading
                self._semaphore = threading.Semaphore(value)
        
        else:  # fork or threads
            import threading
            self._semaphore = threading.Semaphore(value)
    
    def acquire(self, blocking: bool = True, timeout: Optional[float] = None) -> bool:
        """Acquire the semaphore."""
        return self._semaphore.acquire(blocking=blocking, timeout=timeout)
    
    def release(self) -> None:
        """Release the semaphore."""
        self._semaphore.release()
    
    def __enter__(self) -> 'WorkerSemaphore':
        """Context manager entry."""
        self._semaphore.__enter__()
        return self
    
    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Context manager exit."""
        self._semaphore.__exit__(exc_type, exc_val, exc_tb)


# ============================================================================
# WorkerLocal: Pool-Aware Thread-Local/Greenlet-Local Storage
# ============================================================================

class WorkerLocal:
    """
    Pool-aware thread-local/greenlet-local storage.
    
    This class provides a unified interface for storing per-worker data that
    automatically adapts to the detected pool type:
    - Fork pool: threading.local() (thread-local storage, process-isolated)
    - Threads pool: threading.local() (thread-local storage)
    - Gevent pool: gevent.local.local() (greenlet-local storage)
    
    Usage:
        ```python
        # Create worker-local storage
        worker_local = WorkerLocal()
        
        # Set attributes (per-worker)
        worker_local.request_id = "req-123"
        worker_local.db_connection = create_connection()
        
        # Access attributes (worker-specific)
        request_id = worker_local.request_id
        
        # Check if attribute exists
        if hasattr(worker_local, 'db_connection'):
            conn = worker_local.db_connection
        
        # Clean up when done
        del worker_local.db_connection
        
        # Or clear everything
        worker_local.clear()
        ```
    
    Common Use Cases:
    - Request context (request_id, user_id, correlation_id)
    - Per-worker database connections
    - Per-worker caches or buffers
    - Distributed tracing context (span IDs, trace IDs)
    - Per-execution temporary state
    
    Why This Matters:
    - threading.local() doesn't work with greenlets (data leaks across greenlets)
    - gevent.local.local() is greenlet-aware (proper isolation)
    - This abstraction picks the right one automatically
    
    Memory Management Best Practices:
    - Always clean up resources stored in WorkerLocal
    - Use try/finally blocks or context managers
    - In thread pools, workers are reused - clean up between tasks
    - In fork pools, each worker has isolated memory (automatic cleanup on exit)
    - Call clear() at the end of task execution to prevent memory leaks
    
    Example Patterns:
        ```python
        # Pattern 1: Context manager for automatic cleanup
        @contextmanager
        def request_context(request_id: str):
            local = WorkerLocal()
            local.request_id = request_id
            local.start_time = time.time()
            try:
                yield local
            finally:
                local.clear()
        
        # Pattern 2: Connection pooling
        db_local = WorkerLocal()
        
        def get_db_connection():
            if not hasattr(db_local, 'connection'):
                db_local.connection = create_db_connection()
            return db_local.connection
        
        def close_db_connection():
            if hasattr(db_local, 'connection'):
                db_local.connection.close()
                del db_local.connection
        
        # Pattern 3: Distributed tracing
        trace_local = WorkerLocal()
        
        def start_span(trace_id: str, span_id: str):
            trace_local.trace_id = trace_id
            trace_local.span_id = span_id
            trace_local.start_time = time.time()
        
        def end_span():
            duration = time.time() - trace_local.start_time
            logger.info("Span completed", 
                       trace_id=trace_local.trace_id,
                       span_id=trace_local.span_id,
                       duration=duration)
            trace_local.clear()
        ```
    
    Thread Safety:
    - Fork pool: Each process has isolated memory (no shared state issues)
    - Threads pool: Each thread has its own local storage (thread-safe)
    - Gevent pool: Each greenlet has its own local storage (greenlet-safe)
    
    Comparison with contextvars.ContextVar:
    - WorkerLocal: Simple attribute-based storage, per-worker isolation
    - ContextVar: Async context propagation, automatic inheritance in async tasks
    - Use WorkerLocal for: DB connections, worker state, simple request context
    - Use ContextVar for: Async context propagation, complex nested async calls
    """
    
    def __init__(self):
        """Initialize worker-local storage based on current pool type."""
        pool_type = get_current_pool_type()
        
        if pool_type == "gevent":
            try:
                import gevent.local
                self._local = gevent.local.local()
                logger.debug("WorkerLocal initialized with gevent.local")
            except ImportError:
                logger.warning("gevent not available, falling back to threading.local")
                import threading
                self._local = threading.local()
        
        else:  # fork or threads
            import threading
            self._local = threading.local()
            logger.debug("WorkerLocal initialized with threading.local", pool_type=pool_type)
    
    def __getattr__(self, name: str) -> Any:
        """
        Get attribute from worker-local storage.
        
        Args:
            name: Attribute name
            
        Returns:
            Attribute value
            
        Raises:
            AttributeError: If attribute doesn't exist
        """
        if name == '_local':
            # Avoid recursion when accessing internal _local
            return object.__getattribute__(self, name)
        return getattr(self._local, name)
    
    def __setattr__(self, name: str, value: Any) -> None:
        """
        Set attribute in worker-local storage.
        
        Args:
            name: Attribute name
            value: Attribute value
        """
        if name == '_local':
            # Set internal _local directly
            object.__setattr__(self, name, value)
        else:
            setattr(self._local, name, value)
    
    def __delattr__(self, name: str) -> None:
        """
        Delete attribute from worker-local storage.
        
        Args:
            name: Attribute name
            
        Raises:
            AttributeError: If attribute doesn't exist
        """
        delattr(self._local, name)
    
    def clear(self) -> None:
        """
        Clear all attributes from worker-local storage.
        
        Useful for cleaning up between task executions in worker pools
        to prevent memory leaks and stale data.
        
        Example:
            ```python
            worker_local = WorkerLocal()
            worker_local.request_id = "req-123"
            worker_local.data = large_object
            
            try:
                process_request()
            finally:
                worker_local.clear()  # Clean up everything
            ```
        """
        if hasattr(self._local, '__dict__'):
            self._local.__dict__.clear()
    
    def get(self, name: str, default: Any = None) -> Any:
        """
        Get attribute with default value if not present.
        
        Args:
            name: Attribute name
            default: Default value if attribute doesn't exist
            
        Returns:
            Attribute value or default
            
        Example:
            ```python
            worker_local = WorkerLocal()
            request_id = worker_local.get('request_id', 'unknown')
            ```
        """
        return getattr(self._local, name, default)
    
    def set(self, name: str, value: Any) -> None:
        """
        Set attribute (alternative to direct assignment).
        
        Args:
            name: Attribute name
            value: Attribute value
            
        Example:
            ```python
            worker_local = WorkerLocal()
            worker_local.set('request_id', 'req-123')
            # Equivalent to: worker_local.request_id = 'req-123'
            ```
        """
        setattr(self._local, name, value)
    
    def has(self, name: str) -> bool:
        """
        Check if attribute exists.
        
        Args:
            name: Attribute name
            
        Returns:
            bool: True if attribute exists, False otherwise
            
        Example:
            ```python
            worker_local = WorkerLocal()
            if worker_local.has('db_connection'):
                conn = worker_local.db_connection
            ```
        """
        return hasattr(self._local, name)


# ============================================================================
# WorkerThread: Pool-Aware Thread/Greenlet Spawning
# ============================================================================

class WorkerThread:
    """
    Pool-aware thread/greenlet spawning and management.
    
    This class provides a unified interface for spawning concurrent operations
    that automatically adapts to the pool type:
    - Fork/Threads: Spawns real OS threads
    - Eventlet: Spawns greenlets
    - Gevent: Spawns greenlets
    
    Usage:
        ```python
        def background_task(arg1, arg2):
            print(f"Running with {arg1}, {arg2}")
        
        # Works on all pool types
        thread = WorkerThread(target=background_task, args=(1, 2))
        thread.start()
        thread.join()  # Wait for completion
        ```
    
    Why This Matters:
    - Threading pools: Need real threads
    - Eventlet/Gevent: Need greenlets for cooperative scheduling
    - This abstraction handles both cases automatically
    
    IMPORTANT: When to NOT use WorkerThread:
    - If you need a REAL OS thread in gevent/eventlet (e.g., for asyncio isolation)
    - Use concurrent.futures.ThreadPoolExecutor directly instead
    - See async_helpers.py for example of asyncio isolation pattern
    """
    
    def __init__(
        self,
        target: Callable,
        args: tuple = (),
        kwargs: Optional[Dict[str, Any]] = None,
        daemon: bool = False,
    ):
        """
        Initialize worker thread/greenlet.
        
        Args:
            target: Function to execute
            args: Positional arguments for target
            kwargs: Keyword arguments for target
            daemon: Whether to run as daemon (background)
        """
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}
        self.daemon = daemon
        self._thread = None
        self._greenlet = None
        pool_type = get_current_pool_type()
        
        if pool_type == "gevent":
            try:
                import gevent
                self._pool_type = "gevent"
            except ImportError:
                self._pool_type = "threading"
        
        else:  # fork or threads
            self._pool_type = "threading"
    
    def start(self) -> None:
        """Start the thread/greenlet."""
        if self._pool_type == "gevent":
            import gevent
            self._greenlet = gevent.spawn(self.target, *self.args, **self.kwargs)
        
        else:  # threading
            import threading
            self._thread = threading.Thread(
                target=self.target,
                args=self.args,
                kwargs=self.kwargs,
                daemon=self.daemon
            )
            self._thread.start()
    
    def join(self, timeout: Optional[float] = None) -> None:
        """
        Wait for thread/greenlet to complete.
        
        Args:
            timeout: Maximum time to wait (None = infinite)
        """
        if self._pool_type == "gevent":
            if self._greenlet:
                cast(Any, self._greenlet).join(timeout=timeout)
        else:  # threading
            if self._thread:
                self._thread.join(timeout=timeout)
    
    def is_alive(self) -> bool:
        """
        Check if thread/greenlet is still running.
        
        Returns:
            bool: True if running, False otherwise
        """
        if self._pool_type == "gevent":
            return self._greenlet is not None and not self._greenlet.dead
        else:  # threading
            return self._thread is not None and self._thread.is_alive()


# ============================================================================
# worker_sleep: Pool-Aware Sleep with Cooperative Yielding
# ============================================================================

def worker_sleep(seconds: float) -> None:
    """
    Sleep for the given duration with pool-aware cooperative yielding.
    
    This function automatically adapts to the pool type:
    - Fork/Threads: Uses time.sleep() (blocks OS thread)
    - Gevent: Uses gevent.sleep() (yields to other greenlets)
    
    Usage:
        ```python
        # This works correctly on all pool types
        worker_sleep(1.0)  # Sleep for 1 second
        ```
    
    Why This Matters:
    - time.sleep() blocks the entire event loop on gevent
    - gevent.sleep() yields cooperatively
    - This function picks the right one automatically
    
    Args:
        seconds: Duration to sleep (in seconds)
    """
    pool_type = get_current_pool_type()
    
    if pool_type == "gevent":
        try:
            import gevent
            gevent.sleep(seconds)
        except ImportError:
            time.sleep(seconds)
    
    else:  # fork or threads
        time.sleep(seconds)


# ============================================================================
# WorkerExecutor: Pool-Aware Concurrent Execution
# ============================================================================

class WorkerExecutor:
    """
    Pool-aware concurrent executor (ADR-0033).
    
    This class provides a unified interface for concurrent execution that
    automatically adapts to the pool type, mirroring ThreadPoolExecutor's API:
    - Fork/Threads: Uses concurrent.futures.ThreadPoolExecutor
    - Gevent: Uses gevent.pool.Pool
    
    Usage:
        ```python
        # Execute multiple tasks concurrently
        with WorkerExecutor(max_workers=10) as executor:
            futures = [executor.submit(task, arg) for arg in args]
            results = [f.result() for f in futures]
        
        # Map function over iterable
        with WorkerExecutor(max_workers=5) as executor:
            results = list(executor.map(process_item, items))
        ```
    
    Why This Matters:
    - ThreadPoolExecutor spawns real OS threads (inefficient on gevent)
    - gevent.pool.Pool uses cooperative greenlets (efficient)
    - This class picks the right implementation automatically
    
    Benefits:
    - Write once, run efficiently on all pool types
    - Maintains ThreadPoolExecutor-compatible API
    - True cooperative concurrency on gevent
    - No thread explosion on high-concurrency workers
    
    IMPORTANT: When to NOT use WorkerExecutor:
    - If you need asyncio isolation (use run_async_safe from async_helpers)
    - If you need real OS threads for CPU-bound work
    - For async operations, use run_async_safe() instead
    """
    
    def __init__(self, max_workers: Optional[int] = None):
        """
        Initialize worker pool.
        
        Args:
            max_workers: Maximum number of concurrent workers.
                        None means use default (depends on pool type)
        """
        self.max_workers = max_workers
        self._pool: Any = None
        self._pool_type = get_current_pool_type()
        self._futures = []
        
        logger.debug("Creating WorkerExecutor", pool_type=self._pool_type, max_workers=max_workers)
    
    def __enter__(self):
        """Context manager entry - create the pool."""
        if self._pool_type == "gevent":
            try:
                from gevent.pool import Pool
                # gevent.pool.Pool(size) - None means unlimited, but we should bound it
                size = self.max_workers if self.max_workers is not None else 100
                self._pool = Pool(size=size)
                logger.debug("Created gevent.pool.Pool", size=size)
            except ImportError:
                logger.warning("gevent not available, falling back to ThreadPoolExecutor")
                from concurrent.futures import ThreadPoolExecutor
                self._pool = ThreadPoolExecutor(max_workers=self.max_workers)
                self._pool_type = "threads"  # submit/map/shutdown must use ThreadPoolExecutor API
        
        else:  # fork or threads
            from concurrent.futures import ThreadPoolExecutor
            self._pool = ThreadPoolExecutor(max_workers=self.max_workers)
            logger.debug("Created ThreadPoolExecutor", max_workers=self.max_workers)
        
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - cleanup the pool."""
        self.shutdown(wait=True)
        return False
    
    def submit(self, fn: Callable, *args, **kwargs):
        """
        Submit a function to be executed with the given arguments.
        
        Args:
            fn: Function to execute
            *args: Positional arguments for fn
            **kwargs: Keyword arguments for fn
        
        Returns:
            Future-like object with result() method
        """
        if self._pool is None:
            raise RuntimeError("WorkerExecutor not entered - use 'with WorkerExecutor() as executor'")
        
        if self._pool_type == "gevent":
            # Green pools return greenlets, wrap them to match Future API
            greenlet = self._pool.spawn(fn, *args, **kwargs)
            future = _GreenletFuture(greenlet)
            self._futures.append(future)
            return future
        else:
            # ThreadPoolExecutor returns real Futures
            future = self._pool.submit(fn, *args, **kwargs)
            self._futures.append(future)
            return future
    
    def map(self, fn: Callable, *iterables, timeout: Optional[float] = None):
        """
        Map a function over iterables concurrently.
        
        Args:
            fn: Function to apply
            *iterables: Iterables to map over
            timeout: Maximum time to wait for completion (not supported for green pools)
        
        Yields:
            Results in order of input
        """
        if self._pool is None:
            raise RuntimeError("WorkerExecutor not entered - use 'with WorkerExecutor() as executor'")
        
        if self._pool_type == "gevent":
            # Green pools have imap() method
            for result in cast(Any, self._pool).imap(fn, *iterables):
                yield result
        else:
            # ThreadPoolExecutor.map signature varies across typeshed versions
            for result in cast(Any, self._pool).map(fn, *iterables, timeout=timeout):
                yield result
    
    def shutdown(self, wait: bool = True, cancel_futures: bool = False):
        """
        Shutdown the pool.
        
        Args:
            wait: Wait for pending tasks to complete
            cancel_futures: Cancel pending futures (only supported for ThreadPoolExecutor)
        """
        if self._pool is None:
            return
        
        if self._pool_type == "gevent":
            if wait:
                # Wait for all spawned greenlets to complete
                for future in self._futures:
                    try:
                        future.result()
                    except Exception:
                        pass  # Errors already captured in future
            # Green pools don't have explicit shutdown
            self._pool = None
        else:
            # ThreadPoolExecutor has shutdown()
            self._pool.shutdown(wait=wait, cancel_futures=cancel_futures)
        
        logger.debug("WorkerExecutor shutdown", pool_type=self._pool_type, wait=wait)


class _GreenletFuture:
    """
    Wrapper to make greenlets compatible with concurrent.futures.Future API.
    
    This allows green pools to be used with the same API as ThreadPoolExecutor.
    """
    
    def __init__(self, greenlet):
        self._greenlet = greenlet
        self._result = None
        self._exception = None
        self._done = False
    
    def result(self, timeout: Optional[float] = None):
        """
        Get the result of the greenlet.
        
        Args:
            timeout: Maximum time to wait (seconds)
        
        Returns:
            Result of the greenlet execution
        
        Raises:
            Exception: If greenlet raised an exception
        """
        if not self._done:
            try:
                # Wait for greenlet to complete
                if timeout is not None:
                    import gevent
                    with gevent.Timeout(timeout):
                        self._result = self._greenlet.get()
                else:
                    self._result = self._greenlet.get()
                self._done = True
            except Exception as e:
                self._exception = e
                self._done = True
                raise
        
        if self._exception:
            raise self._exception
        return self._result
    
    def exception(self, timeout: Optional[float] = None):
        """Get the exception raised by the greenlet, if any."""
        if not self._done:
            try:
                self.result(timeout=timeout)
            except Exception:
                pass  # Exception already stored
        return self._exception
    
    def done(self) -> bool:
        """Check if the greenlet has completed."""
        if not self._done:
            # Check if greenlet is dead (gevent/eventlet)
            try:
                self._done = self._greenlet.dead
            except AttributeError:
                # Fallback: try to check if it's ready
                try:
                    self._done = self._greenlet.ready()
                except AttributeError:
                    pass
        return self._done
    
    def cancel(self) -> bool:
        """Cancel the greenlet (not supported, returns False)."""
        return False
    
    def cancelled(self) -> bool:
        """Check if cancelled (always False for greenlets)."""
        return False


# ============================================================================
# Subprocess Helper
# ============================================================================


def worker_run_subprocess(
    args,
    *,
    capture_output: bool = False,
    check: bool = False,
    cwd=None,
    env=None,
    timeout=None,
    text: bool = False,
    **kwargs,
):
    """
    Pool-aware subprocess.run() replacement (ADR-0033).

    On gevent pools, ``subprocess.run()`` can deadlock because gevent's event
    loop is not running while the calling greenlet is blocked waiting for the
    child process to exit.  The fix is ``gevent.subprocess.run()`` which
    replaces the blocking wait with gevent-cooperative I/O on the child's
    stdout/stderr pipes.

    On fork/threads pools, plain ``subprocess.run()`` is used unchanged.

    Usage:
        from motet.core.workers.concurrency_primitives import worker_run_subprocess
        import subprocess  # only for exception types

        result = worker_run_subprocess(
            ["git", "clone", "--depth=1", url, tmpdir],
            capture_output=True,
            check=True,
            timeout=60,
        )

    Raises:
        subprocess.TimeoutExpired, subprocess.CalledProcessError — same as
        stdlib subprocess, regardless of pool type.

    Notes:
        - ``gevent.subprocess`` is a drop-in for ``subprocess`` and raises the
          same exception types (re-exported from the stdlib module).
        - If gevent is not installed at runtime (e.g., during unit tests on a
          non-gevent process), falls back to stdlib subprocess.
    """
    pool_type = get_current_pool_type()

    if pool_type == "gevent":
        try:
            import gevent.subprocess as _gsub
            return _gsub.run(
                args,
                capture_output=capture_output,
                check=check,
                cwd=cwd,
                env=env,
                timeout=timeout,
                text=text,
                **kwargs,
            )
        except ImportError:
            pass  # gevent not installed — fall through to stdlib

    import subprocess as _sub
    return _sub.run(
        args,
        capture_output=capture_output,
        check=check,
        cwd=cwd,
        env=env,
        timeout=timeout,
        text=text,
        **kwargs,
    )


# ============================================================================
# Convenience Functions
# ============================================================================

def get_pool_info() -> dict:
    """
    Get information about the current pool type and available primitives.
    
    Returns:
        dict: Pool type and feature information
    """
    pool_type = get_current_pool_type()
    
    return {
        "pool_type": pool_type,
        "is_cooperative": pool_type in ("eventlet", "gevent"),
        "supports_os_threads": pool_type in ("fork", "threads"),
        "supports_async_bridge": pool_type == "gevent",  # asyncio-gevent available
        "primitives_available": {
            "WorkerLock": True,
            "WorkerRLock": True,
            "WorkerEvent": True,
            "WorkerSemaphore": True,
            "WorkerLocal": True,
            "WorkerThread": True,
            "WorkerExecutor": True,
            "worker_sleep": True,
            "worker_run_subprocess": True,
        },
        "async_operations": {
            "use_run_async_safe": True,  # From motet.core.utils.async_helpers
            "supports_playwright": True,
            "supports_httpx": True,
        }
    }


# ============================================================================
# Module Exports
# ============================================================================

__all__ = [
    # Pool detection
    "detect_worker_pool_type",
    "get_current_pool_type",
    "get_pool_info",
    
    # Primitives
    "WorkerLock",
    "WorkerRLock",
    "WorkerEvent",
    "WorkerSemaphore",
    "WorkerLocal",
    "WorkerThread",
    "WorkerExecutor",
    "worker_sleep",
    
    # Subprocess
    "worker_run_subprocess",

    # Protocols (for type hints)
    "LockProtocol",
    "EventProtocol",
]

