"""
Motet - Encryption Context Constants

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    Centralizes envelope encryption "context" labels used across Redis-backed storage
    layers. Using a single enum avoids string drift (typos / mismatched
    contexts) that can break decryptability and AAD verification.

Dependencies:
    - enum: Enum implementation

Usage:
    from motet.core.security.encryption_contexts import EncryptionContext

    ctx = EncryptionContext.CMD_META.value
    encrypt_result = envelope_encrypt_bytes(..., context=ctx, aad=...)

Notes:
    - These are intentionally stable and should only change via an explicit ADR update.
    - If a context label changes, existing ciphertext becomes undecryptable unless
      migration tooling is provided.
"""

from __future__ import annotations

from enum import Enum


class EncryptionContext(str, Enum):
    """Stable context labels for envelope encryption and AAD binding."""

    # Command execution blob storage (MsgPack-wrapped)
    COMMAND_DATA = "command_data"
    COMMAND_RESULT = "command_result"

    # Command execution metadata and streaming (Redis hashes/streams)
    COMMAND_STREAM = "command_stream"
    CMD_META = "cmd_meta"

    # MCP transport streams
    MCP_STREAM = "mcp_stream"

    # Redis-backed storage layers
    MEMORY = "memory"
    TOOL_ARTIFACT = "tool_artifact"
    ENCRYPTED_PAYLOAD_STORE = "encrypted_payload_store"

    # Vault-adjacent caches
    VAULT_CACHE = "vault_cache"


__all__ = ["EncryptionContext"]


