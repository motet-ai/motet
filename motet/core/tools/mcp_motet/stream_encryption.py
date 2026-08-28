"""
Motet - MCP Stream Encryption Helpers

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Shared helpers for encrypting Redis Streams payloads used by the MCP Motet transport.

    MCP request/response/log/event stream entries must not store message bodies in plaintext. The system stores minimal routing metadata in plaintext (e.g. message_type)
    and stores the full message body encrypted in an `_envelope` field.

    This module centralizes:
    - tenant/motet scope resolution for stream encryption
    - encoding of encrypted stream field dictionaries for XADD
    - decoding with the same AAD stream-key binding as encode (issue #235)

Dependencies:
    - os: Environment-based fallbacks for discovery/startup contexts
    - motet.core.security.encrypted_stream_codec: Envelope encoding for stream fields
    - motet.core.security.envelope_decode_helpers: MCP envelope decrypt
    - motet.core.tools.mcp_motet.protocol: Stream parsing helpers and Visibility enum
    - structlog: Structured logging for diagnostics

Usage:
    from motet.core.tools.mcp_motet.stream_encryption import (
        resolve_mcp_stream_scope,
        encode_mcp_stream_fields,
        decode_mcp_stream_fields,
    )

    tenant_id, motet_id = resolve_mcp_stream_scope(stream_name, message)
    fields = encode_mcp_stream_fields(
        message_data=message.model_dump(mode="json"),
        tenant_id=tenant_id,
        motet_id=motet_id,
        message_type=message.stream_type.value,
    )

Notes:
    - Env fallback is intended for discovery/startup contexts; production paths should
      prefer explicit scope or instance_key-derived scope.
    - Message bodies are encrypted; there is no plaintext fallback.
    - AAD ``stream_key`` is the logical bus name (``[manager_id:]mcp-…``), not
      the physical ``{tid}:mcp:`` key (issue #235). Encode and decode must use
      the same helper or tenant-prefixed streams fail AES-GCM.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Tuple

import structlog

from motet.core.security.encrypted_stream_codec import encode_encrypted_message_data
from motet.core.security.aad_helpers import compute_mcp_stream_aad
from motet.core.security.encryption_contexts import EncryptionContext
from motet.core.security.envelope_decode_helpers import decode_mcp_stream_envelope
from motet.core.tools.mcp_motet.protocol import (
    MCPStreamMessage,
    Visibility,
    logical_mcp_bus_name,
    parse_instance_key,
    parse_stream_name,
)

logger = structlog.get_logger(__name__)


def resolve_mcp_stream_scope(
    *,
    stream_name: str,
    message: MCPStreamMessage,
    allow_env_fallback: bool = True,
    default_tenant_id: str = "default",
    default_motet_id: str = "default",
) -> Tuple[str, str]:
    """
    Resolve (tenant_id, motet_id) for encrypting MCP stream message bodies.

    Precedence:
    - Explicit fields on the message (preferred)
    - Parse from instance_key (preferred for proxy-published logs/events)
    - Environment fallback for discovery/startup contexts (optional)

    Args:
        stream_name: Redis stream name (used to determine visibility)
        message: MCP stream message
        allow_env_fallback: If True, fall back to env/defaults when scope is missing
        default_tenant_id: Default tenant_id when env fallback is enabled
        default_motet_id: Default motet_id when env fallback is enabled

    Returns:
        (tenant_id, motet_id)
    """
    tenant_id = getattr(message, "tenant_id", None)
    motet_id = getattr(message, "motet_id", None)

    # If tenant_id/motet_id not on message, extract from instance_key
    instance_key = getattr(message, "instance_key", None)
    if (not tenant_id or not motet_id) and isinstance(instance_key, str) and instance_key:
        stream_parsed = parse_stream_name(stream_name)
        if stream_parsed:
            visibility_str = stream_parsed.get("visibility")
            if visibility_str:
                try:
                    visibility = Visibility(visibility_str)
                    instance_parsed = parse_instance_key(
                        service_id=message.service_id,
                        visibility=visibility,
                        instance_key=instance_key,
                    )
                    tenant_id = tenant_id or instance_parsed.get("tenant_id")
                    motet_id = motet_id or instance_parsed.get("motet_id")
                except (ValueError, KeyError) as e:
                    logger.warning(
                        "mcp_stream_scope_parse_failed",
                        stream_name=stream_name,
                        instance_key=getattr(message, "instance_key", None),
                        error=str(e),
                    )

    if allow_env_fallback:
        tenant_id = tenant_id or os.getenv("MOTET_TENANT_ID") or default_tenant_id
        motet_id = motet_id or os.getenv("MOTET_MOTET_ID") or default_motet_id

    tenant_id = (str(tenant_id) if tenant_id is not None else "").strip()
    motet_id = (str(motet_id) if motet_id is not None else "").strip()

    if not tenant_id:
        raise ValueError("tenant_id is required for MCP stream encryption")
    if not motet_id:
        raise ValueError("motet_id is required for MCP stream encryption")

    return tenant_id, motet_id


def should_purge_on_kek_mismatch(error: str) -> bool:
    """True when decrypt failed on KEK mismatch and purge is enabled."""
    if os.getenv("MOTET_MCP_PURGE_ON_KEK_MISMATCH", "false").lower() != "true":
        return False
    return "Key unwrapping failed" in error or "KEK fingerprint mismatch" in error


def encode_mcp_stream_fields(
    *,
    stream_name: str,
    message: MCPStreamMessage,
    tenant_id: str,
    motet_id: str,
    message_type: str,
    context: str = EncryptionContext.MCP_STREAM.value,
) -> Dict[str, Any]:
    """
    Encode fields for Redis XADD with encrypted message body.

    Args:
        message_data: Full message body to encrypt (JSON-serializable dict)
        tenant_id: Tenant scope for encryption
        motet_id: Motet scope for encryption
        message_type: Plaintext routing type (e.g. "requests", "responses", "logs", "events")
        context: Envelope encryption context label

    Returns:
        Dict of stream fields including plaintext `message_type` and encrypted `_envelope`.
    """
    if not tenant_id:
        raise ValueError("tenant_id is required for MCP stream encryption")
    if not motet_id:
        raise ValueError("motet_id is required for MCP stream encryption")

    # Bind ciphertext to stream scope + identifiers to prevent cut/paste across streams/requests.
    request_id = getattr(message, "request_id", None) or getattr(message, "id", "") or ""
    service_id = getattr(message, "service_id", "") or ""
    if not request_id:
        raise ValueError("request_id is required for MCP stream AAD binding")
    if not service_id:
        raise ValueError("service_id is required for MCP stream AAD binding")

    aad = compute_mcp_stream_aad(
        stream_key=logical_mcp_bus_name(stream_name),
        message_type=str(message_type),
        request_id=str(request_id),
        tenant_id=str(tenant_id),
        motet_id=str(motet_id),
        service_id=str(service_id),
    )

    return encode_encrypted_message_data(
        message_data=message.model_dump(mode="json"),
        tenant_id=str(tenant_id),
        motet_id=str(motet_id),
        context=context,
        aad=aad,
        include_plaintext={
            "message_type": str(message_type),
            "request_id": str(request_id),
            "service_id": str(service_id),
        },
    )


def decode_mcp_stream_fields(
    *,
    stream_name: str,
    envelope_json: str,
    message_type: str,
    request_id: str,
    tenant_id: str,
    motet_id: str,
    service_id: str,
) -> Any:
    """Decrypt an MCP stream `_envelope` using the same AAD key as encode."""
    return decode_mcp_stream_envelope(
        envelope_json=envelope_json,
        stream_key=logical_mcp_bus_name(stream_name),
        message_type=message_type,
        request_id=request_id,
        tenant_id=tenant_id,
        motet_id=motet_id,
        service_id=service_id,
    )


__all__ = [
    "resolve_mcp_stream_scope",
    "should_purge_on_kek_mismatch",
    "encode_mcp_stream_fields",
    "decode_mcp_stream_fields",
]


