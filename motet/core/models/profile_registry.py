"""
Motet - Model Profile Registry (bundle-contributed model config)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    In-memory registry for bundle-contributed model profiles. Used by
    bundle_reload to merge config/models.yaml "profiles" into the worker so
    bundle-deployed model config is available. Supports namespaced profile names
    (bundle_id.profile_name) and unregister by prefix on bundle unload.

Dependencies:
    - typing: Type hints
    - structlog: Logging

Usage:
    from motet.core.models.profile_registry import get_model_profile_registry

    registry = get_model_profile_registry()
    registry.register_profile("sales.default", {"provider": "openai", "name": "gpt-4o-mini"})
    names = registry.get_all_profile_names()
    registry.unregister_profile("sales.default")

Notes:
    - Worker-local in-memory storage; not shared across workers.
    - Bundle model spec merge is add-only (V1); unload removes only that bundle's profiles.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import structlog

from ..registry import ScopedRegistry, ScopeFilter, RegistryEntry, scope_from_qualified_name

logger = structlog.get_logger(__name__)

_global_registry: "ModelProfileRegistry | None" = None


class ModelProfileRegistry(ScopedRegistry[Dict[str, Any]]):
    """
    In-memory registry of bundle-contributed model profile config.
    Keys are namespaced (bundle_id.profile_name). Used by core.reload_bundle
    and core.unload_bundle for config/models.yaml merge/unmerge.
    """

    def __init__(self) -> None:
        super().__init__(registry_name="model_profile_registry")

    def register_profile(self, profile_name: str, profile_data: Dict[str, Any]) -> None:
        """Register a profile (namespaced name -> config dict)."""
        self.register(
            profile_name,
            dict(profile_data),
            scope=scope_from_qualified_name(profile_name),
        )
        logger.debug("profile_registry_registered", profile_name=profile_name)

    def unregister_profile(self, profile_name: str) -> None:
        """Remove a profile by name."""
        if self.unregister(profile_name):
            logger.debug("profile_registry_unregistered", profile_name=profile_name)

    def get_all_profile_names(self) -> List[str]:
        """Return all registered profile names (for unload by prefix)."""
        return list(self.list_items().keys())

    def get_profile(self, profile_name: str) -> Dict[str, Any] | None:
        """Get profile data by name, or None if not registered."""
        return self.get(profile_name)

    # Unified API convenience wrappers for callers preferring explicit names.
    def list_visible_profiles(self, scope_filter: ScopeFilter) -> Dict[str, Dict[str, Any]]:
        return self.list_visible(scope_filter)

    def list_profile_entries(self) -> List[RegistryEntry[Dict[str, Any]]]:
        return self.list_entries()

    def get_profile_entry(self, profile_name: str) -> Optional[RegistryEntry[Dict[str, Any]]]:
        return self.get_entry(profile_name)


def get_model_profile_registry() -> ModelProfileRegistry:
    """Return the global model profile registry (singleton per process)."""
    global _global_registry
    if _global_registry is None:
        _global_registry = ModelProfileRegistry()
    return _global_registry


__all__ = ["ModelProfileRegistry", "get_model_profile_registry"]
