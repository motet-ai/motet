"""
Unit tests for task stream agent_id attribution (ADR-0083).

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    Verifies orchestrator helpers attach agent_id from Redis stream fields to yielded events.

Dependencies:
    - pytest
    - motet.core.orchestration.orchestrator

Usage:
    pytest tests/unit/core/test_orchestrator_stream_agent_id.py -q

Notes:
    - Complements encrypted stream writer path in MotetContext._stream_event_raw
"""

from motet.core.orchestration.orchestrator import _with_stream_agent_id


def test_with_stream_agent_id_adds_when_present() -> None:
    fields = {"agent_id": "core.default", "event": "token"}
    out = _with_stream_agent_id({"event": "token", "data": "hi"}, fields)
    assert out["agent_id"] == "core.default"
    assert out["data"] == "hi"


def test_with_stream_agent_id_unchanged_when_absent() -> None:
    fields = {"event": "token"}
    base = {"event": "token", "data": "x"}
    out = _with_stream_agent_id(base, fields)
    assert out is base
    assert "agent_id" not in out


def test_with_stream_agent_id_adds_parent_when_present() -> None:
    fields = {
        "agent_id": "core.default.spawn-1",
        "parent_agent_id": "core.default",
        "event": "token",
    }
    out = _with_stream_agent_id({"event": "token", "data": "hi"}, fields)
    assert out["agent_id"] == "core.default.spawn-1"
    assert out["parent_agent_id"] == "core.default"
