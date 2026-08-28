"""
Motet - MCP Per-Service Restart Budget

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Sliding-window restart cap for MCP services. Restores the intent
    (bounded automatic recovery) that was lost when health monitoring moved
    into the asyncio instance manager. One exhausted service is marked failed;
    other services keep running.

Dependencies:
    - os: MOTET_MCP_RESTART_MAX_PER_HOUR / MOTET_MCP_RESTART_WINDOW_SECONDS
    - time: event timestamps

Usage:
    budget = ServiceRestartBudget()
    if budget.is_exhausted("playwright"):
        ...
    if budget.record("playwright"):
        await recreate()

Notes:
    - Default: 3 restarts per 3600s window.
    - Safe for a single asyncio event loop; not process-shared.
"""

from __future__ import annotations

import os
import time
from typing import Dict, List, Optional


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


class ServiceRestartBudget:
    """Sliding-window restart limiter keyed by ``service_id``."""

    def __init__(
        self,
        max_restarts: Optional[int] = None,
        window_seconds: Optional[float] = None,
    ) -> None:
        self.max_restarts = (
            max_restarts
            if max_restarts is not None
            else max(0, _env_int("MOTET_MCP_RESTART_MAX_PER_HOUR", 3))
        )
        self.window_seconds = (
            window_seconds
            if window_seconds is not None
            else float(max(1, _env_int("MOTET_MCP_RESTART_WINDOW_SECONDS", 3600)))
        )
        self._events: Dict[str, List[float]] = {}

    def _prune(self, service_id: str, now: float) -> List[float]:
        cutoff = now - self.window_seconds
        kept = [ts for ts in self._events.get(service_id, []) if ts > cutoff]
        self._events[service_id] = kept
        return kept

    def count(self, service_id: str, now: Optional[float] = None) -> int:
        """Restarts recorded in the current window."""
        return len(self._prune(service_id, now if now is not None else time.time()))

    def remaining(self, service_id: str, now: Optional[float] = None) -> int:
        """Restarts still allowed in the current window (0 if exhausted)."""
        return max(0, self.max_restarts - self.count(service_id, now))

    def is_exhausted(self, service_id: str, now: Optional[float] = None) -> bool:
        """True when another restart would exceed the cap."""
        if self.max_restarts <= 0:
            return True
        return self.remaining(service_id, now) <= 0

    def record(self, service_id: str, now: Optional[float] = None) -> bool:
        """
        Record a restart attempt.

        Returns:
            True if the restart is within budget (caller should recreate).
            False if the service is exhausted (caller should mark failed).
        """
        ts = now if now is not None else time.time()
        if self.is_exhausted(service_id, ts):
            return False
        self._prune(service_id, ts).append(ts)
        return True
