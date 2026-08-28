"""
Motet - Redis Decode Helpers

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Shared helpers for normalizing Redis return types into Python-native types.
    Redis clients may return `bytes` for keys and values depending on configuration
    (decode_responses, client library defaults, etc.). This module centralizes the
    decode logic to avoid duplicated, slightly-different implementations across
    encryption call sites.

Dependencies:
    - typing: Type hints

Usage:
    from motet.core.security.redis_decode_helpers import normalize_redis_mapping

    raw = redis.hgetall("some:key")
    fields = normalize_redis_mapping(raw)
    envelope_json = fields.get("_envelope", "")

Notes:
    - Keys are always normalized to `str`.
    - `bytes` values are decoded as UTF-8 with replacement to avoid exceptions.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping


def _to_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    return str(value)


def normalize_redis_mapping(data: Mapping[Any, Any]) -> Dict[str, Any]:
    """
    Normalize a Redis-returned mapping into a dict[str, Any]:
    - keys are coerced to str
    - bytes values are decoded to str
    - all other values are preserved as-is
    """
    out: Dict[str, Any] = {}
    for k, v in (data or {}).items():
        out[_to_str(k)] = _to_str(v) if isinstance(v, (bytes, bytearray)) else v
    return out


def normalize_redis_str_mapping(data: Mapping[Any, Any]) -> Dict[str, str]:
    """
    Normalize a Redis-returned mapping into a dict[str, str] by coercing
    both keys and values to strings.
    """
    out: Dict[str, str] = {}
    for k, v in (data or {}).items():
        out[_to_str(k)] = _to_str(v)
    return out


__all__ = ["normalize_redis_mapping", "normalize_redis_str_mapping"]


