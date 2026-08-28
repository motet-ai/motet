"""
Motet - MotetMCPClient list_tools Encryption Tests

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Unit tests ensuring MCP tools/list requests are published to Redis Streams using
    encrypted-only payloads (ADR-0056). This prevents regressions where list_tools()
    accidentally publishes plaintext `message_data` while consumers expect `_envelope`.

Dependencies:
    - pytest: test runner
    - unittest.mock: patching dependencies
    - motet.core.tools.mcp_motet.client.motet_mcp_client: class under test

Usage:
    pytest tests/unit/tools/mcp_motet/client/test_motet_mcp_client_list_tools_encrypted.py
"""

from __future__ import annotations

import base64
from typing import Any, Dict, Optional
from unittest.mock import patch

from motet.core.tools.mcp_motet.client.motet_mcp_client import MotetMCPClient


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


class FakeRedisClient:
    def __init__(self) -> None:
        self.last_xadd_stream: Optional[str] = None
        self.last_xadd_fields: Optional[Dict[str, Any]] = None

    def xadd(self, stream: str, fields: Dict[str, Any]) -> str:
        self.last_xadd_stream = stream
        self.last_xadd_fields = fields
        return "0-1"


def test_list_tools_publishes_encrypted_envelope():
    fake_redis = FakeRedisClient()
    client = MotetMCPClient(manager_id="mcp-manager-test")

    with patch("motet.core.distributed.redis_manager.get_sync_redis_client", return_value=fake_redis), patch(
        "motet.core.security.encryption_service.get_encryption_service", return_value=DummyEncryptionService()
    ), patch.object(MotetMCPClient, "_wait_for_response_sync", return_value={"tools": []}):
        result = client.list_tools(
            service_id="weather",
            tenant_id="discovery-tenant",
            visibility=None,  # allow heuristic scope
            timeout_seconds=1,
        )

    assert isinstance(result, dict)
    assert fake_redis.last_xadd_fields is not None

    fields = fake_redis.last_xadd_fields
    assert fields.get("message_type") == "requests"
    assert fields.get("service_id") == "weather"
    assert fields.get("request_id")  # included for AAD binding
    assert "_envelope" in fields
    assert "message_data" not in fields  # no plaintext message body


