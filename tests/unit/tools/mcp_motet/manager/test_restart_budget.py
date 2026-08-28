"""
Motet - MCP restart budget unit tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-13

Description:
    Sliding-window restart cap for MCP services (ADR-0032 intent).

Dependencies:
    - motet.core.tools.mcp_motet.manager.restart_budget

Usage:
    pytest tests/unit/tools/mcp_motet/manager/test_restart_budget.py
"""

from motet.core.tools.mcp_motet.manager.restart_budget import ServiceRestartBudget


def test_budget_allows_up_to_max_then_exhausts() -> None:
    budget = ServiceRestartBudget(max_restarts=3, window_seconds=3600)
    now = 1_000_000.0
    assert budget.record("playwright", now) is True
    assert budget.record("playwright", now + 1) is True
    assert budget.record("playwright", now + 2) is True
    assert budget.record("playwright", now + 3) is False
    assert budget.is_exhausted("playwright", now + 3)
    assert budget.remaining("playwright", now + 3) == 0


def test_budget_is_per_service() -> None:
    budget = ServiceRestartBudget(max_restarts=1, window_seconds=3600)
    now = 1_000_000.0
    assert budget.record("playwright", now) is True
    assert not budget.is_exhausted("weather", now)
    assert budget.record("weather", now) is True
    assert budget.is_exhausted("playwright", now)


def test_budget_window_expires() -> None:
    budget = ServiceRestartBudget(max_restarts=1, window_seconds=10)
    now = 1_000_000.0
    assert budget.record("svc", now) is True
    assert budget.is_exhausted("svc", now + 5)
    assert not budget.is_exhausted("svc", now + 11)
    assert budget.record("svc", now + 11) is True
