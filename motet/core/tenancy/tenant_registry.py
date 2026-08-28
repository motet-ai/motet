"""
Motet - Tenant / Motet Catalog Registry

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Redis-backed catalog of tenants and their Motets (deployment environments).
    This is the operator-managed source of truth for manage-app scope selectors
    and CLI/API listing — distinct from JWT claim remapping
    (MOTET_TENANT_ID_MAP_JSON) and from ScopedRegistry visibility grants.

Dependencies:
    - re: ID slug validation
    - motet.core.distributed.redis_manager: get_sync_redis_client
    - structlog: structured logging

Usage:
    from motet.core.tenancy.tenant_registry import TenantRegistry

    registry = TenantRegistry()
    registry.create_tenant(tenant_id="acme", name="Acme Corp")
    registry.create_motet(tenant_id="acme", motet_id="prod", name="Production")
    tenants = registry.list_tenants(include_motets=True)

Notes:
    - Redis client id: ``tenant_registry``
    - Keys: ``{tenant}:tenant:meta``,
      ``motet:tenant:index`` (global, unprefixed),
      ``{tenant}:tenant:motet:{motet}``,
      ``{tenant}:tenant:motet:index``
    - Motet here means environment/deployment id (prod/staging/dev), not the
      Motet product name
    - ``create_tenant`` best-effort applies a tenant Valkey ACL user
      (``ACL SETUSER``). Default/Celery login stays unrestricted.
    - Tenant ids ``motet``, ``imf``, and other shared first segments
      (``celery``, ``worker``, ``lock``, …) are reserved so ``{tenant}:…``
      cannot collide with ``motet:…`` / ElastiCache ``~{tenant}:*``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

import structlog

from motet.core.distributed.redis_manager import get_sync_redis_client
from motet.core.distributed.tenant_keys import (
    RESERVED_TENANT_IDS,
    delete_candidate_keys,
    first_existing_key,
    hgetall_first,
    is_reserved_tenant_id,
    product_key,
    smembers_union,
    tenant_key,
)

logger = structlog.get_logger(__name__)

REDIS_CLIENT_ID = "tenant_registry"
TENANT_META_LOGICAL = "tenant:meta"
TENANT_INDEX_KEY = product_key("tenant:index")
MOTET_META_PREFIX = "tenant:motet:"
MOTET_INDEX_LOGICAL = "tenant:motet:index"

ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
VALID_STATUSES = frozenset({"active", "disabled"})

# Request-scoped sentinel meaning "every tenant the caller may see", used by
# APIs that can aggregate across the catalog. ID_PATTERN requires a leading
# alphanumeric, so this can never collide with a real tenant id.
ALL_TENANTS = "__all__"


class TenantRegistryError(Exception):
    """Base error for tenant catalog operations."""


class TenantNotFoundError(TenantRegistryError):
    """Tenant id is not in the catalog."""


class MotetNotFoundError(TenantRegistryError):
    """Motet id is not in the catalog for the given tenant."""


class TenantConflictError(TenantRegistryError):
    """Tenant already exists."""


class MotetConflictError(TenantRegistryError):
    """Motet already exists under the tenant."""


class TenantValidationError(TenantRegistryError):
    """Invalid id, status, or request payload."""


@dataclass(frozen=True)
class MotetRecord:
    """Catalog entry for a Motet (environment) under a tenant."""

    id: str
    tenant_id: str
    name: str
    status: str
    created_at: str
    updated_at: str
    description: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "name": self.name,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "description": self.description,
        }


@dataclass(frozen=True)
class TenantRecord:
    """Catalog entry for a tenant organization."""

    id: str
    name: str
    status: str
    created_at: str
    updated_at: str
    description: Optional[str] = None
    motets: Optional[List[MotetRecord]] = None

    def to_dict(self, *, include_motets: bool = False) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "description": self.description,
        }
        if include_motets or self.motets is not None:
            payload["motets"] = [m.to_dict() for m in (self.motets or [])]
        return payload


def validate_catalog_id(value: str, *, field_name: str = "id") -> str:
    """Normalize and validate a tenant or motet id slug."""
    candidate = (value or "").strip().lower()
    if not ID_PATTERN.match(candidate):
        raise TenantValidationError(
            f"Invalid {field_name}: {value!r}. Must match "
            f"{ID_PATTERN.pattern} (lowercase slug, 1-63 chars)."
        )
    if field_name == "tenant_id" and is_reserved_tenant_id(candidate):
        reserved = ", ".join(sorted(RESERVED_TENANT_IDS))
        raise TenantValidationError(
            f"Invalid tenant_id: {value!r}. Reserved (shared Valkey prefix): "
            f"{reserved}."
        )
    return candidate


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class TenantRegistry:
    """Redis-backed tenant and Motet catalog."""

    def __init__(self) -> None:
        self._r = get_sync_redis_client(REDIS_CLIENT_ID)

    def _tenant_meta_logical(self) -> str:
        return TENANT_META_LOGICAL

    def _tenant_meta_key(self, tenant_id: str) -> str:
        return tenant_key(tenant_id, self._tenant_meta_logical())

    def _tenant_meta_keys(self, tenant_id: str) -> tuple[str, ...]:
        return (tenant_key(tenant_id, self._tenant_meta_logical()),)

    def _motet_meta_logical(self, motet_id: str) -> str:
        return f"{MOTET_META_PREFIX}{motet_id}"

    def _motet_meta_key(self, tenant_id: str, motet_id: str) -> str:
        return tenant_key(tenant_id, self._motet_meta_logical(motet_id))

    def _motet_meta_keys(self, tenant_id: str, motet_id: str) -> tuple[str, ...]:
        return (tenant_key(tenant_id, self._motet_meta_logical(motet_id)),)

    def _motet_index_logical(self) -> str:
        return MOTET_INDEX_LOGICAL

    def _motet_index_key(self, tenant_id: str) -> str:
        return tenant_key(tenant_id, self._motet_index_logical())

    def _motet_index_keys(self, tenant_id: str) -> tuple[str, ...]:
        return (tenant_key(tenant_id, self._motet_index_logical()),)

    def _to_text(self, value: Any) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, str):
            return value
        if isinstance(value, (bytes, bytearray)):
            return value.decode("utf-8")
        logger.warning(
            "invalid_tenant_registry_redis_value_type",
            value_type=type(value).__name__,
        )
        return None

    def _exists_any(self, keys: tuple[str, ...]) -> bool:
        return first_existing_key(self._r, keys) is not None

    def _hgetall_candidates(self, keys: tuple[str, ...]) -> Dict[str, str]:
        raw = hgetall_first(self._r, keys)
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

    def _smembers_candidates(self, keys: tuple[str, ...]) -> Set[str]:
        return set(smembers_union(self._r, keys))

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
                "invalid_tenant_registry_smembers_type",
                value_type=type(raw).__name__,
            )
            return set()
        out: Set[str] = set()
        for value in raw:
            text = self._to_text(value)
            if text is not None:
                out.add(text)
        return out

    def _record_from_hash(
        self, data: Dict[str, str], *, tenant_id: str
    ) -> Optional[TenantRecord]:
        if not data.get("id"):
            return None
        return TenantRecord(
            id=data["id"],
            name=data.get("name") or data["id"],
            status=data.get("status") or "active",
            created_at=data.get("created_at") or "",
            updated_at=data.get("updated_at") or "",
            description=data.get("description") or None,
        )

    def _motet_from_hash(self, data: Dict[str, str]) -> Optional[MotetRecord]:
        if not data.get("id") or not data.get("tenant_id"):
            return None
        return MotetRecord(
            id=data["id"],
            tenant_id=data["tenant_id"],
            name=data.get("name") or data["id"],
            status=data.get("status") or "active",
            created_at=data.get("created_at") or "",
            updated_at=data.get("updated_at") or "",
            description=data.get("description") or None,
        )

    def create_tenant(
        self,
        *,
        tenant_id: str,
        name: Optional[str] = None,
        description: Optional[str] = None,
        status: str = "active",
    ) -> TenantRecord:
        """Create a tenant catalog entry. Raises TenantConflictError if exists."""
        tid = validate_catalog_id(tenant_id, field_name="tenant_id")
        if status not in VALID_STATUSES:
            raise TenantValidationError(
                f"Invalid status: {status!r}. Must be one of {sorted(VALID_STATUSES)}"
            )
        key = self._tenant_meta_key(tid)
        if self._exists_any(self._tenant_meta_keys(tid)):
            raise TenantConflictError(f"Tenant already exists: {tid}")

        now = _utcnow_iso()
        record = TenantRecord(
            id=tid,
            name=(name or tid).strip() or tid,
            status=status,
            created_at=now,
            updated_at=now,
            description=(description.strip() if description else None) or None,
        )
        mapping = {
            "id": record.id,
            "name": record.name,
            "status": record.status,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "description": record.description or "",
        }
        self._r.hset(key, mapping=mapping)
        self._r.sadd(TENANT_INDEX_KEY, tid)
        from motet.core.distributed.tenant_acl import provision_tenant_acl

        provision_tenant_acl(self._r, tid)
        logger.info("tenant_catalog_created", tenant_id=tid)
        return record

    def get_tenant(self, tenant_id: str, *, include_motets: bool = False) -> TenantRecord:
        """Get a tenant by id. Raises TenantNotFoundError if missing."""
        tid = validate_catalog_id(tenant_id, field_name="tenant_id")
        data = self._hgetall_candidates(self._tenant_meta_keys(tid))
        record = self._record_from_hash(data, tenant_id=tid)
        if record is None:
            raise TenantNotFoundError(f"Tenant not found: {tid}")
        if include_motets:
            motets = self.list_motets(tid)
            return TenantRecord(
                id=record.id,
                name=record.name,
                status=record.status,
                created_at=record.created_at,
                updated_at=record.updated_at,
                description=record.description,
                motets=motets,
            )
        return record

    def update_tenant(
        self,
        tenant_id: str,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        status: Optional[str] = None,
    ) -> TenantRecord:
        """Update mutable tenant fields."""
        existing = self.get_tenant(tenant_id)
        if status is not None and status not in VALID_STATUSES:
            raise TenantValidationError(
                f"Invalid status: {status!r}. Must be one of {sorted(VALID_STATUSES)}"
            )
        now = _utcnow_iso()
        updated = TenantRecord(
            id=existing.id,
            name=(name.strip() if name is not None else existing.name) or existing.id,
            status=status if status is not None else existing.status,
            created_at=existing.created_at,
            updated_at=now,
            description=(
                description.strip()
                if description is not None
                else existing.description
            )
            or None,
        )
        self._r.hset(
            self._tenant_meta_key(updated.id),
            mapping={
                "id": updated.id,
                "name": updated.name,
                "status": updated.status,
                "created_at": updated.created_at,
                "updated_at": updated.updated_at,
                "description": updated.description or "",
            },
        )
        logger.info("tenant_catalog_updated", tenant_id=updated.id)
        return updated

    def delete_tenant(self, tenant_id: str, *, force: bool = False) -> None:
        """Delete a tenant. Refuses if motets remain unless force=True."""
        tid = validate_catalog_id(tenant_id, field_name="tenant_id")
        if not self._exists_any(self._tenant_meta_keys(tid)):
            raise TenantNotFoundError(f"Tenant not found: {tid}")

        motet_ids = sorted(self._smembers_candidates(self._motet_index_keys(tid)))
        if motet_ids and not force:
            raise TenantValidationError(
                f"Tenant {tid} has {len(motet_ids)} motet(s); "
                "delete them first or pass force=True"
            )
        for mid in motet_ids:
            delete_candidate_keys(self._r, self._motet_meta_keys(tid, mid))
        delete_candidate_keys(self._r, self._motet_index_keys(tid))
        delete_candidate_keys(self._r, self._tenant_meta_keys(tid))
        self._r.srem(product_key("tenant:index"), tid)
        logger.info(
            "tenant_catalog_deleted",
            tenant_id=tid,
            motets_removed=len(motet_ids),
            force=force,
        )

    def list_tenants(
        self,
        *,
        include_motets: bool = False,
        status: Optional[str] = None,
        tenant_ids: Optional[Set[str]] = None,
    ) -> List[TenantRecord]:
        """List tenants, optionally filtered by id set and/or status."""
        if status is not None and status not in VALID_STATUSES:
            raise TenantValidationError(
                f"Invalid status: {status!r}. Must be one of {sorted(VALID_STATUSES)}"
            )
        ids = sorted(smembers_union(self._r, product_key("tenant:index")))
        if tenant_ids is not None:
            allowed = {validate_catalog_id(t, field_name="tenant_id") for t in tenant_ids}
            ids = [i for i in ids if i in allowed]

        results: List[TenantRecord] = []
        for tid in ids:
            try:
                record = self.get_tenant(tid, include_motets=include_motets)
            except TenantNotFoundError:
                self._r.srem(product_key("tenant:index"), tid)
                continue
            if status is not None and record.status != status:
                continue
            results.append(record)
        return results

    def create_motet(
        self,
        *,
        tenant_id: str,
        motet_id: str,
        name: Optional[str] = None,
        description: Optional[str] = None,
        status: str = "active",
    ) -> MotetRecord:
        """Create a Motet under an existing tenant."""
        tid = validate_catalog_id(tenant_id, field_name="tenant_id")
        mid = validate_catalog_id(motet_id, field_name="motet_id")
        if status not in VALID_STATUSES:
            raise TenantValidationError(
                f"Invalid status: {status!r}. Must be one of {sorted(VALID_STATUSES)}"
            )
        # Ensure parent tenant exists
        self.get_tenant(tid)

        key = self._motet_meta_key(tid, mid)
        if self._exists_any(self._motet_meta_keys(tid, mid)):
            raise MotetConflictError(f"Motet already exists: {tid}/{mid}")

        now = _utcnow_iso()
        record = MotetRecord(
            id=mid,
            tenant_id=tid,
            name=(name or mid).strip() or mid,
            status=status,
            created_at=now,
            updated_at=now,
            description=(description.strip() if description else None) or None,
        )
        self._r.hset(
            key,
            mapping={
                "id": record.id,
                "tenant_id": record.tenant_id,
                "name": record.name,
                "status": record.status,
                "created_at": record.created_at,
                "updated_at": record.updated_at,
                "description": record.description or "",
            },
        )
        self._r.sadd(self._motet_index_key(tid), mid)
        logger.info("motet_catalog_created", tenant_id=tid, motet_id=mid)
        return record

    def get_motet(self, tenant_id: str, motet_id: str) -> MotetRecord:
        """Get a Motet under a tenant."""
        tid = validate_catalog_id(tenant_id, field_name="tenant_id")
        mid = validate_catalog_id(motet_id, field_name="motet_id")
        self.get_tenant(tid)
        data = self._hgetall_candidates(self._motet_meta_keys(tid, mid))
        record = self._motet_from_hash(data)
        if record is None:
            raise MotetNotFoundError(f"Motet not found: {tid}/{mid}")
        return record

    def update_motet(
        self,
        tenant_id: str,
        motet_id: str,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        status: Optional[str] = None,
    ) -> MotetRecord:
        """Update mutable Motet fields."""
        existing = self.get_motet(tenant_id, motet_id)
        if status is not None and status not in VALID_STATUSES:
            raise TenantValidationError(
                f"Invalid status: {status!r}. Must be one of {sorted(VALID_STATUSES)}"
            )
        now = _utcnow_iso()
        updated = MotetRecord(
            id=existing.id,
            tenant_id=existing.tenant_id,
            name=(name.strip() if name is not None else existing.name) or existing.id,
            status=status if status is not None else existing.status,
            created_at=existing.created_at,
            updated_at=now,
            description=(
                description.strip()
                if description is not None
                else existing.description
            )
            or None,
        )
        self._r.hset(
            self._motet_meta_key(updated.tenant_id, updated.id),
            mapping={
                "id": updated.id,
                "tenant_id": updated.tenant_id,
                "name": updated.name,
                "status": updated.status,
                "created_at": updated.created_at,
                "updated_at": updated.updated_at,
                "description": updated.description or "",
            },
        )
        logger.info(
            "motet_catalog_updated",
            tenant_id=updated.tenant_id,
            motet_id=updated.id,
        )
        return updated

    def delete_motet(self, tenant_id: str, motet_id: str) -> None:
        """Delete a Motet from the catalog."""
        tid = validate_catalog_id(tenant_id, field_name="tenant_id")
        mid = validate_catalog_id(motet_id, field_name="motet_id")
        if not self._exists_any(self._motet_meta_keys(tid, mid)):
            # Still confirm tenant exists for clearer errors
            self.get_tenant(tid)
            raise MotetNotFoundError(f"Motet not found: {tid}/{mid}")
        delete_candidate_keys(self._r, self._motet_meta_keys(tid, mid))
        for index_key in self._motet_index_keys(tid):
            self._r.srem(index_key, mid)
        logger.info("motet_catalog_deleted", tenant_id=tid, motet_id=mid)

    def list_motets(
        self,
        tenant_id: str,
        *,
        status: Optional[str] = None,
    ) -> List[MotetRecord]:
        """List Motets for a tenant."""
        tid = validate_catalog_id(tenant_id, field_name="tenant_id")
        self.get_tenant(tid)
        if status is not None and status not in VALID_STATUSES:
            raise TenantValidationError(
                f"Invalid status: {status!r}. Must be one of {sorted(VALID_STATUSES)}"
            )
        results: List[MotetRecord] = []
        for mid in sorted(self._smembers_candidates(self._motet_index_keys(tid))):
            data = self._hgetall_candidates(self._motet_meta_keys(tid, mid))
            record = self._motet_from_hash(data)
            if record is None:
                for index_key in self._motet_index_keys(tid):
                    self._r.srem(index_key, mid)
                continue
            if status is not None and record.status != status:
                continue
            results.append(record)
        return results

    def ensure_defaults(self) -> Dict[str, Any]:
        """
        Idempotently seed a minimal catalog for local/dev:

        - ``motet-global`` / ``default`` — platform/operator tenant (also typically
          listed in ``MOTET_TENANT_GLOBAL_IDS`` for cross-tenant JWT scope)
        - ``default`` / ``default`` + ``prod`` — local single-tenant stub
        - ``demo`` / ``dev`` — demo org stub

        The ``motet-global`` display name deliberately reads as "platform" rather
        than "global": it is one tenant holding platform-level activity, not a
        cross-tenant view. Selecting every tenant is a separate choice.

        Returns counts of newly created tenants and Motets.
        """
        created = {"tenants": 0, "motets": 0}
        # (tenant_id, tenant_name, tenant_description, motet_id, motet_name)
        defaults = [
            (
                "motet-global",
                "Motet Platform",
                "Platform tenant holding operator and platform-level activity",
                "default",
                "Default",
            ),
            ("default", "Default", None, "default", "Default"),
            ("demo", "Demo", None, "dev", "Development"),
        ]
        for (
            tenant_id,
            tenant_name,
            tenant_description,
            motet_id,
            motet_name,
        ) in defaults:
            try:
                self.create_tenant(
                    tenant_id=tenant_id,
                    name=tenant_name,
                    description=tenant_description,
                )
                created["tenants"] += 1
            except TenantConflictError:
                pass
            try:
                self.create_motet(
                    tenant_id=tenant_id,
                    motet_id=motet_id,
                    name=motet_name,
                )
                created["motets"] += 1
            except (MotetConflictError, TenantNotFoundError):
                pass
        # Also seed prod under default (common ops-dashboard stub)
        try:
            self.create_motet(
                tenant_id="default",
                motet_id="prod",
                name="Production",
            )
            created["motets"] += 1
        except (MotetConflictError, TenantNotFoundError):
            pass
        logger.info("tenant_catalog_defaults_ensured", **created)
        return created
