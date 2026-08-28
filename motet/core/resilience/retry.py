"""
Motet - Retry Logic

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Retry logic implementation for the Motet distributed framework.
    Provides exponential backoff with jitter for resilient distributed operations.

Dependencies:
    - random: Random number generation for jitter
    - typing: Type hints and annotations
    - Callable operations

Usage:
    from motet.core.resilience.retry import retry_with_backoff
    
    # Retry with backoff
    result = await retry_with_backoff(operation, max_attempts=3)

Notes:
    - Supports exponential backoff with jitter
    - Includes configurable retry attempts
    - Provides exception filtering
    - Integrates with distributed architecture
"""

from __future__ import annotations

import random
from typing import Any, Callable, TypeVar


T = TypeVar("T")


def _worker_sleep(seconds: float) -> None:
    """Lazy import of worker_sleep to avoid circular dependencies."""
    from ..workers.concurrency_primitives import worker_sleep
    worker_sleep(seconds)


def exponential_backoff(attempt: int, base: float = 0.5, cap: float = 5.0, jitter: float = 0.1) -> float:
    delay = min(cap, base * (2 ** max(0, attempt - 1)))
    return max(0.0, delay + random.uniform(-jitter, jitter))


def retry(fn: Callable[[], T], *, max_attempts: int = 3, should_retry: Callable[[Exception], bool] | None = None) -> T:
    attempt = 0
    while True:
        try:
            return fn()
        except Exception as exc:
            attempt += 1
            if attempt >= max_attempts or (should_retry and not should_retry(exc)):
                raise
            # Pool-aware cooperative yielding between attempts
            _worker_sleep(exponential_backoff(attempt))


def retry_sync(fn: Callable[[], T], *, max_attempts: int = 3, should_retry: Callable[[Exception], bool] | None = None) -> T:
    """Sync version of retry_async for backward compatibility"""
    return retry(fn, max_attempts=max_attempts, should_retry=should_retry)


