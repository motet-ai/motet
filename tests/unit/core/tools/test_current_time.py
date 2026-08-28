"""
Motet - Current Time Builtin Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-07-27

Description:
    Unit tests for core.current_time wall-clock builtin.

Usage:
    pytest tests/unit/core/tools/test_current_time.py
"""

from __future__ import annotations

from motet.core.tools.builtin.current_time import register, run
from motet.core.tools.registry import ToolRegistry


def test_current_time_run_defaults_to_utc() -> None:
    out = run({})
    assert out.get("status") == "success"
    result = out.get("result") or {}
    assert result.get("timezone") == "UTC"
    assert result.get("iso_utc", "").endswith("Z")
    assert isinstance(result.get("unix_timestamp"), int)
    assert result.get("date_utc")


def test_current_time_run_accepts_iana_timezone() -> None:
    out = run({"timezone": "America/New_York"})
    assert out.get("status") == "success"
    result = out.get("result") or {}
    assert result.get("timezone") == "America/New_York"
    assert result.get("iso_local")


def test_current_time_run_rejects_unknown_timezone() -> None:
    out = run({"timezone": "Not/A_Zone"})
    assert out.get("status") == "error"
    assert "unknown timezone" in (out.get("error") or "")


def test_current_time_registers_tool() -> None:
    registry = ToolRegistry()
    register(registry)
    assert registry.supports("core.current_time")
