"""
Motet - Tool Result Formatting Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-13

Description:
    Unit tests for shared MCP text unwrap in result_formatting
    (used by agentic-loop observations and core.transform mcp_text).

Dependencies:
    - pytest: Test runner
    - motet.core.tools.result_formatting: Helpers under test

Usage:
    pytest tests/unit/core/tools/test_result_formatting.py -q
"""

from motet.core.tools.result_formatting import extract_text_from_mcp_result
from motet.core.reasoning.react.loop_observations import (
    extract_text_from_mcp_result as loop_extract,
)


def test_content_array_text() -> None:
    text = extract_text_from_mcp_result(
        {"content": [{"type": "text", "text": '{"ok": true}'}]}
    )
    assert text == '{"ok": true}'


def test_structured_content_result() -> None:
    assert extract_text_from_mcp_result(
        {"structuredContent": {"result": "hello"}}
    ) == "hello"


def test_string_passthrough() -> None:
    assert extract_text_from_mcp_result('{"a": 1}') == '{"a": 1}'


def test_empty_returns_empty() -> None:
    assert extract_text_from_mcp_result(None) == ""
    assert extract_text_from_mcp_result({}) == ""


def test_loop_observations_reexports_same_helper() -> None:
    payload = {"content": [{"type": "text", "text": "same"}]}
    assert loop_extract(payload) == extract_text_from_mcp_result(payload)
