"""
Motet - Valkey Search Vector Store (LTM semantic memory)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Valkey Search (RediSearch-compatible) implementation of VectorStoreBase for long-term
    memory semantic search. Uses FT.CREATE / FT.SEARCH with HNSW vector index and
    TAG fields for tenant/principal/motet/conversation/agent isolation and tag filters. Shares
    operational patterns with function discovery but uses a separate index and
    key prefix so memory vectors never share namespace with tool/workflow discovery.

    Embedding is decoupled: the store accepts an optional ``embedding_fn`` callable to delegate
    embedding to a centralized EmbeddingService (or any external provider). When not provided,
    falls back to an internal SentenceTransformer when no embedding_fn is provided. Pre-computed
    vectors can be written via ``add_with_vectors()``.

Dependencies:
    - sentence_transformers: Embeddings fallback (only loaded when no embedding_fn provided)
    - UnifiedRedisManager: Sync binary Redis client for HASH + binary vector payloads
    - structlog: Logging

Usage:
    from motet.core.memory.valkey_vector_store import ValkeyVectorStore

    # With injected embedding function (preferred in workers)
    store = ValkeyVectorStore(
        index_name="imf_memory_vectors_idx",
        key_prefix="memvec:",
        embedding_fn=embedding_service.embed,
        embedding_dim=384,
    )

    # With internal SentenceTransformer (backward compat / standalone)
    store = ValkeyVectorStore(
        index_name="imf_memory_vectors_idx",
        key_prefix="memvec:",
        embedding_model="sentence-transformers/all-MiniLM-L12-v2",
    )

    store.add([memory_item])
    store.add_with_vectors([memory_item], [pre_computed_vector])
    results = store.query("user query", top_k=5, tags=["ltm"],
        tenant_id="t1", principal_id="p1", agent_id="core.default")

Notes:
    - Index holds vectors plus retrieval fields; encrypted KV remains source of truth for full
      payloads. This store does not persist full content/metadata payloads.
    - Requires Valkey Search / Redis Stack FT module on the server used by the binary client.
    - The ``agent_id`` TAG field enables direct agent isolation in FT.SEARCH queries.
      The ``agent:`` prefix tag is also written to ``user_tags`` for tag-based
      queries (e.g. ``--tag agent:core.default``).
"""

from __future__ import annotations

import json
import os
import re
from array import array
from datetime import datetime, timezone
from typing import Any, List, Optional, cast

import structlog

from ..types import MemoryItem
from .base import VectorStoreBase, CacheMixin

logger = structlog.get_logger(__name__)

# Characters we escape inside TAG query values.
# Note: hyphen ('-'), colon (':'), and dot ('.') are intentionally excluded because
# escaping them can prevent matches for common IDs and namespaced tags
# (e.g., UUIDs, tenant IDs, and agent:core.default tags).
_TAG_QUERY_SPECIAL_RE = re.compile(r'([,<>{}\[\]";!@#$%^&*()+=~\\])')


def _escape_tag_query_value(value: str) -> str:
    """Escape a value used inside @field:{...} TAG filter (RediSearch dialect 2)."""
    return _TAG_QUERY_SPECIAL_RE.sub(r"\\\1", value)


def _tag_field_value_from_list(tags: List[str]) -> str:
    """
    Format tags for a RediSearch TAG field stored in HASH.

    Commas separate tags; commas inside a tag must be escaped with backslash per RediSearch rules.
    """
    out: List[str] = []
    for t in tags:
        s = str(t)
        s = s.replace("\\", "\\\\").replace(",", "\\,")
        out.append(s)
    return ",".join(out)


def _tags_list_from_field(raw: str) -> List[str]:
    """Parse TAG field value back to a list of tags (comma-separated; Motet tags rarely contain commas)."""
    if not raw:
        return []
    return [t.strip() for t in raw.split(",") if t.strip()]


def _build_identity_filter(
    *,
    tenant_id: Optional[str] = None,
    principal_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    motet_id: Optional[str] = None,
    agent_id: Optional[str] = None,
) -> str:
    """
    Build RediSearch TAG filter expression for tenant/principal/conversation/motet/agent isolation.
    Returns empty string if no filters; otherwise returns predicate like
    @tenant_id:{t} @principal_id:{p} @conversation_id:{c} @motet_id:{m} @agent_id:{a}.
    """
    parts: List[str] = []
    for field, val in (
        ("tenant_id", tenant_id),
        ("principal_id", principal_id),
        ("conversation_id", conversation_id),
        ("motet_id", motet_id),
        ("agent_id", agent_id),
    ):
        if val and str(val).strip():
            esc = _escape_tag_query_value(str(val).strip())
            parts.append(f"@{field}:{{{esc}}}")
    return " ".join(parts) if parts else ""


class ValkeyVectorStore(VectorStoreBase, CacheMixin):
    """Vector store for LTM memory using Valkey Search KNN + TAG filters (ADR-0092)."""

    def __init__(
        self,
        *,
        index_name: Optional[str] = None,
        key_prefix: Optional[str] = None,
        redis_client_id: str = "memory_vector_valkey",
        embedding_model: str = "sentence-transformers/all-MiniLM-L12-v2",
        embedding_fn: Optional[Any] = None,
        embedding_dim: Optional[int] = None,
        enable_embedding_cache: bool = True,
        enable_result_cache: bool = False,
    ) -> None:
        self._index_name = index_name or os.getenv(
            "MOTET_MEMORY_VECTOR_VALKEY_INDEX", "imf_memory_vectors_idx"
        )
        self._key_prefix = key_prefix or os.getenv(
            "MOTET_MEMORY_VECTOR_VALKEY_PREFIX", "memvec:"
        )
        self._redis_client_id = redis_client_id

        from ..distributed.redis_manager import get_redis_manager

        self._redis = get_redis_manager().get_sync_binary_client(redis_client_id)

        if embedding_fn is not None:
            self._embedding_fn = embedding_fn
            self._embedder = None
            if embedding_dim is None:
                probe = embedding_fn("probe")
                self._dim = len(probe)
            else:
                self._dim = embedding_dim
        else:
            from sentence_transformers import SentenceTransformer  # type: ignore

            self._embedder = SentenceTransformer(embedding_model)
            self._embedding_fn = None
            probe = self._embedder.encode(["probe"], convert_to_numpy=True)[0]
            self._dim = int(probe.shape[0])

        self._init_cache(enable_embedding_cache=enable_embedding_cache, enable_result_cache=enable_result_cache)
        super().__init__(ttl_seconds=None)
        self._ensure_index()
        self._validate_index_schema()

    def _embed_text(self, text: str) -> List[float]:
        if self._embed_cache_enabled and text in self._embed_cache:
            return self._embed_cache[text]
        if self._embedding_fn is not None:
            emb = self._embedding_fn(text)
            emb = emb.tolist() if hasattr(emb, "tolist") else list(emb)
        else:
            embedder = self._embedder
            if embedder is None:
                raise RuntimeError("ValkeyVectorStore requires embedding_fn or default SentenceTransformer")
            emb = embedder.encode([text], convert_to_numpy=True)[0].tolist()
        if self._embed_cache_enabled:
            self._embed_cache[text] = emb
        return emb

    def _to_float32_bytes(self, values: List[float]) -> bytes:
        buf = array("f", [float(v) for v in values])
        return buf.tobytes()

    def _ensure_index(self) -> None:
        try:
            self._redis.execute_command("FT.INFO", self._index_name)
            return
        except Exception:
            pass
        try:
            self._redis.execute_command(
                "FT.CREATE",
                self._index_name,
                "ON",
                "HASH",
                "PREFIX",
                "1",
                self._key_prefix,
                "SCHEMA",
                "memory_id",
                "TAG",
                "tenant_id",
                "TAG",
                "principal_id",
                "TAG",
                "motet_id",
                "TAG",
                "conversation_id",
                "TAG",
                "scope_type",
                "TAG",
                "memory_type",
                "TAG",
                "agent_id",
                "TAG",
                "user_tags",
                "TAG",
                "embedding",
                "VECTOR",
                "HNSW",
                "10",
                "TYPE",
                "FLOAT32",
                "DIM",
                str(self._dim),
                "DISTANCE_METRIC",
                "COSINE",
                "M",
                "16",
                "EF_CONSTRUCTION",
                "200",
            )
            logger.info(
                "valkey_memory_vector_index_created",
                index=self._index_name,
                prefix=self._key_prefix,
                dim=self._dim,
            )
        except Exception as e:
            logger.error(
                "valkey_memory_vector_index_create_failed",
                index=self._index_name,
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True,
            )
            raise

    def _validate_index_schema(self) -> None:
        """Validate index dimension matches embedder (catches embedding model changes)."""
        try:
            info = self._redis.execute_command("FT.INFO", self._index_name)
            idx_dim = self._extract_vector_dim_from_ft_info(info)
            if idx_dim is not None and idx_dim != self._dim:
                logger.warning(
                    "valkey_index_dimension_mismatch",
                    index=self._index_name,
                    index_dim=idx_dim,
                    embedder_dim=self._dim,
                    message="Index dimension does not match embedder; FT.SEARCH may fail. Recreate index or change embedding model.",
                )
        except Exception as e:
            logger.debug("valkey_index_schema_validation_skipped", error=str(e))

    def _extract_vector_dim_from_ft_info(self, info: Any) -> Optional[int]:
        """Extract vector dimension from FT.INFO response (nested key-value structure)."""
        if not info or not isinstance(info, (list, tuple)):
            return None

        def _find_dim(obj: Any) -> Optional[int]:
            if isinstance(obj, (list, tuple)):
                for i, item in enumerate(obj):
                    key = item
                    if isinstance(key, (bytes, bytearray)):
                        key = key.decode("utf-8", errors="replace")
                    if str(key).lower() == "dim" and i + 1 < len(obj):
                        val = obj[i + 1]
                        if isinstance(val, (bytes, bytearray)):
                            val = val.decode("utf-8", errors="replace")
                        try:
                            return int(val)
                        except (TypeError, ValueError):
                            pass
                    found = _find_dim(item)
                    if found is not None:
                        return found
            return None

        return _find_dim(info)

    def _logical_hash_key(self, memory_id: str) -> str:
        return f"{self._key_prefix}{memory_id}"

    def _hash_key(self, memory_id: str, tenant_id: Optional[str] = None) -> str:
        from ..distributed.tenant_keys import tenant_key

        logical = self._logical_hash_key(memory_id)
        tid = (tenant_id or "").strip()
        if tid and tid != "none":
            return tenant_key(tid, logical)
        return logical

    def _hash_keys(self, memory_id: str, tenant_id: Optional[str] = None) -> tuple[str, ...]:
        from ..distributed.tenant_keys import tenant_key

        logical = self._logical_hash_key(memory_id)
        tid = (tenant_id or "").strip()
        if tid and tid != "none":
            return (tenant_key(tid, logical),)
        return (logical,)

    def _tenant_index_name(self, tenant_id: str) -> str:
        return f"{self._index_name}:{tenant_id}"

    def _ensure_tenant_index(self, tenant_id: str) -> None:
        from ..distributed.tenant_keys import tenant_key

        tid = (tenant_id or "").strip()
        if not tid or tid == "none":
            return
        index_name = self._tenant_index_name(tid)
        prefix = tenant_key(tid, self._key_prefix.rstrip(":")) + ":"
        try:
            self._redis.execute_command("FT.INFO", index_name)
            return
        except Exception:
            pass
        try:
            self._redis.execute_command(
                "FT.CREATE",
                index_name,
                "ON",
                "HASH",
                "PREFIX",
                "1",
                prefix,
                "SCHEMA",
                "memory_id",
                "TAG",
                "tenant_id",
                "TAG",
                "principal_id",
                "TAG",
                "motet_id",
                "TAG",
                "conversation_id",
                "TAG",
                "scope_type",
                "TAG",
                "memory_type",
                "TAG",
                "agent_id",
                "TAG",
                "user_tags",
                "TAG",
                "embedding",
                "VECTOR",
                "HNSW",
                "10",
                "TYPE",
                "FLOAT32",
                "DIM",
                str(self._dim),
                "DISTANCE_METRIC",
                "COSINE",
                "M",
                "16",
                "EF_CONSTRUCTION",
                "200",
            )
            logger.info(
                "valkey_memory_vector_tenant_index_created",
                index=index_name,
                prefix=prefix,
                dim=self._dim,
            )
        except Exception as e:
            logger.error(
                "valkey_memory_vector_tenant_index_create_failed",
                index=index_name,
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True,
            )
            raise

    def _memory_item_to_mapping(self, item: MemoryItem, vector_bytes: bytes) -> dict:
        tags = list(item.tags or [])
        user_tags = _tag_field_value_from_list(tags)
        tenant = (item.tenant_id or "") or "none"
        principal = (item.principal_id or "") or "none"
        motet = (item.motet_id or "") or "none"
        conv = (item.conversation_id or "") or "none"
        scope_t = (item.scope_type or "") or "none"
        mtype = (item.type or "") or "none"
        agent = str((item.metadata or {}).get("agent_id", "") or "").strip() or "none"
        created = item.created_at
        if hasattr(created, "isoformat"):
            created_s = created.isoformat()
        else:
            created_s = str(created)
        # Persist only non-sensitive retrieval metadata in the vector index.
        # Full content and full metadata remain in KV storage (ADR-0092).
        meta = {"scope_id": (item.metadata or {}).get("scope_id")}
        return {
            b"memory_id": str(item.id).encode("utf-8"),
            b"tenant_id": tenant.encode("utf-8"),
            b"principal_id": principal.encode("utf-8"),
            b"motet_id": motet.encode("utf-8"),
            b"conversation_id": conv.encode("utf-8"),
            b"scope_type": scope_t.encode("utf-8"),
            b"memory_type": mtype.encode("utf-8"),
            b"agent_id": agent.encode("utf-8"),
            b"user_tags": user_tags.encode("utf-8"),
            b"metadata_json": json.dumps(meta, ensure_ascii=False).encode("utf-8"),
            b"created_at": created_s.encode("utf-8"),
            b"embedding": vector_bytes,
        }

    def _decode(self, raw: Any) -> str:
        if raw is None:
            return ""
        if isinstance(raw, (bytes, bytearray)):
            return raw.decode("utf-8", errors="replace")
        return str(raw)

    def _memory_item_from_field_map(self, field_map: dict[Any, Any], distance: float) -> Optional[MemoryItem]:
        try:
            def _g(*names: str) -> Any:
                for n in names:
                    if n in field_map:
                        return field_map[n]
                    nb = n.encode("utf-8")
                    if nb in field_map:
                        return field_map[nb]
                return None

            mid = self._decode(_g("memory_id"))
            content = self._decode(_g("content"))
            ut_raw = self._decode(_g("user_tags"))
            tags = _tags_list_from_field(ut_raw) if ut_raw else []
            meta_raw = _g("metadata_json")
            meta: dict = {}
            if meta_raw:
                try:
                    meta = json.loads(self._decode(meta_raw))
                except Exception:
                    meta = {}
            created_raw = self._decode(_g("created_at"))
            try:
                created_at = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
            except Exception:
                created_at = datetime.now(timezone.utc)

            def _tag_or_none(name: str) -> Optional[str]:
                v = _g(name)
                s = self._decode(v)
                if not s or s == "none":
                    return None
                return s

            tenant_id = _tag_or_none("tenant_id")
            principal_id = _tag_or_none("principal_id")
            motet_id = _tag_or_none("motet_id")
            conversation_id = _tag_or_none("conversation_id")
            scope_type = _tag_or_none("scope_type") or "working"
            mtype = self._decode(_g("memory_type")) or "memory"
            agent_id = _tag_or_none("agent_id")

            meta = dict(meta)
            sim = max(0.0, min(1.0, 1.0 / (1.0 + float(distance))))
            meta["search_score"] = sim
            if agent_id:
                meta["agent_id"] = agent_id

            return MemoryItem(
                id=mid or "unknown",
                type=mtype,
                content=content,
                tags=tags,
                metadata=meta,
                created_at=created_at,
                motet_id=motet_id,
                tenant_id=tenant_id,
                principal_id=principal_id,
                conversation_id=conversation_id,
                scope_type=scope_type,
                scope_id=meta.get("scope_id"),
            )
        except Exception as e:
            logger.debug("valkey_memory_item_reconstruct_failed", error=str(e))
            return None

    def add(self, items: List[MemoryItem]) -> None:
        for m in items:
            emb = self._embed_text(m.content)
            vec_bytes = self._to_float32_bytes(emb)
            mapping = self._memory_item_to_mapping(m, vec_bytes)
            if m.tenant_id:
                self._ensure_tenant_index(m.tenant_id)
            self._redis.hset(self._hash_key(m.id, m.tenant_id), mapping=mapping)

    def add_with_vectors(self, items: List[MemoryItem], vectors: List[List[float]]) -> None:
        """Write items with pre-computed embedding vectors (skips internal embedding)."""
        if len(items) != len(vectors):
            raise ValueError(f"items ({len(items)}) and vectors ({len(vectors)}) must have same length")
        for m, vec in zip(items, vectors):
            vec_bytes = self._to_float32_bytes(vec)
            mapping = self._memory_item_to_mapping(m, vec_bytes)
            if m.tenant_id:
                self._ensure_tenant_index(m.tenant_id)
            self._redis.hset(self._hash_key(m.id, m.tenant_id), mapping=mapping)

    def _build_knn_query(
        self,
        *,
        top_k: int,
        vec_bytes: bytes,
        tags: Optional[List[str]] = None,
        tenant_id: Optional[str] = None,
        principal_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        motet_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        index_name: Optional[str] = None,
    ) -> List[Any]:
        knn = max(int(top_k), 1)
        base = f"*=>[KNN {knn} @embedding $vec AS vector_distance]"
        filter_parts: List[str] = []

        # Identity isolation (ADR-0092)
        id_filter = _build_identity_filter(
            tenant_id=tenant_id,
            principal_id=principal_id,
            conversation_id=conversation_id,
            motet_id=motet_id,
            agent_id=agent_id,
        )
        if id_filter:
            filter_parts.append(id_filter)

        # User tags (e.g. ltm)
        if tags:
            esc = [_escape_tag_query_value(t) for t in tags if t]
            if esc:
                inner = "|".join(esc)
                filter_parts.append(f"@user_tags:{{{inner}}}")

        if filter_parts:
            filter_expr = " ".join(filter_parts)
            q = f"({filter_expr})=>[KNN {knn} @embedding $vec AS vector_distance]"
        else:
            q = base
        return [
            "FT.SEARCH",
            index_name or self._index_name,
            q,
            "PARAMS",
            "2",
            "vec",
            vec_bytes,
            "RETURN",
            "12",
            "memory_id",
            "user_tags",
            "metadata_json",
            "created_at",
            "tenant_id",
            "principal_id",
            "motet_id",
            "conversation_id",
            "scope_type",
            "memory_type",
            "agent_id",
            "vector_distance",
            "DIALECT",
            "2",
        ]

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
        cache_key = None
        if self._result_cache_enabled:
            cache_key = (
                text,
                tuple(tags or []),
                top_k,
                tenant_id,
                principal_id,
                conversation_id,
                motet_id,
                agent_id,
            )
            if cache_key in self._query_cache:
                return self._query_cache[cache_key]

        qvec = self._embed_text(text)
        vec_bytes = self._to_float32_bytes(qvec)
        index_names = [self._index_name]
        tid = (tenant_id or "").strip()
        if tid:
            index_names = [self._tenant_index_name(tid), self._index_name]
        resp = None
        for index_name in index_names:
            cmd = self._build_knn_query(
                top_k=top_k,
                vec_bytes=vec_bytes,
                tags=tags,
                tenant_id=tenant_id,
                principal_id=principal_id,
                conversation_id=conversation_id,
                motet_id=motet_id,
                agent_id=agent_id,
                index_name=index_name,
            )
            try:
                candidate = self._redis.execute_command(*cmd)
            except Exception:
                continue
            if candidate and len(candidate) >= 2:
                resp = candidate
                break
        if resp is None:
            resp = []

        out: List[MemoryItem] = []
        if not resp or len(resp) < 2:
            if self._result_cache_enabled and cache_key is not None:
                self._query_cache[cache_key] = out
            return out

        for i in range(1, len(resp), 2):
            if i + 1 >= len(resp):
                break
            fields = resp[i + 1]
            if not isinstance(fields, list):
                continue
            field_map: dict[Any, Any] = {}
            for j in range(0, len(fields), 2):
                if j + 1 >= len(fields):
                    break
                k = fields[j]
                v = fields[j + 1]
                key = k.decode() if isinstance(k, (bytes, bytearray)) else str(k)
                field_map[key] = v
            raw_dist = field_map.get("vector_distance", 1.0)
            if isinstance(raw_dist, (bytes, bytearray)):
                raw_dist = raw_dist.decode()
            try:
                dist = float(raw_dist)
            except Exception:
                dist = 1.0
            item = self._memory_item_from_field_map(field_map, dist)
            if not item:
                continue
            # Defense-in-depth: FT.SEARCH already filters by @user_tags, but
            # we re-check client-side to guard against TAG tokenization edge cases.
            if tags:
                if not any(t in (item.tags or []) for t in tags):
                    continue
            out.append(item)

        if self._result_cache_enabled and cache_key is not None:
            self._query_cache[cache_key] = out
        return out

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
        if not tag:
            return []
        esc = _escape_tag_query_value(tag)
        filter_parts: List[str] = [f"@user_tags:{{{esc}}}"]
        id_filter = _build_identity_filter(
            tenant_id=tenant_id,
            principal_id=principal_id,
            conversation_id=conversation_id,
            motet_id=motet_id,
            agent_id=agent_id,
        )
        if id_filter:
            filter_parts.append(id_filter)
        q = " ".join(filter_parts)
        try:
            resp = self._redis.execute_command(
                "FT.SEARCH",
                self._index_name,
                q,
                "LIMIT",
                "0",
                str(max(limit, 1)),
                "RETURN",
                "11",
                "memory_id",
                "user_tags",
                "metadata_json",
                "created_at",
                "tenant_id",
                "principal_id",
                "motet_id",
                "conversation_id",
                "scope_type",
                "memory_type",
                "agent_id",
                "DIALECT",
                "2",
            )
        except Exception as e:
            logger.warning("valkey_list_by_tag_failed", tag=tag, error=str(e))
            return []

        out: List[MemoryItem] = []
        if not resp or len(resp) < 2:
            return out
        for i in range(1, len(resp), 2):
            if i + 1 >= len(resp):
                break
            fields = resp[i + 1]
            if not isinstance(fields, list):
                continue
            field_map: dict[str, Any] = {}
            for j in range(0, len(fields), 2):
                if j + 1 >= len(fields):
                    break
                k = fields[j]
                v = fields[j + 1]
                key = k.decode() if isinstance(k, (bytes, bytearray)) else str(k)
                field_map[key] = v
            item = self._memory_item_from_field_map(field_map, 0.0)
            if item:
                out.append(item)
            if len(out) >= limit:
                break
        return out

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
        items = self.list_by_tag(
            tag,
            limit=10000,
            tenant_id=tenant_id,
            principal_id=principal_id,
            conversation_id=conversation_id,
            motet_id=motet_id,
            agent_id=agent_id,
        )
        if not items:
            return 0
        n = 0
        for it in items:
            try:
                self._redis.delete(*self._hash_keys(it.id, it.tenant_id))
                n += 1
            except Exception:
                continue
        return n

    def update_tags(
        self,
        ids: List[str],
        tags: List[str],
        op: str,
        *,
        tenant_id: Optional[str] = None,
        principal_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        motet_id: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> int:
        if not ids:
            return 0
        updated = 0
        for mid in ids:
            key = None
            raw: dict[Any, Any] = {}
            try:
                for candidate in self._hash_keys(mid, tenant_id):
                    found = cast(dict[Any, Any], self._redis.hgetall(candidate))
                    if found:
                        key = candidate
                        raw = found
                        break
                if not raw or key is None:
                    continue
                field_map: dict[Any, Any] = {}
                for bk, bv in raw.items():
                    kb = bk.decode() if isinstance(bk, (bytes, bytearray)) else str(bk)
                    field_map[kb] = bv

                # Verify ownership when identity filters are provided
                if tenant_id and self._decode(field_map.get("tenant_id")) not in (tenant_id, "none", ""):
                    continue
                if principal_id and self._decode(field_map.get("principal_id")) not in (principal_id, "none", ""):
                    continue
                if motet_id and self._decode(field_map.get("motet_id")) not in (motet_id, "none", ""):
                    continue

                ut_raw = self._decode(field_map.get("user_tags"))
                current = set(_tags_list_from_field(ut_raw) if ut_raw else [])
                if op == "add":
                    current.update(tags)
                elif op == "remove":
                    for t in tags:
                        current.discard(t)
                elif op == "set":
                    current = set(tags)
                new_tags = sorted(current)
                new_field = _tag_field_value_from_list(new_tags)
                self._redis.hset(key, "user_tags", new_field)
                updated += 1
            except Exception as e:
                logger.debug("valkey_update_tags_failed", memory_id=mid, error=str(e))
                continue
        return updated

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
        if not ids:
            return 0
        deleted = 0
        for mid in ids:
            try:
                key = None
                raw: dict[Any, Any] = {}
                for candidate in self._hash_keys(mid, tenant_id):
                    found = cast(dict[Any, Any], self._redis.hgetall(candidate))
                    if found:
                        key = candidate
                        raw = found
                        break
                if not raw or key is None:
                    continue
                field_map: dict[Any, Any] = {}
                for bk, bv in raw.items():
                    kb = bk.decode() if isinstance(bk, (bytes, bytearray)) else str(bk)
                    field_map[kb] = bv
                if tenant_id and self._decode(field_map.get("tenant_id")) not in (tenant_id, "none", ""):
                    continue
                if principal_id and self._decode(field_map.get("principal_id")) not in (principal_id, "none", ""):
                    continue
                if motet_id and self._decode(field_map.get("motet_id")) not in (motet_id, "none", ""):
                    continue
                self._redis.delete(*self._hash_keys(mid, tenant_id))
                deleted += 1
            except Exception as e:
                logger.debug("valkey_delete_ids_failed", memory_id=mid, error=str(e))
                continue
        return deleted

    def clear_all(
        self,
        *,
        tenant_id: Optional[str] = None,
        principal_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        motet_id: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> int:
        """Clear vector documents. When filters provided, only matching docs; else all (logs warning)."""
        id_filter = _build_identity_filter(
            tenant_id=tenant_id,
            principal_id=principal_id,
            conversation_id=conversation_id,
            motet_id=motet_id,
            agent_id=agent_id,
        )
        if id_filter:
            return self._clear_by_filter(id_filter, limit=100000)
        logger.warning(
            "valkey_clear_all_unscoped",
            index=self._index_name,
            message="clear_all called without tenant/principal filters; clearing entire index",
        )
        cursor = 0
        keys: List[str] = []
        pat = f"{self._key_prefix}*"
        while True:
            cursor, batch = cast(tuple[int, list[Any]], self._redis.scan(cursor=cursor, match=pat, count=500))
            if batch:
                for bk in batch:
                    keys.append(bk.decode() if isinstance(bk, (bytes, bytearray)) else str(bk))
            if cursor == 0:
                break
        if keys:
            self._redis.delete(*keys)
        return len(keys)

    def _clear_by_filter(self, filter_expr: str, limit: int = 10000) -> int:
        """Delete documents matching RediSearch filter (paginated)."""
        n = 0
        page_size = min(limit, 1000)
        while n < limit:
            try:
                resp = self._redis.execute_command(
                    "FT.SEARCH",
                    self._index_name,
                    filter_expr,
                    "LIMIT",
                    "0",
                    str(page_size),
                    "NOCONTENT",
                    "DIALECT",
                    "2",
                )
            except Exception:
                break
            if not resp or len(resp) < 2:
                break
            total_hits: Optional[int] = None
            try:
                total_hits = int(resp[0])
            except Exception:
                total_hits = None
            batch_keys = []
            for i in range(1, len(resp)):
                rk = resp[i]
                key = rk.decode() if isinstance(rk, (bytes, bytearray)) else str(rk)
                batch_keys.append(key)
            deleted_this_round = 0
            for key in batch_keys:
                try:
                    self._redis.delete(key)
                    n += 1
                    deleted_this_round += 1
                    if n >= limit:
                        break
                except Exception:
                    continue
            # Always re-query from offset 0 because deleting documents mutates result set.
            # If no key could be deleted, stop to avoid a tight loop on undeletable records.
            if deleted_this_round == 0:
                break
            if total_hits is not None and n >= total_hits:
                break
        return n

    def clear_by_tag(self, tag: str) -> int:
        return self.delete_by_tag(tag)

    def clear_by_type(
        self,
        type_name: str,
        *,
        tenant_id: Optional[str] = None,
        principal_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        motet_id: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> int:
        if not type_name:
            return 0
        esc = _escape_tag_query_value(type_name)
        filter_parts: List[str] = [f"@memory_type:{{{esc}}}"]
        id_filter = _build_identity_filter(
            tenant_id=tenant_id,
            principal_id=principal_id,
            conversation_id=conversation_id,
            motet_id=motet_id,
            agent_id=agent_id,
        )
        if id_filter:
            filter_parts.append(id_filter)
        q = " ".join(filter_parts)
        return self._clear_by_filter(q, limit=10000)


__all__ = ["ValkeyVectorStore"]
