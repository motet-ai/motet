"""
Motet - Memory Management

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Memory management system for the Motet distributed framework.
    Provides in-memory, Redis, and vector store implementations with unified interface.

Dependencies:
    - Redis: Distributed memory storage
    - Valkey Search: LTM semantic index
    - ChromaDB, pgvector: Optional for non-memory use (e.g. migration, tests)
    - Base store implementations

Usage:
    from motet.core.memory import InMemoryStore, RedisStore, ChromaVectorStore
    
    # Create memory store
    store = InMemoryStore(ttl_seconds=3600)
    
    # Store and retrieve data
    await store.store("key", data)
    data = await store.retrieve("key")

Notes:
    - Supports multiple storage backends
    - Provides unified interface across implementations
    - Includes TTL and expiration support
    - Integrates with distributed architecture
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

from ..types import BaseRegistry
from .base import BaseStore, VectorStoreBase, CacheMixin
from .inmemory import InMemoryStore
from .redis_store import RedisStore
from .chroma_store import ChromaVectorStore
from .pgvector_store import PGVectorStore
from .valkey_vector_store import ValkeyVectorStore


class StoreRegistry(BaseRegistry[Any]):
    def __init__(self) -> None:
        self._map: Dict[Tuple[str, str], Callable[..., Any]] = {}

    def register(self, key1: str, key2: str, factory: Callable[..., Any], **metadata: Any) -> None:
        _ = metadata  # Protocol allows extra metadata; this registry only stores factories
        self._map[(key1, key2)] = factory

    def build(self, key1: str, key2: str, **kwargs: Any) -> Any:
        cls = self._map.get((key1, key2))
        if not cls:
            raise KeyError(f"No store registered for kind={key1} backend={key2}")
        return cls(**kwargs)

    def get(self, key1: str, key2: str) -> Optional[Callable[..., Any]]:  # type: ignore[name-defined]
        return self._map.get((key1, key2))

    def list(self, key1_filter: Optional[str] = None) -> List[Tuple[str, str]]:  # type: ignore[name-defined]
        keys = list(self._map.keys())
        if key1_filter is not None:
            keys = [k for k in keys if k[0] == key1_filter]
        return keys

    def supports(self, key1: str, key2: str) -> bool:
        return (key1, key2) in self._map


store_registry = StoreRegistry()
store_registry.register("memory", "inmemory", InMemoryStore)
store_registry.register("memory", "redis", RedisStore)
store_registry.register("vector", "valkey", ValkeyVectorStore)


__all__ = [
    "BaseStore",
    "VectorStoreBase",
    "CacheMixin",
    "InMemoryStore",
    "RedisStore",
    "ChromaVectorStore",
    "PGVectorStore",
    "ValkeyVectorStore",
    "store_registry",
]


