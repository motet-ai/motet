"""
Motet - Structured DOCX Preparation Strategy Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-05-07

Description:
    Unit tests for the ADR-0110 structure-aware DOCX preparation strategy. The
    tests build small DOCX payloads in memory and verify heading-aware section
    chunks, table coordinates, and deterministic selector priority.

Dependencies:
    - python-docx for creating in-memory DOCX payloads
    - motet.core.artifacts.preparation for strategy and selector contracts

Usage:
    pytest tests/unit/core/artifacts/preparation/test_docx_strategy.py

Notes:
    - Tests stay local and deterministic; no embedding service or Valkey Search
      runtime is needed for preparation-level coverage.
"""

from __future__ import annotations

import io
from types import SimpleNamespace

import pytest

from motet.core.artifacts.preparation.models import ArtifactPayloadInfo, TableCoord, TextCoord
from motet.core.artifacts.preparation.selector import ArtifactPrepSelector
from motet.core.artifacts.preparation.strategies.docx import (
    DOCX_CONTENT_TYPE,
    DOCX_STRUCTURED_STRATEGY_ID,
    DocxStructuredPreparationStrategy,
)
from motet.core.artifacts.preparation.strategy import ArtifactPrepContext

docx = pytest.importorskip("docx")


def _docx_payload() -> bytes:
    document = docx.Document()
    document.add_heading("EPIC 3: Artifact RAG", level=1)
    document.add_paragraph("Issue 3.1 describes chunking documents by their semantic structure.")
    document.add_paragraph("Acceptance criteria require clean citations without awkward mid-word starts.")
    document.add_heading("Issue 3.2: Tables", level=2)
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Field"
    table.cell(0, 1).text = "Value"
    table.cell(1, 0).text = "Strategy"
    table.cell(1, 1).text = "docx_structured"
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _context(payload: bytes) -> ArtifactPrepContext:
    return ArtifactPrepContext(
        artifact=SimpleNamespace(
            id="artifact-1",
            kind="source",
            metadata={"tags": ["rag"]},
            created_at=10.0,
            expires_at=20.0,
        ),
        payload=payload,
        payload_info=ArtifactPayloadInfo(
            content_type=DOCX_CONTENT_TYPE,
            extension=".docx",
            bytes=len(payload),
            filename="requirements.docx",
        ),
        tenant_id="tenant-1",
        principal_id="principal-1",
        motet_id="motet-1",
        conversation_id="conv-1",
        source_artifact_id="artifact-1",
        artifact_tags=["rag"],
        config={"chunk_size": 256},
    )


def test_selector_prefers_structured_docx_strategy() -> None:
    context = _context(_docx_payload())

    selection = ArtifactPrepSelector().select(context)

    assert selection.strategy.manifest.strategy_id == DOCX_STRUCTURED_STRATEGY_ID
    assert selection.plan.strategy_id == DOCX_STRUCTURED_STRATEGY_ID


def test_docx_strategy_emits_heading_and_table_chunks() -> None:
    context = _context(_docx_payload())
    strategy = DocxStructuredPreparationStrategy()
    plan = strategy.plan(context)

    result = strategy.prepare(plan, context)

    assert result.prep_state == "prep_complete"
    assert result.chunk_cache_key
    assert [chunk.chunk_kind for chunk in result.chunks] == ["section", "section", "table"]
    assert result.chunks[0].prep_strategy_id == DOCX_STRUCTURED_STRATEGY_ID
    assert isinstance(result.chunks[0].coordinates, TextCoord)
    assert result.chunks[0].coordinates.heading_path == ["EPIC 3: Artifact RAG"]
    assert "Issue 3.1 describes chunking" in result.chunks[0].content_text
    assert isinstance(result.chunks[1].coordinates, TextCoord)
    assert result.chunks[1].coordinates.heading_path == ["EPIC 3: Artifact RAG", "Issue 3.2: Tables"]
    assert isinstance(result.chunks[2].coordinates, TableCoord)
    assert result.chunks[2].coordinates.kind == "table"
    assert result.chunks[2].coordinates.headers == ["Field", "Value"]
    assert result.chunks[2].coordinates.range == "table:1"
    assert result.chunks[2].structured_payload is not None
    assert result.chunks[2].structured_payload["heading_path"] == ["EPIC 3: Artifact RAG", "Issue 3.2: Tables"]

