"""
Motet - Artifact Search Tool Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-05-05

Description:
    Unit tests for the ADR-0063 agent-facing artifact search tool. The tests
    validate that the tool delegates to scoped RAG retrieval and keeps broader
    retrieval scopes behind deterministic execution metadata.

Dependencies:
    - pytest monkeypatch for MotetContext stubbing
    - motet.core.tools.builtin.search_artifacts for tool behavior

Usage:
    pytest tests/unit/core/tools/test_search_artifacts_tool.py

Notes:
    - These tests call the built-in tool function directly; distributed routing
      is covered by command and integration tests.
"""

from __future__ import annotations

from typing import Any

from motet.core.tools.builtin import search_artifacts


class _MotetStub:
    def __init__(self, *, metadata: dict[str, Any] | None = None) -> None:
        self.metadata = metadata or {}
        self.conversation_id = "conv-1"
        self.request_data = None

    def do(self, _command: Any, *, data: Any) -> dict[str, Any]:
        self.request_data = data
        return {
            "chunks": [{"source_artifact_id": "source-1", "content_text": "refund policy"}],
            "chunk_count": 1,
            "context_text": "[Source: policy.pdf] refund policy",
            "token_budget": 3000,
            "hybrid_enabled": True,
        }


def _captured_request_data(motet: _MotetStub) -> Any:
    assert motet.request_data is not None
    return motet.request_data


def test_search_artifacts_defaults_to_conversation_scope(monkeypatch) -> None:  # noqa: ANN001
    motet = _MotetStub()
    monkeypatch.setattr(search_artifacts, "_get_motet_context_optional", lambda: motet)

    result = search_artifacts.run({
        "query": "What does the PDF say about refunds?",
        "artifact_tags": ["policy"],
    })

    assert result["status"] == "ok"
    assert result["resolved_scope"] == "conversation"
    assert result["chunk_count"] == 1
    request_data = _captured_request_data(motet)
    assert request_data.scope == "conversation"
    assert request_data.artifact_tags == ["policy"]
    assert request_data.conversation_id == "conv-1"


def test_search_artifacts_downgrades_broader_scope_without_policy(monkeypatch) -> None:  # noqa: ANN001
    motet = _MotetStub()
    monkeypatch.setattr(search_artifacts, "_get_motet_context_optional", lambda: motet)

    result = search_artifacts.run({"query": "Search my documents for refunds.", "scope": "principal"})

    assert result["requested_scope"] == "principal"
    assert result["resolved_scope"] == "conversation"
    assert result["scope_downgraded"] is True
    request_data = _captured_request_data(motet)
    assert request_data.scope == "conversation"


def test_search_artifacts_allows_broader_scope_with_policy_metadata(monkeypatch) -> None:  # noqa: ANN001
    motet = _MotetStub(metadata={"artifact_rag_scope": "principal"})
    monkeypatch.setattr(search_artifacts, "_get_motet_context_optional", lambda: motet)

    result = search_artifacts.run({"query": "Search my documents for refunds.", "scope": "principal"})

    assert result["resolved_scope"] == "principal"
    assert result["scope_downgraded"] is False
    request_data = _captured_request_data(motet)
    assert request_data.scope == "principal"
