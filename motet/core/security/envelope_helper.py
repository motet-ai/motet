"""
Motet - Envelope Encryption Helper

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Shared helper utilities for performing envelope encryption and decryption
    across Redis-backed storage layers. Provides consistent AES-256-GCM data
    protection with per-tenant DEKs that are wrapped by the tenant KEK stored
    in Vault. Centralizes timing metrics so Redis consumers (command data,
    schedules, memory, vault cache, etc.) can log consistent telemetry.

Dependencies:
    - cryptography: AES-GCM primitives used for encrypting payload bytes
    - structlog: structured logging for observability context
    - motet.core.security.encryption_service: key management + wrapping

Usage:
    from motet.core.security.envelope_helper import envelope_encrypt_bytes
    result = envelope_encrypt_bytes(
        payload_bytes=data,
        tenant_id="tenant-123",
        encryption_service=get_encryption_service(),
        context="command_data"
    )
    storage_blob = {
        **result.envelope,
        "metadata": {...}
    }

    decrypt_result = envelope_decrypt_bytes(
        storage_blob,
        encryption_service=get_encryption_service(),
        context="command_data"
    )
    plaintext_bytes = decrypt_result.plaintext

Notes:
    - All helpers enforce tenant_id presence
    - Timing metrics are returned so callers can log/forward them
    - The helper focuses on byte payloads; callers own serialization
"""

from __future__ import annotations

import base64
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Protocol, TYPE_CHECKING

import structlog
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

if TYPE_CHECKING:  # pragma: no cover
    from .encryption_service import EncryptionService  # noqa: F401

logger = structlog.get_logger(__name__)


class EnvelopeKeyProvider(Protocol):
    """Protocol describing the wrap/unwrap capabilities we need."""

    def wrap_key(self, dek: bytes, tenant_id: str) -> Dict[str, Any]:
        ...

    def unwrap_key(self, wrapped_blob: Dict[str, Any]) -> bytes:
        ...


@dataclass
class EnvelopeEncryptResult:
    """Result container for envelope encryption operations."""

    envelope: Dict[str, Any]
    encryption_time_ms: float
    dek_wrap_time_ms: float


@dataclass
class EnvelopeDecryptResult:
    """Result container for envelope decryption operations."""

    plaintext: bytes
    tenant_id: str
    decryption_time_ms: float
    dek_unwrap_time_ms: float


def _require_tenant_id(tenant_id: str, context: str) -> None:
    if not tenant_id:
        raise ValueError(
            f"Envelope encryption requires tenant_id (context={context}). "
            "Refusing to continue to avoid collapsing tenant isolation."
        )


def _require_encryption_service(
    encryption_service: EnvelopeKeyProvider,
    context: str,
) -> EnvelopeKeyProvider:
    if encryption_service is None:
        raise ValueError(f"Encryption service unavailable for context={context}")
    return encryption_service


def envelope_encrypt_bytes(
    payload_bytes: bytes,
    tenant_id: str,
    encryption_service: EnvelopeKeyProvider,
    *,
    context: str,
    aad: bytes | None = None,
) -> EnvelopeEncryptResult:
    """
    Encrypt arbitrary bytes using the standard envelope (DEK + wrapped DEK) flow.
    """
    _require_tenant_id(tenant_id, context)
    service = _require_encryption_service(encryption_service, context)

    encryption_start = time.time()
    dek = AESGCM.generate_key(bit_length=256)
    aesgcm = AESGCM(dek)
    iv = os.urandom(12)
    encrypted_bytes = aesgcm.encrypt(iv, payload_bytes, aad)
    encryption_time_ms = (time.time() - encryption_start) * 1000

    wrap_start = time.time()
    wrapped_dek = service.wrap_key(dek, tenant_id)
    dek_wrap_time_ms = (time.time() - wrap_start) * 1000

    envelope = {
        "encrypted": True,
        "encryption_mode": "envelope-v1",
        "encryption": {
            "encrypted_data": base64.b64encode(encrypted_bytes).decode("utf-8"),
            "iv": base64.b64encode(iv).decode("utf-8"),
            "tenant_id": tenant_id,
            "encryption_version": "aes-256-gcm-v1",
        },
        "dek": wrapped_dek,
    }

    logger.debug(
        "envelope_encrypt_bytes_success",
        context=context,
        tenant_id=tenant_id,
        encryption_time_ms=round(encryption_time_ms, 2),
        dek_wrap_time_ms=round(dek_wrap_time_ms, 2),
    )

    return EnvelopeEncryptResult(
        envelope=envelope,
        encryption_time_ms=encryption_time_ms,
        dek_wrap_time_ms=dek_wrap_time_ms,
    )


def envelope_decrypt_bytes(
    envelope: Dict[str, Any],
    encryption_service: EnvelopeKeyProvider,
    *,
    context: str,
    aad: bytes | None = None,
) -> EnvelopeDecryptResult:
    """
    Decrypt bytes produced by `envelope_encrypt_bytes`.
    """
    service = _require_encryption_service(encryption_service, context)
    encrypted_blob = envelope.get("encryption")
    wrapped_dek = envelope.get("dek")

    if not encrypted_blob or not wrapped_dek:
        raise ValueError(f"Envelope missing encryption data (context={context})")

    tenant_id = encrypted_blob.get("tenant_id")
    _require_tenant_id(tenant_id, context)

    dek_unwrap_start = time.time()
    dek = service.unwrap_key(wrapped_dek)
    dek_unwrap_time_ms = (time.time() - dek_unwrap_start) * 1000

    decrypt_start = time.time()
    aesgcm = AESGCM(dek)
    iv = base64.b64decode(encrypted_blob["iv"])
    encrypted_bytes = base64.b64decode(encrypted_blob["encrypted_data"])
    plaintext = aesgcm.decrypt(iv, encrypted_bytes, aad)
    decryption_time_ms = (time.time() - decrypt_start) * 1000

    logger.debug(
        "envelope_decrypt_bytes_success",
        context=context,
        tenant_id=tenant_id,
        dek_unwrap_time_ms=round(dek_unwrap_time_ms, 2),
        decryption_time_ms=round(decryption_time_ms, 2),
    )

    return EnvelopeDecryptResult(
        plaintext=plaintext,
        tenant_id=tenant_id,
        decryption_time_ms=decryption_time_ms,
        dek_unwrap_time_ms=dek_unwrap_time_ms,
    )


__all__ = [
    "EnvelopeEncryptResult",
    "EnvelopeDecryptResult",
    "envelope_encrypt_bytes",
    "envelope_decrypt_bytes",
]

