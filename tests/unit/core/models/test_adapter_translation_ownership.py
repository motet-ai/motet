"""
Motet - Adapter Translation Ownership Tests (ADR-0137)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-20

Description:
    Phase 0 allowlist and codec/Message/strict-tools contracts for ADR-0137.
    ``tool_canonical_to_wire`` / ``tool_wire_to_canonical`` may be called only
    from the permitted files (model outbound, inbound helper, OpenAI HTTP
    facade, definition module). Leftover ``tool_calls`` keys are discarded
    (issue #225). Also covers the MCP wire-name round-trip used by the live
    adapter matrix.

Dependencies:
    - ast: call-site scan of motet/ Python sources
    - motet.core.models.adapters.tool_call_codec
    - motet.core.types: Message, ToolCallRequest, CanonicalToolSchema

Usage:
    pytest tests/unit/core/models/test_adapter_translation_ownership.py
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from motet.core.commands.builtin.model import (
    _apply_wire_names,
    _apply_wire_names_to_messages,
    _tools_to_canonical,
)
from motet.core.models.adapters.provider_builtin_tools import tool_canonical_to_wire
from motet.core.models.adapters.tool_call_codec import (
    inbound_tool_call_request,
    tool_call_request_from_unknown,
    tool_calls_from_message,
)
from motet.core.types import CanonicalToolSchema, Message, ToolCallRequest


REPO_ROOT = Path(__file__).resolve().parents[4]
MOTET_ROOT = REPO_ROOT / "motet"
_CONVERT_DEFINITION = (
    MOTET_ROOT / "core" / "models" / "adapters" / "provider_builtin_tools.py"
)
_NAME_CONVERT_ALLOWLISTS = {
    "tool_canonical_to_wire": {
        MOTET_ROOT / "core" / "commands" / "builtin" / "model.py",
        MOTET_ROOT / "interfaces" / "api" / "openai_compat" / "translation.py",
        _CONVERT_DEFINITION,
    },
    "tool_wire_to_canonical": {
        MOTET_ROOT / "core" / "models" / "adapters" / "tool_call_codec.py",
        MOTET_ROOT / "interfaces" / "api" / "openai_compat" / "translation.py",
        MOTET_ROOT / "interfaces" / "api" / "openai_compat" / "execution.py",
        _CONVERT_DEFINITION,
    },
}


def _func_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _scan_name_convert_sites() -> dict[str, tuple[set[Path], set[Path]]]:
    """One walk of motet/: call sites and defs for both convert functions."""
    names = set(_NAME_CONVERT_ALLOWLISTS)
    found: dict[str, tuple[set[Path], set[Path]]] = {name: (set(), set()) for name in names}
    for path in MOTET_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        resolved = path.resolve()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = _func_name(node.func)
                if name in names:
                    found[name][0].add(resolved)
            elif isinstance(node, ast.FunctionDef) and node.name in names:
                found[node.name][1].add(resolved)
    return found


def test_name_convert_caller_allowlists() -> None:
    """Inbound and outbound name convert stay on the ADR-0137 / #225 sites."""
    sites = _scan_name_convert_sites()
    definition = _CONVERT_DEFINITION.resolve()
    for name, allowed_paths in _NAME_CONVERT_ALLOWLISTS.items():
        allowed = {path.resolve() for path in allowed_paths}
        call_files, def_files = sites[name]
        stray_calls = sorted(
            str(path.relative_to(REPO_ROOT)) for path in call_files if path not in allowed
        )
        stray_defs = sorted(
            str(path.relative_to(REPO_ROOT)) for path in def_files if path != definition
        )
        assert stray_calls == [], f"{name} call sites outside allowlist: {stray_calls}"
        assert stray_defs == [], f"{name} defined outside provider_builtin_tools: {stray_defs}"


def test_message_discards_leftover_tool_calls() -> None:
    """Issue #225: pre-0137 ``tool_calls`` keys are dropped, not lifted."""
    msg = Message(
        role="assistant",
        content="",
        tool_calls=[
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "mcp.github.list_repos", "arguments": '{"org":"x"}'},
            }
        ],
    )
    assert msg.tool_calls_canonical is None
    dumped = msg.model_dump()
    assert "tool_calls" not in dumped


def test_message_tool_calls_canonical_round_trip() -> None:
    req = ToolCallRequest(
        call_id="call_9",
        tool_name="core.web_search",
        arguments_json='{"q":"x"}',
        arguments={"q": "x"},
    )
    msg = Message(role="assistant", content="", tool_calls_canonical=[req])
    again = ToolCallRequest.model_validate(msg.tool_calls_canonical[0].model_dump())
    assert again.call_id == "call_9"
    assert again.tool_name == "core.web_search"


def test_codec_lifts_responses_and_canonical_dicts() -> None:
    cc = tool_call_request_from_unknown(
        {"id": "c1", "function": {"name": "core.help", "arguments": "{}"}}
    )
    assert cc is not None
    assert cc.call_id == "c1"
    assert cc.tool_name == "core.help"

    resp = tool_call_request_from_unknown(
        {"call_id": "c2", "name": "core.web_search", "arguments": '{"q":"a"}'}
    )
    assert resp is not None
    assert resp.call_id == "c2"
    assert resp.tool_name == "core.web_search"

    can = tool_call_request_from_unknown(
        {"call_id": "c3", "tool_name": "bundle.tool", "arguments_json": "{}"}
    )
    assert can is not None
    assert can.tool_name == "bundle.tool"


def test_inbound_helper_maps_wire_name() -> None:
    req = inbound_tool_call_request(
        call_id="call_1",
        tool_name="mcp__github__list_repos",
        arguments_json="{}",
        tool_call_index=2,
    )
    assert req.tool_name == "mcp.github.list_repos"
    assert req.tool_call_index == 2


def test_tool_calls_from_message_prefers_canonical() -> None:
    msg = Message(
        role="assistant",
        content="",
        tool_calls_canonical=[
            ToolCallRequest(call_id="a", tool_name="core.help", arguments_json="{}")
        ],
    )
    calls = tool_calls_from_message(msg)
    assert [c.tool_name for c in calls] == ["core.help"]


def test_tool_calls_from_message_ignores_leftover_key() -> None:
    calls = tool_calls_from_message(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call_1", "function": {"name": "core.help", "arguments": "{}"}}
            ],
        }
    )
    assert calls == []


def test_extract_tool_calls_from_response_ignores_leftover_key() -> None:
    from motet.core.tools.tool_calls_parser import extract_tool_calls_from_response

    leftover_only = extract_tool_calls_from_response(
        {"tool_calls": [{"id": "c1", "function": {"name": "core.help", "arguments": "{}"}}]}
    )
    assert leftover_only == []

    raw_only = extract_tool_calls_from_response(
        {"raw": {"tool_calls": [{"id": "c1", "function": {"name": "core.help", "arguments": "{}"}}]}}
    )
    assert raw_only == []

    canonical = extract_tool_calls_from_response(
        {
            "tool_calls_canonical": [
                {"call_id": "c1", "tool_name": "core.help", "arguments_json": "{}"}
            ]
        }
    )
    assert len(canonical) == 1
    assert canonical[0]["tool_name"] == "core.help"


def test_strict_tools_rejects_chat_completions_dict() -> None:
    with pytest.raises(ValueError, match="Unrecognized tool schema"):
        _tools_to_canonical(
            [{"type": "function", "function": {"name": "core.help", "parameters": {}}}],
            strict=True,
        )


def test_strict_tools_accepts_canonical_schema() -> None:
    schema = CanonicalToolSchema(name="core.help", description="d", json_schema={})
    out = _tools_to_canonical([schema], strict=True)
    assert out is not None
    assert out[0].name == "core.help"


def test_canonical_like_dict_accepted() -> None:
    out = _tools_to_canonical(
        [{"name": "core.help", "json_schema": {"type": "object"}, "description": "d"}],
        strict=True,
    )
    assert out is not None
    assert out[0].name == "core.help"


def test_model_apply_wire_names_mcp_round_trip() -> None:
    """Live-matrix path without spend: outbound wire + inbound canonical + replay."""
    canonical = "mcp.test.add_two_numbers"
    wire = tool_canonical_to_wire(canonical)
    assert wire == "mcp__test__add_two_numbers"

    schemas = _apply_wire_names(
        [CanonicalToolSchema(name=canonical, description="d", json_schema={})]
    )
    assert schemas is not None
    assert schemas[0].name == wire

    inbound = inbound_tool_call_request(
        call_id="c1",
        tool_name=wire,
        arguments_json='{"a": 7, "b": 5}',
    )
    assert inbound.tool_name == canonical

    replay = [
        Message(role="user", content="add"),
        Message(role="assistant", content="", tool_calls_canonical=[inbound]),
        Message(role="tool", name=canonical, content='{"sum": 12}', tool_call_id="c1"),
    ]
    wired_msgs = _apply_wire_names_to_messages(replay)
    assert wired_msgs is not None
    assert wired_msgs[1].tool_calls_canonical is not None
    assert wired_msgs[1].tool_calls_canonical[0].tool_name == wire
    assert wired_msgs[2].name == wire
