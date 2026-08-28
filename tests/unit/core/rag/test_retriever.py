"""
Motet - Artifact RAG Retriever Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-05-07

Description:
    Unit tests for ADR-0063 artifact RAG retrieval selection, including
    application-layer hybrid vector/keyword fusion that works without native
    Valkey TEXT fields.

Dependencies:
    - motet.core.rag.retriever for scoring and retrieval selection
    - motet.core.rag.types for structured chunk results and scopes

Usage:
    pytest tests/unit/core/rag/test_retriever.py

Notes:
    - Repository and embedding behavior are faked so the tests remain local and
      deterministic.
"""

from __future__ import annotations

from motet.core.rag.retriever import ArtifactRagRetriever
from motet.core.rag.types import ArtifactChunkSearchResult, ArtifactRetrievalScope
from motet.core.artifacts.preparation import TextCoord


def _chunk(
    *,
    source_artifact_id: str,
    chunk_index: int,
    content_text: str,
    similarity: float,
    filename: str = "sample.txt",
) -> ArtifactChunkSearchResult:
    return ArtifactChunkSearchResult(
        source_artifact_id=source_artifact_id,
        derived_artifact_id=f"derived-{source_artifact_id}",
        chunk_index=chunk_index,
        chunk_kind="text",
        content_text=content_text,
        content_hash=f"hash-{source_artifact_id}-{chunk_index}",
        coordinates=TextCoord(byte_start=0, byte_end=len(content_text.encode("utf-8"))),
        modality="text",
        prep_strategy_id="text_default",
        prep_strategy_version="1.0.0",
        content_type="text/plain",
        filename=filename,
        tenant_id="tenant-1",
        principal_id="principal-1",
        motet_id="motet-1",
        role="user",
        conversation_id="conv-1",
        created_at=1.0,
        vector_distance=max(0.0, 1.0 - similarity),
        similarity=similarity,
    )


def test_format_chunk_includes_heading_path() -> None:
    chunk = _chunk(
        source_artifact_id="docx-source",
        chunk_index=0,
        content_text="Structured DOCX section content.",
        similarity=0.88,
        filename="requirements.docx",
    )
    chunk.coordinates.heading_path = ["EPIC 3", "Issue 3.1"]

    formatted = ArtifactRagRetriever.format_chunk(chunk)

    assert "[Source: requirements.docx, section EPIC 3 > Issue 3.1;" in formatted


class _RepoStub:
    def __init__(self) -> None:
        self.vector_top_k = 0

    def search(self, **kwargs):  # noqa: ANN001
        self.vector_top_k = kwargs["top_k"]
        return [
            _chunk(
                source_artifact_id="semantic",
                chunk_index=0,
                content_text="General mission overview with semantically related planning notes.",
                similarity=0.92,
            )
        ]

    def list_scoped_chunks(self, **kwargs):  # noqa: ANN001
        return [
            _chunk(
                source_artifact_id="lexical",
                chunk_index=0,
                content_text="The Neptune launch budget is 42 million credits.",
                similarity=0.0,
                filename="neptune-launch.txt",
            )
        ]


class _AccountRepoStub:
    def __init__(self) -> None:
        self.vector_top_k = 0
        self._chunks = [
            _chunk(
                source_artifact_id="terms",
                chunk_index=5,
                content_text=(
                    "Payments and billing\n"
                    "Your account may be charged when subscription payments renew."
                ),
                similarity=0.26,
            ),
            _chunk(
                source_artifact_id="terms",
                chunk_index=3,
                content_text=(
                    "6.1 Your Account\n"
                    "Account registration requires accurate information and you are responsible "
                    "for activity under your account."
                ),
                similarity=0.34,
            ),
        ]

    def search(self, **kwargs):  # noqa: ANN001
        self.vector_top_k = kwargs["top_k"]
        return list(self._chunks)

    def list_scoped_chunks(self, **kwargs):  # noqa: ANN001
        return list(self._chunks)


def test_hybrid_retrieval_promotes_keyword_exact_match() -> None:
    repo = _RepoStub()
    retriever = ArtifactRagRetriever(repository=repo, embedding_fn=lambda _text: [0.1, 0.2, 0.3])

    selection = retriever.retrieve(
        query_text="What is the Neptune launch budget?",
        tenant_id="tenant-1",
        motet_id="motet-1",
        principal_id="principal-1",
        role="user",
        conversation_id="conv-1",
        scope=ArtifactRetrievalScope.CONVERSATION,
        top_k=1,
        vector_weight=0.4,
        lexical_weight=0.6,
        candidate_multiplier=3,
    )

    assert repo.vector_top_k == 3
    assert selection.chunks[0].source_artifact_id == "lexical"
    assert selection.chunks[0].lexical_score > 0.0
    assert selection.chunks[0].hybrid_score > 0.0
    assert "Neptune launch budget" in selection.context_text


def test_hybrid_rerank_ignores_generic_stopwords_and_boosts_section_headings() -> None:
    repo = _AccountRepoStub()
    retriever = ArtifactRagRetriever(repository=repo, embedding_fn=lambda _text: [0.1, 0.2, 0.3])

    selection = retriever.retrieve(
        query_text="What about my account?",
        tenant_id="tenant-1",
        motet_id="motet-1",
        principal_id="principal-1",
        role="user",
        conversation_id="conv-1",
        scope=ArtifactRetrievalScope.CONVERSATION,
        top_k=1,
        vector_weight=0.7,
        lexical_weight=0.3,
        candidate_multiplier=2,
    )

    assert ArtifactRagRetriever._meaningful_query_terms("What about my account?") == ["account"]
    assert selection.chunks[0].chunk_index == 3
    assert ArtifactRagRetriever.rerank_boost("What about my account?", selection.chunks[0]) > 0.0
    assert "6.1 Your Account" in selection.context_text


def test_vector_only_retrieval_preserves_similarity_ranking() -> None:
    repo = _RepoStub()
    retriever = ArtifactRagRetriever(repository=repo, embedding_fn=lambda _text: [0.1, 0.2, 0.3])

    selection = retriever.retrieve(
        query_text="mission planning",
        tenant_id="tenant-1",
        motet_id="motet-1",
        principal_id="principal-1",
        role="user",
        conversation_id="conv-1",
        scope=ArtifactRetrievalScope.CONVERSATION,
        top_k=1,
        hybrid_enabled=False,
    )

    assert repo.vector_top_k == 1
    assert selection.chunks[0].source_artifact_id == "semantic"
    assert selection.chunks[0].hybrid_score == selection.chunks[0].similarity
