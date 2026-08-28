"""
Motet - Artifact RAG Chunker Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-05-05

Description:
    Unit tests for ADR-0110 text preparation chunking.

Dependencies:
    - motet.core.artifacts.preparation.strategies.text for text splitting behavior

Usage:
    pytest tests/unit/core/rag/test_chunker.py

Notes:
    - Tests avoid tokenizer dependencies and validate the worker-safe
      character-budget chunking implementation.
"""

from __future__ import annotations

from motet.core.artifacts.preparation.strategies.text import chunk_text_to_prepared_chunks


def test_chunk_text_preserves_identity_and_offsets() -> None:
    text = (
        "Page 1\n\n"
        + "\n\n".join(
            [
                "Paragraph with enough detail to exercise chunk boundaries and byte offsets. " * 3
                for _ in range(6)
            ]
        )
    )

    chunks = chunk_text_to_prepared_chunks(
        text,
        source_artifact_id="source-1",
        derived_artifact_id="derived-1",
        tenant_id="tenant-1",
        principal_id="principal-1",
        motet_id="motet-1",
        conversation_id="conv-1",
        filename="sample.pdf",
        chunk_size=256,
        chunk_overlap=32,
        created_at=10.0,
        expires_at=20.0,
    )

    assert len(chunks) >= 2
    assert chunks[0].source_artifact_id == "source-1"
    assert chunks[0].derived_artifact_id == "derived-1"
    assert chunks[0].tenant_id == "tenant-1"
    assert chunks[0].conversation_id == "conv-1"
    assert chunks[0].filename == "sample.pdf"
    assert chunks[0].coordinates.byte_start == 0
    assert chunks[0].coordinates.byte_end > chunks[0].coordinates.byte_start
    assert chunks[0].prep_strategy_id == "text_default"
    assert chunks[0].content_hash


def test_chunk_text_returns_empty_for_blank_text() -> None:
    chunks = chunk_text_to_prepared_chunks(
        "   ",
        source_artifact_id="source-1",
        derived_artifact_id="derived-1",
        tenant_id="tenant-1",
        principal_id="principal-1",
        motet_id="motet-1",
        conversation_id="conv-1",
    )

    assert chunks == []
