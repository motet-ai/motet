"""
Motet - ToolInvocation Persistence Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-27

Description:
    Unit tests for ToolInvocation persistence semantics (ADR-0061).

    Specifically validates that ToolInvocation records are persisted via an upsert
    key derived from (conversation_id, tool_call_id) so a single logical tool call
    transitions from STARTED -> terminal status without creating duplicate memory items.
    Also covers oversized-result offload policy (_should_store_oversized_tool_result)
    and TTL / derivation flags on _store_tool_artifact.

Dependencies:
    - pytest: test runner
    - motet.core.commands.builtin.tool: ToolInvocation persistence helper
    - motet.core.tools.tool_transcripts: ToolInvocation model and status enum

Usage:
    pytest tests/unit/core/orchestration/test_tool_invocation_persistence.py

Notes:
    - This test intentionally does not require Redis or docker-compose; it uses fakes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class FakeMotet:
    tenant_id: str = "tenant-a"
    principal_id: str = "principal-1"
    motet_id: str = "default"
    task_id: str = "task-123"
    command_id: str = "cmd-123"
    parent_command_id: str = "cmd-parent"
    conversation_id: str = "conv-xyz"
    memory: Any = None


class FakeMemoryManager:
    def __init__(self) -> None:
        self.last_item_id: Optional[str] = None
        self.last_type: Optional[str] = None
        self.last_metadata: Optional[Dict[str, Any]] = None

    def store_memory(self, *, item_id: Optional[str] = None, type: str, metadata: Dict[str, Any], **kwargs) -> Dict[str, Any]:
        self.last_item_id = item_id
        self.last_type = type
        self.last_metadata = metadata
        return {"id": item_id or "generated-id"}


class FakeStack:
    def __init__(self) -> None:
        self.config = type("Config", (), {})()


class FakeMotetWithDo(FakeMotet):
    def __init__(self) -> None:
        super().__init__()
        self.do_command: Any = None
        self.do_data: Any = None

    def do(self, command: Any, data: Any) -> Dict[str, Any]:
        self.do_command = command
        self.do_data = data
        return {"artifact_id": "artifact-tool-1"}


def test_persist_tool_invocation_uses_stable_item_id() -> None:
    from motet.core.tools.tool_transcripts import ToolInvocation, ToolInvocationStatus
    from motet.core.commands.builtin.tool import _persist_tool_invocation

    fake_motet = FakeMotet()
    fake_mem = FakeMemoryManager()
    fake_motet.memory = fake_mem

    inv = ToolInvocation(
        tool_name="mcp.google_workspace.list_docs_in_folder",
        tool_call_id="call_abc123",
        provider="mcp",
        status=ToolInvocationStatus.STARTED,
        conversation_id=fake_motet.conversation_id,
        tenant_id=fake_motet.tenant_id,
        principal_id=fake_motet.principal_id,
        motet_id=fake_motet.motet_id,
        task_id=fake_motet.task_id,
        command_id=fake_motet.command_id,
        parent_command_id=fake_motet.parent_command_id,
    )

    _persist_tool_invocation(stack=object(), motet=fake_motet, invocation=inv)

    assert fake_mem.last_type == "tool_invocation"
    assert fake_mem.last_item_id == "tool_invocation:conv-xyz:call_abc123"
    assert isinstance(fake_mem.last_metadata, dict)
    assert fake_mem.last_metadata.get("tool_call_id") == "call_abc123"


def test_store_tool_artifact_uses_artifact_lifecycle_command() -> None:
    import json

    from motet.core.commands.builtin.artifacts import create_artifact
    from motet.core.commands.builtin.tool import _store_tool_artifact

    fake_motet = FakeMotetWithDo()
    artifact_id = _store_tool_artifact(
        stack=FakeStack(),
        motet=fake_motet,
        raw_result={"status": "success", "rows": [{"name": "alpha"}]},
        tool_name="core.search",
        tool_call_id="call_123",
        content_type="application/json",
    )

    assert artifact_id == "artifact-tool-1"
    assert fake_motet.do_command is create_artifact
    assert fake_motet.do_data.kind == "tool_artifact"
    assert fake_motet.do_data.content_type == "application/json"
    assert fake_motet.do_data.conversation_id == "conv-xyz"
    assert fake_motet.do_data.ttl_seconds is None
    assert fake_motet.do_data.trigger_derivations is True
    assert fake_motet.do_data.include_text_derivation_for_json is True
    assert fake_motet.do_data.metadata["tool_name"] == "core.search"
    assert fake_motet.do_data.metadata["tool_call_id"] == "call_123"
    assert fake_motet.do_data.metadata["conversation_id"] == "conv-xyz"
    assert json.loads(fake_motet.do_data.payload.decode("utf-8"))["rows"][0]["name"] == "alpha"


def test_store_tool_artifact_oversized_ttl_and_no_derivations() -> None:
    """Oversized offloads expire and skip derivations (Redis growth bound)."""
    from motet.core.commands.builtin.tool import _store_tool_artifact

    fake_motet = FakeMotetWithDo()
    serialized = '{"status": "success", "big": "x"}'
    artifact_id = _store_tool_artifact(
        stack=FakeStack(),
        motet=fake_motet,
        raw_result={"status": "success", "big": "x"},
        tool_name="core.file_read",
        tool_call_id="call_456",
        serialized_result=serialized,
        ttl_seconds=604800,
        trigger_derivations=False,
    )

    assert artifact_id == "artifact-tool-1"
    assert fake_motet.do_data.ttl_seconds == 604800
    assert fake_motet.do_data.trigger_derivations is False
    assert fake_motet.do_data.include_text_derivation_for_json is False
    # Pre-serialized payload is reused verbatim (no second json.dumps).
    assert fake_motet.do_data.payload == serialized.encode("utf-8")


def _stack_with_config(**overrides: object) -> FakeStack:
    stack = FakeStack()
    stack.config = type("Config", (), {
        "store_tool_artifacts": True,
        "tool_artifact_denylist": None,
        "tool_result_artifact_min_bytes": 8192,
        **overrides,
    })()
    return stack


def test_should_store_oversized_tool_result_size_threshold() -> None:
    from motet.core.commands.builtin.tool import _should_store_oversized_tool_result

    stack = _stack_with_config()
    assert _should_store_oversized_tool_result(
        stack=stack, tool_name="core.file_read", result_size_bytes=8192
    )
    assert not _should_store_oversized_tool_result(
        stack=stack, tool_name="core.file_read", result_size_bytes=8191
    )


def test_should_store_oversized_tool_result_respects_deny() -> None:
    from motet.core.commands.builtin.tool import _should_store_oversized_tool_result

    # Sensitive-name heuristic denies regardless of size.
    stack = _stack_with_config()
    assert not _should_store_oversized_tool_result(
        stack=stack, tool_name="core.oauth_fetch", result_size_bytes=100_000  # gitleaks:allow
    )
    # Explicit denylist denies.
    stack = _stack_with_config(tool_artifact_denylist="core.file_read")
    assert not _should_store_oversized_tool_result(
        stack=stack, tool_name="core.file_read", result_size_bytes=100_000
    )
    # Disabled store denies.
    stack = _stack_with_config(store_tool_artifacts=False)
    assert not _should_store_oversized_tool_result(
        stack=stack, tool_name="core.file_read", result_size_bytes=100_000
    )

