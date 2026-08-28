"""Capability inference for local-only core.file_* built-ins."""

from __future__ import annotations

from motet.core.commands.capabilities import WorkerCapability
from motet.core.commands.builtin.tool import _infer_tool_capabilities
from motet.core.commands.command_data_classes import ToolExecutionData


def test_file_read_infer_capabilities() -> None:
    caps = _infer_tool_capabilities(
        ToolExecutionData(
            tool_name="core.file_read",
            parameters={"path": "/tmp/x"},
        )
    )
    assert WorkerCapability.EDGE_FILE_READ in caps
    assert WorkerCapability.EDGE_EXECUTION in caps
    assert WorkerCapability.TOOL_EXECUTION in caps
    assert WorkerCapability.FILE_OPERATIONS not in caps


def test_file_write_infer_capabilities() -> None:
    caps = _infer_tool_capabilities(
        ToolExecutionData(
            tool_name="core.file_write",
            parameters={"path": "/tmp/x", "content": "a"},
        )
    )
    assert WorkerCapability.EDGE_FILE_WRITE in caps
    assert WorkerCapability.EDGE_EXECUTION in caps


def test_file_search_infer_capabilities() -> None:
    caps = _infer_tool_capabilities(
        ToolExecutionData(
            tool_name="core.file_search",
            parameters={"root": "/tmp", "pattern": "*.py"},
        )
    )
    assert WorkerCapability.EDGE_FILE_SEARCH in caps
    assert WorkerCapability.EDGE_EXECUTION in caps


def test_file_edit_infer_capabilities() -> None:
    caps = _infer_tool_capabilities(
        ToolExecutionData(
            tool_name="core.file_edit",
            parameters={
                "path": "/tmp/x",
                "old_string": "a",
                "new_string": "b",
            },
        )
    )
    assert WorkerCapability.EDGE_FILE_WRITE in caps
    assert WorkerCapability.EDGE_EXECUTION in caps


def test_file_grep_infer_capabilities() -> None:
    caps = _infer_tool_capabilities(
        ToolExecutionData(
            tool_name="core.file_grep",
            parameters={"root": "/tmp", "pattern": "foo"},
        )
    )
    assert WorkerCapability.EDGE_FILE_SEARCH in caps
    assert WorkerCapability.EDGE_EXECUTION in caps
