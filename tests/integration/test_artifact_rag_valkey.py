"""
Motet - Artifact RAG Valkey Integration Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-05-05

Description:
    Docker-backed integration test for the ADR-0063 artifact RAG Valkey Search
    repository. It verifies real FT.CREATE, HSET vector storage, KNN retrieval,
    metadata scope filters, and fail-closed query behavior against the test
    Valkey bundle service.

Dependencies:
    - pytest for integration test structure
    - redis-py through UnifiedRedisManager for Valkey access
    - motet.core.rag for chunking and repository behavior

Usage:
    docker compose -f tests/docker-compose.test.yml run --rm test-runner \
      python -m pytest tests/integration/test_artifact_rag_valkey.py -v

Notes:
    - This test uses deterministic local vectors and does not require an
      embedding server or distributed workers.
    - Native TEXT capability is probed explicitly. The test documents the
      runtime capability without requiring TEXT support for the portable path.
"""

from __future__ import annotations

import os
import time
import uuid
from typing import Any, Generator, cast

import pytest

from motet.core.distributed.redis_manager import get_redis_manager
from motet.core.artifacts.preparation.strategies.text import chunk_text_to_prepared_chunks
from motet.core.rag import ArtifactChunkRepository
from motet.core.rag.types import ArtifactRetrievalScope


@pytest.fixture
def valkey_repository() -> Generator[ArtifactChunkRepository, None, None]:
    if not os.getenv("MOTET_REDIS_URL"):
        pytest.skip("MOTET_REDIS_URL is required for artifact RAG Valkey integration test")

    redis_client = get_redis_manager().get_sync_binary_client(f"artifact_rag_integration_{uuid.uuid4().hex}")
    try:
        redis_client.ping()
        redis_client.execute_command("FT._LIST")
    except Exception as exc:
        pytest.skip(f"Valkey Search is not available: {exc}")

    repo = ArtifactChunkRepository(redis_client=redis_client, embedding_dim=3)
    yield repo


@pytest.mark.integration
@pytest.mark.requires_redis
def test_artifact_rag_valkey_native_text_capability_probe(valkey_repository: ArtifactChunkRepository) -> None:
    supported = valkey_repository.supports_native_text_fields()

    assert isinstance(supported, bool)


@pytest.mark.integration
@pytest.mark.requires_redis
def test_artifact_rag_valkey_upsert_and_scoped_search(valkey_repository: ArtifactChunkRepository) -> None:
    tenant_id = f"tenant-rag-{uuid.uuid4().hex}"
    try:
        now = time.time()
        chunks = chunk_text_to_prepared_chunks(
            "Project Apollo budget is $42. The launch checklist is approved.\n\n"
            "Unrelated garden notes mention tomatoes and basil.",
            source_artifact_id="source-apollo",
            derived_artifact_id="derived-apollo",
            tenant_id=tenant_id,
            principal_id="principal-1",
            motet_id="motet-1",
            conversation_id="conv-1",
            filename="apollo.txt",
            created_at=now,
            expires_at=now + 3600,
            chunk_size=256,
            chunk_overlap=0,
        )
        assert chunks

        written = valkey_repository.upsert_chunks(chunks, [[0.1, 0.2, 0.3] for _ in chunks])
        assert written == len(chunks)

        results = valkey_repository.search(
            query_embedding=[0.1, 0.2, 0.3],
            tenant_id=tenant_id,
            motet_id="motet-1",
            principal_id="principal-1",
            role="user",
            conversation_id="conv-1",
            scope=ArtifactRetrievalScope.CONVERSATION,
            top_k=3,
        )

        assert results
        assert results[0].source_artifact_id == "source-apollo"
        assert results[0].derived_artifact_id == "derived-apollo"
        assert results[0].tenant_id == tenant_id
        assert results[0].principal_id == "principal-1"
        assert "Apollo" in results[0].content_text

        with pytest.raises(ValueError, match="conversation_id is required"):
            valkey_repository.search(
                query_embedding=[0.1, 0.2, 0.3],
                tenant_id=tenant_id,
                motet_id="motet-1",
                principal_id="principal-1",
                role="user",
                conversation_id="",
                scope=ArtifactRetrievalScope.CONVERSATION,
                top_k=3,
            )
    finally:
        redis_client = valkey_repository._redis
        cursor = 0
        keys = []
        while True:
            cursor, batch = cast(
                tuple[int, list[Any]],
                redis_client.scan(
                    cursor=cursor,
                    match=f"{valkey_repository.key_prefix(tenant_id)}*",
                    count=500,
                ),
            )
            keys.extend(batch or [])
            if int(cursor) == 0:
                break
        if keys:
            redis_client.delete(*keys)
        try:
            redis_client.execute_command("FT.DROPINDEX", valkey_repository.index_name(tenant_id))
        except Exception:
            pass
