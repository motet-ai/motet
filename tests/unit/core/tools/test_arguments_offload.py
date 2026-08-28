"""
Motet - Tool Arguments Offload Unit Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-07-17

Description:
    Unit tests for ADR-0061 oversized tool-argument offload and transcript hydration.
    Ensures we never mid-string truncate arguments for provider replay, store full JSON
    in artifacts, and fail-closed when artifacts are missing.

Dependencies:
    - pytest
    - motet.core.tools.arguments_offload
    - motet.core.types.ToolCallRequest / ToolCallResult

Usage:
    pytest tests/unit/core/tools/test_arguments_offload.py -q
"""

from __future__ import annotations

import json
from typing import Any, Dict

from motet.core.tools.arguments_offload import (
    ARGUMENTS_OFFLOADED_MARKER,
    arguments_unsafe_for_provider_replay,
    hash_arguments_json,
    hydrate_transcript_tool_arguments,
    plan_arguments_storage,
)
from motet.core.types import ToolCallRequest, ToolCallResult


def test_plan_arguments_storage_keeps_small_payload_inline() -> None:
    full = json.dumps({"path": "a.py", "content": "x" * 100})
    inline, digest, needs_artifact, truncated = plan_arguments_storage(full, 8192)
    assert inline == full
    assert digest == hash_arguments_json(full)
    assert needs_artifact is False
    assert truncated is False


def test_plan_arguments_storage_offloads_large_payload_with_valid_preview() -> None:
    full = json.dumps({"path": "big.py", "content": "Z" * 20_000})
    assert len(full.encode("utf-8")) > 8192
    inline, digest, needs_artifact, truncated = plan_arguments_storage(full, 8192)
    assert needs_artifact is True
    assert truncated is True
    assert digest == hash_arguments_json(full)
    assert "...[truncated]" not in inline
    preview = json.loads(inline)
    assert preview[ARGUMENTS_OFFLOADED_MARKER] is True
    assert preview["arguments_hash"] == digest
    assert preview["bytes"] == len(full.encode("utf-8"))


def test_hydrate_replaces_preview_with_full_arguments() -> None:
    full = json.dumps({"path": "big.py", "content": "hello-world"})
    preview, digest, _, _ = plan_arguments_storage(full, max_args_bytes=32)
    assert digest
    items = [
        ToolCallRequest(
            call_id="call_1",
            tool_name="core.file_edit",
            arguments_json=preview,
            arguments_artifact_id="art-args-1",
        ),
        ToolCallResult(
            call_id="call_1",
            tool_name="core.file_edit",
            output={"ok": True},
        ),
    ]
    store: Dict[str, str] = {"art-args-1": full}
    out = hydrate_transcript_tool_arguments(items, fetch_arguments=store.get)
    assert len(out) == 2
    assert isinstance(out[0], ToolCallRequest)
    assert out[0].arguments_json == full
    assert json.loads(out[0].arguments_json)["content"] == "hello-world"


def test_hydrate_omits_call_when_artifact_missing() -> None:
    preview, _, _, _ = plan_arguments_storage(json.dumps({"x": "y" * 5000}), 64)
    items = [
        ToolCallRequest(
            call_id="call_missing",
            tool_name="core.file_edit",
            arguments_json=preview,
            arguments_artifact_id="missing",
        ),
        ToolCallResult(call_id="call_missing", tool_name="core.file_edit", output={}),
        ToolCallRequest(
            call_id="call_ok",
            tool_name="core.file_read",
            arguments_json='{"path":"a.py"}',
        ),
    ]
    out = hydrate_transcript_tool_arguments(items, fetch_arguments=lambda _aid: None)
    assert len(out) == 1
    assert isinstance(out[0], ToolCallRequest)
    assert out[0].call_id == "call_ok"


def test_hydrate_omits_legacy_mid_string_truncation() -> None:
    broken = '{"content":"' + ("a" * 100) + "...[truncated]"
    assert arguments_unsafe_for_provider_replay(broken) is True
    items = [
        ToolCallRequest(
            call_id="legacy",
            tool_name="core.file_edit",
            arguments_json=broken,
        ),
        ToolCallResult(call_id="legacy", tool_name="core.file_edit", output={}),
    ]
    out = hydrate_transcript_tool_arguments(items, fetch_arguments=lambda _aid: None)
    assert out == []


def test_store_tool_arguments_artifact_uses_tool_arguments_kind() -> None:
    from motet.core.commands.builtin.artifacts import create_artifact
    from motet.core.commands.builtin.tool import _store_tool_arguments_artifact

    class FakeMotet:
        conversation_id = "conv-1"
        tenant_id = "t1"
        principal_id = "p1"
        do_command: Any = None
        do_data: Any = None

        def do(self, command: Any, data: Any) -> Dict[str, Any]:
            self.do_command = command
            self.do_data = data
            return {"artifact_id": "art-args-9"}

    motet = FakeMotet()
    full = json.dumps({"content": "Z" * 100})
    digest = hash_arguments_json(full)
    artifact_id = _store_tool_arguments_artifact(
        motet=motet,  # type: ignore[arg-type]
        full_arguments_json=full,
        tool_name="core.file_edit",
        tool_call_id="call_9",
        arguments_hash=digest,
    )
    assert artifact_id == "art-args-9"
    assert motet.do_command is create_artifact
    assert motet.do_data.kind == "tool_arguments"
    assert motet.do_data.trigger_derivations is False
    assert motet.do_data.include_text_derivation_for_json is False
    assert motet.do_data.payload.decode("utf-8") == full


def test_build_transcript_items_preserves_arguments_artifact_id() -> None:
    from motet.core.conversations.transcript_codec import build_transcript_items_for_turn
    from motet.core.tools.tool_transcripts import ToolInvocation, ToolInvocationStatus

    inv = ToolInvocation(
        tool_name="core.file_edit",
        tool_call_id="call_z",
        arguments_json='{"_motet_arguments_offloaded":true}',
        arguments_truncated=True,
        arguments_artifact_id="art-z",
        status=ToolInvocationStatus.SUCCESS,
        preview_observation="ok",
    )
    items = build_transcript_items_for_turn(messages=[], invocations=[inv])
    req = next(i for i in items if isinstance(i, ToolCallRequest))
    assert req.arguments_artifact_id == "art-z"


def test_openai_responses_replay_uses_hydrated_arguments() -> None:
    """End-to-end: hydrated transcript args survive Responses function_call formatting."""
    from motet.core.conversations.transcript_rendering import render_transcript_items_to_messages
    from motet.core.models.adapters.providers.openai_responses import _format_messages_for_openai
    from motet.core.types import RequestContext

    full = json.dumps({"path": "x.py", "content": "print(1)\n" + ("#" * 9000)})
    preview, _, _, _ = plan_arguments_storage(full, 8192)
    items = hydrate_transcript_tool_arguments(
        [
            ToolCallRequest(
                call_id="call_big",
                tool_name="core.file_edit",
                arguments_json=preview,
                arguments_artifact_id="art-big",
            ),
            ToolCallResult(call_id="call_big", tool_name="core.file_edit", output={"ok": True}),
        ],
        fetch_arguments=lambda _aid: full,
    )
    messages = render_transcript_items_to_messages(items)
    out_items = _format_messages_for_openai(
        messages=messages,
        model_name="grok-4.5",
        request_context=RequestContext(enable_multimodal=False),
    )
    fc = next(i for i in out_items if i.get("type") == "function_call")
    assert fc["arguments"] == full
    assert "...[truncated]" not in fc["arguments"]
    json.loads(fc["arguments"])  # must be valid JSON for xAI
