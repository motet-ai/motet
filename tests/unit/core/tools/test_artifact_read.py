"""
Motet - Artifact Read Tool Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-23

Description:
    Unit tests for the ADR-0120 core.artifact_read built-in tool, including
    raw-payload fallback for oversized tool_artifact offloads that skip
    text derivation.

Dependencies:
    - types.SimpleNamespace for lightweight artifact store stubs
    - motet.core.tools.builtin.artifact_read for tool behavior

Usage:
    pytest tests/unit/core/tools/test_artifact_read.py
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Optional

from motet.core.artifacts import ArtifactKind
from motet.core.tools.builtin import artifact_read as artifact_read_module


class _ArtifactMeta:
    def __init__(
        self,
        *,
        artifact_id: str,
        kind: str,
        source_artifact_id: Optional[str] = None,
        content_type: str = "text/plain",
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        self.id = artifact_id
        self.kind = kind
        self.source_artifact_id = source_artifact_id
        self.content_type = content_type
        self.metadata = metadata or {}


class _ArtifactStoreStub:
    def __init__(self) -> None:
        self._meta: dict[str, _ArtifactMeta] = {
            "source-1": _ArtifactMeta(
                artifact_id="source-1",
                kind=ArtifactKind.USER_UPLOAD.value,
                content_type="application/pdf",
                metadata={"filename": "notes.pdf"},
            ),
            "derived-1": _ArtifactMeta(
                artifact_id="derived-1",
                kind=ArtifactKind.DERIVED_TEXT.value,
                source_artifact_id="source-1",
            ),
        }
        self._payloads: dict[str, Any] = {
            "derived-1": "alpha beta gamma",
        }

    def get_metadata(self, artifact_id: str) -> Optional[_ArtifactMeta]:
        return self._meta.get(artifact_id)

    def find_derived(self, *, source_artifact_id: str, kind: Any) -> Optional[_ArtifactMeta]:
        for meta in self._meta.values():
            if meta.source_artifact_id == source_artifact_id and meta.kind == kind.value:
                return meta
        return None

    def get(self, artifact_id: str) -> Any:
        return self._payloads.get(artifact_id)


def test_artifact_read_resolves_source_to_derived_text(monkeypatch: Any) -> None:
    store = _ArtifactStoreStub()
    motet = SimpleNamespace(artifact_store=store)
    monkeypatch.setattr(artifact_read_module, "_get_motet_context_optional", lambda: motet)

    result = artifact_read_module.run({"artifact_id": "source-1"})

    assert result["status"] == "ok"
    assert result["source_artifact_id"] == "source-1"
    assert result["derived_artifact_id"] == "derived-1"
    assert "alpha beta gamma" in result["text"]
    assert "artifact_id='derived-1'" in result["context_text"]
    assert "read_source" not in result


def test_artifact_read_windowed_read(monkeypatch: Any) -> None:
    store = _ArtifactStoreStub()
    motet = SimpleNamespace(artifact_store=store)
    monkeypatch.setattr(artifact_read_module, "_get_motet_context_optional", lambda: motet)

    result = artifact_read_module.run(
        {"artifact_id": "derived-1", "offset_chars": 6, "max_chars": 4}
    )

    assert result["status"] == "ok"
    assert result["offset_chars"] == 6
    assert result["text"] == "beta"


def test_artifact_read_returns_not_ready_when_derivation_missing(monkeypatch: Any) -> None:
    store = _ArtifactStoreStub()
    store._meta["video-1"] = _ArtifactMeta(
        artifact_id="video-1",
        kind=ArtifactKind.USER_UPLOAD.value,
        content_type="video/mp4",
    )
    motet = SimpleNamespace(artifact_store=store)
    monkeypatch.setattr(artifact_read_module, "_get_motet_context_optional", lambda: motet)

    result = artifact_read_module.run({"artifact_id": "video-1"})

    assert result["status"] == "not_ready"
    assert result["source_artifact_id"] == "video-1"


def test_artifact_read_falls_back_to_raw_tool_artifact_payload(monkeypatch: Any) -> None:
    """Oversized tool offloads skip derivation; artifact_read must return raw JSON."""
    store = _ArtifactStoreStub()
    raw = b'{"content":[{"type":"text","text":"CNN homepage headlines..."}]}'
    store._meta["tool-1"] = _ArtifactMeta(
        artifact_id="tool-1",
        kind=ArtifactKind.TOOL_ARTIFACT.value,
        content_type="application/json",
        metadata={"filename": "mcp_playwright_browser_snapshot.json", "tool_name": "mcp.playwright.browser_snapshot"},
    )
    store._payloads["tool-1"] = raw
    motet = SimpleNamespace(artifact_store=store)
    monkeypatch.setattr(artifact_read_module, "_get_motet_context_optional", lambda: motet)

    result = artifact_read_module.run({"artifact_id": "tool-1"})

    assert result["status"] == "ok"
    assert result["read_source"] == "raw_payload"
    assert result["derived_artifact_id"] is None
    assert result["source_artifact_id"] == "tool-1"
    assert "CNN homepage headlines" in result["text"]
    assert "raw tool result payload" in result["context_text"]


def test_artifact_read_raw_fallback_supports_windowing(monkeypatch: Any) -> None:
    store = _ArtifactStoreStub()
    store._meta["tool-2"] = _ArtifactMeta(
        artifact_id="tool-2",
        kind=ArtifactKind.TOOL_ARTIFACT.value,
        content_type="application/json",
    )
    store._payloads["tool-2"] = b"abcdefghij"
    motet = SimpleNamespace(artifact_store=store)
    monkeypatch.setattr(artifact_read_module, "_get_motet_context_optional", lambda: motet)

    result = artifact_read_module.run(
        {"artifact_id": "tool-2", "offset_chars": 2, "max_chars": 4}
    )

    assert result["status"] == "ok"
    assert result["read_source"] == "raw_payload"
    assert result["text"] == "cdef"
    assert result["truncated"] is True


def test_artifact_read_observation_includes_text() -> None:
    text = "# Amazon RDS pricing\n\nOn-demand db.t3.medium is $0.072/hr."
    observation = artifact_read_module._format_observation(
        {
            "status": "ok",
            "text": text,
            "returned_chars": len(text),
            "total_chars": len(text),
        }
    )
    assert "On-demand db.t3.medium" in observation
    assert "artifact_read(status=" not in observation
