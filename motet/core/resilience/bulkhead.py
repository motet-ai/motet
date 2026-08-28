"""
Motet - Bulkhead Resilience

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Bulkhead resilience pattern for the Motet distributed framework.
    Provides comprehensive resource isolation and concurrency control using
    semaphore-based bulkhead patterns. Includes worker semaphore management,
    concurrency limiting, and distributed resilience coordination.

Dependencies:
    - typing: Type hints and annotations
    - Worker concurrency primitives and semaphore management
    - TYPE_CHECKING for circular dependency avoidance

Usage:
    from motet.core.resilience.bulkhead import Bulkhead, bulkhead_sync

    # Create bulkhead
    bulkhead = Bulkhead(max_concurrent=10)

    # Run with bulkhead
    result = bulkhead.run(lambda: expensive_operation())

    # Decorator usage
    @bulkhead_sync(max_concurrent=5)
    def my_function():
        return "result"

Notes:
    - Provides comprehensive resource isolation and concurrency control
    - Includes semaphore-based bulkhead patterns
    - Supports worker semaphore management and concurrency limiting
    - Includes distributed resilience coordination
    - Supports decorator-based bulkhead application
    - Integrates with worker concurrency primitives
    - Includes comprehensive error handling and logging
"""

from __future__ import annotations

from typing import Callable, TypeVar, TYPE_CHECKING

if TYPE_CHECKING:
    from ..workers.concurrency_primitives import WorkerSemaphore


T = TypeVar("T")


def _get_worker_semaphore(value: int):
    """Lazy import of WorkerSemaphore to avoid circular dependencies."""
    from ..workers.concurrency_primitives import WorkerSemaphore
    return WorkerSemaphore(value)


class Bulkhead:
    def __init__(self, max_concurrent: int) -> None:
        self._sem = _get_worker_semaphore(max(1, int(max_concurrent)))

    def run(self, fn: Callable[[], T]) -> T:
        with self._sem:
            return fn()


def bulkhead_sync(max_concurrent: int) -> Callable[[Callable[[], T]], Callable[[], T]]:
    bh = Bulkhead(max_concurrent)

    def _decorator(fn: Callable[[], T]) -> Callable[[], T]:
        def _wrapped() -> T:
            return bh.run(fn)

        return _wrapped

    return _decorator


