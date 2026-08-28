"""
Motet - Schedule Command delay_seconds Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-07-27

Description:
    Unit tests for relative delayed scheduling via delay_seconds on
    core.schedule_command and ScheduleCommand resolution.

Usage:
    pytest tests/unit/core/tools/test_schedule_command_delay.py
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from motet.core.commands.builtin.schedule import resolve_delayed_scheduled_at
from motet.core.tools.builtin.schedule_command import run


def test_schedule_command_tool_rejects_delayed_without_anchor() -> None:
    with patch(
        "motet.core.tools.builtin.schedule_command.get_runtime_stack",
        return_value=SimpleNamespace(_task_id="t1", _conversation_id="c1"),
    ), patch(
        "motet.core.workers.invoker_context.resolve_current_identity",
        return_value=SimpleNamespace(tenant_id="tenant", principal_id="user"),
    ):
        out = run(
            {
                "command_type": "core.agent_turn",
                "command_data": {"messages": [{"role": "user", "content": "hi"}]},
                "schedule_type": "delayed",
            }
        )
    assert out.get("status") == "error"
    assert "scheduled_at" in (out.get("error") or "")
    assert "delay_seconds" in (out.get("error") or "")


def test_schedule_command_tool_rejects_both_absolute_and_relative() -> None:
    with patch(
        "motet.core.tools.builtin.schedule_command.get_runtime_stack",
        return_value=SimpleNamespace(_task_id="t1", _conversation_id="c1"),
    ), patch(
        "motet.core.workers.invoker_context.resolve_current_identity",
        return_value=SimpleNamespace(tenant_id="tenant", principal_id="user"),
    ):
        out = run(
            {
                "command_type": "core.agent_turn",
                "command_data": {"messages": [{"role": "user", "content": "hi"}]},
                "schedule_type": "delayed",
                "scheduled_at": "2026-07-27T14:30:00Z",
                "delay_seconds": 30,
            }
        )
    assert out.get("status") == "error"
    assert "not both" in (out.get("error") or "")


def test_schedule_command_tool_passes_delay_seconds_to_service() -> None:
    fake_cmd = object()
    create_schedule = MagicMock(return_value=fake_cmd)
    execute_command = MagicMock(
        return_value={"status": "success", "schedule_id": "sched-1", "schedule_type": "delayed"}
    )

    with patch(
        "motet.core.tools.builtin.schedule_command.get_runtime_stack",
        return_value=SimpleNamespace(_task_id="t1", _conversation_id="c1"),
    ), patch(
        "motet.core.workers.invoker_context.resolve_current_identity",
        return_value=SimpleNamespace(tenant_id="tenant", principal_id="user"),
    ), patch(
        "motet.core.commands.builtin.schedule.ScheduleCommandService.create_schedule",
        create_schedule,
    ), patch(
        "motet.core.workers.global_invoker.initialize",
    ), patch(
        "motet.core.workers.global_invoker.execute_command",
        execute_command,
    ):
        out = run(
            {
                "command_type": "core.agent_turn",
                "command_data": {"messages": [{"role": "user", "content": "hi"}]},
                "schedule_type": "delayed",
                "delay_seconds": 30,
                "name": "in-30s",
            }
        )

    assert out.get("status") == "success"
    assert (out.get("result") or {}).get("schedule_id") == "sched-1"
    kwargs = create_schedule.call_args.kwargs
    assert kwargs["delay_seconds"] == 30
    assert kwargs["scheduled_at"] is None


def test_resolve_delayed_scheduled_at_prefers_absolute() -> None:
    absolute = datetime(2026, 7, 27, 14, 30, tzinfo=timezone.utc)
    assert resolve_delayed_scheduled_at(absolute, 30) == absolute


def test_resolve_delayed_scheduled_at_applies_relative_delay() -> None:
    now = datetime(2026, 7, 27, 14, 0, 0, tzinfo=timezone.utc)
    resolved = resolve_delayed_scheduled_at(None, 90, now=now)
    assert resolved == now + timedelta(seconds=90)


def test_resolve_delayed_scheduled_at_rejects_non_positive() -> None:
    with pytest.raises(ValueError, match="positive"):
        resolve_delayed_scheduled_at(None, 0)
