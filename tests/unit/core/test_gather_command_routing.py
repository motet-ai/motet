"""
Motet - Concurrency Routing Regression Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-23

Description:
    Regression tests for concurrency command routing behavior. Validates that
    GatherCommand, DispatchCommand, and MapCommand respect WorkerRouter output
    and fail fast for strict-routing commands (local capability / explicit target).

Dependencies:
    - pytest: Assertion framework and exception assertions
    - unittest.mock: Router/task signature mocking
    - motet.core.commands.concurrency: Commands under test
    - motet.core.commands.builtin.tool: Tool command factory

Usage:
    pytest -q tests/unit/core/test_gather_command_routing.py

Notes:
    - These tests mock Celery task signatures and do not require running workers.
    - Strict-routing guard prevents unsafe fallback to generic Celery routing.
"""

import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from motet.core.commands.concurrency import (
    DEFAULT_JOIN_TIMEOUT_SECONDS,
    GatherCommand,
    DispatchCommand,
    MapCommand,
    join_celery_timeout_seconds,
    join_wait_timeout_seconds,
)
from motet.core.commands.capabilities import WorkerCapability
from motet.core.commands.builtin.tool import tool_execution
from motet.core.commands.command_data_classes import ToolExecutionData


def _make_edge_tool_command(task_id: str = "task-1") -> object:
    """Create a command and enforce local capability for strict-routing tests."""
    child = tool_execution(
        data=ToolExecutionData(tool_name="core.clipboard_write", parameters={"text": "x"}),
        task_id=task_id,
        conversation_id="conv-1",
    )
    # Ensure strict-routing behavior is covered even if capability inference changes.
    child.distributed_context.required_capabilities = set(
        getattr(child.distributed_context, "required_capabilities", set())
    )
    child.distributed_context.required_capabilities.add(WorkerCapability.EDGE_EXECUTION)
    return child


def test_gather_routes_child_to_selected_worker_queue() -> None:
    """GatherCommand should honor WorkerRouter.route_command selected worker."""
    child = _make_edge_tool_command()
    gather = GatherCommand.create(
        commands=[child],
        task_id="task-1",
        conversation_id="conv-1",
    )

    mock_router = MagicMock()
    mock_router.route_command.return_value = SimpleNamespace(
        selected_worker={"worker_id": "edge_123"},
        strategy_used="least_loaded",
        error=None,
    )

    base_sig = MagicMock(name="base_sig")
    queued_sig = MagicMock(name="queued_sig")
    base_sig.set.return_value = queued_sig
    process_mock = MagicMock()
    process_mock.s.return_value = base_sig

    with patch("motet.core.workers.command_tasks.process_distributed_command", process_mock):
        routed = gather._create_routed_tasks([child], mock_router)

    assert len(routed) == 1
    base_sig.set.assert_called_once_with(queue="worker.edge_123")
    assert routed[0] is queued_sig


def test_gather_strict_routing_fails_when_no_worker_selected() -> None:
    """GatherCommand must fail fast for strict-routing commands."""
    child = _make_edge_tool_command(task_id="task-2")
    gather = GatherCommand.create(
        commands=[child],
        task_id="task-2",
        conversation_id="conv-1",
    )
    mock_router = MagicMock()
    mock_router.route_command.return_value = SimpleNamespace(
        selected_worker=None,
        strategy_used="least_loaded",
        error="No workers passed filtering",
    )

    with pytest.raises(RuntimeError, match="Strict routing required"):
        gather._create_routed_tasks([child], mock_router)


def test_dispatch_strict_routing_fails_when_no_worker_selected() -> None:
    """DispatchCommand must fail fast for strict-routing commands."""
    child = _make_edge_tool_command(task_id="task-3")
    dispatch = DispatchCommand.create(
        commands=[child],
        task_id="task-3",
        conversation_id="conv-1",
    )
    mock_router = MagicMock()
    mock_router.select_worker_for_command.return_value = None

    with pytest.raises(RuntimeError, match="Strict routing required"):
        dispatch._create_routed_tasks([child], mock_router)


def test_map_strict_routing_fails_when_no_worker_selected() -> None:
    """MapCommand must fail fast for strict-routing commands."""
    child = _make_edge_tool_command(task_id="task-4")
    map_cmd = MapCommand.create(
        command_type="tool_execution",
        inputs=[{"tool_name": "core.clipboard_write", "parameters": {"text": "x"}}],
        task_id="task-4",
        conversation_id="conv-1",
    )
    mock_router = MagicMock()
    mock_router.select_worker_for_command.return_value = None

    with pytest.raises(RuntimeError, match="Strict routing required"):
        map_cmd._create_routed_tasks([child], mock_router)


def test_map_aggregate_uses_nested_adr_status_for_failure() -> None:
    """MapCommand counts nested ADR error as failed even if event status is success."""
    map_cmd = MapCommand.create(
        command_type="core.reload_bundle",
        inputs=[{"bundle_id": "expert-panel"}],
        task_id="task-map-aggregate-1",
        conversation_id="conv-1",
    )

    completed_commands = {
        "cmd-1": {
            "command_id": "cmd-1",
            "status": "success",  # event transport status
            "result": {
                "status": "error",  # nested ADR-0029 command status (authoritative)
                "error": {
                    "type": "CommandExecutionError",
                    "message": "reload failed",
                    "details": {"bundle_id": "expert-panel"},
                },
            },
        }
    }

    aggregated = map_cmd._aggregate_batch_results(completed_commands, ordered_command_ids=["cmd-1"])

    assert aggregated["status"] == "error"
    assert aggregated["data"]["successful"] == 0
    assert aggregated["data"]["failed"] == 1
    assert aggregated["data"]["results"][0]["status"] == "error"
    assert aggregated["data"]["results"][0]["error"]["message"] == "reload failed"


def test_map_aggregate_reports_partial_success_from_nested_status() -> None:
    """MapCommand reports partial_success when nested ADR statuses are mixed."""
    map_cmd = MapCommand.create(
        command_type="core.reload_bundle",
        inputs=[{"bundle_id": "a"}, {"bundle_id": "b"}],
        task_id="task-map-aggregate-2",
        conversation_id="conv-1",
    )

    completed_commands = {
        "cmd-ok": {
            "command_id": "cmd-ok",
            "status": "success",
            "result": {"status": "success", "data": {"registered_tools": ["a.tool"]}},
        },
        "cmd-fail": {
            "command_id": "cmd-fail",
            "status": "success",
            "result": {
                "status": "error",
                "error": {"type": "RuntimeError", "message": "import failed", "details": {}},
            },
        },
    }

    aggregated = map_cmd._aggregate_batch_results(
        completed_commands,
        ordered_command_ids=["cmd-ok", "cmd-fail"],
    )

    assert aggregated["status"] == "partial_success"
    assert aggregated["data"]["successful"] == 1
    assert aggregated["data"]["failed"] == 1
    assert aggregated["data"]["results"][0]["status"] == "success"
    assert aggregated["data"]["results"][0]["data"] == {"registered_tools": ["a.tool"]}
    assert aggregated["data"]["results"][0]["metadata"]["command_id"] == "cmd-ok"
    assert aggregated["data"]["results"][1]["status"] == "error"


def test_join_celery_timeout_covers_child_plus_overhead() -> None:
    assert join_celery_timeout_seconds(requested=None, child_timeouts=[]) == DEFAULT_JOIN_TIMEOUT_SECONDS
    assert join_celery_timeout_seconds(requested=366, child_timeouts=[366]) == 396
    assert join_celery_timeout_seconds(requested=600, child_timeouts=[300]) == 600


def test_join_wait_timeout_leaves_persist_slack() -> None:
    assert join_wait_timeout_seconds(own_timeout=396, child_max=366) == 381.0


def test_gather_create_sets_celery_timeout_from_children() -> None:
    child = _make_edge_tool_command()
    child.distributed_context.timeout_seconds = 300
    gather = GatherCommand.create(
        commands=[child],
        task_id="task-timeout",
        conversation_id="conv-1",
    )
    assert gather.distributed_context.timeout_seconds == 330
