"""
Motet - Surfaces Catalog Registry

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Redis-backed catalog of conversation surfaces plus
    per-agent surface allow-list overlays. Surfaces are explicit catalog entries
    (not auto-created on chat). Empty/missing agent allow-lists mean all catalog
    surfaces. Redis overlays override AgentConfig.allowed_surface_ids for manage-UI
    edits without mutating in-process agent registries. Surface ids accept
    lowercase snake_case or kebab-case (letters, digits, underscores, hyphens).

Dependencies:
    - re: surface id validation
    - json: agent allow-list serialization
    - motet.core.distributed.redis_manager: get_sync_redis_client
    - structlog: structured logging

Usage:
    from motet.core.surfaces import SurfaceRegistry

    registry = SurfaceRegistry()
    registry.ensure_builtins()
    registry.create(surface_id="partner_portal", display_name="Partner Portal")
    registry.set_agent_allowlist("core.default", ["demo_chat", "cli"])

Notes:
    - Redis client id: ``surface_registry``
    - Keys: ``motet:surface:meta:{id}``, ``motet:surface:index``,
      ``motet:surface:agent_allow:{qualified_agent_id}``
    - Builtin surfaces are seeded on ensure_builtins(); they cannot be deleted
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

import structlog

from motet.core.distributed.redis_manager import get_sync_redis_client
from motet.core.distributed.tenant_keys import (
    first_existing_key,
    product_key,
    smembers_union,
)

logger = structlog.get_logger(__name__)

REDIS_CLIENT_ID = "surface_registry"
SURFACE_META_PREFIX = product_key("surface:meta:")
SURFACE_INDEX_KEY = product_key("surface:index")
AGENT_ALLOW_PREFIX = product_key("surface:agent_allow:")

# Stable channel IDs used across Motet writers today (ADR-0083 + openai_compat).
BUILTIN_SURFACES: Dict[str, Dict[str, str]] = {
    "demo_chat": {
        "display_name": "Demo Chat",
        "description": "Chat Explorer / demo chat default surface",
    },
    "openai_compat": {
        "display_name": "OpenAI Compatible",
        "description": "OpenAI-compatible API facade (/v1) conversations",
    },
    "ops_dashboard": {
        "display_name": "Ops Dashboard",
        "description": "Manage / ops dashboard admin chat",
    },
    "cli": {
        "display_name": "CLI",
        "description": "Motet CLI chat surface",
    },
}

# Align with tenant/motet catalog slugs: allow kebab-case product ids
# (e.g. memo-intake) as well as snake_case builtins (demo_chat).
ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{1,62}$")


class SurfaceRegistryError(Exception):
    """Base error for surface catalog operations."""


class SurfaceNotFoundError(SurfaceRegistryError):
    """Surface id is not in the catalog."""


class SurfaceConflictError(SurfaceRegistryError):
    """Surface already exists."""


class SurfaceValidationError(SurfaceRegistryError):
    """Invalid id or request payload."""


@dataclass(frozen=True)
class SurfaceRecord:
    """Catalog entry for a conversation surface / channel."""

    id: str
    display_name: str
    description: Optional[str]
    builtin: bool
    created_at: str
    updated_at: str
    created_by: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "description": self.description,
            "builtin": self.builtin,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "created_by": self.created_by,
        }


def validate_surface_id(value: str, *, field_name: str = "surface_id") -> str:
    """Normalize and validate a surface id slug (snake_case or kebab-case)."""
    candidate = (value or "").strip().lower()
    if not ID_PATTERN.match(candidate):
        raise SurfaceValidationError(
            f"Invalid {field_name}: {value!r}. Must match "
            f"{ID_PATTERN.pattern} (lowercase, start with letter, "
            f"letters/digits/underscores/hyphens, 2-63 chars)."
        )
    return candidate


def normalize_allowlist(ids: Optional[List[str]]) -> Optional[List[str]]:
    """
    Normalize an allow-list.

    Returns None meaning "all catalog surfaces". Empty input lists mean all.
    """
    if ids is None:
        return None
    cleaned: List[str] = []
    seen: Set[str] = set()
    for raw in ids:
        if not isinstance(raw, str):
            continue
        sid = raw.strip().lower()
        if not sid or sid in seen:
            continue
        validate_surface_id(sid)
        seen.add(sid)
        cleaned.append(sid)
    return cleaned or None


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class SurfaceRegistry:
    """Redis-backed surfaces catalog and agent allow-list overlays."""

    def __init__(self) -> None:
        self._r = get_sync_redis_client(REDIS_CLIENT_ID)

    def _meta_key(self, surface_id: str) -> str:
        return f"{SURFACE_META_PREFIX}{surface_id}"

    def _meta_keys(self, surface_id: str) -> tuple[str, ...]:
        return (product_key(f"surface:meta:{surface_id}"),)

    def _index_keys(self) -> tuple[str, ...]:
        return (product_key("surface:index"),)

    def _agent_allow_key(self, qualified_agent_id: str) -> str:
        return f"{AGENT_ALLOW_PREFIX}{qualified_agent_id}"

    def _agent_allow_keys(self, qualified_agent_id: str) -> tuple[str, ...]:
        return (product_key(f"surface:agent_allow:{qualified_agent_id}"),)

    def _exists_any(self, keys: tuple[str, ...]) -> bool:
        return first_existing_key(self._r, keys) is not None

    def _to_text(self, value: Any) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, str):
            return value
        if isinstance(value, (bytes, bytearray)):
            return value.decode("utf-8")
        logger.warning(
            "invalid_surface_registry_redis_value_type",
            value_type=type(value).__name__,
        )
        return None

    def _hgetall_text(self, key: str) -> Dict[str, str]:
        raw = self._r.hgetall(key)
        if not raw or not isinstance(raw, dict):
            return {}
        normalized: Dict[str, str] = {}
        for k, v in raw.items():
            key_text = self._to_text(k)
            value_text = self._to_text(v)
            if key_text is None or value_text is None:
                continue
            normalized[key_text] = value_text
        return normalized

    def _smembers_text(self, key: str) -> Set[str]:
        raw = self._r.smembers(key)
        if not raw:
            return set()
        if not isinstance(raw, set):
            logger.warning(
                "invalid_surface_registry_smembers_type",
                value_type=type(raw).__name__,
            )
            return set()
        out: Set[str] = set()
        for value in raw:
            text = self._to_text(value)
            if text is not None:
                out.add(text)
        return out

    def _record_from_hash(self, data: Dict[str, str]) -> Optional[SurfaceRecord]:
        if not data.get("id"):
            return None
        return SurfaceRecord(
            id=data["id"],
            display_name=data.get("display_name") or data["id"],
            description=data.get("description") or None,
            builtin=(data.get("builtin") or "").lower() in {"1", "true", "yes"},
            created_at=data.get("created_at") or "",
            updated_at=data.get("updated_at") or "",
            created_by=data.get("created_by") or None,
        )

    def ensure_builtins(self) -> int:
        """Seed built-in surfaces if missing. Returns count created."""
        created = 0
        for surface_id, meta in BUILTIN_SURFACES.items():
            key = self._meta_key(surface_id)
            if self._exists_any(self._meta_keys(surface_id)):
                continue
            now = _utcnow_iso()
            mapping = {
                "id": surface_id,
                "display_name": meta["display_name"],
                "description": meta.get("description") or "",
                "builtin": "true",
                "created_at": now,
                "updated_at": now,
                "created_by": "system",
            }
            self._r.hset(key, mapping=mapping)
            self._r.sadd(SURFACE_INDEX_KEY, surface_id)
            created += 1
            logger.info("surface_catalog_builtin_seeded", surface_id=surface_id)
        return created

    def register_if_absent(
        self,
        *,
        surface_id: str,
        display_name: Optional[str] = None,
        description: Optional[str] = None,
        created_by: Optional[str] = None,
    ) -> tuple[bool, SurfaceRecord]:
        """
        Create a surface if missing; no-op if it already exists.

        Returns ``(created, record)``. Existing entries are left unchanged
        (display_name / description are not overwritten).
        """
        self.ensure_builtins()
        sid = validate_surface_id(surface_id)
        if self.exists(sid):
            return False, self.get(sid)
        record = self.create(
            surface_id=sid,
            display_name=display_name,
            description=description,
            created_by=created_by,
            builtin=False,
        )
        return True, record

    def create(
        self,
        *,
        surface_id: str,
        display_name: Optional[str] = None,
        description: Optional[str] = None,
        created_by: Optional[str] = None,
        builtin: bool = False,
    ) -> SurfaceRecord:
        """Create a surface catalog entry. Raises SurfaceConflictError if exists."""
        sid = validate_surface_id(surface_id)
        key = self._meta_key(sid)
        if self._exists_any(self._meta_keys(sid)):
            raise SurfaceConflictError(f"Surface already exists: {sid}")

        now = _utcnow_iso()
        record = SurfaceRecord(
            id=sid,
            display_name=(display_name or sid).strip() or sid,
            description=(description.strip() if description else None) or None,
            builtin=builtin,
            created_at=now,
            updated_at=now,
            created_by=(created_by.strip() if created_by else None) or None,
        )
        mapping = {
            "id": record.id,
            "display_name": record.display_name,
            "description": record.description or "",
            "builtin": "true" if record.builtin else "false",
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "created_by": record.created_by or "",
        }
        self._r.hset(key, mapping=mapping)
        self._r.sadd(SURFACE_INDEX_KEY, sid)
        logger.info("surface_catalog_created", surface_id=sid, created_by=record.created_by)
        return record

    def get(self, surface_id: str) -> SurfaceRecord:
        """Get a surface by id. Raises SurfaceNotFoundError if missing."""
        sid = validate_surface_id(surface_id)
        found = first_existing_key(self._r, self._meta_keys(sid))
        data = self._hgetall_text(found or self._meta_key(sid))
        record = self._record_from_hash(data)
        if record is None:
            raise SurfaceNotFoundError(f"Surface not found: {sid}")
        return record

    def exists(self, surface_id: str) -> bool:
        """Return True if the surface id is in the catalog."""
        try:
            sid = validate_surface_id(surface_id)
        except SurfaceValidationError:
            return False
        return self._exists_any(self._meta_keys(sid))

    def list_surfaces(self) -> List[SurfaceRecord]:
        """List all catalog surfaces, sorted by id."""
        ids = sorted(smembers_union(self._r, self._index_keys()))
        out: List[SurfaceRecord] = []
        for sid in ids:
            found = first_existing_key(self._r, self._meta_keys(sid))
            data = self._hgetall_text(found or self._meta_key(sid))
            record = self._record_from_hash(data)
            if record is not None:
                out.append(record)
        return out

    def update(
        self,
        surface_id: str,
        *,
        display_name: Optional[str] = None,
        description: Optional[str] = None,
    ) -> SurfaceRecord:
        """Update mutable surface fields."""
        existing = self.get(surface_id)
        now = _utcnow_iso()
        updated = SurfaceRecord(
            id=existing.id,
            display_name=(
                display_name.strip()
                if display_name is not None
                else existing.display_name
            )
            or existing.id,
            description=(
                description.strip()
                if description is not None
                else existing.description
            )
            or None,
            builtin=existing.builtin,
            created_at=existing.created_at,
            updated_at=now,
            created_by=existing.created_by,
        )
        self._r.hset(
            self._meta_key(updated.id),
            mapping={
                "id": updated.id,
                "display_name": updated.display_name,
                "description": updated.description or "",
                "builtin": "true" if updated.builtin else "false",
                "created_at": updated.created_at,
                "updated_at": updated.updated_at,
                "created_by": updated.created_by or "",
            },
        )
        logger.info("surface_catalog_updated", surface_id=updated.id)
        return updated

    def delete(self, surface_id: str) -> None:
        """Delete a non-builtin surface. Raises on missing or builtin."""
        existing = self.get(surface_id)
        if existing.builtin:
            raise SurfaceValidationError(
                f"Cannot delete builtin surface: {existing.id}"
            )
        for key in self._meta_keys(existing.id):
            self._r.delete(key)
        for key in self._index_keys():
            self._r.srem(key, existing.id)
        logger.info("surface_catalog_deleted", surface_id=existing.id)

    # ------------------------------------------------------------------
    # Agent allow-list overlays (manage UI / runtime policy)
    # ------------------------------------------------------------------

    def get_agent_allowlist_overlay(
        self, qualified_agent_id: str
    ) -> tuple[bool, Optional[List[str]]]:
        """
        Return ``(found, allowlist)`` for an agent overlay.

        - ``found=False`` → no overlay; fall back to AgentConfig
        - ``found=True, allowlist=None`` → overlay explicitly allows all surfaces
        - ``found=True, allowlist=[...]`` → overlay restricts to those ids
        """
        qid = (qualified_agent_id or "").strip()
        if not qid:
            raise SurfaceValidationError("qualified_agent_id is required")
        found = first_existing_key(self._r, self._agent_allow_keys(qid))
        raw = self._r.get(found) if found else None
        text = self._to_text(raw)
        if text is None:
            return False, None
        text = text.strip()
        if not text or text == "*" or text == "[]":
            return True, None
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as e:
            logger.warning(
                "surface_agent_allowlist_invalid_json",
                qualified_agent_id=qid,
                error=str(e),
            )
            return False, None
        if parsed is None or parsed == []:
            return True, None
        if not isinstance(parsed, list):
            logger.warning(
                "surface_agent_allowlist_invalid_type",
                qualified_agent_id=qid,
                value_type=type(parsed).__name__,
            )
            return False, None
        return True, normalize_allowlist([str(x) for x in parsed])

    def set_agent_allowlist(
        self,
        qualified_agent_id: str,
        surface_ids: Optional[List[str]],
        *,
        clear: bool = False,
    ) -> Optional[List[str]]:
        """
        Set or clear the Redis overlay for an agent.

        When ``clear=True``, removes the overlay so AgentConfig / default-all
        applies. ``surface_ids is None`` without clear stores explicit all.
        Non-empty lists must reference existing catalog ids.
        """
        qid = (qualified_agent_id or "").strip()
        if not qid:
            raise SurfaceValidationError("qualified_agent_id is required")
        key = self._agent_allow_key(qid)
        if clear:
            for item in self._agent_allow_keys(qid):
                self._r.delete(item)
            logger.info("surface_agent_allowlist_cleared", qualified_agent_id=qid)
            return None

        normalized = normalize_allowlist(surface_ids)
        if normalized:
            self.ensure_builtins()
            missing = [sid for sid in normalized if not self.exists(sid)]
            if missing:
                raise SurfaceValidationError(
                    f"Unknown surface id(s) not in catalog: {', '.join(missing)}"
                )
            payload = json.dumps(normalized)
        else:
            payload = "[]"
        self._r.set(key, payload)
        logger.info(
            "surface_agent_allowlist_set",
            qualified_agent_id=qid,
            surface_ids=normalized,
        )
        return normalized

    def clear_agent_allowlist(self, qualified_agent_id: str) -> None:
        """Remove Redis overlay so AgentConfig / default-all applies."""
        self.set_agent_allowlist(qualified_agent_id, None, clear=True)


def require_existing_surface(
    surface_id: str,
    *,
    registry: Optional[SurfaceRegistry] = None,
) -> str:
    """Normalize a surface id and confirm it exists in the catalog.

    Raises:
        SurfaceValidationError: invalid slug
        SurfaceNotFoundError: not in catalog
    """
    normalized = validate_surface_id(surface_id)
    reg = registry or SurfaceRegistry()
    reg.ensure_builtins()
    if not reg.exists(normalized):
        raise SurfaceNotFoundError(f"Surface not found: {normalized}")
    return normalized


def resolve_effective_allowlist(
    *,
    qualified_agent_id: str,
    config_allowed_surface_ids: Optional[List[str]] = None,
    registry: Optional[SurfaceRegistry] = None,
) -> Optional[List[str]]:
    """
    Resolve effective allow-list for an agent.

    Returns None when the agent may use any catalog surface.
    Redis overlay wins over AgentConfig when present.
    """
    reg = registry or SurfaceRegistry()
    found, overlay = reg.get_agent_allowlist_overlay(qualified_agent_id)
    if found:
        return overlay
    return normalize_allowlist(config_allowed_surface_ids)


def agent_may_use_surface(
    *,
    qualified_agent_id: str,
    surface_id: str,
    config_allowed_surface_ids: Optional[List[str]] = None,
    registry: Optional[SurfaceRegistry] = None,
) -> bool:
    """Return True if the agent is allowed on the surface (catalog membership separate)."""
    allowed = resolve_effective_allowlist(
        qualified_agent_id=qualified_agent_id,
        config_allowed_surface_ids=config_allowed_surface_ids,
        registry=registry,
    )
    if allowed is None:
        return True
    try:
        sid = validate_surface_id(surface_id)
    except SurfaceValidationError:
        return False
    return sid in allowed
