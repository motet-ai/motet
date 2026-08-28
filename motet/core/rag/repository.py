"""
Motet - Artifact RAG Valkey Repository

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Stores and retrieves artifact text chunks in a tenant-scoped Valkey
    Search index. The repository owns FT.CREATE/FT.SEARCH command construction,
    deterministic chunk keys, isolation filters, and TTL synchronization with
    source artifact retention. ``ensure_index`` migrates stale schemas (e.g.
    indexes created before ``artifact_tags`` TAG) by dropping and recreating
    the index definition while leaving HASH chunk documents intact.

Dependencies:
    - array for FLOAT32 vector serialization
    - re for RediSearch TAG escaping
    - motet.core.distributed.redis_manager for sync binary Redis clients
    - motet.core.rag.types for chunk and search result models

Usage:
    repository = ArtifactChunkRepository()
    repository.upsert_chunks(chunks, embeddings)
    results = repository.search(query_embedding, tenant_id="tenant", ...)

Notes:
    - Search is fail-closed when required scope fields are absent.
    - Chunk text is stored as plaintext in HASH values. Native TEXT indexing is
      enabled only when the runtime capability probe succeeds; otherwise the
      retriever uses application-layer lexical fusion over scoped HASH fields.
    - Valkey Search in this stack does not support FT.ALTER; schema drift is
      fixed by FT.DROPINDEX (no DD) + FT.CREATE so existing HASHes are rescanned.
"""

from __future__ import annotations

import json
import math
import re
import uuid
from array import array
from typing import Any, Iterable, List, Optional, cast, get_args

import structlog
from pydantic import TypeAdapter

from ..artifacts.preparation import (
    ArtifactIndexState,
    ArtifactModality,
    ArtifactPrepState,
    ChunkCoordinate,
    ChunkKind,
    PreparedArtifactChunk,
    TextCoord,
)
from .types import ArtifactChunkSearchResult, ArtifactRetrievalScope
from ..distributed.tenant_keys import tenant_key

logger = structlog.get_logger(__name__)
_COORDINATE_ADAPTER = TypeAdapter(ChunkCoordinate)
_VALID_ARTIFACT_MODALITIES = set(get_args(ArtifactModality))
_VALID_ARTIFACT_PREP_STATES = set(get_args(ArtifactPrepState))
_VALID_ARTIFACT_INDEX_STATES = set(get_args(ArtifactIndexState))
_VALID_CHUNK_KINDS = set(get_args(ChunkKind))

_TAG_QUERY_SPECIAL_RE = re.compile(r'([,<>{}\[\]";!@#$%^&*()+=~\\])')

# Fields that retrieval / cache lookups filter on. Older indexes created before
# these existed (notably artifact_tags) must be migrated — Valkey Search here
# does not support FT.ALTER SCHEMA ADD.
_REQUIRED_INDEX_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "source_artifact_id",
        "tenant_id",
        "principal_id",
        "motet_id",
        "role",
        "conversation_id",
        "artifact_tags",
        "prep_strategy_id",
        "chunk_cache_key",
        "embedding",
    }
)


def _escape_tag_query_value(value: str) -> str:
    """Escape a RediSearch TAG query value while preserving common ID separators."""

    return _TAG_QUERY_SPECIAL_RE.sub(r"\\\1", value)


def _decode(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    return value


def _normalize_tags(values: Optional[list[str]]) -> list[str]:
    """Return stable non-empty tag strings for indexing and filtering."""

    return list(dict.fromkeys(str(value).strip() for value in values or [] if str(value).strip()))


def _encode_tags(values: Optional[list[str]]) -> str:
    """Encode tags for Redis HASH storage and RediSearch TAG indexing."""

    return ",".join(_normalize_tags(values))


def _decode_tags(value: Any) -> list[str]:
    """Decode comma-separated artifact tags from HASH/Search fields."""

    raw = str(_decode(value) or "")
    return _normalize_tags([part for part in raw.split(",")])


def _decode_chunk_kind(value: Any) -> ChunkKind:
    """Decode and validate a stored chunk kind literal from Redis fields."""

    raw = str(_decode(value) or "text").strip()
    if raw in _VALID_CHUNK_KINDS:
        return cast(ChunkKind, raw)
    logger.warning("artifact_rag_invalid_chunk_kind", chunk_kind=raw)
    return "text"


def _decode_modality(value: Any) -> ArtifactModality:
    """Decode and validate a stored artifact modality literal from Redis fields."""

    raw = str(_decode(value) or "text").strip()
    if raw in _VALID_ARTIFACT_MODALITIES:
        return cast(ArtifactModality, raw)
    logger.warning("artifact_rag_invalid_modality", modality=raw)
    return "text"


def _decode_prep_state(value: Any) -> ArtifactPrepState:
    """Decode and validate a stored artifact preparation state literal."""

    raw = str(_decode(value) or "prep_complete").strip()
    if raw in _VALID_ARTIFACT_PREP_STATES:
        return cast(ArtifactPrepState, raw)
    logger.warning("artifact_rag_invalid_prep_state", prep_state=raw)
    return "prep_complete"


def _decode_index_state(value: Any) -> ArtifactIndexState:
    """Decode and validate a stored artifact index state literal."""

    raw = str(_decode(value) or "index_complete").strip()
    if raw in _VALID_ARTIFACT_INDEX_STATES:
        return cast(ArtifactIndexState, raw)
    logger.warning("artifact_rag_invalid_index_state", index_state=raw)
    return "index_complete"


def _decode_json(value: Any, default: Any) -> Any:
    raw = _decode(value)
    if raw in (None, ""):
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw))
    except Exception:
        return default


def _decode_coordinates(doc: dict[str, Any]) -> ChunkCoordinate:
    raw = _decode_json(doc.get("coordinates"), {})
    if isinstance(raw, dict) and raw.get("kind"):
        try:
            return cast(ChunkCoordinate, _COORDINATE_ADAPTER.validate_python(raw))
        except Exception:
            pass
    return TextCoord(
        byte_start=int(float(doc.get("byte_range_start") or 0)),
        byte_end=int(float(doc.get("byte_range_end") or 0)),
        page_number=(
            int(float(doc["page_number"]))
            if str(doc.get("page_number") or "").strip() not in ("", "0")
            else None
        ),
    )


class ArtifactChunkRepository:
    """Valkey Search repository for artifact chunk vectors."""

    def __init__(
        self,
        *,
        redis_client: Any = None,
        redis_client_id: str = "artifact_rag_valkey",
        embedding_dim: int = 384,
        native_text_mode: str = "auto",
    ) -> None:
        self._redis_client_id = redis_client_id
        self._dim = int(embedding_dim or 384)
        self._native_text_mode = str(native_text_mode or "auto").strip().lower()
        if self._native_text_mode not in {"auto", "disabled", "required"}:
            raise ValueError("native_text_mode must be one of: auto, disabled, required")
        self._native_text_supported: Optional[bool] = None
        if redis_client is not None:
            self._redis = redis_client
        else:
            from ..distributed.redis_manager import get_redis_manager

            self._redis = get_redis_manager().get_sync_binary_client(redis_client_id)

    @staticmethod
    def index_name(tenant_id: str) -> str:
        """Return the per-tenant artifact chunk index name."""

        return f"artifact_chunks:{tenant_id}"

    @staticmethod
    def logical_key_prefix(tenant_id: str) -> str:
        """Pre-Phase-2 per-tenant artifact chunk key prefix."""

        return f"artifact_chunk:{tenant_id}:"

    @classmethod
    def key_prefix(cls, tenant_id: str) -> str:
        """Return the per-tenant artifact chunk key prefix (tenant-prefixed)."""

        return tenant_key(tenant_id, f"artifact_chunk:{tenant_id}") + ":"

    @classmethod
    def key_prefixes(cls, tenant_id: str) -> tuple[str, ...]:
        prefixed = cls.key_prefix(tenant_id)
        logical = cls.logical_key_prefix(tenant_id)
        if prefixed == logical:
            return (prefixed,)
        return (prefixed, logical)

    @classmethod
    def chunk_key(
        cls,
        *,
        tenant_id: str,
        source_artifact_id: str,
        chunk_index: int,
        prep_strategy_id: str = "text_default",
    ) -> str:
        """Return the deterministic Valkey key for a source artifact chunk."""

        return f"{cls.key_prefix(tenant_id)}{source_artifact_id}:{prep_strategy_id}:{int(chunk_index)}"

    @staticmethod
    def _to_float32_bytes(values: Iterable[float]) -> bytes:
        buf = array("f", [float(value) for value in values])
        return buf.tobytes()

    def supports_native_text_fields(self) -> bool:
        """Probe whether this Valkey Search runtime accepts TEXT fields."""

        if self._native_text_supported is not None:
            return self._native_text_supported

        index_name = f"artifact_chunks_text_probe:{uuid.uuid4().hex}"
        prefix = f"artifact_chunk_text_probe:{uuid.uuid4().hex}:"
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
                "probe_text",
                "TEXT",
            )
            self._native_text_supported = True
        except Exception as e:
            self._native_text_supported = False
            logger.info(
                "artifact_rag_native_text_probe_unsupported",
                error=str(e),
                error_type=type(e).__name__,
            )
        finally:
            try:
                self._redis.execute_command("FT.DROPINDEX", index_name)
            except Exception:
                pass
        return bool(self._native_text_supported)

    def _native_text_fields_enabled(self) -> bool:
        if self._native_text_mode == "disabled":
            return False
        supported = self.supports_native_text_fields()
        if self._native_text_mode == "required" and not supported:
            raise RuntimeError("Valkey Search native TEXT fields are required but not supported")
        return supported

    @staticmethod
    def _indexed_field_names(info: Any) -> set[str]:
        """Extract schema field identifiers from an FT.INFO response."""

        names: set[str] = set()

        def _walk(obj: Any) -> None:
            if isinstance(obj, (list, tuple)):
                i = 0
                while i < len(obj):
                    key = obj[i]
                    if isinstance(key, (bytes, bytearray)):
                        key = key.decode("utf-8", errors="replace")
                    if isinstance(key, str) and key.lower() == "identifier" and i + 1 < len(obj):
                        value = obj[i + 1]
                        if isinstance(value, (bytes, bytearray)):
                            value = value.decode("utf-8", errors="replace")
                        if isinstance(value, str) and value:
                            names.add(value)
                        i += 2
                        continue
                    _walk(obj[i])
                    i += 1

        _walk(info)
        return names

    def _missing_required_index_fields(self, info: Any) -> set[str]:
        present = self._indexed_field_names(info)
        return set(_REQUIRED_INDEX_FIELD_NAMES) - present

    def _index_missing_tenant_prefix(self, info: Any, tenant_id: str) -> bool:
        """True when FT.INFO does not list the tenant-prefixed HASH prefix."""
        wanted = self.key_prefix(tenant_id)
        found: list[str] = []

        def _walk(obj: Any) -> None:
            if isinstance(obj, (bytes, bytearray)):
                found.append(obj.decode("utf-8", errors="replace"))
                return
            if isinstance(obj, str):
                found.append(obj)
                return
            if isinstance(obj, (list, tuple)):
                for item in obj:
                    _walk(item)

        _walk(info)
        return wanted not in found

    def ensure_index(self, *, tenant_id: str) -> None:
        """Create the tenant-scoped Valkey Search index if needed.

        If an index already exists but lacks required filter fields (schema
        drift from older FT.CREATE definitions), drop the index definition
        without deleting HASH documents and recreate so existing chunks are
        rescanned under the current schema.
        """

        if not tenant_id:
            raise ValueError("tenant_id is required to create artifact chunk index")
        index_name = self.index_name(tenant_id)

        info: Any = None
        try:
            info = self._redis.execute_command("FT.INFO", index_name)
        except Exception:
            info = None

        if info is not None:
            if self._native_text_mode == "required" and not self.supports_native_text_fields():
                raise RuntimeError("Valkey Search native TEXT fields are required but not supported")
            missing = self._missing_required_index_fields(info)
            missing_prefix = self._index_missing_tenant_prefix(info, tenant_id)
            if not missing and not missing_prefix:
                return
            try:
                # Keep documents (no DD) — only the search definition is stale.
                self._redis.execute_command("FT.DROPINDEX", index_name)
                logger.warning(
                    "artifact_rag_index_schema_migrated",
                    index=index_name,
                    tenant_id=tenant_id,
                    missing_fields=sorted(missing),
                    missing_tenant_prefix=missing_prefix,
                    message=(
                        "Dropped stale artifact chunk index and recreating with "
                        "current schema (HASH chunk documents retained)."
                    ),
                )
            except Exception as e:
                logger.error(
                    "artifact_rag_index_schema_migrate_failed",
                    index=index_name,
                    tenant_id=tenant_id,
                    missing_fields=sorted(missing),
                    missing_tenant_prefix=missing_prefix,
                    error=str(e),
                    error_type=type(e).__name__,
                    exc_info=True,
                )
                raise RuntimeError(
                    f"Failed to migrate stale artifact chunk index {index_name}: {e}"
                ) from e

        self._create_index(tenant_id=tenant_id, index_name=index_name)

    def _create_index(self, *, tenant_id: str, index_name: str) -> None:
        """FT.CREATE the current artifact-chunk schema for one tenant."""

        schema: list[Any] = [
            "FT.CREATE",
            index_name,
            "ON",
            "HASH",
            "PREFIX",
            str(len(self.key_prefixes(tenant_id))),
            *self.key_prefixes(tenant_id),
            "SCHEMA",
            "source_artifact_id",
            "TAG",
            "derived_artifact_id",
            "TAG",
            "tenant_id",
            "TAG",
            "principal_id",
            "TAG",
            "motet_id",
            "TAG",
            "role",
            "TAG",
            "conversation_id",
            "TAG",
            "content_type",
            "TAG",
            "artifact_tags",
            "TAG",
            "chunk_kind",
            "TAG",
            "modality",
            "TAG",
            "prep_strategy_id",
            "TAG",
            "prep_strategy_version",
            "TAG",
            "prep_state",
            "TAG",
            "index_state",
            "TAG",
            "chunk_index",
            "NUMERIC",
            "byte_range_start",
            "NUMERIC",
            "byte_range_end",
            "NUMERIC",
            "page_number",
            "NUMERIC",
            "created_at",
            "NUMERIC",
            "expires_at",
            "NUMERIC",
            "content_hash",
            "TAG",
            "chunk_cache_key",
            "TAG",
            "confidence",
            "NUMERIC",
        ]
        if self._native_text_fields_enabled():
            schema.extend(
                [
                    "filename",
                    "TEXT",
                    "content_text",
                    "TEXT",
                ]
            )
        schema.extend(
            [
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
            ]
        )
        self._redis.execute_command(*schema)

    def delete_source_chunks(self, *, tenant_id: str, source_artifact_id: str) -> int:
        """Delete all indexed chunks for a source artifact in one tenant."""

        if not tenant_id or not source_artifact_id:
            return 0
        keys: list[Any] = []
        seen: set[str] = set()
        for prefix in self.key_prefixes(tenant_id):
            pattern = f"{prefix}{source_artifact_id}:*"
            for key in self._scan_keys(pattern=pattern, count=500):
                decoded = key.decode("utf-8") if isinstance(key, (bytes, bytearray)) else str(key)
                if decoded in seen:
                    continue
                seen.add(decoded)
                keys.append(key)
        if keys:
            self._redis.delete(*keys)
        return len(keys)

    def count_source_chunks(self, *, tenant_id: str, source_artifact_id: str) -> int:
        """Count indexed chunk HASH keys for a source artifact (ADR-0063 ops / status)."""

        if not tenant_id or not source_artifact_id:
            return 0
        seen: set[str] = set()
        for prefix in self.key_prefixes(tenant_id):
            pattern = f"{prefix}{source_artifact_id}:*"
            for key in self._scan_keys(pattern=pattern, count=500):
                decoded = key.decode("utf-8") if isinstance(key, (bytes, bytearray)) else str(key)
                seen.add(decoded)
        return len(seen)

    def count_source_chunks_by_strategy(self, *, tenant_id: str, source_artifact_id: str) -> dict[str, int]:
        """Count indexed chunks grouped by preparation strategy for one source artifact."""

        counts: dict[str, int] = {}
        if not tenant_id or not source_artifact_id:
            return counts
        for prefix in self.key_prefixes(tenant_id):
            pattern = f"{prefix}{source_artifact_id}:*"
            for key in self._scan_keys(pattern=pattern, count=500):
                raw = self._redis.hgetall(key)
                if not isinstance(raw, dict):
                    continue
                doc = {str(_decode(field)): _decode(value) for field, value in raw.items()}
                strategy_id = str(doc.get("prep_strategy_id") or "unknown")
                counts[strategy_id] = counts.get(strategy_id, 0) + 1
        return counts

    def count_chunks_matching_cache_key(
        self,
        *,
        tenant_id: str,
        source_artifact_id: str,
        prep_strategy_id: str,
        chunk_cache_key: str,
    ) -> int:
        """Return how many indexed chunks match source, strategy, and preparation cache key."""

        if not tenant_id or not source_artifact_id or not prep_strategy_id or not chunk_cache_key:
            return 0
        index_name = self.index_name(tenant_id)
        try:
            self._redis.execute_command("FT.INFO", index_name)
        except Exception:
            return 0
        query = (
            f"(@source_artifact_id:{{{_escape_tag_query_value(source_artifact_id)}}}) "
            f"(@prep_strategy_id:{{{_escape_tag_query_value(prep_strategy_id)}}}) "
            f"(@chunk_cache_key:{{{_escape_tag_query_value(chunk_cache_key)}}})"
        )
        try:
            raw = self._redis.execute_command("FT.SEARCH", index_name, query, "LIMIT", "0", "0")
        except Exception as e:
            logger.warning(
                "artifact_chunk_cache_key_search_failed",
                error=str(e),
                error_type=type(e).__name__,
            )
            return 0
        if not raw:
            return 0
        try:
            return int(raw[0])
        except (TypeError, ValueError):
            return 0

    def _scan_keys(self, *, pattern: str, count: int = 500) -> list[Any]:
        """Return keys matching a pattern from a synchronous Redis SCAN."""

        cursor = 0
        keys: list[Any] = []
        while True:
            scan_result = cast(
                tuple[int, list[Any]],
                self._redis.scan(cursor=cursor, match=pattern, count=count),
            )
            cursor, batch = scan_result
            keys.extend(batch or [])
            if int(cursor) == 0:
                break
        return keys

    def upsert_chunks(
        self,
        chunks: list[PreparedArtifactChunk],
        embeddings: list[list[float]],
        *,
        replace_source: bool = True,
    ) -> int:
        """Store chunks and vectors, replacing existing chunks for the source by default."""

        if not chunks:
            return 0
        if len(chunks) != len(embeddings):
            raise ValueError("chunks and embeddings must have the same length")

        tenant_id = chunks[0].tenant_id
        source_artifact_id = chunks[0].source_artifact_id
        self.ensure_index(tenant_id=tenant_id)
        if replace_source:
            self.delete_source_chunks(tenant_id=tenant_id, source_artifact_id=source_artifact_id)

        written = 0
        for chunk, embedding in zip(chunks, embeddings):
            key = self.chunk_key(
                tenant_id=chunk.tenant_id,
                source_artifact_id=chunk.source_artifact_id,
                chunk_index=chunk.chunk_index,
                prep_strategy_id=chunk.prep_strategy_id,
            )
            mapping: dict[str, Any] = {
                "source_artifact_id": chunk.source_artifact_id,
                "derived_artifact_id": chunk.derived_artifact_id or "",
                "tenant_id": chunk.tenant_id,
                "principal_id": chunk.principal_id,
                "motet_id": chunk.motet_id,
                "role": chunk.role or "user",
                "conversation_id": chunk.conversation_id,
                "content_type": chunk.content_type,
                "artifact_tags": _encode_tags(chunk.artifact_tags),
                "filename": chunk.filename or "",
                "chunk_kind": chunk.chunk_kind,
                "content_text": chunk.content_text,
                "structured_payload": json.dumps(chunk.structured_payload or {}, sort_keys=True, default=str),
                "coordinates": json.dumps(chunk.coordinates.model_dump(mode="json"), sort_keys=True, default=str),
                "modality": chunk.modality,
                "confidence": float(chunk.confidence),
                "prep_strategy_id": chunk.prep_strategy_id,
                "prep_strategy_version": chunk.prep_strategy_version,
                "prep_state": chunk.prep_state,
                "index_state": "index_complete",
                "chunk_cache_key": chunk.chunk_cache_key,
                "chunk_index": int(chunk.chunk_index),
                "byte_range_start": int(getattr(chunk.coordinates, "byte_start", 0) or 0),
                "byte_range_end": int(getattr(chunk.coordinates, "byte_end", 0) or 0),
                "page_number": int(
                    getattr(chunk.coordinates, "page_number", None)
                    or getattr(chunk.coordinates, "page", None)
                    or 0
                ),
                "created_at": float(chunk.created_at),
                "expires_at": float(chunk.expires_at or 0),
                "content_hash": chunk.content_hash,
                "embedding": self._to_float32_bytes(embedding),
            }
            self._redis.hset(key, mapping=mapping)
            if chunk.expires_at:
                self._redis.expireat(key, int(chunk.expires_at))
            written += 1
        return written

    def _required_filter(
        self,
        *,
        scope: ArtifactRetrievalScope,
        tenant_id: str,
        motet_id: str,
        principal_id: Optional[str],
        role: Optional[str],
        conversation_id: Optional[str],
        artifact_ids: Optional[list[str]] = None,
        artifact_tags: Optional[list[str]] = None,
    ) -> str:
        if not tenant_id or not motet_id:
            raise ValueError("tenant_id and motet_id are required for artifact RAG retrieval")
        if not role:
            raise ValueError("role is required for artifact RAG retrieval")
        if scope in (ArtifactRetrievalScope.CONVERSATION, ArtifactRetrievalScope.PRINCIPAL) and not principal_id:
            raise ValueError("principal_id is required for artifact RAG retrieval")
        if scope is ArtifactRetrievalScope.CONVERSATION and not conversation_id:
            raise ValueError("conversation_id is required for conversation-scoped artifact RAG retrieval")

        filters = [
            ("tenant_id", tenant_id),
            ("motet_id", motet_id),
            ("role", role),
        ]
        if principal_id and scope in (ArtifactRetrievalScope.CONVERSATION, ArtifactRetrievalScope.PRINCIPAL):
            filters.append(("principal_id", principal_id))
        if conversation_id and scope is ArtifactRetrievalScope.CONVERSATION:
            filters.append(("conversation_id", conversation_id))

        parts = [f"@{field}:{{{_escape_tag_query_value(str(value))}}}" for field, value in filters]
        if artifact_ids:
            escaped_ids = "|".join(_escape_tag_query_value(str(value)) for value in artifact_ids if str(value).strip())
            if escaped_ids:
                parts.append(f"@source_artifact_id:{{{escaped_ids}}}")
        for tag in _normalize_tags(artifact_tags):
            parts.append(f"@artifact_tags:{{{_escape_tag_query_value(tag)}}}")
        return " ".join(parts)

    def build_search_command(
        self,
        *,
        query_embedding: list[float],
        tenant_id: str,
        motet_id: str,
        principal_id: Optional[str],
        role: Optional[str],
        conversation_id: Optional[str],
        scope: ArtifactRetrievalScope = ArtifactRetrievalScope.CONVERSATION,
        artifact_ids: Optional[list[str]] = None,
        artifact_tags: Optional[list[str]] = None,
        top_k: int = 5,
    ) -> list[Any]:
        """Build an FT.SEARCH command for scoped artifact chunk KNN retrieval."""

        filter_query = self._required_filter(
            scope=scope,
            tenant_id=tenant_id,
            motet_id=motet_id,
            principal_id=principal_id,
            role=role,
            conversation_id=conversation_id,
            artifact_ids=artifact_ids,
            artifact_tags=artifact_tags,
        )
        vec_bytes = self._to_float32_bytes(query_embedding)
        return [
            "FT.SEARCH",
            self.index_name(tenant_id),
            f"({filter_query})=>[KNN {max(int(top_k), 1)} @embedding $vec AS vector_distance]",
            "PARAMS",
            "2",
            "vec",
            vec_bytes,
            "RETURN",
            "29",
            "source_artifact_id",
            "derived_artifact_id",
            "chunk_index",
            "chunk_kind",
            "content_text",
            "structured_payload",
            "content_hash",
            "coordinates",
            "byte_range_start",
            "byte_range_end",
            "page_number",
            "content_type",
            "artifact_tags",
            "filename",
            "modality",
            "confidence",
            "prep_strategy_id",
            "prep_strategy_version",
            "prep_state",
            "index_state",
            "chunk_cache_key",
            "tenant_id",
            "principal_id",
            "motet_id",
            "role",
            "conversation_id",
            "created_at",
            "expires_at",
            "vector_distance",
            "DIALECT",
            "2",
        ]

    def search(
        self,
        *,
        query_embedding: list[float],
        tenant_id: str,
        motet_id: str,
        principal_id: Optional[str],
        role: Optional[str],
        conversation_id: Optional[str],
        scope: ArtifactRetrievalScope = ArtifactRetrievalScope.CONVERSATION,
        artifact_ids: Optional[list[str]] = None,
        artifact_tags: Optional[list[str]] = None,
        top_k: int = 5,
    ) -> list[ArtifactChunkSearchResult]:
        """Search artifact chunks with mandatory scope filters."""

        self.ensure_index(tenant_id=tenant_id)
        command = self.build_search_command(
            query_embedding=query_embedding,
            tenant_id=tenant_id,
            motet_id=motet_id,
            principal_id=principal_id,
            role=role,
            conversation_id=conversation_id,
            scope=scope,
            artifact_ids=artifact_ids,
            artifact_tags=artifact_tags,
            top_k=top_k,
        )
        response = self._redis.execute_command(*command)
        return self._parse_search_response(response)

    def _parse_search_response(self, response: Any) -> list[ArtifactChunkSearchResult]:
        if not isinstance(response, list) or not response:
            return []
        results: list[ArtifactChunkSearchResult] = []
        idx = 1
        while idx + 1 < len(response):
            fields = response[idx + 1]
            idx += 2
            if not isinstance(fields, list):
                continue
            doc: dict[str, Any] = {}
            for field_idx in range(0, len(fields), 2):
                if field_idx + 1 >= len(fields):
                    break
                doc[str(_decode(fields[field_idx]))] = _decode(fields[field_idx + 1])
            try:
                distance = float(doc.get("vector_distance") or 0.0)
                similarity = max(0.0, min(1.0, 1.0 - distance)) if math.isfinite(distance) else 0.0
                results.append(
                    ArtifactChunkSearchResult(
                        source_artifact_id=str(doc.get("source_artifact_id") or ""),
                        derived_artifact_id=str(doc.get("derived_artifact_id") or "") or None,
                        chunk_index=int(float(doc.get("chunk_index") or 0)),
                        chunk_kind=_decode_chunk_kind(doc.get("chunk_kind")),
                        content_text=str(doc.get("content_text") or ""),
                        structured_payload=_decode_json(doc.get("structured_payload"), None),
                        content_hash=str(doc.get("content_hash") or ""),
                        coordinates=_decode_coordinates(doc),
                        content_type=str(doc.get("content_type") or "text/plain"),
                        artifact_tags=_decode_tags(doc.get("artifact_tags")),
                        filename=str(doc.get("filename") or "") or None,
                        modality=_decode_modality(doc.get("modality")),
                        confidence=float(doc.get("confidence") or 1.0),
                        prep_strategy_id=str(doc.get("prep_strategy_id") or "text_default"),
                        prep_strategy_version=str(doc.get("prep_strategy_version") or "1.0.0"),
                        prep_state=_decode_prep_state(doc.get("prep_state")),
                        index_state=_decode_index_state(doc.get("index_state")),
                        chunk_cache_key=str(doc.get("chunk_cache_key") or ""),
                        tenant_id=str(doc.get("tenant_id") or ""),
                        principal_id=str(doc.get("principal_id") or ""),
                        motet_id=str(doc.get("motet_id") or ""),
                        role=str(doc.get("role") or "user"),
                        conversation_id=str(doc.get("conversation_id") or ""),
                        created_at=float(doc.get("created_at") or 0),
                        expires_at=float(doc["expires_at"]) if str(doc.get("expires_at") or "").strip() else None,
                        vector_distance=distance,
                        similarity=similarity,
                    )
                )
            except Exception as e:
                logger.warning("artifact_rag_search_result_parse_failed", error=str(e), raw_fields=doc)
        return results

    def _parse_hash_mapping(self, mapping: Any) -> Optional[ArtifactChunkSearchResult]:
        if not isinstance(mapping, dict) or not mapping:
            return None
        doc = {str(_decode(key)): _decode(value) for key, value in mapping.items()}
        try:
            return ArtifactChunkSearchResult(
                source_artifact_id=str(doc.get("source_artifact_id") or ""),
                derived_artifact_id=str(doc.get("derived_artifact_id") or "") or None,
                chunk_index=int(float(doc.get("chunk_index") or 0)),
                chunk_kind=_decode_chunk_kind(doc.get("chunk_kind")),
                content_text=str(doc.get("content_text") or ""),
                structured_payload=_decode_json(doc.get("structured_payload"), None),
                content_hash=str(doc.get("content_hash") or ""),
                coordinates=_decode_coordinates(doc),
                content_type=str(doc.get("content_type") or "text/plain"),
                artifact_tags=_decode_tags(doc.get("artifact_tags")),
                filename=str(doc.get("filename") or "") or None,
                modality=_decode_modality(doc.get("modality")),
                confidence=float(doc.get("confidence") or 1.0),
                prep_strategy_id=str(doc.get("prep_strategy_id") or "text_default"),
                prep_strategy_version=str(doc.get("prep_strategy_version") or "1.0.0"),
                prep_state=_decode_prep_state(doc.get("prep_state")),
                index_state=_decode_index_state(doc.get("index_state")),
                chunk_cache_key=str(doc.get("chunk_cache_key") or ""),
                tenant_id=str(doc.get("tenant_id") or ""),
                principal_id=str(doc.get("principal_id") or ""),
                motet_id=str(doc.get("motet_id") or ""),
                role=str(doc.get("role") or "user"),
                conversation_id=str(doc.get("conversation_id") or ""),
                created_at=float(doc.get("created_at") or 0),
                expires_at=float(doc["expires_at"]) if str(doc.get("expires_at") or "").strip() else None,
                vector_distance=float(doc.get("vector_distance") or 1.0),
                similarity=float(doc.get("similarity") or 0.0),
            )
        except Exception as e:
            logger.warning("artifact_rag_hash_result_parse_failed", error=str(e), raw_fields=doc)
            return None

    def list_scoped_chunks(
        self,
        *,
        tenant_id: str,
        motet_id: str,
        principal_id: Optional[str],
        role: Optional[str],
        conversation_id: Optional[str],
        scope: ArtifactRetrievalScope = ArtifactRetrievalScope.CONVERSATION,
        artifact_ids: Optional[list[str]] = None,
        artifact_tags: Optional[list[str]] = None,
        max_candidates: int = 200,
    ) -> list[ArtifactChunkSearchResult]:
        """Return bounded scoped chunks for application-layer lexical retrieval."""

        self._required_filter(
            scope=scope,
            tenant_id=tenant_id,
            motet_id=motet_id,
            principal_id=principal_id,
            role=role,
            conversation_id=conversation_id,
            artifact_ids=artifact_ids,
            artifact_tags=artifact_tags,
        )
        required_tags = set(_normalize_tags(artifact_tags))
        prefixes = self.key_prefixes(tenant_id)
        if artifact_ids:
            patterns = [
                f"{prefix}{source_id}:*"
                for prefix in prefixes
                for source_id in artifact_ids
                if str(source_id).strip()
            ]
        else:
            patterns = [f"{prefix}*" for prefix in prefixes]
        chunks: list[ArtifactChunkSearchResult] = []
        seen_keys: set[str] = set()
        limit = max(1, int(max_candidates or 200))
        for pattern in patterns:
            for key in self._scan_keys(pattern=pattern):
                key_str = str(_decode(key))
                if key_str in seen_keys:
                    continue
                seen_keys.add(key_str)
                raw = self._redis.hgetall(key)
                chunk = self._parse_hash_mapping(raw)
                if chunk is None:
                    continue
                if chunk.tenant_id != tenant_id or chunk.motet_id != motet_id or chunk.role != role:
                    continue
                if scope in (ArtifactRetrievalScope.CONVERSATION, ArtifactRetrievalScope.PRINCIPAL):
                    if chunk.principal_id != str(principal_id or ""):
                        continue
                if scope is ArtifactRetrievalScope.CONVERSATION and chunk.conversation_id != str(conversation_id or ""):
                    continue
                if required_tags and not required_tags.issubset(set(chunk.artifact_tags or [])):
                    continue
                chunks.append(chunk)
                if len(chunks) >= limit:
                    return chunks
        return chunks
