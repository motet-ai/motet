"""
Motet - MCP Stream Encryption Unit Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-20

Description:
    Encode/decode round-trip for MCP I/O envelopes after issue #235. AAD must
    bind the logical bus name so tenant-prefixed physical keys still decrypt.
    SCAN globs must not match ``{manager}:mcp-control``.

Dependencies:
    - pytest
    - unittest.mock
    - motet.core.tools.mcp_motet.stream_encryption
    - motet.core.tools.mcp_motet.protocol

Usage:
    pytest tests/unit/tools/mcp_motet/test_stream_encryption.py -q
"""

from __future__ import annotations

import base64
from fnmatch import fnmatch
from typing import Any, Dict
from unittest.mock import patch

import pytest
from cryptography.exceptions import InvalidTag
from motet.core.security.envelope_decode_helpers import decode_mcp_stream_envelope
from motet.core.tools.mcp_motet.manager.control_commands import mcp_control_stream_key
from motet.core.tools.mcp_motet.protocol import (
    MCPRequestMessage,
    StreamType,
    Visibility,
    generate_stream_name,
    logical_mcp_bus_name,
    mcp_io_stream_scan_patterns,
)
from motet.core.tools.mcp_motet.stream_encryption import (
    decode_mcp_stream_fields,
    encode_mcp_stream_fields,
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


def _tenant_request() -> tuple[str, MCPRequestMessage]:
    stream_name = generate_stream_name(
        "weather",
        Visibility.MOTET,
        "weather:acme-corp:production",
        StreamType.REQUESTS,
        manager_id="mcp-local-default",
    )
    message = MCPRequestMessage(
        id="req-235",
        service_id="weather",
        instance_key="weather:acme-corp:production",
        tenant_id="acme-corp",
        motet_id="production",
        jsonrpc_request={"jsonrpc": "2.0", "id": "req-235", "method": "tools/list"},
    )
    return stream_name, message


def test_aad_stream_key_strips_tenant_and_family() -> None:
    physical = (
        "acme-corp:mcp:mcp-local-default:mcp-weather-motet-acme-corp:production-requests"
    )
    assert logical_mcp_bus_name(physical) == (
        "mcp-local-default:mcp-weather-motet-acme-corp:production-requests"
    )


def test_encode_rejects_pre_235_physical_name() -> None:
    """Leftover {manager}:mcp-… keys are not encrypted after bounce."""
    _, message = _tenant_request()
    with pytest.raises(ValueError, match="Invalid stream name format"):
        encode_mcp_stream_fields(
            stream_name=(
                "mcp-local-default:mcp-weather-motet-acme-corp:production-requests"
            ),
            message=message,
            tenant_id="acme-corp",
            motet_id="production",
            message_type="requests",
        )


def test_encode_decode_round_trip_on_tenant_prefixed_physical_key() -> None:
    stream_name, message = _tenant_request()
    assert stream_name.startswith("acme-corp:mcp:mcp-local-default:")

    with patch(
        "motet.core.security.encryption_service.get_encryption_service",
        return_value=DummyEncryptionService(),
    ):
        fields = encode_mcp_stream_fields(
            stream_name=stream_name,
            message=message,
            tenant_id="acme-corp",
            motet_id="production",
            message_type="requests",
        )
        decoded = decode_mcp_stream_fields(
            stream_name=stream_name,
            envelope_json=str(fields["_envelope"]),
            message_type="requests",
            request_id="req-235",
            tenant_id="acme-corp",
            motet_id="production",
            service_id="weather",
        )

    assert decoded["id"] == "req-235"
    assert decoded["service_id"] == "weather"
    assert decoded["jsonrpc_request"]["method"] == "tools/list"


def test_decode_with_physical_stream_key_directly_fails_aad() -> None:
    """Callers must not pass the physical {tid}:mcp: key into raw envelope AAD."""
    stream_name, message = _tenant_request()

    with patch(
        "motet.core.security.encryption_service.get_encryption_service",
        return_value=DummyEncryptionService(),
    ):
        fields = encode_mcp_stream_fields(
            stream_name=stream_name,
            message=message,
            tenant_id="acme-corp",
            motet_id="production",
            message_type="requests",
        )
        with pytest.raises(InvalidTag):
            decode_mcp_stream_envelope(
                envelope_json=str(fields["_envelope"]),
                stream_key=stream_name,
                message_type="requests",
                request_id="req-235",
                tenant_id="acme-corp",
                motet_id="production",
                service_id="weather",
            )


def test_io_scan_patterns_do_not_match_manager_control_stream() -> None:
    control = mcp_control_stream_key("mcp-local-default")
    assert control == "mcp-local-default:mcp-control"
    for pattern in mcp_io_stream_scan_patterns("mcp-local-default"):
        assert not fnmatch(control, pattern)
    io_global = "mcp:mcp-local-default:mcp-weather-global-global-requests"
    io_tenant = "acme:mcp:mcp-local-default:mcp-weather-motet-acme:production-requests"
    patterns = mcp_io_stream_scan_patterns("mcp-local-default")
    assert any(fnmatch(io_global, p) for p in patterns)
    assert any(fnmatch(io_tenant, p) for p in patterns)
