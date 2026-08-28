"""
Motet - Surfaces Package

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Conversation surface catalog and per-agent allow-lists.
    See registry.py for Redis-backed CRUD used by the surfaces API, Chat Explorer,
    and manage-app Surfaces page.

Dependencies:
    - motet.core.surfaces.registry: catalog and allow-list helpers

Usage:
    from motet.core.surfaces import SurfaceRegistry, resolve_effective_allowlist

    registry = SurfaceRegistry()
    created, record = registry.register_if_absent(
        surface_id="partner_portal",
        display_name="Partner Portal",
        created_by="bundle:acme",
    )

Notes:
    - Surfaces are explicitly registered; chat does not auto-create catalog entries
    - Bundles may declare surfaces in config/surfaces.yaml; deploy uses register_if_absent
"""

from .registry import (
    BUILTIN_SURFACES,
    SurfaceConflictError,
    SurfaceNotFoundError,
    SurfaceRecord,
    SurfaceRegistry,
    SurfaceRegistryError,
    SurfaceValidationError,
    agent_may_use_surface,
    normalize_allowlist,
    require_existing_surface,
    resolve_effective_allowlist,
    validate_surface_id,
)

__all__ = [
    "BUILTIN_SURFACES",
    "SurfaceConflictError",
    "SurfaceNotFoundError",
    "SurfaceRecord",
    "SurfaceRegistry",
    "SurfaceRegistryError",
    "SurfaceValidationError",
    "agent_may_use_surface",
    "normalize_allowlist",
    "require_existing_surface",
    "resolve_effective_allowlist",
    "validate_surface_id",
]
