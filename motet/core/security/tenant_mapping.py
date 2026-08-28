"""
Motet - Tenant Mapping Utilities

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Utilities for translating external tenant claims (e.g., Keycloak organization
    identifiers) into canonical Motet tenant IDs. Supports JSON-configured
    mappings and explicit global-tenant whitelists to power cross-tenant
    operations without relying on implicit defaults.

Dependencies:
    - json: Configuration parsing
    - functools: Lightweight caching
    - structlog: Structured logging

Usage:
    from motet.core.security.tenant_mapping import resolve_tenant_id

    tenant_id = resolve_tenant_id(cfg, raw_tenant_value, claims)

Notes:
    - Mapping configuration is sourced from Config attributes to avoid tight
      coupling to environment variables.
    - Both mapping dictionaries and global tenant lists accept JSON or
      comma-separated strings for convenience.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any, Dict, Optional, Set

import structlog

from motet.core.config import Config

logger = structlog.get_logger(__name__)


def _normalize(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


@lru_cache(maxsize=32)
def _parse_mapping(raw: Optional[str]) -> Dict[str, str]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except json.JSONDecodeError:
        logger.warning("tenant_mapping_invalid_json", raw=raw)
    mapping: Dict[str, str] = {}
    for part in str(raw).split(","):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key and value:
            mapping[key] = value
    return mapping


@lru_cache(maxsize=32)
def _parse_global(raw: Optional[str]) -> Set[str]:
    if not raw:
        return set()
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return {str(v).strip() for v in data if str(v).strip()}
    except json.JSONDecodeError:
        pass
    return {v.strip() for v in str(raw).split(",") if v.strip()}


def resolve_tenant_id(cfg: Config, raw_value: Any, claims: Dict[str, Any]) -> Optional[str]:
    """
    Resolve canonical tenant identifier.

    Args:
        cfg: Runtime configuration
        raw_value: Value pulled directly from JWT/service account claims
        claims: Mutable claims dictionary for annotating scope metadata

    Returns:
        Canonical tenant identifier or None if no value could be resolved.
    """
    tenant_value = _normalize(raw_value)
    tenant_origin = claims.get("tenant_origin")
    if not tenant_value:
        logger.info(
            "resolve_tenant_id: No raw tenant value provided",
            raw_value=raw_value,
            tenant_origin=tenant_origin
        )
        return None

    mapping = _parse_mapping(getattr(cfg, "tenant_id_map_json", None))
    canonical = mapping.get(tenant_value, tenant_value)
    
    logger.info(
        "resolve_tenant_id: Mapping tenant",
        raw_value=tenant_value,
        canonical=canonical,
        mapping_keys=list(mapping.keys()) if mapping else [],
        was_mapped=tenant_value != canonical,
        tenant_origin=tenant_origin
    )

    global_ids = _parse_global(getattr(cfg, "tenant_global_ids", None))
    if canonical in global_ids:
        claims["tenant_scope"] = "global"
        logger.info("resolve_tenant_id: Marked as global tenant", canonical=canonical)

    return canonical


__all__ = ["resolve_tenant_id"]

