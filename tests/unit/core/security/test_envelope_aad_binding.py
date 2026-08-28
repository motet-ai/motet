"""
Motet - Envelope Encryption AAD Binding Tests

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-18

Description:
    Unit tests for AES-GCM AAD binding in envelope encryption (ADR-0056 hardening).
    Ensures decryption fails if the AAD changes (prevents cut-and-paste attacks).

Dependencies:
    - pytest: test runner
    - cryptography: AESGCM is used by implementation

Usage:
    pytest tests/unit/core/security/test_envelope_aad_binding.py
"""

from __future__ import annotations

import base64
from typing import Any, Dict

import pytest

from motet.core.security.envelope_helper import envelope_encrypt_bytes, envelope_decrypt_bytes
from motet.core.security.aad_helpers import compute_command_data_aad, compute_command_result_aad


class DummyEncryptionService:
    def wrap_key(self, dek: bytes, tenant_id: str) -> Dict[str, Any]:
        return {
            "wrapped_key": base64.b64encode(dek).decode("ascii"),
            "iv": base64.b64encode(b"0123456789ab").decode("ascii"),
            "tenant_id": tenant_id,
            "encryption_version": "aes-256-gcm-v1",
        }

    def unwrap_key(self, wrapped_blob: Dict[str, Any]) -> bytes:
        return base64.b64decode(wrapped_blob["wrapped_key"])


def test_envelope_decrypt_fails_when_aad_mismatch() -> None:
    svc = DummyEncryptionService()
    tenant_id = "tenant-a"
    plaintext = b"hello"

    aad1 = b"scope-1"
    aad2 = b"scope-2"

    enc = envelope_encrypt_bytes(
        payload_bytes=plaintext,
        tenant_id=tenant_id,
        encryption_service=svc,
        context="test",
        aad=aad1,
    )

    ok = envelope_decrypt_bytes(enc.envelope, encryption_service=svc, context="test", aad=aad1)
    assert ok.plaintext == plaintext

    with pytest.raises(Exception):
        envelope_decrypt_bytes(enc.envelope, encryption_service=svc, context="test", aad=aad2)


def test_command_data_aad_binding_prevents_cut_and_paste() -> None:
    svc = DummyEncryptionService()
    tenant_id = "tenant-a"
    plaintext = b"command-data-payload"

    aad_ok = compute_command_data_aad(command_id="cmd-1", tenant_id=tenant_id, motet_id="test-motet")
    aad_wrong_command = compute_command_data_aad(command_id="cmd-2", tenant_id=tenant_id, motet_id="test-motet")

    enc = envelope_encrypt_bytes(
        payload_bytes=plaintext,
        tenant_id=tenant_id,
        encryption_service=svc,
        context="command_data",
        aad=aad_ok,
    )

    ok = envelope_decrypt_bytes(enc.envelope, encryption_service=svc, context="command_data", aad=aad_ok)
    assert ok.plaintext == plaintext

    with pytest.raises(Exception):
        envelope_decrypt_bytes(enc.envelope, encryption_service=svc, context="command_data", aad=aad_wrong_command)


def test_command_result_aad_binding_prevents_cut_and_paste() -> None:
    svc = DummyEncryptionService()
    tenant_id = "tenant-a"
    plaintext = b"command-result-payload"

    aad_ok = compute_command_result_aad(command_id="cmd-1", tenant_id=tenant_id, motet_id="test-motet")
    aad_wrong_tenant = compute_command_result_aad(command_id="cmd-1", tenant_id="tenant-b", motet_id="test-motet")

    enc = envelope_encrypt_bytes(
        payload_bytes=plaintext,
        tenant_id=tenant_id,
        encryption_service=svc,
        context="command_result",
        aad=aad_ok,
    )

    ok = envelope_decrypt_bytes(enc.envelope, encryption_service=svc, context="command_result", aad=aad_ok)
    assert ok.plaintext == plaintext

    with pytest.raises(Exception):
        envelope_decrypt_bytes(enc.envelope, encryption_service=svc, context="command_result", aad=aad_wrong_tenant)


def test_encrypted_payload_decrypt_accepts_legacy_logical_aad_after_rename() -> None:
    """Ciphertext written under the unprefixed key still decrypts after RENAME."""
    from motet.core.security.encrypted_payload_store import IsolationContext, _EncryptedPayloadLogic

    svc = DummyEncryptionService()
    logic = _EncryptedPayloadLogic(svc)
    isolation = IsolationContext(tenant_id="acme", motet_id="default", principal_id="user-1")
    logical = "imf:mem:default:acme:mem-1"
    phase2 = "acme:imf:mem:default:acme:mem-1"
    collapsed = "acme:mem:default:mem-1"
    payload = b'{"memory":{"id":"mem-1","content":"hello"}}'

    mapping, _ = logic.prepare_put(
        key=logical,
        payload=payload,
        isolation=isolation,
        context="memory",
    )
    result = logic.process_get(
        key=phase2,
        data=mapping,
        isolation=isolation,
        context="memory",
    )
    assert result.plaintext == payload
    result_collapsed = logic.process_get(
        key=collapsed,
        data=mapping,
        isolation=isolation,
        context="memory",
    )
    assert result_collapsed.plaintext == payload

    mapping_new, _ = logic.prepare_put(
        key=collapsed,
        payload=payload,
        isolation=isolation,
        context="memory",
    )
    result_new = logic.process_get(
        key=collapsed,
        data=mapping_new,
        isolation=isolation,
        context="memory",
    )
    assert result_new.plaintext == payload


def test_encrypted_payload_decrypt_accepts_historical_physical_aad() -> None:
    """Ciphertext bound to a Phase 2 physical key still decrypts after collapse."""
    from motet.core.security.aad_helpers import compute_encrypted_payload_store_aad
    from motet.core.security.encrypted_payload_store import IsolationContext, _EncryptedPayloadLogic
    from motet.core.security.json_helpers import json_dumps_compact

    svc = DummyEncryptionService()
    logic = _EncryptedPayloadLogic(svc)
    isolation = IsolationContext(tenant_id="acme", motet_id="default", principal_id="user-1")
    phase2 = "acme:imf:mem:default:acme:mem-1"
    collapsed = "acme:mem:default:mem-1"
    payload = b'{"memory":{"id":"mem-1","content":"hello"}}'
    aad = compute_encrypted_payload_store_aad(
        key=phase2,
        payload_context="memory",
        tenant_id="acme",
        motet_id="default",
        principal_id="user-1",
    )
    enc = envelope_encrypt_bytes(
        payload_bytes=payload,
        tenant_id="acme",
        encryption_service=svc,
        context="encrypted_payload_store",
        aad=aad,
    )
    mapping = {
        "_envelope": json_dumps_compact(enc.envelope),
        "tenant_id": "acme",
        "motet_id": "default",
        "principal_id": "user-1",
    }
    result = logic.process_get(
        key=collapsed,
        data=mapping,
        isolation=isolation,
        context="memory",
    )
    assert result.plaintext == payload


