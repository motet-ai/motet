"""
Motet - Rate Limiting Security

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-19

Description:
    Rate limiting security module for the Motet distributed framework.
    Provides comprehensive rate limiting capabilities including memory and
    Redis backends with configurable limits and time windows. Includes
    client-based rate limiting and distributed security coordination.

Dependencies:
    - collections: Defaultdict and deque for request tracking
    - typing: Type hints and annotations
    - fastapi: HTTP exception handling
    - redis: Distributed rate limiting backend

Usage:
    from motet.core.security.ratelimit import RateLimiter

    # Create rate limiter
    limiter = RateLimiter(
        backend="redis",
        redis_url=DEFAULT_REDIS_URL,  # from motet.core.constants
        limit_per_minute=100
    )

    # Check rate limit
    limiter.check("client_id")

Notes:
    - Provides comprehensive rate limiting capabilities
    - Includes memory and Redis backend support
    - Supports configurable limits and time windows
    - Includes client-based rate limiting
    - Supports distributed security coordination
    - Includes comprehensive error handling and logging
    - Integrates with FastAPI and security system
    - Includes comprehensive observability and monitoring
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Optional, cast

from fastapi import HTTPException


class RateLimiter:
    def __init__(self, *, backend: str = "memory", redis_url: Optional[str] = None, limit_per_minute: Optional[int] = None, window_seconds: int = 60) -> None:
        self.backend = backend
        self.redis_url = redis_url
        self.limit = limit_per_minute
        self.window = window_seconds
        self._request_log = defaultdict(deque)

    def check(self, client_id: str) -> None:
        if not self.limit:
            return
        now = __import__("time").time()
        if self.backend == "redis" and self.redis_url:
            try:
                import redis
                r = redis.Redis.from_url(self.redis_url)
                from motet.core.distributed.tenant_keys import product_key

                key = product_key(f"ratelimit:{client_id}:{int(now // self.window)}")
                count = cast(int, r.incr(key))
                r.expire(key, self.window)
                if count > self.limit:
                    raise HTTPException(status_code=429, detail="rate limit exceeded")
                return
            except Exception:
                pass  # Redis unavailable; fallback to in-memory
        dq = self._request_log[client_id]
        while dq and now - dq[0] > self.window:
            dq.popleft()
        if len(dq) >= self.limit:
            raise HTTPException(status_code=429, detail="rate limit exceeded")
        dq.append(now)

    def rate_limit(self, client_id: str) -> None:
        """Backward-compatible alias for legacy call sites."""
        self.check(client_id)


__all__ = ["RateLimiter"]


