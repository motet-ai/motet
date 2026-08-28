"""
Motet SDK - Concurrency primitives (pool-agnostic).

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

When your bundle runs inside the Motet runtime, this module is replaced by the
runtime's pool-aware implementation (fork/threads/gevent). Use these primitives
in bundle code so that the same code works correctly on all worker pool types.

For async work from sync command code (e.g. httpx.AsyncClient, Playwright), use
run_async_safe(coro) so execution is correct on gevent/fork/threads pools.

When the SDK is used standalone (e.g. tests, type checking), this module provides
a threading-only fallback so imports and basic usage work without the runtime.

Usage:
    from motet_sdk.concurrency import WorkerLock, worker_sleep, WorkerExecutor, run_async_safe

    lock = WorkerLock()
    with lock:
        worker_sleep(0.1)

    async def fetch():
        async with httpx.AsyncClient() as client:
            return (await client.get("https://example.com")).json()

    data = run_async_safe(fetch())  # Safe from sync command on all pool types

Dependencies:
    - threading, time, concurrent.futures, asyncio (stdlib only for fallback)

Notes:
    - In the runtime: motet.core.workers.concurrency_primitives is injected;
      run_async_safe comes from motet.core.utils.async_helpers.
    - High-Concurrency Worker Support.
"""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any, Callable, Coroutine, Iterator, Optional

# ---------------------------------------------------------------------------
# Fallback implementation (stdlib only). Replaced at runtime by the real module.
# ---------------------------------------------------------------------------


def worker_sleep(seconds: float) -> None:
    """Sleep for the given duration. In the runtime, uses cooperative yielding on gevent."""
    time.sleep(seconds)


class WorkerLock:
    """Pool-aware mutual exclusion lock. In the runtime, uses gevent lock on gevent pool."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        if timeout < 0:
            return self._lock.acquire(blocking=blocking)
        return self._lock.acquire(blocking=blocking, timeout=timeout)

    def release(self) -> None:
        self._lock.release()

    def __enter__(self) -> WorkerLock:
        self._lock.acquire()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self._lock.release()

    def locked(self) -> bool:
        return self._lock.locked()


class WorkerRLock:
    """Pool-aware reentrant lock. In the runtime, uses gevent RLock on gevent pool."""

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        if timeout < 0:
            return self._lock.acquire(blocking=blocking)
        return self._lock.acquire(blocking=blocking, timeout=timeout)

    def release(self) -> None:
        self._lock.release()

    def __enter__(self) -> WorkerRLock:
        self._lock.acquire()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self._lock.release()


class WorkerEvent:
    """Pool-aware event for cross-thread/greenlet signaling."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def set(self) -> None:
        self._event.set()

    def clear(self) -> None:
        self._event.clear()

    def wait(self, timeout: Optional[float] = None) -> bool:
        return self._event.wait(timeout=timeout)

    def is_set(self) -> bool:
        return self._event.is_set()


class WorkerSemaphore:
    """Pool-aware semaphore for resource pooling and bounded concurrency."""

    def __init__(self, value: int = 1) -> None:
        self._sem = threading.Semaphore(value)

    def acquire(self, blocking: bool = True, timeout: Optional[float] = None) -> bool:
        return self._sem.acquire(blocking=blocking, timeout=timeout)

    def release(self) -> None:
        self._sem.release()

    @contextmanager
    def __call__(self) -> Iterator[None]:
        self._sem.acquire()
        try:
            yield
        finally:
            self._sem.release()

    def __enter__(self) -> WorkerSemaphore:
        self._sem.acquire()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self._sem.release()


class WorkerLocal:
    """Pool-aware thread-local/greenlet-local storage. In the runtime, uses gevent.local on gevent pool."""

    def __init__(self) -> None:
        self._local = threading.local()

    def __getattr__(self, name: str) -> Any:
        if name == "_local":
            return object.__getattribute__(self, name)
        return getattr(self._local, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "_local":
            object.__setattr__(self, name, value)
        else:
            setattr(self._local, name, value)

    def __delattr__(self, name: str) -> None:
        delattr(self._local, name)

    def clear(self) -> None:
        if hasattr(self._local, "__dict__"):
            self._local.__dict__.clear()

    def get(self, name: str, default: Any = None) -> Any:
        return getattr(self._local, name, default)

    def set(self, name: str, value: Any) -> None:
        setattr(self._local, name, value)

    def has(self, name: str) -> bool:
        return hasattr(self._local, name)


class WorkerThread:
    """Pool-aware thread/greenlet spawning. In the runtime, spawns greenlets on gevent pool."""

    def __init__(
        self,
        target: Callable[..., Any],
        args: tuple = (),
        kwargs: Optional[dict] = None,
        daemon: bool = False,
    ) -> None:
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}
        self.daemon = daemon
        self._thread = threading.Thread(
            target=target,
            args=args,
            kwargs=self.kwargs,
            daemon=daemon,
        )

    def start(self) -> None:
        self._thread.start()

    def join(self, timeout: Optional[float] = None) -> None:
        self._thread.join(timeout=timeout)

    def is_alive(self) -> bool:
        return self._thread.is_alive()


class WorkerExecutor:
    """Pool-aware concurrent executor. In the runtime, uses gevent pool on gevent workers."""

    def __init__(self, max_workers: Optional[int] = None) -> None:
        self.max_workers = max_workers
        self._pool: Optional[ThreadPoolExecutor] = None

    def __enter__(self) -> WorkerExecutor:
        self._pool = ThreadPoolExecutor(max_workers=self.max_workers)
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._pool:
            self._pool.shutdown(wait=True)
            self._pool = None

    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if self._pool is None:
            raise RuntimeError("WorkerExecutor not entered - use 'with WorkerExecutor() as executor'")
        return self._pool.submit(fn, *args, **kwargs)

    def map(
        self,
        fn: Callable[..., Any],
        *iterables: Any,
        timeout: Optional[float] = None,
    ) -> Iterator[Any]:
        if self._pool is None:
            raise RuntimeError("WorkerExecutor not entered - use 'with WorkerExecutor() as executor'")
        return self._pool.map(fn, *iterables, timeout=timeout)

    def shutdown(self, wait: bool = True, cancel_futures: bool = False) -> None:
        if self._pool:
            self._pool.shutdown(wait=wait, cancel_futures=cancel_futures)
            self._pool = None


def run_async_safe(coro: Coroutine[Any, Any, Any]) -> Any:
    """
    Run an async coroutine from sync code in a pool-aware way.

    In the runtime (gevent/fork/threads), uses the appropriate strategy so async
    code (e.g. httpx, Playwright) works correctly. In the SDK fallback, uses
    asyncio.run() or a thread with a new loop if one is already running.

    Usage:
        async def fetch():
            async with httpx.AsyncClient() as client:
                return (await client.get(url)).json()
        data = run_async_safe(fetch())
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        # Already inside an event loop; run in a separate thread with new loop
        def _run() -> Any:
            new_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(new_loop)
            try:
                return new_loop.run_until_complete(coro)
            finally:
                new_loop.close()
                asyncio.set_event_loop(None)

        with ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(_run).result()
    new_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(new_loop)
    try:
        return new_loop.run_until_complete(coro)
    finally:
        new_loop.close()
        asyncio.set_event_loop(None)


__all__ = [
    "WorkerLock",
    "WorkerRLock",
    "WorkerEvent",
    "WorkerSemaphore",
    "WorkerLocal",
    "WorkerThread",
    "WorkerExecutor",
    "worker_sleep",
    "run_async_safe",
]
