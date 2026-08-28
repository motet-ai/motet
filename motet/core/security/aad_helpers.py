"""
Motet - AAD Helpers for Envelope Encryption

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Centralized helpers for computing Associated Authenticated Data (AAD) bytes for
    AES-GCM envelope encryption.

    These helpers exist to prevent subtle drift between writers and readers when
    hashing AAD material for:
    - Command data/results stored as envelope-encrypted MsgPack blobs (`context="command_data"` / `context="command_result"`)
    - Command streaming events stored in Redis Streams (`context="command_stream"`)
    - Command metadata stored under `cmd:meta:{command_id}` (`context="cmd_meta"`)
    - MCP stream messages stored in Redis Streams (`context="mcp_stream"`)
    - Generic encrypted Redis hash payloads (`context="encrypted_payload_store"`)

Dependencies:
    - json: Deterministic JSON serialization for AAD material
    - hashlib: SHA-256 hashing to produce fixed-length AAD bytes

Usage:
    from motet.core.security.aad_helpers import (
        compute_command_stream_aad,
        compute_cmd_meta_aad,
    )

    aad = compute_command_stream_aad(
        stream_key="task:abc:response",
        event="token",
        task_id="abc",
        command_id="cmd-1",
        tenant_id="tenant-1",
        motet_id="default",
    )

    meta_aad = compute_cmd_meta_aad(
        command_id="cmd-1",
        tenant_id="tenant-1",
        motet_id="default",
    )

    mcp_aad = compute_mcp_stream_aad(
        stream_key="mcp-zoom-user-...",
        message_type="requests",
        request_id="req-123",
        tenant_id="tenant-1",
        motet_id="default",
        service_id="zoom",
    )

    blob_aad = compute_encrypted_payload_store_aad(
        key="art:123",
        payload_context="tool_artifact",
        tenant_id="tenant-1",
        motet_id="default",
        principal_id="user-1",
    )

Notes:
    - AAD is hashed (SHA-256) to keep it compact and stable across platforms.
    - Inputs are normalized to strings and stripped to reduce accidental mismatches.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def _norm(value: Any) -> str:
    """Normalize an AAD component to a stable string representation."""
    if value is None:
        return ""
    return str(value).strip()


def compute_command_stream_aad(
    *,
    stream_key: str,
    event: str,
    task_id: str,
    command_id: str,
    tenant_id: str,
    motet_id: str,
    version: int = 1,
) -> bytes:
    """
    Compute AAD for Redis stream event envelopes in the command streaming path.

    This MUST match both writer (`MotetContext.stream_event`) and reader
    (`DistributedOrchestrator`) AAD computation.
    """
    aad_material = json.dumps(
        {
            "v": int(version),
            "context": "command_stream",
            "stream_key": _norm(stream_key),
            "event": _norm(event),
            "task_id": _norm(task_id),
            "command_id": _norm(command_id),
            "tenant_id": _norm(tenant_id),
            "motet_id": _norm(motet_id),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8", errors="ignore")
    return hashlib.sha256(aad_material).digest()


def compute_command_blob_aad(
    *,
    command_id: str,
    tenant_id: str,
    motet_id: str,
    payload_context: str,
    version: int = 1,
) -> bytes:
    """
    Compute AAD for command data/result envelopes stored as MsgPack blobs.

    This binds ciphertext to the logical payload_context (e.g., "command_data",
    "command_result") plus the command_id + tenant_id + motet_id, preventing cut-and-paste
    substitution across commands within the same tenant and across motets.
    """
    aad_material = json.dumps(
        {
            "v": int(version),
            "context": "command_blob",
            "payload_context": _norm(payload_context),
            "command_id": _norm(command_id),
            "tenant_id": _norm(tenant_id),
            "motet_id": _norm(motet_id),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8", errors="ignore")
    return hashlib.sha256(aad_material).digest()


def compute_command_data_aad(*, command_id: str, tenant_id: str, motet_id: str, version: int = 1) -> bytes:
    """Compute AAD for `command_data` envelope encryption."""
    return compute_command_blob_aad(
        command_id=command_id,
        tenant_id=tenant_id,
        motet_id=motet_id,
        payload_context="command_data",
        version=version,
    )


def compute_command_result_aad(*, command_id: str, tenant_id: str, motet_id: str, version: int = 1) -> bytes:
    """Compute AAD for `command_result` envelope encryption."""
    return compute_command_blob_aad(
        command_id=command_id,
        tenant_id=tenant_id,
        motet_id=motet_id,
        payload_context="command_result",
        version=version,
    )


def compute_cmd_meta_aad(
    *,
    command_id: str,
    tenant_id: str,
    motet_id: str,
    version: int = 1,
) -> bytes:
    """
    Compute AAD for `cmd:meta:{command_id}` envelope encryption.

    This MUST match both writer (`RedisCommandDataManager.store/update_command_metadata`)
    and any readers (debug endpoints) that decrypt the `_envelope`.
    """
    aad_material = json.dumps(
        {
            "v": int(version),
            "context": "cmd_meta",
            "command_id": _norm(command_id),
            "tenant_id": _norm(tenant_id),
            "motet_id": _norm(motet_id),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8", errors="ignore")
    return hashlib.sha256(aad_material).digest()


def compute_mcp_stream_aad(
    *,
    stream_key: str,
    message_type: str,
    request_id: str,
    tenant_id: str,
    motet_id: str,
    service_id: str,
    version: int = 1,
) -> bytes:
    """
    Compute AAD for MCP stream envelopes.

    This binds ciphertext to:
    - stream name (logical bus name ``[manager_id:]mcp-…``, not ``{tid}:``)
    - message_type + request_id (routing/association)
    - tenant_id + motet_id + service_id (isolation + service)
    """
    aad_material = json.dumps(
        {
            "v": int(version),
            "context": "mcp_stream",
            "stream_key": _norm(stream_key),
            "message_type": _norm(message_type),
            "request_id": _norm(request_id),
            "tenant_id": _norm(tenant_id),
            "motet_id": _norm(motet_id),
            "service_id": _norm(service_id),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8", errors="ignore")
    return hashlib.sha256(aad_material).digest()


def compute_encrypted_payload_store_aad(
    *,
    key: str,
    payload_context: str,
    tenant_id: str,
    motet_id: str = "",
    principal_id: str = "",
    version: int = 1,
) -> bytes:
    """
    Compute AAD for the generic encrypted payload store (Redis hashes).

    This binds ciphertext to:
    - a stable logical key name (not the physical Redis key; issue #218)
    - the higher-level payload_context (e.g. tool_artifact, memory)
    - tenant/motet/principal isolation identifiers (when available)
    """
    aad_material = json.dumps(
        {
            "v": int(version),
            "context": "encrypted_payload_store",
            "key": _norm(key),
            "payload_context": _norm(payload_context),
            "tenant_id": _norm(tenant_id),
            "motet_id": _norm(motet_id),
            "principal_id": _norm(principal_id),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8", errors="ignore")
    return hashlib.sha256(aad_material).digest()


__all__ = [
    "compute_command_blob_aad",
    "compute_command_data_aad",
    "compute_command_result_aad",
    "compute_command_stream_aad",
    "compute_cmd_meta_aad",
    "compute_mcp_stream_aad",
    "compute_encrypted_payload_store_aad",
]


