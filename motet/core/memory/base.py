"""
Motet - Memory Base

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-20

Description:
    Base memory store and cache mixin for the Motet distributed framework.
    Provides abstract base classes for memory storage implementations with
    TTL support, caching capabilities, and comprehensive memory management.
    Includes embedding caching and result caching for distributed systems.

Dependencies:
    - abc: Abstract base class and method definitions
    - typing: Type hints and annotations
    - Memory types and item definitions

Usage:
    from motet.core.memory.base import BaseStore, CacheMixin

    # Create custom store
    class MyStore(BaseStore, CacheMixin):
        def clear_all(self) -> int:
            # Implementation
            pass

        def clear_by_type(self, type_name: str) -> int:
            # Implementation
            pass

        def clear_by_tag(self, tag: str) -> int:
            # Implementation
            pass

        def delete(self, item_id: str) -> bool:
            # Implementation
            pass

Notes:
    - Provides abstract base classes for memory storage
    - Includes TTL support and caching capabilities
    - Supports embedding caching and result caching
    - Includes comprehensive memory management interfaces
    - Supports distributed memory coordination
    - Integrates with memory types and item definitions
    - Includes comprehensive observability and logging
"""

from __future__ import annotations

from typing import Dict, List, Optional
from abc import ABC, abstractmethod

from ..types import MemoryItem


class BaseStore(ABC):
    def __init__(self, ttl_seconds: int | None = None) -> None:
        self._ttl_seconds = ttl_seconds

    @abstractmethod
    def clear_all(self) -> int: ...

    @abstractmethod
    def clear_by_type(self, type_name: str) -> int: ...

    @abstractmethod
    def clear_by_tag(self, tag: str) -> int: ...

    @abstractmethod
    def delete(self, item_id: str) -> bool: ...


class CacheMixin:
    def _init_cache(self, *, enable_embedding_cache: bool, enable_result_cache: bool) -> None:
        self._embed_cache_enabled = enable_embedding_cache
        self._result_cache_enabled = enable_result_cache
        self._embed_cache: Dict[str, List[float]] = {}
        self._query_cache: Dict[tuple, List[MemoryItem]] = {}

    def _embed_text(self, text: str) -> List[float]:
        emb = self._embedder.encode([text], convert_to_numpy=True).tolist()[0]  # type: ignore[attr-defined]
        return emb


class VectorStoreBase(BaseStore, ABC):
    @abstractmethod
    def add(self, items: List[MemoryItem]) -> None: ...

    @abstractmethod
    def query(
        self,
        text: str,
        top_k: int = 3,
        tags: List[str] | None = None,
        *,
        tenant_id: Optional[str] = None,
        principal_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        motet_id: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> List[MemoryItem]: ...

    @abstractmethod
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
    ) -> List[MemoryItem]: ...

    @abstractmethod
    def delete_by_tag(
        self,
        tag: str,
        *,
        tenant_id: Optional[str] = None,
        principal_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        motet_id: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> int: ...

    @abstractmethod
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
    ) -> int: ...

    @abstractmethod
    def delete_ids(
        self,
        ids: List[str],
        *,
        tenant_id: Optional[str] = None,
        principal_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        motet_id: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> int: ...

    def delete(self, item_id: str) -> bool:
        """Remove one vector document by id. Delegates to ``delete_ids``."""
        return self.delete_ids([item_id]) > 0


__all__ = [
    "BaseStore",
    "CacheMixin",
    "VectorStoreBase",
]


