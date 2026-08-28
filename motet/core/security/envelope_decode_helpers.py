"""
Motet - Envelope Decode Helpers

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Shared helpers for decoding encrypted `_envelope` payloads stored in Redis
    (Streams and hashes) using envelope encryption.

    This module centralizes the "compute AAD -> decrypt envelope -> JSON parse"
    logic to prevent subtle drift between writers and readers across:
    - Command streaming events (context="command_stream")
    - Command metadata blobs (context="cmd_meta")
    - MCP request/response stream messages (context="mcp_stream", typically no AAD)

Dependencies:
    - json: Payload decoding
    - motet.core.security.encrypted_stream_codec: Low-level envelope decrypt/parse
    - motet.core.security.aad_helpers: Stable AAD computation

Usage:
    from motet.core.security.envelope_decode_helpers import (
        decode_command_stream_envelope,
        decode_cmd_meta_envelope,
        decode_mcp_stream_envelope,
    )

    payload = decode_command_stream_envelope(
        envelope_json=envelope_str,
        stream_key="task:trace:response",
        event="token",
        task_id="trace",
        command_id="cmd-1",
        tenant_id="tenant-1",
        motet_id="default",
    )

Notes:
    - This module is intentionally small and side-effect free.
    - These helpers are fail-closed: callers should handle exceptions upstream.
"""

from __future__ import annotations

import json
from typing import Any, Dict

from .aad_helpers import compute_cmd_meta_aad, compute_command_stream_aad, compute_mcp_stream_aad
from .encryption_contexts import EncryptionContext
from .encrypted_stream_codec import decode_encrypted_message_data


def decode_command_stream_envelope(
    *,
    envelope_json: str,
    stream_key: str,
    event: str,
    task_id: str,
    command_id: str,
    tenant_id: str,
    motet_id: str,
) -> Dict[str, Any]:
    """Decode a `command_stream` `_envelope` with AAD binding."""
    aad = compute_command_stream_aad(
        stream_key=stream_key,
        event=event,
        task_id=task_id,
        command_id=command_id,
        tenant_id=tenant_id,
        motet_id=motet_id,
    )
    decoded = decode_encrypted_message_data(
        envelope_json=envelope_json,
        context=EncryptionContext.COMMAND_STREAM.value,
        aad=aad,
    )
    return decoded if isinstance(decoded, dict) else {}


def decode_cmd_meta_envelope(
    *,
    envelope_json: str,
    command_id: str,
    tenant_id: str,
    motet_id: str,
) -> Dict[str, Any]:
    """Decode a `cmd_meta` `_envelope` with AAD binding."""
    aad = compute_cmd_meta_aad(command_id=command_id, tenant_id=tenant_id, motet_id=motet_id)
    decoded = decode_encrypted_message_data(
        envelope_json=envelope_json,
        context=EncryptionContext.CMD_META.value,
        aad=aad,
    )
    return decoded if isinstance(decoded, dict) else {}


def decode_mcp_stream_envelope(
    *,
    envelope_json: str,
    stream_key: str,
    message_type: str,
    request_id: str,
    tenant_id: str,
    motet_id: str,
    service_id: str,
) -> Any:
    """Decode an MCP stream `_envelope` with AAD binding."""
    aad = compute_mcp_stream_aad(
        stream_key=stream_key,
        message_type=message_type,
        request_id=request_id,
        tenant_id=tenant_id,
        motet_id=motet_id,
        service_id=service_id,
    )
    return decode_encrypted_message_data(
        envelope_json=envelope_json,
        context=EncryptionContext.MCP_STREAM.value,
        aad=aad,
    )


def parse_json_maybe(value: Any) -> Dict[str, Any]:
    """
    Best-effort parse to dict for event bodies that store JSON in a string field.
    Returns {} if value is empty or not a dict JSON.
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        if not value:
            return {}
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


__all__ = [
    "decode_command_stream_envelope",
    "decode_cmd_meta_envelope",
    "decode_mcp_stream_envelope",
    "parse_json_maybe",
]


