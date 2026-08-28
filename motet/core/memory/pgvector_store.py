"""
Motet - PGVector Store

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-20

Description:
    PostgreSQL vector store implementation for the Motet distributed framework.
    Provides comprehensive vector storage and retrieval capabilities using PostgreSQL
    with pgvector extension. Embeddings can be delegated to the shared embedding
    service via `embedding_fn`, with a local SentenceTransformer fallback for
    standalone deployments. Includes embedding caching, result caching, and
    distributed memory coordination for production systems.

Dependencies:
    - psycopg: PostgreSQL database connectivity
    - sentence_transformers: Fallback embedding model for standalone text vectorization
    - typing: Type hints and annotations
    - Base vector store and cache mixins

Usage:
    from motet.core.memory.pgvector_store import PGVectorStore

    # Create store
    store = PGVectorStore(
        dsn="postgresql://user:pass@localhost/db",
        table="imf_embeddings",
        embedding_model="sentence-transformers/all-MiniLM-L12-v2"
    )

    # Add memories
    store.add(memory_items)

    # Search memories
    results = store.search("query", limit=10)

Notes:
    - Provides comprehensive vector storage and retrieval
    - Includes embedding caching and result caching
    - Supports distributed memory coordination
    - Includes comprehensive error handling and logging
    - Integrates with PostgreSQL and pgvector extension
    - Supports distributed memory management
    - Includes comprehensive observability and monitoring
"""

from __future__ import annotations

import structlog
from typing import Any, List, Optional, cast

from ..types import MemoryItem
from .base import VectorStoreBase, CacheMixin

logger = structlog.get_logger(__name__)


class PGVectorStore(VectorStoreBase, CacheMixin):
    def __init__(
        self,
        dsn: str,
        table: str = "imf_embeddings",
        embedding_model: str = "sentence-transformers/all-MiniLM-L12-v2",
        *,
        embedding_fn: Optional[Any] = None,
        embedding_dim: Optional[int] = None,
        enable_embedding_cache: bool = True,
        enable_result_cache: bool = False,
    ) -> None:
        import psycopg

        self._dsn = dsn
        self._table = table
        self._embedding_fn = embedding_fn
        if embedding_fn is not None:
            self._embedder = None
            if embedding_dim is None:
                probe = embedding_fn("probe")
                self._embedding_dim = len(probe.tolist() if hasattr(probe, "tolist") else list(probe))
            else:
                self._embedding_dim = int(embedding_dim)
        else:
            from sentence_transformers import SentenceTransformer  # type: ignore

            self._embedder = SentenceTransformer(embedding_model)
            probe = self._embedder.encode(["probe"], convert_to_numpy=True)[0]
            self._embedding_dim = int(probe.shape[0])
        self._init_cache(enable_embedding_cache=enable_embedding_cache, enable_result_cache=enable_result_cache)
        super().__init__(ttl_seconds=None)
        with psycopg.connect(self._dsn, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                cast(
                    Any,
                    f"""
                CREATE TABLE IF NOT EXISTS {table} (
                    id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    tags TEXT[],
                    metadata JSONB,
                    embedding VECTOR({self._embedding_dim})
                );
                """,
                )
            )

    def _embed_text(self, text: str) -> List[float]:
        if self._embed_cache_enabled and text in self._embed_cache:
            return self._embed_cache[text]
        if self._embedding_fn is not None:
            emb_raw = self._embedding_fn(text)
            emb = emb_raw.tolist() if hasattr(emb_raw, "tolist") else list(emb_raw)
        else:
            embedder = self._embedder
            if embedder is None:
                raise RuntimeError("PGVectorStore requires embedding_fn or default SentenceTransformer")
            emb = embedder.encode([text], convert_to_numpy=True)[0].tolist()
        if self._embed_cache_enabled:
            self._embed_cache[text] = emb
        return [float(value) for value in emb]

    def _to_vector_literal(self, values: List[float]) -> str:
        return "[" + ", ".join(str(float(v)) for v in values) + "]"

    def add(self, items: List[MemoryItem]) -> None:
        import psycopg
        from psycopg.types.json import Json

        embeddings = [self._embed_text(m.content) for m in items]
        with psycopg.connect(self._dsn, autocommit=True) as conn, conn.cursor() as cur:
            for item, emb in zip(items, embeddings):
                cur.execute(
                    cast(
                        Any,
                        f"INSERT INTO {self._table} (id, content, tags, metadata, embedding) VALUES (%s, %s, %s, %s, %s::vector) ON CONFLICT (id) DO NOTHING",
                    ),
                    (item.id, item.content, item.tags, Json(item.metadata or {}), self._to_vector_literal(emb)),
                )

    def query(
        self,
        text: str,
        top_k: int = 3,
        tags: Optional[List[str]] = None,
        *,
        tenant_id: Optional[str] = None,
        principal_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        motet_id: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> List[MemoryItem]:
        import psycopg

        cache_key = None
        if self._result_cache_enabled:
            cache_key = (text, tuple(tags or []), top_k)
            if cache_key in self._query_cache:
                return self._query_cache[cache_key]
        query_vector = self._to_vector_literal(self._embed_text(text))
        tag_filter_sql = ""
        if tags:
            tag_filter_sql = " AND tags && %s"
            params: List[object] = [tags, query_vector]
        else:
            params = [query_vector]
        sql = f"SELECT id, content, tags, metadata FROM {self._table} ORDER BY embedding <-> %s::vector LIMIT {top_k}"
        sql = sql.replace("ORDER BY", f"WHERE true{tag_filter_sql} ORDER BY") if tag_filter_sql else sql
        results: List[MemoryItem] = []
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            cur.execute(cast(Any, sql), params)
            for _id, content, _tags, metadata in cur.fetchall():
                try:
                    results.append(MemoryItem(id=_id, type="rag_chunk", content=content, tags=list(_tags or []), metadata=metadata or {}))
                except Exception as e:
                    logger.debug("pgvector_memory_item_skipped", id=_id, error=str(e))
                    continue
        if self._result_cache_enabled and cache_key is not None:
            self._query_cache[cache_key] = results
        return results

    def list_by_tag(
        self,
        tag: str,
        limit: int = 10,
        *,
        tenant_id: Optional[str] = None,
        principal_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        motet_id: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> List[MemoryItem]:
        import psycopg

        results: List[MemoryItem] = []
        sql = f"SELECT id, content, tags, metadata FROM {self._table} WHERE %s = ANY(tags) ORDER BY RANDOM() LIMIT {limit}"
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            cur.execute(cast(Any, sql), (tag,))
            for _id, content, _tags, metadata in cur.fetchall():
                try:
                    results.append(MemoryItem(id=_id, type="rag_chunk", content=content, tags=list(_tags or []), metadata=metadata or {}))
                except Exception as e:
                    logger.debug("pgvector_memory_item_skipped", id=_id, error=str(e))
                    continue
        return results

    def delete_by_tag(
        self,
        tag: str,
        *,
        tenant_id: Optional[str] = None,
        principal_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        motet_id: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> int:
        import psycopg
        deleted = 0
        try:
            with psycopg.connect(self._dsn, autocommit=True) as conn, conn.cursor() as cur:  # type: ignore[attr-defined]
                cur.execute(cast(Any, f"DELETE FROM {self._table} WHERE %s = ANY(tags)"), (tag,))
                deleted = cur.rowcount or 0
        except Exception:
            pass  # best-effort; return 0 on failure
        return int(deleted)

    def delete_ids(
        self,
        ids: List[str],
        *,
        tenant_id: Optional[str] = None,
        principal_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        motet_id: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> int:
        import psycopg

        if not ids:
            return 0
        try:
            with psycopg.connect(self._dsn, autocommit=True) as conn, conn.cursor() as cur:  # type: ignore[attr-defined]
                cur.execute(cast(Any, f"DELETE FROM {self._table} WHERE id = ANY(%s)"), (ids,))
                return int(cur.rowcount or 0)
        except Exception:
            return 0

    def update_tags(self, ids: List[str], tags: List[str], op: str, *, tenant_id: Optional[str] = None, principal_id: Optional[str] = None, conversation_id: Optional[str] = None, motet_id: Optional[str] = None, agent_id: Optional[str] = None) -> int:
        import psycopg
        if not ids:
            return 0
        updated = 0
        try:
            with psycopg.connect(self._dsn, autocommit=True) as conn, conn.cursor() as cur:  # type: ignore[attr-defined]
                if op == "add":
                    for mid in ids:
                        cur.execute(
                            cast(
                                Any,
                                f"UPDATE {self._table} SET tags = (SELECT ARRAY(SELECT DISTINCT UNNEST(COALESCE(tags, ARRAY[]::text[])) UNION SELECT UNNEST(%s::text[]))) WHERE id = %s",
                            ),
                            (tags, mid),
                        )
                        updated += cur.rowcount or 0
                elif op == "remove":
                    for mid in ids:
                        cur.execute(
                            cast(
                                Any,
                                f"UPDATE {self._table} SET tags = (SELECT ARRAY(SELECT t FROM UNNEST(COALESCE(tags, ARRAY[]::text[])) t WHERE t <> ALL(%s::text[]))) WHERE id = %s",
                            ),
                            (tags, mid),
                        )
                        updated += cur.rowcount or 0
                elif op == "set":
                    for mid in ids:
                        cur.execute(
                            cast(Any, f"UPDATE {self._table} SET tags = %s::text[] WHERE id = %s"),
                            (tags, mid),
                        )
                        updated += cur.rowcount or 0
        except Exception:
            pass  # best-effort; return 0 on failure
        return int(updated)

    def clear_all(self) -> int:
        """Delete all rows from the vector table. Required by BaseStore."""
        import psycopg
        try:
            with psycopg.connect(self._dsn, autocommit=True) as conn, conn.cursor() as cur:
                cur.execute(cast(Any, f"DELETE FROM {self._table}"))
                return int(cur.rowcount or 0)
        except Exception:
            return 0

    def clear_by_tag(self, tag: str) -> int:
        """Delete all rows that have the given tag. Required by BaseStore."""
        return self.delete_by_tag(tag)

    def clear_by_type(self, type_name: str) -> int:
        """Delete all rows whose metadata type matches. Required by BaseStore."""
        import psycopg
        try:
            with psycopg.connect(self._dsn, autocommit=True) as conn, conn.cursor() as cur:
                cur.execute(
                    cast(Any, f"DELETE FROM {self._table} WHERE metadata->>'type' = %s"),
                    (type_name,),
                )
                return int(cur.rowcount or 0)
        except Exception:
            return 0


__all__ = ["PGVectorStore"]


