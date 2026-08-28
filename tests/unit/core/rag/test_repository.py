"""
Motet - Artifact RAG Repository Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-17

Description:
    Unit tests for ADR-0063 Valkey Search command construction, fail-closed
    retrieval filters, deterministic keys, and response parsing.

Dependencies:
    - unittest.mock for fake Redis clients
    - pytest for failure assertions
    - motet.core.rag.repository for repository behavior

Usage:
    pytest tests/unit/core/rag/test_repository.py

Notes:
    - Redis/Valkey is mocked; no integration services are required.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from motet.core.rag.repository import ArtifactChunkRepository
from motet.core.rag.types import ArtifactRetrievalScope


def test_chunk_key_is_deterministic() -> None:
    key = ArtifactChunkRepository.chunk_key(
        tenant_id="tenant-1",
        source_artifact_id="source-1",
        chunk_index=3,
    )

    assert key == "tenant-1:artifact_chunk:tenant-1:source-1:text_default:3"


def test_build_search_command_enforces_conversation_scope() -> None:
    repo = ArtifactChunkRepository(redis_client=MagicMock(), embedding_dim=3)

    command = repo.build_search_command(
        query_embedding=[0.1, 0.2, 0.3],
        tenant_id="tenant-1",
        motet_id="motet-1",
        principal_id="principal-1",
        role="user",
        conversation_id="conv-1",
        scope=ArtifactRetrievalScope.CONVERSATION,
        artifact_ids=["source-1"],
        artifact_tags=["contracts", "project:alpha"],
        top_k=4,
    )

    assert command[0] == "FT.SEARCH"
    assert command[1] == "artifact_chunks:tenant-1"
    query = command[2]
    assert "@tenant_id:{tenant-1}" in query
    assert "@principal_id:{principal-1}" in query
    assert "@motet_id:{motet-1}" in query
    assert "@role:{user}" in query
    assert "@conversation_id:{conv-1}" in query
    assert "@source_artifact_id:{source-1}" in query
    assert "@artifact_tags:{contracts}" in query
    assert "@artifact_tags:{project:alpha}" in query
    assert "KNN 4" in query


def test_build_search_command_fails_closed_without_conversation() -> None:
    repo = ArtifactChunkRepository(redis_client=MagicMock(), embedding_dim=3)

    with pytest.raises(ValueError, match="conversation_id is required"):
        repo.build_search_command(
            query_embedding=[0.1, 0.2, 0.3],
            tenant_id="tenant-1",
            motet_id="motet-1",
            principal_id="principal-1",
            role="user",
            conversation_id="",
            scope=ArtifactRetrievalScope.CONVERSATION,
        )


def test_ensure_index_adds_text_fields_when_probe_succeeds() -> None:
    redis_client = MagicMock()
    redis_client.execute_command.side_effect = [
        Exception("missing index"),
        "OK",  # TEXT probe FT.CREATE
        "OK",  # TEXT probe FT.DROPINDEX
        "OK",  # real FT.CREATE
    ]
    repo = ArtifactChunkRepository(redis_client=redis_client, embedding_dim=3, native_text_mode="auto")

    repo.ensure_index(tenant_id="tenant-1")

    real_create = redis_client.execute_command.call_args_list[-1].args
    assert "filename" in real_create
    assert "content_text" in real_create
    assert "TEXT" in real_create


def test_ensure_index_omits_text_fields_when_probe_fails() -> None:
    redis_client = MagicMock()
    redis_client.execute_command.side_effect = [
        Exception("missing index"),
        Exception("TEXT unsupported"),
        Exception("probe index absent"),
        "OK",
    ]
    repo = ArtifactChunkRepository(redis_client=redis_client, embedding_dim=3, native_text_mode="auto")

    repo.ensure_index(tenant_id="tenant-1")

    real_create = redis_client.execute_command.call_args_list[-1].args
    assert "filename" not in real_create
    assert "content_text" not in real_create


def test_indexed_field_names_parses_flat_and_nested_ft_info() -> None:
    flat = ["identifier", "tenant_id", "type", "TAG", "identifier", "artifact_tags", "type", "TAG"]
    nested = ["attributes", ["identifier", b"embedding", "type", "VECTOR", "identifier", "role"]]
    assert ArtifactChunkRepository._indexed_field_names(flat) == {"tenant_id", "artifact_tags"}
    assert ArtifactChunkRepository._indexed_field_names(nested) == {"embedding", "role"}


def test_ensure_index_noop_when_schema_complete() -> None:
    # Minimal complete set of required field identifiers
    from motet.core.rag.repository import _REQUIRED_INDEX_FIELD_NAMES

    attrs: list[Any] = []
    for name in sorted(_REQUIRED_INDEX_FIELD_NAMES):
        attrs.extend(["identifier", name, "type", "TAG"])
    info = ["attributes", attrs, ArtifactChunkRepository.key_prefix("tenant-1")]
    redis_client = MagicMock()
    redis_client.execute_command.return_value = info
    repo = ArtifactChunkRepository(redis_client=redis_client, embedding_dim=3, native_text_mode="disabled")

    repo.ensure_index(tenant_id="tenant-1")

    redis_client.execute_command.assert_called_once_with("FT.INFO", "artifact_chunks:tenant-1")


def test_ensure_index_migrates_stale_schema_missing_artifact_tags() -> None:
    stale_info = [
        "attributes",
        [
            "identifier",
            "tenant_id",
            "type",
            "TAG",
            "identifier",
            "embedding",
            "type",
            "VECTOR",
            "identifier",
            "source_artifact_id",
            "type",
            "TAG",
            "identifier",
            "principal_id",
            "type",
            "TAG",
            "identifier",
            "motet_id",
            "type",
            "TAG",
            "identifier",
            "role",
            "type",
            "TAG",
            "identifier",
            "conversation_id",
            "type",
            "TAG",
            "identifier",
            "prep_strategy_id",
            "type",
            "TAG",
            "identifier",
            "chunk_cache_key",
            "type",
            "TAG",
            # artifact_tags intentionally missing
        ],
    ]
    redis_client = MagicMock()
    redis_client.execute_command.side_effect = [
        stale_info,  # FT.INFO
        "OK",  # FT.DROPINDEX
        "OK",  # FT.CREATE (native text disabled)
    ]
    repo = ArtifactChunkRepository(redis_client=redis_client, embedding_dim=3, native_text_mode="disabled")

    repo.ensure_index(tenant_id="motet-global")

    calls = [c.args for c in redis_client.execute_command.call_args_list]
    assert calls[0][:2] == ("FT.INFO", "artifact_chunks:motet-global")
    assert calls[1][:2] == ("FT.DROPINDEX", "artifact_chunks:motet-global")
    assert calls[2][0] == "FT.CREATE"
    assert "artifact_tags" in calls[2]
    assert "TAG" in calls[2]


def test_parse_search_response_returns_chunk_results() -> None:
    repo = ArtifactChunkRepository(redis_client=MagicMock(), embedding_dim=3)
    response = [
        1,
        b"artifact_chunk:tenant-1:source-1:0",
        [
            b"source_artifact_id",
            b"source-1",
            b"derived_artifact_id",
            b"derived-1",
            b"chunk_index",
            b"0",
            b"chunk_kind",
            b"text",
            b"content_text",
            b"Relevant text",
            b"coordinates",
            b'{"byte_end":13,"byte_start":0,"kind":"text","page_number":2}',
            b"prep_strategy_id",
            b"text_default",
            b"prep_strategy_version",
            b"1.0.0",
            b"modality",
            b"text",
            b"confidence",
            b"1.0",
            b"prep_state",
            b"prep_complete",
            b"index_state",
            b"index_complete",
            b"chunk_cache_key",
            b"cache",
            b"content_hash",
            b"hash",
            b"byte_range_start",
            b"0",
            b"byte_range_end",
            b"13",
            b"page_number",
            b"2",
            b"content_type",
            b"application/pdf",
            b"artifact_tags",
            b"contracts,project:alpha",
            b"filename",
            b"sample.pdf",
            b"tenant_id",
            b"tenant-1",
            b"principal_id",
            b"principal-1",
            b"motet_id",
            b"motet-1",
            b"role",
            b"user",
            b"conversation_id",
            b"conv-1",
            b"created_at",
            b"10",
            b"expires_at",
            b"20",
            b"vector_distance",
            b"0.25",
        ],
    ]

    results = repo._parse_search_response(response)

    assert len(results) == 1
    assert results[0].source_artifact_id == "source-1"
    assert results[0].derived_artifact_id == "derived-1"
    assert results[0].content_text == "Relevant text"
    assert results[0].artifact_tags == ["contracts", "project:alpha"]
    assert getattr(results[0].coordinates, "page_number", None) == 2
    assert results[0].similarity == 0.75


def test_list_scoped_chunks_filters_hash_candidates() -> None:
    redis_client = MagicMock()
    redis_client.scan.side_effect = [
        (0, []),
        (0, [b"artifact_chunk:tenant-1:source-1:0"]),
    ]
    redis_client.hgetall.return_value = {
        b"source_artifact_id": b"source-1",
        b"derived_artifact_id": b"derived-1",
        b"chunk_index": b"0",
        b"chunk_kind": b"text",
        b"content_text": b"Relevant budget text",
        b"coordinates": b'{"byte_end":20,"byte_start":0,"kind":"text"}',
        b"prep_strategy_id": b"text_default",
        b"prep_strategy_version": b"1.0.0",
        b"modality": b"text",
        b"confidence": b"1.0",
        b"prep_state": b"prep_complete",
        b"index_state": b"index_complete",
        b"chunk_cache_key": b"cache",
        b"content_hash": b"hash",
        b"byte_range_start": b"0",
        b"byte_range_end": b"20",
        b"page_number": b"0",
        b"content_type": b"text/plain",
        b"artifact_tags": b"budget,project:alpha",
        b"filename": b"budget.txt",
        b"tenant_id": b"tenant-1",
        b"principal_id": b"principal-1",
        b"motet_id": b"motet-1",
        b"role": b"user",
        b"conversation_id": b"conv-1",
        b"created_at": b"10",
        b"expires_at": b"",
    }
    repo = ArtifactChunkRepository(redis_client=redis_client, embedding_dim=3)

    results = repo.list_scoped_chunks(
        tenant_id="tenant-1",
        motet_id="motet-1",
        principal_id="principal-1",
        role="user",
        conversation_id="conv-1",
        scope=ArtifactRetrievalScope.CONVERSATION,
        artifact_tags=["project:alpha"],
    )

    assert len(results) == 1
    assert results[0].source_artifact_id == "source-1"
    assert results[0].artifact_tags == ["budget", "project:alpha"]
    assert results[0].content_text == "Relevant budget text"


def test_count_source_chunks_scans_matching_source_keys() -> None:
    redis_client = MagicMock()
    redis_client.scan.side_effect = [
        (0, []),
        (2, [b"artifact_chunk:tenant-1:source-1:text_default:0"]),
        (0, [b"artifact_chunk:tenant-1:source-1:text_default:1"]),
    ]
    repo = ArtifactChunkRepository(redis_client=redis_client, embedding_dim=3)

    count = repo.count_source_chunks(tenant_id="tenant-1", source_artifact_id="source-1")

    assert count == 2
    assert redis_client.scan.call_args_list[0].kwargs["match"] == (
        "tenant-1:artifact_chunk:tenant-1:source-1:*"
    )
    assert redis_client.scan.call_args_list[1].kwargs["match"] == (
        "artifact_chunk:tenant-1:source-1:*"
    )


def test_count_source_chunks_returns_zero_without_scope() -> None:
    redis_client = MagicMock()
    repo = ArtifactChunkRepository(redis_client=redis_client, embedding_dim=3)

    assert repo.count_source_chunks(tenant_id="", source_artifact_id="source-1") == 0
    assert repo.count_source_chunks(tenant_id="tenant-1", source_artifact_id="") == 0
    redis_client.scan.assert_not_called()
