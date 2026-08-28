"""
Motet - Memories CLI Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    Unit tests for `motet-cli memories` commands that wrap Memories HTTP API
    endpoints (list, find, tag, forget, clear, vector-list).

Dependencies:
    - pytest: Test framework
    - click.testing: CliRunner
    - motet.cli.memories: memories_group

Usage:
    pytest tests/unit/cli/test_memories_cli.py
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest
from click.testing import CliRunner

from motet.cli.memories import memories_group


class _Resp:
    """Simple response stub with JSON payload."""

    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def json(self) -> Any:
        return self._payload


def _capture_api(monkeypatch: pytest.MonkeyPatch, payload: Any) -> List[Dict[str, Any]]:
    """Patch memories CLI HTTP helpers and capture requests."""

    calls: List[Dict[str, Any]] = []

    def fake_api_request(method: str, url: str, **kwargs: Any) -> _Resp:
        calls.append({"method": method, "url": url, **kwargs})
        return _Resp(payload)

    monkeypatch.setattr("motet_sdk.cli.memories.get_api_headers", lambda: {"Authorization": "Bearer t"})
    monkeypatch.setattr("motet_sdk.cli.memories.api_request", fake_api_request)
    return calls


def test_memories_list_calls_list_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _capture_api(monkeypatch, [{"id": "mem-1", "content": "hello"}])
    runner = CliRunner()

    result = runner.invoke(
        memories_group,
        ["list", "--limit", "3", "--tag", "type:note", "--api-url", "http://localhost:8000"],
    )

    assert result.exit_code == 0, result.output
    assert '"id": "mem-1"' in result.output
    assert calls[0]["method"] == "GET"
    assert calls[0]["url"] == "http://localhost:8000/api/v1/memories"
    assert calls[0]["params"] == {"limit": 3, "tag": "type:note"}


def test_memories_find_posts_tag_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _capture_api(monkeypatch, {"items": []})
    runner = CliRunner()

    result = runner.invoke(
        memories_group,
        [
            "find",
            "--tags",
            "important,reviewed",
            "--match",
            "all",
            "--types",
            "note,summary",
            "--api-url",
            "http://localhost:8000",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"] == "http://localhost:8000/api/v1/memories/find"
    assert calls[0]["json"]["tags"] == ["important", "reviewed"]
    assert calls[0]["json"]["match"] == "all"
    assert calls[0]["json"]["types"] == ["note", "summary"]


def test_memories_tag_requires_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    _capture_api(monkeypatch, {})
    runner = CliRunner()

    result = runner.invoke(
        memories_group,
        ["tag", "--tags", "important", "--api-url", "http://localhost:8000"],
    )

    assert result.exit_code != 0
    assert "Provide --memory-id" in result.output


def test_memories_tag_posts_body(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _capture_api(monkeypatch, {"updated": 1})
    runner = CliRunner()

    result = runner.invoke(
        memories_group,
        [
            "tag",
            "--tags",
            "important",
            "--memory-id",
            "mem-1",
            "--op",
            "add",
            "--api-url",
            "http://localhost:8000",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"] == "http://localhost:8000/api/v1/memories/tag"
    assert calls[0]["json"] == {
        "tags": ["important"],
        "op": "add",
        "memory_ids": ["mem-1"],
    }


def test_memories_forget_requires_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    _capture_api(monkeypatch, {})
    runner = CliRunner()

    result = runner.invoke(
        memories_group,
        ["forget", "--api-url", "http://localhost:8000"],
    )

    assert result.exit_code != 0
    assert "Provide --memory-id" in result.output


def test_memories_forget_posts_body(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _capture_api(monkeypatch, {"deleted": 1})
    runner = CliRunner()

    result = runner.invoke(
        memories_group,
        [
            "forget",
            "--memory-id",
            "mem-1",
            "--api-url",
            "http://localhost:8000",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"] == "http://localhost:8000/api/v1/memories/forget"
    assert calls[0]["json"] == {"memory_ids": ["mem-1"]}


def test_memories_clear_aborts_without_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _capture_api(monkeypatch, {})
    runner = CliRunner()

    result = runner.invoke(
        memories_group,
        ["clear", "--tag", "type:note", "--api-url", "http://localhost:8000"],
        input="n\n",
    )

    assert result.exit_code != 0
    assert "Clear matching memories" in result.output
    assert calls == []


def test_memories_clear_posts_params(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _capture_api(monkeypatch, {"memory": 2, "vector": 1})
    runner = CliRunner()

    result = runner.invoke(
        memories_group,
        [
            "clear",
            "--tag",
            "type:note",
            "--clear-vector",
            "--yes",
            "--api-url",
            "http://localhost:8000",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"] == "http://localhost:8000/api/v1/memories/clear"
    assert calls[0]["params"] == {"clear_vector": True, "tag": "type:note"}


def test_memories_vector_list(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _capture_api(monkeypatch, [{"id": "vec-1"}])
    runner = CliRunner()

    result = runner.invoke(
        memories_group,
        [
            "vector-list",
            "--tag",
            "conversation",
            "--limit",
            "4",
            "--api-url",
            "http://localhost:8000",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls[0]["method"] == "GET"
    assert calls[0]["url"] == "http://localhost:8000/api/v1/memories/vector/list"
    assert calls[0]["params"] == {"limit": 4, "tag": "conversation"}
