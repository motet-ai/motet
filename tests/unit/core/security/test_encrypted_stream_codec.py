"""
Motet - Encrypted Stream Codec Tests

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Unit tests for the encrypted Redis Streams codec used to protect stream message bodies
    (MCP request/response streams and command streaming). These tests are encrypted-only:
    there is no plaintext/legacy fallback in non-production development mode.

Dependencies:
    - pytest: test runner
    - motet.core.security.encrypted_stream_codec: codec under test

Usage:
    pytest tests/unit/core/security/test_encrypted_stream_codec.py
"""

from __future__ import annotations

import base64
from typing import Any, Dict
from unittest.mock import patch

from motet.core.security.encrypted_stream_codec import (
    encode_encrypted_message_data,
    decode_encrypted_message_data,
)


class DummyEncryptionService:
    """Minimal KEK wrapper used by envelope_helper (wrap/unwrap only)."""

    def wrap_key(self, dek: bytes, tenant_id: str) -> Dict[str, Any]:
        return {
            "wrapped_key": base64.b64encode(dek).decode("ascii"),
            "iv": base64.b64encode(b"0123456789ab").decode("ascii"),
            "tenant_id": tenant_id,
            "encryption_version": "aes-256-gcm-v1",
        }

    def unwrap_key(self, wrapped_blob: Dict[str, Any]) -> bytes:
        return base64.b64decode(wrapped_blob["wrapped_key"])


def test_roundtrip_encrypts_message_data():
    with patch("motet.core.security.encryption_service.get_encryption_service", return_value=DummyEncryptionService()):
        fields = encode_encrypted_message_data(
            message_data={"hello": "world"},
            tenant_id="tenant-a",
            context="mcp_stream",
            include_plaintext={"message_type": "requests"},
        )

        assert fields["message_type"] == "requests"
        assert "_envelope" in fields
        assert "hello" not in fields["_envelope"]  # ciphertext envelope json should not contain plaintext field

        decoded = decode_encrypted_message_data(envelope_json=fields["_envelope"], context="mcp_stream")
        assert decoded == {"hello": "world"}


