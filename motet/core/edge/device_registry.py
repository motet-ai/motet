"""
Motet - Edge Device Registry

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Registers edge worker devices per principal: issues device tokens, stores metadata
    in Redis, supports verification for authentication and revocation.

    extension: stores WireGuard peer configuration (public key, assigned IP,
    server endpoint) for tunnel-based edge worker connectivity.

    (Phase D) extension: registration accepts an explicit worker_id so
    remote app-builder instances can register as ``edge_app_builder_<app>`` —
    matching the multi-app instancing scheme — instead of the derived
    ``edge_<uuid8>``. Explicit ids are validated and uniqueness-claimed in Redis.

Dependencies:
    - secrets, uuid: Token and id generation
    - motet.core.distributed.redis_manager: get_sync_redis_client
    - json: Token row serialization

Usage:
    from motet.core.edge.device_registry import EdgeDeviceRegistry, DeviceRecord

Notes:
    - Device tokens use prefix ld_; treat as secrets (shown once at registration)
    - Register writes ``motet:edge_device:token:{token}`` (and worker / meta /
      lookup / index locators) → tenant so verify and revoke can GET then
      read the tenant key without a keyspace SCAN
    - WireGuard config is stored in Redis hash and returned once at registration
"""

from __future__ import annotations

import json
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

import structlog

from motet.core.distributed.redis_manager import get_sync_redis_client
from motet.core.distributed.tenant_keys import (
    first_existing_key,
    maybe_tenant_key,
    product_key,
    smembers_union,
    tenant_key,
)

logger = structlog.get_logger(__name__)


def expected_worker_id(principal_id: str, device_id: str) -> str:
    """Deterministic worker ID for a local device.

    Uses the first 8 hex chars of the device UUID for brevity while
    retaining sufficient uniqueness (~4 billion values).  Principal and
    device identity are stored separately in WorkerInfo metadata.
    """
    short = device_id.replace("-", "")[:8]
    return f"edge_{short}"


# Explicit worker ids (ADR-0123 Phase D): must look like an edge worker id
# and stay within Celery queue-name-friendly characters. 64 chars total.
WORKER_ID_PATTERN = re.compile(r"^edge_[a-z0-9][a-z0-9_.-]{0,58}$")


def validate_worker_id(worker_id: str) -> str:
    """Validate an explicit device worker id; returns the normalized id.

    Raises ValueError when the id does not match the edge worker id shape
    (``edge_`` prefix, lowercase alphanumerics/underscore/dot/dash, ≤64 chars).
    """
    candidate = (worker_id or "").strip()
    if not WORKER_ID_PATTERN.match(candidate):
        raise ValueError(
            f"Invalid worker_id: {worker_id!r}. Must match "
            f"{WORKER_ID_PATTERN.pattern} (e.g. edge_app_builder_myapp)."
        )
    return candidate


COMMAND_SCOPE_PRINCIPAL = "principal"
COMMAND_SCOPE_TENANT = "tenant"
VALID_COMMAND_SCOPES = {COMMAND_SCOPE_PRINCIPAL, COMMAND_SCOPE_TENANT}


@dataclass(frozen=True)
class DeviceAuthSession:
    """Authentication session for a verified device token."""

    principal_id: str
    tenant_id: str
    device_id: str
    worker_id: str
    command_scope: str = COMMAND_SCOPE_PRINCIPAL

REDIS_CLIENT_ID = "edge_devices"
KEY_PREFIX = "edge_device"


@dataclass(frozen=True)
class DeviceRecord:
    """Non-secret device metadata for listing."""

    device_id: str
    worker_id: str
    device_name: str
    principal_id: str
    tenant_id: str
    created_at: str
    revoked: bool
    command_scope: str = COMMAND_SCOPE_PRINCIPAL
    last_connected_at: Optional[str] = None
    wg_peer_ip: Optional[str] = None


class EdgeDeviceRegistry:
    """Redis-backed device registration and token verification."""

    def __init__(self) -> None:
        self._r = get_sync_redis_client(REDIS_CLIENT_ID)

    def _meta_logical(self, principal_id: str, device_id: str) -> str:
        return f"{KEY_PREFIX}:meta:{principal_id}:{device_id}"

    def _meta_key(self, principal_id: str, device_id: str, tenant_id: Optional[str] = None) -> str:
        return maybe_tenant_key(tenant_id, self._meta_logical(principal_id, device_id))

    def _token_logical(self, token: str) -> str:
        return f"{KEY_PREFIX}:token:{token}"

    def _token_key(self, token: str, tenant_id: Optional[str] = None) -> str:
        return maybe_tenant_key(tenant_id, self._token_logical(token))

    def _lookup_logical(self, principal_id: str, device_id: str) -> str:
        return f"{KEY_PREFIX}:lookup:{principal_id}:{device_id}"

    def _lookup_key(self, principal_id: str, device_id: str, tenant_id: Optional[str] = None) -> str:
        return maybe_tenant_key(tenant_id, self._lookup_logical(principal_id, device_id))

    def _worker_logical(self, worker_id: str) -> str:
        return f"{KEY_PREFIX}:worker:{worker_id}"

    def _worker_key(self, worker_id: str, tenant_id: Optional[str] = None) -> str:
        return maybe_tenant_key(tenant_id, self._worker_logical(worker_id))

    def _index_logical(self, principal_id: str) -> str:
        return f"{KEY_PREFIX}:index:{principal_id}"

    def _index_key(self, principal_id: str, tenant_id: Optional[str] = None) -> str:
        return maybe_tenant_key(tenant_id, self._index_logical(principal_id))

    def _locator_key(self, logical: str) -> str:
        return product_key(logical)

    def _resolve_logical(self, logical: str, tenant_id: Optional[str] = None) -> Optional[str]:
        if tenant_id:
            return first_existing_key(self._r, tenant_key(tenant_id, logical))
        located = self._get_text(self._locator_key(logical))
        if located:
            return first_existing_key(
                self._r, tenant_key(located, logical)
            ) or tenant_key(located, logical)
        return None

    def _to_text(self, value: Any) -> Optional[str]:
        """Normalize Redis values to text for type-safe processing."""
        if value is None:
            return None
        if isinstance(value, str):
            return value
        if isinstance(value, (bytes, bytearray)):
            return value.decode("utf-8")
        logger.warning("invalid_edge_device_redis_value_type", value_type=type(value).__name__)
        return None

    def _get_text(self, key: str) -> Optional[str]:
        """Typed wrapper for Redis GET."""
        return self._to_text(self._r.get(key))

    def _hgetall_text(self, key: str) -> Dict[str, str]:
        """Typed wrapper for Redis HGETALL with string normalization."""
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
        """Typed wrapper for Redis SMEMBERS with string normalization."""
        raw = self._r.smembers(key)
        if not raw:
            return set()
        if not isinstance(raw, set):
            logger.warning("invalid_edge_device_smembers_type", value_type=type(raw).__name__)
            return set()

        normalized: Set[str] = set()
        for value in raw:
            text = self._to_text(value)
            if text is not None:
                normalized.add(text)
        return normalized

    def register_device(
        self,
        *,
        principal_id: str,
        tenant_id: str,
        device_name: str,
        command_scope: str = COMMAND_SCOPE_PRINCIPAL,
        worker_id: Optional[str] = None,
        wg_public_key: Optional[str] = None,
        wg_peer_ip: Optional[str] = None,
        wg_endpoint: Optional[str] = None,
    ) -> tuple[str, str, str]:
        """
        Register a device. Returns (device_id, worker_id, device_token).

        ADR-0095: optionally stores WireGuard peer info (public key, assigned IP,
        server endpoint) for tunnel provisioning.

        ADR-0123 (Phase D): ``worker_id`` may be provided explicitly (e.g.
        ``edge_app_builder_<app>`` for remote builder instances). Explicit ids
        are validated against WORKER_ID_PATTERN and uniqueness-claimed in
        Redis; a ValueError is raised when the id is malformed or already
        claimed by another active device.

        Args:
            command_scope: "principal" (default) — only commands from the
                registering principal are accepted. "tenant" — any principal
                in the same tenant may dispatch commands to this device.
            worker_id: Optional explicit worker id; default derives
                ``edge_<uuid8>`` from the device id.
        """
        if command_scope not in VALID_COMMAND_SCOPES:
            raise ValueError(f"Invalid command_scope: {command_scope!r}. Must be one of {VALID_COMMAND_SCOPES}")

        device_id = str(uuid.uuid4())
        token = f"ld_{secrets.token_urlsafe(32)}"
        explicit_worker = bool(worker_id)
        if worker_id:
            worker_id = validate_worker_id(worker_id)
            if self._resolve_logical(self._worker_logical(worker_id)):
                claimed = False
            else:
                claimed = self._r.set(
                    self._worker_key(worker_id, tenant_id),
                    json.dumps({"principal_id": principal_id, "device_id": device_id}),
                    nx=True,
                )
            if not claimed:
                raise ValueError(
                    f"worker_id already in use: {worker_id!r}. Revoke the "
                    "existing device or choose a different id."
                )
        else:
            worker_id = expected_worker_id(principal_id, device_id)
        created = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

        meta: Dict[str, Any] = {
            "device_id": device_id,
            "principal_id": principal_id,
            "tenant_id": tenant_id,
            "device_name": device_name,
            "worker_id": worker_id,
            "command_scope": command_scope,
            "created_at": created,
            "revoked": "false",
        }
        if wg_public_key:
            meta["wg_public_key"] = wg_public_key
        if wg_peer_ip:
            meta["wg_peer_ip"] = wg_peer_ip
        if wg_endpoint:
            meta["wg_endpoint"] = wg_endpoint

        row = json.dumps(
            {
                "principal_id": principal_id,
                "tenant_id": tenant_id,
                "device_id": device_id,
                "device_name": device_name,
                "worker_id": worker_id,
                "command_scope": command_scope,
            }
        )
        pipe = self._r.pipeline()
        pipe.hset(self._meta_key(principal_id, device_id, tenant_id), mapping=meta)
        pipe.set(self._token_key(token, tenant_id), row)
        pipe.set(self._lookup_key(principal_id, device_id, tenant_id), token)
        pipe.sadd(self._index_key(principal_id, tenant_id), device_id)
        pipe.set(self._locator_key(self._token_logical(token)), tenant_id)
        pipe.set(self._locator_key(self._meta_logical(principal_id, device_id)), tenant_id)
        pipe.set(self._locator_key(self._lookup_logical(principal_id, device_id)), tenant_id)
        pipe.set(self._locator_key(self._index_logical(principal_id)), tenant_id)
        if explicit_worker:
            pipe.set(self._locator_key(self._worker_logical(worker_id)), tenant_id)
        pipe.execute()

        logger.info("edge_device_registered", principal_id=principal_id, device_id=device_id)
        return device_id, worker_id, token

    def verify_token(self, token: str) -> Optional[DeviceAuthSession]:
        """Return session if token is valid and device not revoked."""
        token_key = self._resolve_logical(self._token_logical(token))
        raw_payload = self._get_text(token_key) if token_key else None
        if not raw_payload:
            return None

        data = json.loads(raw_payload)
        principal_id = data["principal_id"]
        device_id = data["device_id"]
        tenant_id = str(data.get("tenant_id") or "")
        meta_key = self._resolve_logical(self._meta_logical(principal_id, device_id), tenant_id)
        meta = self._hgetall_text(meta_key) if meta_key else {}
        if not meta:
            return None
        revoked = str(meta.get("revoked", "false")).lower() == "true"
        if revoked:
            return None
        worker_id = data.get("worker_id") or expected_worker_id(principal_id, device_id)
        return DeviceAuthSession(
            principal_id=principal_id,
            tenant_id=data["tenant_id"],
            device_id=device_id,
            worker_id=worker_id,
            command_scope=data.get("command_scope", COMMAND_SCOPE_PRINCIPAL),
        )

    def revoke_device(
        self,
        principal_id: str,
        device_id: str,
        tenant_id: Optional[str] = None,
    ) -> bool:
        """Revoke device: delete token mapping and mark meta revoked.

        Also releases an explicit worker-id claim (ADR-0123 Phase D) when the
        claim belongs to this device, so the id can be re-registered.
        """
        tenant_hint = tenant_id
        mk = self._resolve_logical(self._meta_logical(principal_id, device_id), tenant_hint)
        if not mk:
            return False
        meta = self._hgetall_text(mk)
        tenant_hint = str(meta.get("tenant_id") or "") or tenant_hint
        lk = self._resolve_logical(self._lookup_logical(principal_id, device_id), tenant_hint)
        token = self._get_text(lk) if lk else None
        pipe = self._r.pipeline(transaction=True)
        if token:
            token_key = self._resolve_logical(self._token_logical(token), tenant_hint)
            if token_key:
                pipe.delete(token_key)
        if lk:
            pipe.delete(lk)
        pipe.hset(mk, "revoked", "true")
        for index_key in (
            (tenant_key(tenant_hint, self._index_logical(principal_id)),)
            if tenant_hint
            else (self._index_logical(principal_id),)
        ):
            pipe.srem(index_key, device_id)
        worker_id = meta.get("worker_id")
        if worker_id:
            claim_key = self._resolve_logical(self._worker_logical(worker_id), tenant_hint)
            claim_raw = self._get_text(claim_key) if claim_key else None
            if claim_raw:
                try:
                    claim = json.loads(claim_raw)
                except ValueError:
                    claim = {}
                if claim.get("device_id") == device_id and claim_key:
                    pipe.delete(claim_key)
                    pipe.delete(self._locator_key(self._worker_logical(worker_id)))
        if token:
            pipe.delete(self._locator_key(self._token_logical(token)))
        pipe.delete(self._locator_key(self._meta_logical(principal_id, device_id)))
        pipe.delete(self._locator_key(self._lookup_logical(principal_id, device_id)))
        pipe.execute()
        logger.info("edge_device_revoked", principal_id=principal_id, device_id=device_id)
        return True

    def list_devices(
        self,
        principal_id: str,
        tenant_id: Optional[str] = None,
    ) -> List[DeviceRecord]:
        index_logical = self._index_logical(principal_id)
        if tenant_id:
            found_index = self._index_key(principal_id, tenant_id)
        else:
            found_index = self._resolve_logical(index_logical)
        index_keys = [found_index] if found_index else [index_logical]
        if found_index and found_index != index_logical:
            index_keys.append(index_logical)
        ids = smembers_union(self._r, [k for k in index_keys if k])
        out: List[DeviceRecord] = []
        for device_id in sorted(ids or []):
            mk = self._resolve_logical(
                self._meta_logical(principal_id, device_id), tenant_id
            )
            h = self._hgetall_text(mk) if mk else {}
            if not h:
                continue
            revoked = str(h.get("revoked", "false")).lower() == "true"
            out.append(
                DeviceRecord(
                    device_id=str(h.get("device_id", device_id)),
                    worker_id=str(h.get("worker_id", "")),
                    device_name=str(h.get("device_name", "")),
                    principal_id=str(h.get("principal_id", principal_id)),
                    tenant_id=str(h.get("tenant_id", "")),
                    created_at=str(h.get("created_at", "")),
                    revoked=revoked,
                    command_scope=str(h.get("command_scope", COMMAND_SCOPE_PRINCIPAL)),
                    last_connected_at=str(h.get("last_connected_at", "")) or None,
                    wg_peer_ip=str(h.get("wg_peer_ip", "")) or None,
                )
            )
        return out

    def touch_connected(self, principal_id: str, device_id: str) -> None:
        """Update last_connected_at on meta (optional)."""
        ts = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        mk = self._resolve_logical(self._meta_logical(principal_id, device_id))
        if mk:
            self._r.hset(mk, "last_connected_at", ts)


__all__ = [
    "COMMAND_SCOPE_PRINCIPAL",
    "COMMAND_SCOPE_TENANT",
    "DeviceRecord",
    "EdgeDeviceRegistry",
    "WORKER_ID_PATTERN",
    "validate_worker_id",
]
