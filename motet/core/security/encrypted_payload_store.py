"""
Motet - Encrypted Redis Payload Store

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-26

Description:
    Provides an explicit, opt-in API for storing and retrieving sensitive payloads in Redis
    using envelope encryption. This module intentionally avoids "invisible"
    encryption in the general Redis manager to preserve normal Redis semantics for
    indices/counters/locks and to make security-sensitive operations explicit at call sites.

    The API is designed around a single pattern:
    - Store minimal plaintext routing/isolation fields in a Redis hash
    - Store an encrypted `_envelope` field containing the payload bytes
    - Require tenant_id (fail closed) when encryption is enabled

Dependencies:
    - json: Serialize envelope structures for Redis hash storage
    - motet.core.security.envelope_helper: Envelope encryption primitives (DEK + wrapped DEK)
    - motet.core.security.encryption_service: KEK wrapping/unwrapping and tenant key management
    - motet.core.distributed.redis_manager: Redis client acquisition

Usage:
    from motet.core.security.encrypted_payload_store import get_sync_encrypted_payload_store

    store = get_sync_encrypted_payload_store(service_name="artifact_store")
    store.put_bytes(
        key="art:123",
        payload=b"secret payload",
        tenant_id="tenant-1",
        motet_id="default",
        principal_id="user-1",
        context="tool_artifact",
        ttl_seconds=3600,
        plaintext_fields={"content_type": "application/octet-stream"},
    )
    result = store.get_bytes(
        key="art:123",
        tenant_id="tenant-1",
        motet_id="default",
        principal_id="user-1",
        context="tool_artifact",
    )

Notes:
    - No plaintext fallback: payloads are encrypted when encryption is enabled.
    - Access control checks are performed using plaintext isolation fields before decryption.
    - Encrypt AAD binds the collapsed logical key name, not the physical Redis
      key. Decrypt retries prior AAD bindings (physical key, Phase 2
      ``{tenant}:imf:…``, unprefixed ``imf:…``). Re-seal leftovers with
      ``scripts/backfill_encrypted_payload_aad.py`` so reads hit the current
      binding first.
"""

from __future__ import annotations

import inspect
import time
from typing import Any, Dict, Optional, Tuple, cast

import structlog

from pydantic import BaseModel, Field, ConfigDict

from .aad_helpers import compute_encrypted_payload_store_aad
from .encryption_contexts import EncryptionContext
from .envelope_helper import envelope_encrypt_bytes, envelope_decrypt_bytes, EnvelopeEncryptResult, EnvelopeDecryptResult
from .json_helpers import json_dumps_compact, json_dumps_compact_bytes, json_loads
from .redis_decode_helpers import normalize_redis_str_mapping

logger = structlog.get_logger(__name__)


async def _maybe_await(value: Any) -> Any:
    """Return awaited value when the Redis client call is awaitable."""
    if inspect.isawaitable(value):
        return await value
    return value


def _require_non_empty(value: Optional[str], field_name: str, *, context: str) -> str:
    if not value:
        raise ValueError(f"{field_name} is required for encrypted payload store (context={context})")
    return value


class IsolationContext(BaseModel):
    """Isolation context for encrypted payload operations."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(..., description="Verified tenant identifier used for KEK lookup and access checks")
    principal_id: Optional[str] = Field(
        default=None,
        description="Optional principal identifier used for access checks (when enforced by the caller)",
    )
    motet_id: Optional[str] = Field(
        default=None,
        description="Optional motet identifier used for access checks (when enforced by the caller)",
    )


class _EncryptedPayloadLogic:
    """Shared logic for encrypted payload storage (sync/async agnostic)."""

    def __init__(self, encryption_service: Any):
        self._encryption_service = encryption_service

    def prepare_put(
        self,
        *,
        key: str,
        payload: bytes,
        isolation: IsolationContext,
        context: str,
        plaintext_fields: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Dict[str, str], EnvelopeEncryptResult]:
        tenant_id = _require_non_empty(isolation.tenant_id, "tenant_id", context=context)

        from motet.core.distributed.tenant_keys import stable_aad_logical_key

        aad_key = stable_aad_logical_key(key, tenant_id)
        aad = compute_encrypted_payload_store_aad(
            key=aad_key,
            payload_context=context,
            tenant_id=tenant_id,
            motet_id=str(isolation.motet_id or ""),
            principal_id=str(isolation.principal_id or ""),
        )
        
        encrypt_result = envelope_encrypt_bytes(
            payload_bytes=payload,
            tenant_id=tenant_id,
            encryption_service=self._encryption_service,
            context=EncryptionContext.ENCRYPTED_PAYLOAD_STORE.value,
            aad=aad,
        )

        mapping: Dict[str, str] = {
            "_envelope": json_dumps_compact(encrypt_result.envelope),
            "tenant_id": tenant_id,
        }
        if isolation.principal_id:
            mapping["principal_id"] = str(isolation.principal_id)
        if isolation.motet_id:
            mapping["motet_id"] = str(isolation.motet_id)

        if plaintext_fields:
            for k, v in plaintext_fields.items():
                mapping[str(k)] = json_dumps_compact(v) if isinstance(v, (dict, list)) else str(v)
                
        return mapping, encrypt_result

    def process_get(
        self,
        *,
        key: str,
        data: Dict[Any, Any],
        isolation: IsolationContext,
        context: str,
    ) -> EnvelopeDecryptResult:
        tenant_id = _require_non_empty(isolation.tenant_id, "tenant_id", context=context)

        normalized = normalize_redis_str_mapping(data)

        stored_tenant = normalized.get("tenant_id")
        stored_principal = normalized.get("principal_id")
        stored_motet = normalized.get("motet_id")

        # Fail-closed: if caller supplies an isolation field, stored value must exist and match.
        if tenant_id and (not stored_tenant or stored_tenant != tenant_id):
            raise PermissionError("tenant_id mismatch")
        if isolation.principal_id and (not stored_principal or stored_principal != isolation.principal_id):
            raise PermissionError("principal_id mismatch")
        if isolation.motet_id and (not stored_motet or stored_motet != isolation.motet_id):
            raise PermissionError("motet_id mismatch")

        envelope_raw = normalized.get("_envelope")
        if not envelope_raw:
            raise ValueError(f"Encrypted payload missing _envelope (key={key}, context={context})")

        try:
            envelope = json_loads(envelope_raw)
        except Exception as e:
            # Catch json.JSONDecodeError and others
            raise ValueError(f"Invalid _envelope JSON (key={key}, context={context}): {e}") from e

        result, _aad_key = self.unlock_envelope(
            key=key,
            envelope=envelope,
            isolation=isolation,
            context=context,
            stored_tenant=str(stored_tenant or tenant_id),
            stored_motet=str(stored_motet or ""),
            stored_principal=str(stored_principal or ""),
        )
        return result

    def unlock_envelope(
        self,
        *,
        key: str,
        envelope: Dict[str, Any],
        isolation: IsolationContext,
        context: str,
        stored_tenant: str,
        stored_motet: str,
        stored_principal: str,
    ) -> Tuple[EnvelopeDecryptResult, str]:
        """
        Decrypt an envelope, trying historical AAD key names.

        Returns ``(result, aad_key_used)`` so a re-seal can skip rows already
        bound to ``stable_aad_logical_key``.
        """
        from cryptography.exceptions import InvalidTag

        from motet.core.distributed.tenant_keys import payload_aad_key_candidates

        aad_tenant = stored_tenant or isolation.tenant_id
        last_invalid: Optional[InvalidTag] = None
        for aad_key in payload_aad_key_candidates(key, aad_tenant):
            aad = compute_encrypted_payload_store_aad(
                key=aad_key,
                payload_context=context,
                tenant_id=aad_tenant,
                motet_id=stored_motet,
                principal_id=stored_principal,
            )
            try:
                result = envelope_decrypt_bytes(
                    envelope=envelope,
                    encryption_service=self._encryption_service,
                    context=EncryptionContext.ENCRYPTED_PAYLOAD_STORE.value,
                    aad=aad,
                )
                return result, aad_key
            except InvalidTag as exc:
                last_invalid = exc
                if aad_key != key:
                    logger.debug(
                        "encrypted_payload_aad_legacy_key_retry",
                        key=key,
                        aad_key=aad_key,
                        context=context,
                    )
                continue
        if last_invalid is not None:
            raise last_invalid
        raise ValueError(f"Encrypted payload decrypt failed (key={key}, context={context})")


class SyncEncryptedPayloadStore:
    """
    Synchronous encrypted payload store (Celery/task-friendly).
    Stores payloads in Redis hashes under `_envelope` and plaintext routing fields.
    """

    def __init__(self, service_name: str, *, redis_client: Any = None, encryption_service: Any = None):
        self.service_name = service_name
        if encryption_service is not None:
            self._encryption_service = encryption_service
        else:
            # Local import to avoid circular import during core module initialization.
            from .encryption_service import get_encryption_service

            self._encryption_service = get_encryption_service()
        
        self._logic = _EncryptedPayloadLogic(self._encryption_service)
        
        if redis_client is not None:
            self._redis = redis_client
        else:
            # Local import to avoid circular import during core module initialization.
            from ..distributed.redis_manager import get_sync_redis_client

            self._redis = get_sync_redis_client(service_name)

    def put_bytes(
        self,
        *,
        key: str,
        payload: bytes,
        isolation: IsolationContext,
        context: str,
        ttl_seconds: Optional[int] = None,
        plaintext_fields: Optional[Dict[str, Any]] = None,
    ) -> None:
        started = time.time()
        mapping, encrypt_result = self._logic.prepare_put(
            key=key,
            payload=payload,
            isolation=isolation,
            context=context,
            plaintext_fields=plaintext_fields,
        )

        self._redis.hset(key, mapping=mapping)
        if ttl_seconds is not None:
            self._redis.expire(key, int(ttl_seconds))

        logger.debug(
            "encrypted_payload_put_bytes",
            key=key,
            context=context,
            tenant_id=isolation.tenant_id,
            bytes=len(payload),
            encryption_time_ms=round(encrypt_result.encryption_time_ms, 2),
            dek_wrap_time_ms=round(encrypt_result.dek_wrap_time_ms, 2),
            total_ms=round((time.time() - started) * 1000, 2),
        )

    def get_bytes(
        self,
        *,
        key: str,
        isolation: IsolationContext,
        context: str,
    ) -> bytes:
        data = cast(Any, self._redis.hgetall(key)) or {}
        if not data:
            raise KeyError(key)

        decrypt_result = self._logic.process_get(
            key=key,
            data=data,
            isolation=isolation,
            context=context,
        )

        logger.debug(
            "encrypted_payload_get_bytes",
            key=key,
            context=context,
            tenant_id=decrypt_result.tenant_id,
            bytes=len(decrypt_result.plaintext),
            dek_unwrap_time_ms=round(decrypt_result.dek_unwrap_time_ms, 2),
            decryption_time_ms=round(decrypt_result.decryption_time_ms, 2),
        )

        return decrypt_result.plaintext

    def put_json(
        self,
        *,
        key: str,
        payload: Any,
        isolation: IsolationContext,
        context: str,
        ttl_seconds: Optional[int] = None,
        plaintext_fields: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Convenience wrapper for JSON payloads."""
        self.put_bytes(
            key=key,
            payload=json_dumps_compact_bytes(payload),
            isolation=isolation,
            context=context,
            ttl_seconds=ttl_seconds,
            plaintext_fields=plaintext_fields,
        )

    def get_json(
        self,
        *,
        key: str,
        isolation: IsolationContext,
        context: str,
    ) -> Any:
        """Convenience wrapper for JSON payloads."""
        raw = self.get_bytes(key=key, isolation=isolation, context=context)
        return json_loads(raw.decode("utf-8", errors="ignore"))


class AsyncEncryptedPayloadStore:
    """Async encrypted payload store (service/async-friendly)."""

    def __init__(self, service_name: str, *, redis_client: Any = None, encryption_service: Any = None):
        self.service_name = service_name
        if encryption_service is not None:
            self._encryption_service = encryption_service
        else:
            # Local import to avoid circular import during core module initialization.
            from .encryption_service import get_encryption_service

            self._encryption_service = get_encryption_service()
            
        self._logic = _EncryptedPayloadLogic(self._encryption_service)

        if redis_client is not None:
            self._redis = redis_client
        else:
            # Local import to avoid circular import during core module initialization.
            from ..distributed.redis_manager import get_redis_client

            self._redis = get_redis_client(service_name)

    async def put_bytes(
        self,
        *,
        key: str,
        payload: bytes,
        isolation: IsolationContext,
        context: str,
        ttl_seconds: Optional[int] = None,
        plaintext_fields: Optional[Dict[str, Any]] = None,
    ) -> None:
        started = time.time()
        mapping, encrypt_result = self._logic.prepare_put(
            key=key,
            payload=payload,
            isolation=isolation,
            context=context,
            plaintext_fields=plaintext_fields,
        )

        await _maybe_await(self._redis.hset(key, mapping=mapping))
        if ttl_seconds is not None:
            await _maybe_await(self._redis.expire(key, int(ttl_seconds)))
            
        logger.debug(
            "encrypted_payload_put_bytes",
            key=key,
            context=context,
            tenant_id=isolation.tenant_id,
            bytes=len(payload),
            encryption_time_ms=round(encrypt_result.encryption_time_ms, 2),
            dek_wrap_time_ms=round(encrypt_result.dek_wrap_time_ms, 2),
            total_ms=round((time.time() - started) * 1000, 2),
        )

    async def get_bytes(
        self,
        *,
        key: str,
        isolation: IsolationContext,
        context: str,
    ) -> bytes:
        data = cast(Any, await _maybe_await(self._redis.hgetall(key)))
        if not data:
            raise KeyError(key)

        decrypt_result = self._logic.process_get(
            key=key,
            data=data,
            isolation=isolation,
            context=context,
        )
        return decrypt_result.plaintext

    async def put_json(
        self,
        *,
        key: str,
        payload: Any,
        isolation: IsolationContext,
        context: str,
        ttl_seconds: Optional[int] = None,
        plaintext_fields: Optional[Dict[str, Any]] = None,
    ) -> None:
        await self.put_bytes(
            key=key,
            payload=json_dumps_compact_bytes(payload),
            isolation=isolation,
            context=context,
            ttl_seconds=ttl_seconds,
            plaintext_fields=plaintext_fields,
        )

    async def get_json(
        self,
        *,
        key: str,
        isolation: IsolationContext,
        context: str,
    ) -> Any:
        raw = await self.get_bytes(key=key, isolation=isolation, context=context)
        return json_loads(raw.decode("utf-8", errors="ignore"))


_sync_stores: Dict[str, SyncEncryptedPayloadStore] = {}
_async_stores: Dict[str, AsyncEncryptedPayloadStore] = {}


def get_sync_encrypted_payload_store(service_name: str) -> SyncEncryptedPayloadStore:
    store = _sync_stores.get(service_name)
    if store is None:
        store = SyncEncryptedPayloadStore(service_name=service_name)
        _sync_stores[service_name] = store
    return store


def get_async_encrypted_payload_store(service_name: str) -> AsyncEncryptedPayloadStore:
    store = _async_stores.get(service_name)
    if store is None:
        store = AsyncEncryptedPayloadStore(service_name=service_name)
        _async_stores[service_name] = store
    return store


__all__ = [
    "IsolationContext",
    "SyncEncryptedPayloadStore",
    "AsyncEncryptedPayloadStore",
    "get_sync_encrypted_payload_store",
    "get_async_encrypted_payload_store",
]
