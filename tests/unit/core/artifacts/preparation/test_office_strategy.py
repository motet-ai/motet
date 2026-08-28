"""
Motet - Office Preparation Strategy Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-05-07

Description:
    Unit tests for ADR-0110 office-document preparation, covering successful
    extraction, empty extraction, and extractor failures.

Dependencies:
    - pytest monkeypatch for deterministic text extraction
    - office preparation strategy models

Usage:
    pytest tests/unit/core/artifacts/preparation/test_office_strategy.py

Notes:
    - The extractor is patched so tests stay fast and do not depend on document
      parsing libraries.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from motet.core.artifacts.preparation.models import ArtifactPayloadInfo, TextCoord
from motet.core.artifacts.preparation.strategies.office import OfficeDocumentPreparationStrategy
from motet.core.artifacts.preparation.strategy import ArtifactPrepContext


def _context() -> ArtifactPrepContext:
    return ArtifactPrepContext(
        artifact=SimpleNamespace(id="artifact-1", kind="source", metadata={"tags": ["office"]}, created_at=1.0),
        payload=b"%PDF test",
        payload_info=ArtifactPayloadInfo(
            content_type="application/pdf",
            extension=".pdf",
            bytes=9,
            filename="paper.pdf",
            content_hash="source-hash",
        ),
        source_artifact_id="source-1",
        artifact_tags=["review"],
        tenant_id="tenant",
        principal_id="principal",
        motet_id="motet",
        conversation_id="conv",
        config={"chunk_size": 256, "chunk_overlap": 0},
    )


def test_office_strategy_chunks_extracted_text(monkeypatch: pytest.MonkeyPatch) -> None:
    from motet.core.media import text_extraction

    monkeypatch.setattr(
        text_extraction,
        "extract_text_from_bytes",
        lambda payload, content_type: "# Heading\n\nExtracted body text",
    )
    strategy = OfficeDocumentPreparationStrategy()
    context = _context()
    plan = strategy.plan(context)

    result = strategy.prepare(plan, context)

    assert result.prep_state == "prep_complete"
    assert len(result.chunks) == 1
    assert result.chunks[0].source_artifact_id == "source-1"
    assert result.chunks[0].artifact_tags == ["review"]
    assert isinstance(result.chunks[0].coordinates, TextCoord)
    assert result.chunks[0].coordinates.extraction_method == "office:application/pdf"
    assert result.chunk_cache_key


def test_office_strategy_reports_empty_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
    from motet.core.media import text_extraction

    monkeypatch.setattr(text_extraction, "extract_text_from_bytes", lambda payload, content_type: "")
    strategy = OfficeDocumentPreparationStrategy()
    context = _context()
    plan = strategy.plan(context)

    result = strategy.prepare(plan, context)

    assert result.prep_state == "prep_failed"
    assert result.chunks == []
    assert result.diagnostics == ["empty_office_text"]


def test_office_strategy_reports_extractor_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from motet.core.media import text_extraction

    def _raise(_payload: bytes, _content_type: str) -> str:
        raise RuntimeError("boom")

    monkeypatch.setattr(text_extraction, "extract_text_from_bytes", _raise)
    strategy = OfficeDocumentPreparationStrategy()
    context = _context()
    plan = strategy.plan(context)

    result = strategy.prepare(plan, context)

    assert result.prep_state == "prep_failed"
    assert result.chunks == []
    assert result.diagnostics == ["office_extract_failed: boom"]
