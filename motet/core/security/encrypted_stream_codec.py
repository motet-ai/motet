"""
Motet - Encrypted Redis Stream Codec

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    A small helper for encrypting and decrypting Redis Streams message payload fields
    using the envelope encryption helpers. This is intended for high-volume
    stream paths (MCP request/response streams and command streaming events) where
    sensitive message bodies must not be stored in plaintext in Redis (including AOF/RDB).

    This codec is encrypted-only: plaintext message bodies are not supported.

Dependencies:
    - json: serialization for stream payloads
    - motet.core.security.envelope_helper: envelope encryption primitives
    - motet.core.security.encryption_service: KEK wrap/unwrap and tenant key management

Usage:
    fields = encode_encrypted_message_data(
        message_data={"foo": "bar"},
        tenant_id="tenant-1",
        context="mcp_stream",
        include_plaintext={"message_type": "requests"},
    )
    # => {"message_type":"requests","_envelope":"{...}"}
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from .json_helpers import json_dumps_compact_bytes

def encode_encrypted_message_data(
    *,
    message_data: Any,
    tenant_id: str,
    motet_id: Optional[str] = None,
    context: str,
    include_plaintext: Optional[Dict[str, Any]] = None,
    aad: Optional[bytes] = None,
) -> Dict[str, Any]:
    """
    Encode a stream entry where the sensitive payload is stored in `_envelope`.

    Returns a dict suitable for XADD field mapping (values are str/int/float/bytes).
    """
    # Lazy imports to avoid circular dependencies during core import graph initialization.
    from .encryption_service import get_encryption_service
    from .envelope_helper import envelope_encrypt_bytes

    encryption_service = get_encryption_service()
    encrypt_result = envelope_encrypt_bytes(
        payload_bytes=json_dumps_compact_bytes(message_data),
        tenant_id=tenant_id,
        encryption_service=encryption_service,
        context=context,
        aad=aad,
    )
    out: Dict[str, Any] = dict(include_plaintext or {})
    if tenant_id:
        out["tenant_id"] = str(tenant_id)
    if motet_id:
        out["motet_id"] = str(motet_id)
    out["_envelope"] = json.dumps(encrypt_result.envelope, ensure_ascii=False, separators=(",", ":"), default=str)
    return out


def decode_encrypted_message_data(
    *,
    envelope_json: str,
    context: str,
    aad: Optional[bytes] = None,
) -> Any:
    """Decode `_envelope` JSON string back to the original message payload."""
    # Lazy imports to avoid circular dependencies during core import graph initialization.
    from .encryption_service import get_encryption_service
    from .envelope_helper import envelope_decrypt_bytes

    encryption_service = get_encryption_service()
    envelope = json.loads(envelope_json)
    decrypt_result = envelope_decrypt_bytes(
        envelope=envelope,
        encryption_service=encryption_service,
        context=context,
        aad=aad,
    )
    return json.loads(decrypt_result.plaintext.decode("utf-8", errors="ignore"))


__all__ = ["encode_encrypted_message_data", "decode_encrypted_message_data"]


