"""
Motet - Unified Registry Package

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Package entrypoint for scoped registry primitives.
    Re-exports the canonical registry base symbols used across agents, tools,
    commands, and workflows.

Dependencies:
    - motet.core.registry.base: Canonical scoped registry models and base classes
    - motet.core.registry.naming: Qualified ID parsing and scope derivation helpers

Usage:
    from motet.core.registry import ScopedRegistry, RegistryScope, ScopeGrant

Notes:
    - Keep this module re-export only; implementation lives in base.py.
"""

from .base import RegistryEntry, RegistryProtocol, RegistryScope, ScopeFilter, ScopeGrant, ScopedRegistry
from .naming import (
    CORE_NAMESPACE,
    normalize_namespace,
    namespace_to_bundle_id,
    parse_qualified_name,
    namespace_from_qualified_name,
    scope_from_qualified_name,
    qualify_with_default_namespace,
)

__all__ = [
    "ScopeGrant",
    "RegistryScope",
    "ScopeFilter",
    "RegistryEntry",
    "RegistryProtocol",
    "ScopedRegistry",
    "CORE_NAMESPACE",
    "normalize_namespace",
    "namespace_to_bundle_id",
    "parse_qualified_name",
    "namespace_from_qualified_name",
    "scope_from_qualified_name",
    "qualify_with_default_namespace",
]
