"""
Motet - Chroma Vector Store

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-20

Description:
    Chroma vector store implementation for the Motet distributed framework.
    Provides comprehensive vector storage and retrieval capabilities using ChromaDB
    with sentence transformers. Includes embedding caching, result caching,
    and distributed memory coordination for distributed systems.

Dependencies:
    - chromadb: Vector database for storage and retrieval
    - sentence_transformers: Embedding model for text vectorization
    - typing: Type hints and annotations
    - Base vector store and cache mixins

Usage:
    from motet.core.memory.chroma_store import ChromaVectorStore

    # Create store
    store = ChromaVectorStore(
        collection_name="memories",
        persist_dir="/path/to/persist",
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
    - Integrates with ChromaDB and sentence transformers
    - Supports distributed memory management
    - Includes comprehensive observability and monitoring
"""

from __future__ import annotations

import structlog
from typing import Any, Dict, List, Mapping, Optional, cast

from ..types import MemoryItem
from .base import VectorStoreBase, CacheMixin

logger = structlog.get_logger(__name__)


def _chroma_metadata_row_to_memory_item(meta: Mapping[str, Any]) -> MemoryItem:
    """Rebuild a MemoryItem from flattened Chroma metadata (see add() sanitization)."""
    reconstructed_meta: Dict[str, Any] = {}
    nested_metadata: Dict[str, Any] = {}
    for key, value in meta.items():
        if key.startswith("meta_"):
            nested_key = key[5:]
            nested_metadata[nested_key] = value
        elif key == "tags" and isinstance(value, str):
            reconstructed_meta[key] = [t.strip() for t in value.split(",") if t.strip()]
        elif key == "created_at" and isinstance(value, str):
            from datetime import datetime

            try:
                reconstructed_meta[key] = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                reconstructed_meta[key] = value
        else:
            reconstructed_meta[key] = value
    if nested_metadata:
        reconstructed_meta["metadata"] = nested_metadata
    return MemoryItem.model_validate(reconstructed_meta)


class ChromaVectorStore(VectorStoreBase, CacheMixin):
    def __init__(
        self,
        collection_name: str,
        persist_dir: Optional[str] = None,
        embedding_model: str = "sentence-transformers/all-MiniLM-L12-v2",
        *,
        enable_embedding_cache: bool = True,
        enable_result_cache: bool = False,
    ) -> None:
        import chromadb  # type: ignore
        from sentence_transformers import SentenceTransformer  # type: ignore

        self._client = chromadb.PersistentClient(path=persist_dir) if persist_dir else chromadb.Client()
        self._collection = self._client.get_or_create_collection(collection_name)
        self._embedder = SentenceTransformer(embedding_model)
        self._init_cache(enable_embedding_cache=enable_embedding_cache, enable_result_cache=enable_result_cache)
        super().__init__(ttl_seconds=None)

    def add(self, items: List[MemoryItem]) -> None:
        texts = [m.content for m in items]
        ids = [m.id for m in items]
        embeddings = []
        for t in texts:
            embeddings.append(self._embed_text(t))
        # Sanitize metadata: ChromaDB only accepts str, int, float, bool, SparseVector, or None
        # Convert datetime objects to ISO format strings, lists to comma-separated strings,
        # and flatten nested dicts
        sanitized_metadatas = []
        for m in items:
            metadata = m.model_dump()
            # Flatten nested metadata dict into top-level keys (prefix with "meta_")
            if "metadata" in metadata and isinstance(metadata["metadata"], dict):
                nested_meta = metadata.pop("metadata")
                for key, value in nested_meta.items():
                    # Prefix nested keys to avoid conflicts
                    metadata[f"meta_{key}"] = value
            # Convert datetime objects to ISO format strings
            if "created_at" in metadata and metadata["created_at"] is not None:
                if hasattr(metadata["created_at"], "isoformat"):
                    metadata["created_at"] = metadata["created_at"].isoformat()
                elif isinstance(metadata["created_at"], str):
                    pass  # Already a string
            # Convert lists to comma-separated strings (ChromaDB doesn't accept lists)
            if "tags" in metadata and isinstance(metadata["tags"], list):
                metadata["tags"] = ",".join(str(tag) for tag in metadata["tags"]) if metadata["tags"] else None
            # Remove any remaining non-primitive values and None values
            sanitized = {}
            for key, value in metadata.items():
                if value is None:
                    # Skip None values - ChromaDB doesn't accept them
                    continue
                elif isinstance(value, (str, int, float, bool)):
                    sanitized[key] = value
                elif isinstance(value, list):
                    sanitized[key] = ",".join(str(v) for v in value) if value else ""
                elif hasattr(value, "isoformat"):  # datetime
                    sanitized[key] = value.isoformat()
                else:
                    # Skip complex types
                    continue
            sanitized_metadatas.append(sanitized)
        self._collection.add(ids=ids, documents=texts, embeddings=embeddings, metadatas=sanitized_metadatas)

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
            cache_key = (text, tuple(tags or []), top_k)
            if cache_key in self._query_cache:
                return self._query_cache[cache_key]
        embedding = self._embed_text(text)
        res = self._collection.query(query_embeddings=[embedding], n_results=top_k)
        out: List[MemoryItem] = []
        raw_metas = res.get("metadatas")
        meta_rows: List[Mapping[str, Any]] = []
        if isinstance(raw_metas, list) and raw_metas:
            first = raw_metas[0]
            if isinstance(first, list):
                meta_rows = [cast(Mapping[str, Any], m) for m in first if isinstance(m, dict)]
        for meta in meta_rows:
            try:
                item = _chroma_metadata_row_to_memory_item(meta)
                if tags and not any(t in (item.tags or []) for t in tags):
                    continue
                out.append(item)
            except Exception as e:
                logger.debug("chroma_memory_item_reconstruction_skipped", error=str(e))
                continue
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
        out: List[MemoryItem] = []
        try:
            res = self._collection.get(where={"tags": {"$contains": tag}}, limit=limit)  # type: ignore[attr-defined]
            metadatas = res.get("metadatas") or []
            for meta in metadatas:
                if not isinstance(meta, dict):
                    continue
                try:
                    out.append(_chroma_metadata_row_to_memory_item(cast(Mapping[str, Any], meta)))
                except Exception as e:
                    logger.debug("chroma_memory_item_construction_skipped", error=str(e))
                    continue
        except Exception:
            for item in self.query(tag, top_k=limit):
                if tag in (item.tags or []):
                    out.append(item)
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
        try:
            self._collection.delete(where={"tags": {"$contains": tag}})  # type: ignore[attr-defined]
            return 0
        except Exception:
            try:
                items = self.list_by_tag(tag, limit=1000)
                if not items:
                    return 0
                ids = [m.id for m in items]
                self._collection.delete(ids=ids)  # type: ignore[attr-defined]
                return len(ids)
            except Exception:
                return 0

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
        try:
            self._collection.delete(ids=ids)  # type: ignore[attr-defined]
            return len(ids)
        except Exception:
            return 0

    def update_tags(self, ids: List[str], tags: List[str], op: str, *, tenant_id: Optional[str] = None, principal_id: Optional[str] = None, conversation_id: Optional[str] = None, motet_id: Optional[str] = None, agent_id: Optional[str] = None) -> int:
        # Chroma python client lacks partial metadata update; emulate by fetching, merging, and re-adding
        try:
            if not ids:
                return 0
            # Fetch existing metadatas
            res = self._collection.get(ids=ids)  # type: ignore[attr-defined]
            metas = res.get("metadatas") or []
            id_list = res.get("ids") or []
            updated = 0
            for idx, mid in enumerate(id_list):
                raw = metas[idx] if idx < len(metas) else {}
                meta_map: Dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
                try:
                    item = _chroma_metadata_row_to_memory_item(meta_map)
                except Exception:
                    tags_val = meta_map.get("tags")
                    if isinstance(tags_val, str):
                        tag_list = [t.strip() for t in tags_val.split(",") if t.strip()]
                    elif isinstance(tags_val, list):
                        tag_list = [str(t) for t in tags_val]
                    else:
                        tag_list = []
                    item = MemoryItem(
                        id=str(mid),
                        type=str(meta_map.get("type") or "rag_chunk"),
                        content=str(meta_map.get("content") or ""),
                        tags=tag_list,
                        metadata=dict(meta_map),
                    )
                current = set(item.tags or [])
                if op == "add":
                    current.update(tags)
                elif op == "remove":
                    for t in tags:
                        current.discard(t)
                elif op == "set":
                    current = set(tags)
                item.tags = sorted(list(current))
                # Re-add with updated metadata only (IDs must match; Chroma upsert behavior applies)
                self._collection.update(ids=[item.id], metadatas=[item.model_dump()])  # type: ignore[attr-defined]
                updated += 1
            return updated
        except Exception:
            return 0
    
    def clear_all(self) -> int:
        """Clear all items from the collection."""
        try:
            # Get all IDs
            res = self._collection.get()  # type: ignore[attr-defined]
            ids = res.get("ids") or []
            if not ids:
                return 0
            # Delete all items
            self._collection.delete(ids=ids)  # type: ignore[attr-defined]
            # Clear caches
            if hasattr(self, '_query_cache'):
                self._query_cache.clear()
            if hasattr(self, '_embed_cache'):
                self._embed_cache.clear()
            return len(ids)
        except Exception:
            return 0
    
    def clear_by_type(self, type_name: str) -> int:
        """Clear all items of a specific type."""
        try:
            # Get all items with matching type
            res = self._collection.get(where={"type": type_name})  # type: ignore[attr-defined]
            ids = res.get("ids") or []
            if not ids:
                return 0
            # Delete matching items
            self._collection.delete(ids=ids)  # type: ignore[attr-defined]
            # Clear caches
            if hasattr(self, '_query_cache'):
                self._query_cache.clear()
            return len(ids)
        except Exception:
            return 0
    
    def clear_by_tag(self, tag: str) -> int:
        """Clear all items with a specific tag."""
        # Use existing delete_by_tag implementation
        return self.delete_by_tag(tag)


__all__ = ["ChromaVectorStore"]


