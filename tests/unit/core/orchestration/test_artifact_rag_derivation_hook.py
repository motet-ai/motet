"""
Motet - Artifact RAG Derivation Hook Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-05-16

Description:
    Unit tests for the ADR-0063 indexing dispatch hook that runs after derived
    text artifact creation succeeds.

Dependencies:
    - types.SimpleNamespace for Motet/config stubs
    - motet.core.commands.builtin.derivation for the hook under test

Usage:
    pytest tests/unit/core/orchestration/test_artifact_rag_derivation_hook.py

Notes:
    - The hook is intentionally tested directly to avoid exercising expensive
      PDF/OCR derivation paths.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Optional

from motet.core.commands.builtin.derivation import _dispatch_artifact_rag_index, _dispatch_artifact_rag_source_index


def _make_meta(
    *,
    id_: str,
    content_type: str,
    kind_value: str,
    source_artifact_id: Optional[str] = None,
    filename: Optional[str] = None,
) -> Any:
    md: dict = {}
    if filename:
        md["filename"] = filename
    return SimpleNamespace(
        id=id_,
        content_type=content_type,
        kind=SimpleNamespace(value=kind_value),
        source_artifact_id=source_artifact_id,
        checksum_sha256=f"checksum-{id_}",
        tenant_id="tenant-1",
        principal_id="principal-1",
        motet_id="motet-1",
        metadata=md,
        bytes=100,
        created_at=0.0,
    )


class _MotetStub:
    task_id = "task-1"
    conversation_id = "conv-1"
    tenant_id = "tenant-1"
    principal_id = "principal-1"
    motet_id = "motet-1"

    def __init__(
        self,
        enabled: bool,
        *,
        source_content_type: str = "text/plain",
        source_filename: str = "source.txt",
    ) -> None:
        self.stack = SimpleNamespace(
            config=SimpleNamespace(
                artifact_rag_enabled=enabled,
                artifact_rag_index_on_derivation=True,
                artifact_rag_chunk_size=3200,
                artifact_rag_chunk_overlap=400,
                artifact_prep_json_max_depth=3,
                artifact_rag_native_text_mode="auto",
            )
        )
        self.dispatched: list[Any] = []
        self._source = _make_meta(
            id_="source-1",
            content_type=source_content_type,
            kind_value="user_upload",
            filename=source_filename,
        )
        self._derived = _make_meta(
            id_="derived-1",
            content_type="text/plain",
            kind_value="derived_text",
            source_artifact_id="source-1",
        )
        self.artifact_store = SimpleNamespace(
            get_metadata=self._get_metadata,
            get=self._get_payload,
        )

    def _get_metadata(self, artifact_id: str) -> Any:
        if artifact_id == "source-1":
            return self._source
        if artifact_id == "derived-1":
            return self._derived
        return None

    def _get_payload(self, artifact_id: str) -> bytes:
        if artifact_id in ("source-1", "derived-1"):
            return b"hello world payload bytes"
        return b""

    def dispatch(self, commands: list[Any]) -> list[str]:
        self.dispatched.extend(commands)
        return ["task-child"]

    def log_fields(self, **extra: Any) -> dict[str, Any]:
        return extra


def test_dispatch_artifact_rag_index_respects_feature_flag() -> None:
    motet = _MotetStub(enabled=False)

    _dispatch_artifact_rag_index(
        motet=motet,
        source_artifact_id="source-1",
        derived_artifact_id="derived-1",
    )

    assert motet.dispatched == []


def test_dispatch_artifact_rag_index_builds_child_command_when_enabled() -> None:
    motet = _MotetStub(enabled=True, source_content_type="text/plain")

    _dispatch_artifact_rag_index(
        motet=motet,
        source_artifact_id="source-1",
        derived_artifact_id="derived-1",
    )

    assert len(motet.dispatched) == 1
    child = motet.dispatched[0]
    assert child.data.source_artifact_id == "source-1"
    assert child.data.derived_artifact_id == "derived-1"


def test_dispatch_artifact_rag_index_uses_source_for_docx_strategy() -> None:
    motet = _MotetStub(
        enabled=True,
        source_content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        source_filename="structured-plan.docx",
    )

    _dispatch_artifact_rag_index(
        motet=motet,
        source_artifact_id="source-1",
        derived_artifact_id="derived-1",
    )

    assert len(motet.dispatched) == 1
    child = motet.dispatched[0]
    assert child.data.source_artifact_id == "source-1"
    assert child.data.derived_artifact_id is None


def test_dispatch_artifact_rag_index_uses_source_for_pptx_strategy() -> None:
    motet = _MotetStub(
        enabled=True,
        source_content_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        source_filename="deck.pptx",
    )

    _dispatch_artifact_rag_index(
        motet=motet,
        source_artifact_id="source-1",
        derived_artifact_id="derived-1",
    )

    assert len(motet.dispatched) == 1
    child = motet.dispatched[0]
    assert child.data.source_artifact_id == "source-1"
    assert child.data.derived_artifact_id is None


def test_dispatch_artifact_rag_source_index_queues_pptx_after_empty_text() -> None:
    motet = _MotetStub(
        enabled=True,
        source_content_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        source_filename="deck.pptx",
    )

    _dispatch_artifact_rag_source_index(
        motet=motet,
        source_artifact_id="source-1",
        reason="derived_text_empty",
    )

    assert len(motet.dispatched) == 1
    child = motet.dispatched[0]
    assert child.data.source_artifact_id == "source-1"
    assert child.data.derived_artifact_id is None
    assert child.data.force_reindex is True
