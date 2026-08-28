"""
Motet - Redis Store

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Redis-based memory store implementation for the Motet distributed framework.
    Provides distributed memory storage with Redis backend and comprehensive
    memory management. Includes memory indexing, tagging, search capabilities,
    and distributed memory coordination for production systems.

Dependencies:
    - typing: Type hints and annotations
    - Memory types and item definitions
    - Base store implementation
    - Redis client for distributed storage

Usage:
    from motet.core.memory.redis_store import RedisStore

    # Create store
    store = RedisStore(redis_client)

    # Store memory
    store.upsert(memory_item)

    # Retrieve memory
    item = store.get("item_id")

    # Search by tag
    items = store.search_by_tag("important")

Notes:
    - Provides distributed memory storage with Redis backend
    - Includes memory indexing and timestamp management
    - Supports memory tagging and search capabilities
    - Includes comprehensive memory management
    - Supports distributed memory coordination
    - Integrates with Redis client and base store
    - Includes comprehensive observability and logging
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, TYPE_CHECKING

import structlog

from ..types import MemoryItem
from .base import BaseStore
from ..distributed.tenant_keys import (
    decode_redis_id,
    tenant_key,
    zrevrange_ids_with_fallback,
)
from ..security.encrypted_payload_store import IsolationContext, SyncEncryptedPayloadStore
from ..security.encryption_contexts import EncryptionContext

logger = structlog.get_logger(__name__)

if TYPE_CHECKING:  # pragma: no cover
    from ..security.encryption_service import EncryptionService


class RedisStore(BaseStore):
    """
    Redis-backed memory store with multi-motet/multi-tenant isolation (ADR-0027).
    
    Uses hierarchical Redis keys (issue #218 collapsed shape only):
    - {tenant_id}:mem:{motet_id}:{memory_id}
    - {tenant_id}:mem:{motet_id}:idx:{scope}
    - {tenant_id}:mem:{motet_id}:principal:{principal_id}:idx
    - {tenant_id}:mem:{motet_id}:conversation:{conversation_id}:idx
    
    The conversation_id index enables O(k) retrieval of conversation memories
    instead of O(n) where k = items in conversation and n = total memories.
    """
    def __init__(
        self, 
        redis_client,
        motet_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        encryption_service=None,
    ) -> None:
        super().__init__(ttl_seconds=None)
        self._r = redis_client
        self._motet_id = motet_id or "default"
        self._tenant_id = tenant_id or "default"
        # Collapsed logical prefix (issue #218). Readers use ``{tenant}:mem:`` only.
        self._logical_prefix = f"mem:{self._motet_id}:"
        self._prefix = tenant_key(self._tenant_id, self._logical_prefix.rstrip(":")) + ":"
        try:
            if encryption_service is not None:
                self._encryption_service = encryption_service
            else:
                from ..security.encryption_service import get_encryption_service

                self._encryption_service = get_encryption_service()
        except Exception as exc:
            logger.warning("memory_encryption_service_unavailable", error=str(exc))
            self._encryption_service = None

        # Centralize encrypted hash storage (ADR-0056): store `_envelope` + isolation fields.
        self._encrypted_store: Optional[SyncEncryptedPayloadStore] = None

    def _key(self, item_id: str) -> str:
        """Generate hierarchical Redis key with motet/tenant isolation"""
        return f"{self._prefix}{item_id}"

    def _item_keys(self, item_id: str) -> tuple[str, ...]:
        return (tenant_key(self._tenant_id, f"{self._logical_prefix}{item_id}"),)

    def _index_key(self, scope: str = "global") -> str:
        """Generate hierarchical index key by scope"""
        return f"{self._prefix}idx:{scope}"

    def _index_keys(self, scope: str = "global") -> tuple[str, ...]:
        return (tenant_key(self._tenant_id, f"{self._logical_prefix}idx:{scope}"),)

    def _principal_index_key(self, principal_id: str) -> str:
        """Generate principal-specific index key"""
        return f"{self._prefix}principal:{principal_id}:idx"

    def _principal_index_keys(self, principal_id: str) -> tuple[str, ...]:
        return (tenant_key(
            self._tenant_id, f"{self._logical_prefix}principal:{principal_id}:idx"
        ),)

    def _conversation_index_key(self, conversation_id: str) -> str:
        """Generate conversation-specific index key for O(k) retrieval"""
        return f"{self._prefix}conversation:{conversation_id}:idx"

    def _conversation_index_keys(self, conversation_id: str) -> tuple[str, ...]:
        return (tenant_key(
            self._tenant_id,
            f"{self._logical_prefix}conversation:{conversation_id}:idx",
        ),)

    def _zrevrange_ids(self, keys: tuple[str, ...], start: int = 0, end: int = -1) -> List[str]:
        return zrevrange_ids_with_fallback(self._r, keys, start=start, end=end)

    def conversation_index_count(self, conversation_id: str) -> int:
        """Member count of the conversation index. Does not decrypt payloads."""
        return len(self._zrevrange_ids(self._conversation_index_keys(conversation_id)))

    def _zrem_all(self, keys: tuple[str, ...], member: str) -> None:
        for key in keys:
            try:
                self._r.zrem(key, member)
            except Exception:
                continue

    def _drop_index_members(self, mid: str, item: Optional[MemoryItem]) -> None:
        self._zrem_all(self._index_keys("global"), mid)
        if not item:
            return
        scope = item.scope_type or "working"
        self._zrem_all(self._index_keys(scope), mid)
        if item.principal_id:
            self._zrem_all(self._principal_index_keys(item.principal_id), mid)
        if item.conversation_id:
            self._zrem_all(self._conversation_index_keys(item.conversation_id), mid)

    def upsert(self, item: MemoryItem) -> None:
        """
        Store memory item with motet/tenant isolation (ADR-0027).
        
        Automatically populates motet_id and tenant_id if not set,
        and adds to appropriate scope-based indices.
        """
        # Ensure motet_id and tenant_id are set (ADR-0027)
        if not item.motet_id:
            item.motet_id = self._motet_id
        if not item.tenant_id:
            raise ValueError("Memory encryption requires tenant_id")
        if not self._encryption_service:
            raise RuntimeError("Encryption service not available for memory store")
        
        if self._encrypted_store is None:
            # Reuse existing redis client + encryption service; keep this explicit and fail-closed.
            self._encrypted_store = SyncEncryptedPayloadStore(
                service_name="memory_store",
                redis_client=self._r,
                encryption_service=self._encryption_service,
            )

        payload = {
            "schema_version": "redis-memory-envelope-v1",
            "memory": item.model_dump(mode="json"),
        }
        
        self._encrypted_store.put_json(
            key=self._key(item.id),
            payload=payload,
            isolation=IsolationContext(
                tenant_id=item.tenant_id,
                principal_id=item.principal_id,
                motet_id=item.motet_id,
            ),
            context=EncryptionContext.MEMORY.value,
        )
        
        # Add to scope-based index (ADR-0027)
        scope = item.scope_type or "working"
        self._r.zadd(self._index_key(scope), {item.id: item.created_at.timestamp()})
        
        # Add to global index for backward compatibility
        self._r.zadd(self._index_key("global"), {item.id: item.created_at.timestamp()})
        
        # Add to principal-specific index if applicable (ADR-0027)
        if item.principal_id:
            self._r.zadd(
                self._principal_index_key(item.principal_id),
                {item.id: item.created_at.timestamp()}
            )
        
        # Add to conversation-specific index for O(k) retrieval (performance optimization)
        if item.conversation_id:
            self._r.zadd(
                self._conversation_index_key(item.conversation_id),
                {item.id: item.created_at.timestamp()}
            )

    def delete(self, item_id: str) -> bool:
        """Remove one item and its scope indices. Returns True if the item existed."""
        item = self.get(item_id)
        self._r.delete(*self._item_keys(item_id))
        self._drop_index_members(item_id, item)
        return item is not None

    def get(self, item_id: str) -> Optional[MemoryItem]:
        try:
            if self._encrypted_store is None:
                self._encrypted_store = SyncEncryptedPayloadStore(
                    service_name="memory_store",
                    redis_client=self._r,
                    encryption_service=self._encryption_service,
                )

            payload = None
            for key in self._item_keys(item_id):
                try:
                    payload = self._encrypted_store.get_json(
                        key=key,
                        isolation=IsolationContext(
                            tenant_id=self._tenant_id,
                            # principal/motet are optional in reads; access checks are enforced when provided.
                            principal_id=None,
                            motet_id=self._motet_id,
                        ),
                        context=EncryptionContext.MEMORY.value,
                    )
                    if payload is not None:
                        break
                except KeyError:
                    continue
            if not isinstance(payload, dict):
                return None
            memory_data = payload.get("memory", payload)
            if not isinstance(memory_data, dict):
                return None
            return MemoryItem(**memory_data)
        except KeyError:
            return None
        except PermissionError as exc:
            # Tenant/motet/principal mismatch: do NOT delete - item belongs to another scope.
            logger.debug(
                "memory_scope_mismatch_skip",
                memory_id=item_id,
                tenant_id=self._tenant_id,
                motet_id=self._motet_id,
                error=str(exc),
            )
            return None
        except Exception as exc:
            logger.error(
                "memory_decryption_failed",
                error=str(exc),
                memory_id=item_id,
                tenant_id=self._tenant_id,
                exc_info=True,
            )
            # Keep ciphertext. AAD mismatch after a key RENAME looks like
            # corruption; deleting it permanently drops conversation history.
            return None

    def search_by_tag(self, tag: str, scope: str = "global") -> List[MemoryItem]:
        """Search memories by tag within a specific scope (ADR-0027)"""
        ids = self._zrevrange_ids(self._index_keys(scope))
        results: List[MemoryItem] = []
        for mid in ids:
            item = self.get(mid)
            if item and tag in (item.tags or []):
                results.append(item)
        return results

    def all(self, scope: str = "global") -> List[MemoryItem]:
        """Get all memories within a specific scope (ADR-0027)"""
        ids = self._zrevrange_ids(self._index_keys(scope))
        out: List[MemoryItem] = []
        for mid in ids:
            item = self.get(mid)
            if item:
                out.append(item)
        return out

    def recent(self, limit: int = 5, tag: Optional[str] = None, scope: str = "global") -> List[MemoryItem]:
        """Get recent memories within a specific scope (ADR-0027)"""
        ids = self._zrevrange_ids(self._index_keys(scope), start=0, end=limit - 1)
        items: List[MemoryItem] = []
        for mid in ids:
            item = self.get(mid)
            if item and (tag is None or tag in (item.tags or [])):
                items.append(item)
        return items

    def by_conversation(
        self,
        conversation_id: str,
        limit: int = 250,
        types: Optional[List[str]] = None,
    ) -> List[MemoryItem]:
        """
        Get memories for a specific conversation using indexed O(k) retrieval.
        
        This method uses the conversation_id index for efficient retrieval,
        avoiding the O(n) scan of all memories required by the generic `all()` method.
        
        Args:
            conversation_id: The conversation ID to retrieve memories for
            limit: Maximum number of memories to return (default 250)
            types: Optional list of memory types to filter (e.g., ["conversation_turn", "tool_invocation"])
            
        Returns:
            List of MemoryItem objects for the conversation, sorted by timestamp (most recent first)
        """
        # Use the conversation-specific index for O(k) retrieval
        ids = self._zrevrange_ids(
            self._conversation_index_keys(conversation_id), start=0, end=limit - 1
        )
        
        items: List[MemoryItem] = []
        for mid in ids:
            item = self.get(mid)
            if item:
                # Filter by types if specified
                if types and item.type not in types:
                    continue
                items.append(item)
        
        return items

    def by_principal(
        self,
        principal_id: str,
        limit: int = 100,
        types: Optional[List[str]] = None,
    ) -> List[MemoryItem]:
        """
        Get memories for a specific principal using indexed O(k) retrieval.
        
        This method uses the principal_id index for efficient retrieval,
        avoiding the O(n) scan of all memories required by the generic `all()` method.
        
        Args:
            principal_id: The principal/user ID to retrieve memories for
            limit: Maximum number of memories to return (default 100)
            types: Optional list of memory types to filter
            
        Returns:
            List of MemoryItem objects for the principal, sorted by timestamp (most recent first)
        """
        # Use the principal-specific index for O(k) retrieval
        ids = self._zrevrange_ids(
            self._principal_index_keys(principal_id), start=0, end=limit - 1
        )
        
        items: List[MemoryItem] = []
        for mid in ids:
            item = self.get(mid)
            if item:
                # Filter by types if specified
                if types and item.type not in types:
                    continue
                items.append(item)
        
        return items

    def _all_ids(self, scope: str = "global") -> List[str]:
        """Get all memory IDs within a specific scope"""
        return self._zrevrange_ids(self._index_keys(scope))

    def clear_all(self) -> int:
        """Clear all memories for this motet/tenant (ADR-0027)"""
        ids = self._all_ids(scope="global")
        for mid in ids:
            item = self.get(mid)
            self._r.delete(*self._item_keys(mid))
            self._drop_index_members(mid, item)
        return len(ids)

    def clear_by_type(self, type_name: str) -> int:
        """Clear memories by type within this motet/tenant (ADR-0027)"""
        removed = 0
        for mid in self._all_ids(scope="global"):
            item = self.get(mid)
            if item and item.type == type_name:
                self._r.delete(*self._item_keys(mid))
                self._drop_index_members(mid, item)
                removed += 1
        return removed

    def clear_by_tag(self, tag: str) -> int:
        """Clear memories by tag within this motet/tenant (ADR-0027)"""
        removed = 0
        for mid in self._all_ids(scope="global"):
            item = self.get(mid)
            if item and tag in (item.tags or []):
                self._r.delete(*self._item_keys(mid))
                self._drop_index_members(mid, item)
                removed += 1
        return removed

__all__ = ["RedisStore"]

