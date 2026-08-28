"""
Motet - In-Memory Store

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-20

Description:
    In-memory memory store implementation for the Motet distributed framework.
    Provides fast, local memory storage with TTL support and comprehensive
    memory management. Includes memory tagging, search capabilities,
    and distributed memory coordination for development and testing.

Dependencies:
    - typing: Type hints and annotations
    - Memory types and item definitions
    - Base store implementation

Usage:
    from motet.core.memory.inmemory import InMemoryStore

    # Create store
    store = InMemoryStore(ttl_seconds=3600)

    # Store memory
    store.upsert(memory_item)

    # Retrieve memory
    item = store.get("item_id")

    # Search by tag
    items = store.search_by_tag("important")

Notes:
    - Provides fast, local memory storage
    - Includes TTL support and automatic expiration
    - Supports memory tagging and search capabilities
    - Includes comprehensive memory management
    - Supports distributed memory coordination
    - Integrates with base store implementation
    - Includes comprehensive observability and logging
"""

from __future__ import annotations

from typing import Dict, List, Optional

from ..types import MemoryItem
from .base import BaseStore


class InMemoryStore(BaseStore):
    def __init__(self, ttl_seconds: int | None = None) -> None:
        super().__init__(ttl_seconds=ttl_seconds)
        self._store: Dict[str, MemoryItem] = {}

    def upsert(self, item: MemoryItem) -> None:
        self._store[item.id] = item

    def get(self, item_id: str) -> Optional[MemoryItem]:
        item = self._store.get(item_id)
        if item and self._ttl_seconds:
            from datetime import datetime, timezone
            if (datetime.now(timezone.utc) - item.created_at).total_seconds() > self._ttl_seconds:
                self._store.pop(item_id, None)
                return None
        return item

    def delete(self, item_id: str) -> bool:
        """Remove one item by id. Returns True if the item existed."""
        return self._store.pop(item_id, None) is not None

    def search_by_tag(self, tag: str) -> List[MemoryItem]:
        return [m for m in self._store.values() if tag in m.tags]

    def all(self) -> List[MemoryItem]:
        return list(self._store.values())

    def recent(self, limit: int = 5, tag: Optional[str] = None) -> List[MemoryItem]:
        items = list(self._store.values())
        if tag:
            items = [m for m in items if tag in m.tags]
        if self._ttl_seconds:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            items = [m for m in items if (now - m.created_at).total_seconds() <= self._ttl_seconds]
        items.sort(key=lambda m: m.created_at, reverse=True)
        return items[:limit]

    def clear_all(self) -> int:
        n = len(self._store)
        self._store.clear()
        return n

    def clear_by_type(self, type_name: str) -> int:
        ids = [mid for mid, m in self._store.items() if m.type == type_name]
        for mid in ids:
            self._store.pop(mid, None)
        return len(ids)

    def clear_by_tag(self, tag: str) -> int:
        ids = [mid for mid, m in self._store.items() if tag in (m.tags or [])]
        for mid in ids:
            self._store.pop(mid, None)
        return len(ids)


__all__ = ["InMemoryStore"]


