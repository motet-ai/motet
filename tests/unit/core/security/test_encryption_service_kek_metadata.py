"""
Motet - EncryptionService KEK Metadata Tests

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Unit tests for ADR-0056 hardening: wrapped DEKs include KEK identity metadata
    (key id + fingerprint) to support rotation diagnostics and audit.

Dependencies:
    - pytest: test runner
    - hashlib/base64: used for fingerprint assertions

Usage:
    pytest tests/unit/core/security/test_encryption_service_kek_metadata.py
"""

from __future__ import annotations

import hashlib

from motet.core.security.encryption_service import EncryptionService


def test_wrap_key_includes_kek_identity_metadata(monkeypatch) -> None:
    svc = EncryptionService(vault_service=None)

    kek = b"x" * 32

    def _fake_get_tenant_key(tenant_id: str) -> bytes:
        return kek

    monkeypatch.setattr(svc, "get_tenant_key", _fake_get_tenant_key)

    tenant_id = "tenant-a"
    dek = b"d" * 32
    wrapped = svc.wrap_key(dek=dek, tenant_id=tenant_id)

    assert wrapped.get("kek_id") == f"encryption:tenant:{tenant_id}"
    assert wrapped.get("kek_fingerprint_sha256") == hashlib.sha256(kek).hexdigest()


