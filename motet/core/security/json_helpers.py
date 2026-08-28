"""
Motet - JSON Helpers (Compact + Deterministic)

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Small, shared JSON helpers used by Redis encryption components.
    Centralizing these functions avoids subtle drift across modules when:
    - dumping compact JSON for storage in Redis hashes/streams
    - converting JSON-serializable objects into bytes for encryption
    - parsing JSON envelopes and payloads

Dependencies:
    - json: serialization/deserialization
    - typing: type hints

Usage:
    from motet.core.security.json_helpers import (
        json_dumps_compact,
        json_dumps_compact_bytes,
        json_loads,
    )

    raw = json_dumps_compact_bytes({"a": 1})
    obj = json_loads('{"a":1}')

Notes:
    - `default=str` is used to keep dumps resilient for debugging metadata payloads.
    - These helpers are intentionally tiny; callers handle schema validation.
"""

from __future__ import annotations

import json
from typing import Any


def json_dumps_compact(obj: Any) -> str:
    """Dump JSON without extra whitespace for storage/transmission."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


def json_dumps_compact_bytes(obj: Any) -> bytes:
    """Dump JSON and encode UTF-8 bytes for encryption."""
    return json_dumps_compact(obj).encode("utf-8", errors="ignore")


def json_loads(value: str) -> Any:
    """Load JSON from a string."""
    return json.loads(value)


__all__ = ["json_dumps_compact", "json_dumps_compact_bytes", "json_loads"]


