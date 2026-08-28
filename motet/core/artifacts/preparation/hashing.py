"""
Motet - Artifact Preparation Hashing

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Provides deterministic hashing helpers for artifact preparation.
    Cache keys include source content, strategy version, canonical configuration,
    and planner decisions so re-preparation can be replayed and invalidated
    without relying on hidden runtime state.

Dependencies:
    - hashlib for SHA256 hashing
    - json for canonical configuration serialization

Usage:
    config_hash = canonical_json_hash({"chunk_size": 3200})
    cache_key = chunk_cache_key(source_content_hash="...", strategy_id="text_default", ...)

Notes:
    - The helpers accept plain dictionaries so strategy modules can avoid
      importing command or API models.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Optional


def canonical_json_hash(value: Any) -> str:
    """Return a stable SHA256 hash for a JSON-serializable value."""

    encoded = json.dumps(value or {}, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def text_content_hash(value: str) -> str:
    """Return a SHA256 hash for text content."""

    return hashlib.sha256((value or "").encode("utf-8", errors="ignore")).hexdigest()


def structured_content_hash(*, content_text: str, structured_payload: Any = None) -> str:
    """Return a SHA256 hash over text plus optional structured payload."""

    payload_hash = canonical_json_hash(structured_payload)
    return hashlib.sha256(f"{content_text or ''}\0{payload_hash}".encode("utf-8", errors="ignore")).hexdigest()


def source_bytes_sha256(payload_bytes: bytes) -> str:
    """Return SHA256 hex digest of raw artifact bytes (source-extraction invariant)."""

    return hashlib.sha256(payload_bytes or b"").hexdigest()


def effective_source_content_hash(*, declared_hash: Optional[str], payload_bytes: bytes) -> str:
    """Prefer declared checksum when set; otherwise hash raw payload bytes (not extracted text)."""

    cleaned = str(declared_hash or "").strip()
    if cleaned:
        return cleaned
    return source_bytes_sha256(payload_bytes)


def chunk_cache_key(
    *,
    source_content_hash: str,
    strategy_id: str,
    strategy_version: str,
    canonical_config_hash: str,
    planner_decision_hash: str = "",
) -> str:
    """Return the ADR-0110 content-addressable preparation chunk cache key."""

    raw = "\0".join(
        [
            source_content_hash or "",
            strategy_id or "",
            strategy_version or "",
            canonical_config_hash or "",
            planner_decision_hash or "",
        ]
    )
    return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()

