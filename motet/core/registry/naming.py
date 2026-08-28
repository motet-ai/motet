"""
Motet - Registry Naming Helpers

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Shared helpers for deriving registry namespace and scope metadata from
    qualified artifact names. Consolidates dotted-ID parsing logic used across
    commands, tools, workflows, and model profile registries.

Dependencies:
    - motet.core.registry.base: RegistryScope model for scope metadata

Usage:
    from motet.core.registry.naming import parse_qualified_name, scope_from_qualified_name

    namespace, local_name = parse_qualified_name("sales.lookup_customer")
    scope = scope_from_qualified_name("sales.lookup_customer")

Notes:
    - Bare names default to the core namespace.
    - Namespace `core` maps to bundle_id=None.
    - Parsing is intentionally permissive to preserve existing behavior.
"""

from __future__ import annotations

from typing import Tuple

from .base import RegistryScope

CORE_NAMESPACE = "core"


def normalize_namespace(namespace: str | None) -> str:
    """Normalize namespace values and default empty input to 'core'."""
    normalized = (namespace or "").strip()
    return normalized or CORE_NAMESPACE


def namespace_to_bundle_id(namespace: str | None) -> str | None:
    """Map namespace to bundle id where core is represented as None."""
    normalized = normalize_namespace(namespace)
    return None if normalized == CORE_NAMESPACE else normalized


def parse_qualified_name(qualified_name: str) -> Tuple[str, str]:
    """
    Parse a qualified artifact name into namespace and local name.

    Examples:
        "bundle_x.echo" -> ("bundle_x", "echo")
        "echo" -> ("core", "echo")
    """
    raw = (qualified_name or "").strip()
    if not raw:
        return CORE_NAMESPACE, ""
    if "." not in raw:
        return CORE_NAMESPACE, raw
    namespace, local_name = raw.split(".", 1)
    return normalize_namespace(namespace), local_name


def namespace_from_qualified_name(qualified_name: str) -> str:
    """Extract namespace from a qualified name with core fallback."""
    namespace, _ = parse_qualified_name(qualified_name)
    return namespace


def scope_from_qualified_name(qualified_name: str) -> RegistryScope:
    """Build a RegistryScope derived from a qualified artifact name."""
    namespace = namespace_from_qualified_name(qualified_name)
    return RegistryScope(
        namespace=namespace,
        bundle_id=namespace_to_bundle_id(namespace),
    )


def qualify_with_default_namespace(name: str, default_namespace: str = CORE_NAMESPACE) -> str:
    """Return a qualified name, prefixing with default_namespace when needed."""
    raw = (name or "").strip()
    if not raw:
        return f"{normalize_namespace(default_namespace)}."
    if "." in raw:
        return raw
    return f"{normalize_namespace(default_namespace)}.{raw}"

