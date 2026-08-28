"""
Motet - Unified Scoped Registry Base

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Shared scope and registry infrastructure for registrable platform artifacts.
    Provides grant-based access metadata (ScopeGrant, RegistryScope), request-side
    filters (ScopeFilter), and a generic ScopedRegistry with pool-agnostic locking.
    This module is the foundation for consistent namespacing, visibility
    checks, and lifecycle operations across registries.

Dependencies:
    - pydantic: Scope and filter models with validation/serialization
    - motet.core.workers.concurrency_primitives: WorkerLock for pool-agnostic synchronization
    - structlog: Structured registry lifecycle logging

Usage:
    from motet.core.registry.base import ScopedRegistry, RegistryScope, ScopeGrant

    class MyRegistry(ScopedRegistry[dict]):
        pass

    registry = MyRegistry(registry_name="my_registry")
    registry.register(
        "core.example",
        {"name": "example"},
        scope=RegistryScope(
            namespace="core",
            grants=[ScopeGrant(role="admin"), ScopeGrant(role="operator")],
        ),
    )

Notes:
    - Scope grants follow tenant:motet:role:principal hierarchy.
    - Scope key parsing fails closed for malformed input.
    - Registry methods are synchronized with WorkerLock and return copies where needed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Generic, List, Optional, Protocol, TypeVar

from pydantic import BaseModel, Field
import structlog

from motet.core.workers.concurrency_primitives import WorkerLock

logger = structlog.get_logger(__name__)

T = TypeVar("T")


class ScopeGrant(BaseModel):
    """A single access rule over tenant:motet:role:principal."""

    tenant_id: str = Field(default="*", description="Tenant selector. '*' matches any tenant.")
    motet_id: str = Field(default="*", description="Motet selector. '*' matches any motet.")
    role: str = Field(default="*", description="Role selector. '*' matches any role.")
    principal_id: str = Field(default="*", description="Principal selector. '*' matches any principal.")

    def matches(self, tenant_id: str, motet_id: str, role: str, principal_id: str) -> bool:
        """Return True when all grant fields match the request context."""
        return (
            (self.tenant_id == "*" or self.tenant_id == tenant_id)
            and (self.motet_id == "*" or self.motet_id == motet_id)
            and (self.role == "*" or self.role == role)
            and (self.principal_id == "*" or self.principal_id == principal_id)
        )

    def to_scope_key(self) -> str:
        """Serialize grant to tenant:motet:role:principal string."""
        return f"{self.tenant_id}:{self.motet_id}:{self.role}:{self.principal_id}"

    @classmethod
    def from_scope_key(cls, key: str) -> "ScopeGrant":
        """Deserialize strict 4-segment scope key and fail closed on malformed input."""
        parts = key.split(":")
        if len(parts) != 4:
            raise ValueError(
                f"Invalid scope key '{key}': expected 4 segments "
                "(tenant:motet:role:principal)"
            )
        if any(not segment for segment in parts):
            raise ValueError(f"Invalid scope key '{key}': empty segments are not allowed")
        return cls(
            tenant_id=parts[0],
            motet_id=parts[1],
            role=parts[2],
            principal_id=parts[3],
        )


class RegistryScope(BaseModel):
    """Visibility and ownership metadata attached to a registered item."""

    namespace: str = Field(default="core", description="Namespace for this item.")
    bundle_id: Optional[str] = Field(default=None, description="Bundle source. None for built-ins.")
    grants: List[ScopeGrant] = Field(
        default_factory=lambda: [ScopeGrant()],
        description="Access grants. Item visible when any grant matches.",
    )

    def is_visible_to(self, tenant_id: str, motet_id: str, role: str, principal_id: str) -> bool:
        """Return True if any grant allows the request context."""
        return any(grant.matches(tenant_id, motet_id, role, principal_id) for grant in self.grants)

    def scope_keys(self) -> List[str]:
        """Serialize grants for string-indexed stores."""
        return [grant.to_scope_key() for grant in self.grants]


class ScopeFilter(BaseModel):
    """Requester context used to evaluate grant visibility."""

    tenant_id: str = Field(default="*", description="Requester tenant ID.")
    motet_id: str = Field(default="*", description="Requester motet ID.")
    role: str = Field(default="*", description="Requester role.")
    principal_id: str = Field(default="*", description="Requester principal ID.")

    def matches_scope(self, scope: RegistryScope) -> bool:
        """Return True if scope grants allow this request context."""
        return scope.is_visible_to(
            tenant_id=self.tenant_id,
            motet_id=self.motet_id,
            role=self.role,
            principal_id=self.principal_id,
        )


@dataclass(frozen=True)
class RegistryEntry(Generic[T]):
    """Stored entry with item payload, scope metadata, and optional metadata."""

    key: str
    item: T
    scope: RegistryScope = field(default_factory=RegistryScope)
    metadata: Dict[str, Any] = field(default_factory=dict)


class RegistryProtocol(Protocol[T]):
    """
    Common public API contract for scope-aware registry consumers.

    This protocol intentionally focuses on shared lookup/list/visibility/lifecycle
    operations used by cross-registry consumers. Domain-specific registration
    signatures (for example ToolRegistry.register(...) vs command registries)
    remain on concrete classes.
    """

    def get(self, key: str) -> Optional[T]:
        """Return item payload for key or None."""
        ...

    def get_entry(self, key: str) -> Optional[RegistryEntry[T]]:
        """Return full entry for key or None."""
        ...

    def list_items(self) -> Dict[str, T]:
        """Return shallow copy of key -> item mapping."""
        ...

    def list_entries(self) -> List[RegistryEntry[T]]:
        """Return list copy of entries."""
        ...

    def list_visible(self, scope_filter: ScopeFilter) -> Dict[str, T]:
        """Return visible key -> item mapping for request context."""
        ...

    def list_visible_entries(self, scope_filter: ScopeFilter) -> List[RegistryEntry[T]]:
        """Return visible entries with metadata and scope."""
        ...

    def unregister(self, key: str) -> bool:
        """Remove key from registry. Returns True if removed."""
        ...

    def unregister_namespace(self, namespace: str) -> List[str]:
        """Remove all entries under namespace prefix and return removed keys."""
        ...


class ScopedRegistry(Generic[T]):
    """Generic registry with namespacing, scope filtering, and lifecycle operations."""

    def __init__(self, registry_name: str) -> None:
        self._registry_name = registry_name
        self._entries: Dict[str, RegistryEntry[T]] = {}
        self._lock = WorkerLock()

    def register(
        self,
        key: str,
        item: T,
        *,
        scope: Optional[RegistryScope] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Register or replace an item entry."""
        with self._lock:
            entry = RegistryEntry(
                key=key,
                item=item,
                scope=scope or RegistryScope(),
                metadata=dict(metadata or {}),
            )
            self._entries[key] = entry

        logger.debug(
            "registry_item_registered",
            registry_name=self._registry_name,
            key=key,
            namespace=entry.scope.namespace,
            scope_keys=entry.scope.scope_keys(),
        )

    def unregister(self, key: str) -> bool:
        """Remove key from registry. Returns True if removed."""
        with self._lock:
            removed = self._entries.pop(key, None)
        if removed is None:
            return False
        logger.debug(
            "registry_item_unregistered",
            registry_name=self._registry_name,
            key=key,
            namespace=removed.scope.namespace,
        )
        return True

    def get(self, key: str) -> Optional[T]:
        """Return item payload for key or None."""
        with self._lock:
            entry = self._entries.get(key)
        return entry.item if entry else None

    def get_entry(self, key: str) -> Optional[RegistryEntry[T]]:
        """Return full entry for key or None."""
        with self._lock:
            return self._entries.get(key)

    def list_items(self) -> Dict[str, T]:
        """Return shallow copy of key -> item mapping."""
        with self._lock:
            return {k: entry.item for k, entry in self._entries.items()}

    def list_entries(self) -> List[RegistryEntry[T]]:
        """Return list copy of entries."""
        with self._lock:
            return list(self._entries.values())

    def list_visible(self, scope_filter: ScopeFilter) -> Dict[str, T]:
        """Return visible key -> item mapping for request context."""
        with self._lock:
            return {
                key: entry.item
                for key, entry in self._entries.items()
                if scope_filter.matches_scope(entry.scope)
            }

    def list_visible_entries(self, scope_filter: ScopeFilter) -> List[RegistryEntry[T]]:
        """Return visible entries with metadata and scope."""
        with self._lock:
            return [entry for entry in self._entries.values() if scope_filter.matches_scope(entry.scope)]

    def unregister_namespace(self, namespace: str) -> List[str]:
        """Remove all entries under namespace prefix and return removed keys."""
        prefix = f"{namespace}."
        with self._lock:
            removed_keys = [key for key in self._entries if key.startswith(prefix)]
            for key in removed_keys:
                self._entries.pop(key, None)
        if removed_keys:
            logger.info(
                "registry_namespace_unregistered",
                registry_name=self._registry_name,
                namespace=namespace,
                removed_count=len(removed_keys),
            )
        return sorted(removed_keys)

    def stats(self) -> Dict[str, Any]:
        """Return summary statistics for observability."""
        with self._lock:
            entries = list(self._entries.values())

        by_namespace: Dict[str, int] = {}
        for entry in entries:
            ns = entry.scope.namespace
            by_namespace[ns] = by_namespace.get(ns, 0) + 1

        return {
            "registry_name": self._registry_name,
            "total": len(entries),
            "by_namespace": by_namespace,
        }
