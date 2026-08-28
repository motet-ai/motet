"""
Motet - HTTP Byte Range Utilities for Artifact Delivery

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Parses HTTP Range headers and normalizes artifact payloads to bytes for
    ranged delivery. Used by artifact download/preview
    endpoints and artifact store get_range implementations.

Dependencies:
    - json for dict payload encoding

Usage:
    start, end = parse_byte_range("bytes=0-499", total_size=1000)
    chunk = payload_bytes[payload_slice(start, end)]
"""

from __future__ import annotations

import json
from typing import Any, Tuple


class ByteRangeError(ValueError):
    """Raised when an HTTP Range header is syntactically or semantically invalid."""


def artifact_payload_to_bytes(payload: Any) -> bytes:
    """Normalize an artifact store payload to raw bytes for ranged slicing."""

    if isinstance(payload, bytes):
        return payload
    if isinstance(payload, str):
        return payload.encode("utf-8")
    if isinstance(payload, dict):
        return json.dumps(payload).encode("utf-8")
    raise TypeError(f"Unsupported artifact payload type: {type(payload).__name__}")


def parse_byte_range(range_header: str, total_size: int) -> Tuple[int, int]:
    """
    Parse an HTTP ``Range: bytes=...`` header.

    Args:
        range_header: Raw Range header value (e.g. ``bytes=0-499``).
        total_size: Total payload size in bytes.

    Returns:
        ``(start, end)`` inclusive byte indices.

    Raises:
        ByteRangeError: On invalid syntax or unsatisfiable range (maps to HTTP 416).
    """

    if total_size < 0:
        raise ByteRangeError("invalid total size")
    if not range_header or not range_header.startswith("bytes="):
        raise ByteRangeError("unsupported range unit")

    spec = range_header[6:].strip()
    if not spec:
        raise ByteRangeError("empty range spec")

    # Browsers send a single range for <video> seek; take the first if multiple.
    if "," in spec:
        spec = spec.split(",", 1)[0].strip()

    if "-" not in spec:
        raise ByteRangeError("malformed range spec")

    start_str, end_str = spec.split("-", 1)

    if start_str == "":
        # Suffix range: bytes=-N (last N bytes)
        if not end_str.isdigit():
            raise ByteRangeError("invalid suffix range")
        suffix_len = int(end_str)
        if suffix_len <= 0:
            raise ByteRangeError("invalid suffix length")
        if total_size == 0:
            raise ByteRangeError("range not satisfiable")
        start = max(0, total_size - suffix_len)
        end = total_size - 1
    elif end_str == "":
        # Open-ended: bytes=N-
        if not start_str.isdigit():
            raise ByteRangeError("invalid open range")
        start = int(start_str)
        if start >= total_size:
            raise ByteRangeError("range not satisfiable")
        end = total_size - 1
    else:
        if not start_str.isdigit() or not end_str.isdigit():
            raise ByteRangeError("invalid closed range")
        start = int(start_str)
        end = int(end_str)
        if start > end:
            raise ByteRangeError("invalid range bounds")
        if start >= total_size:
            raise ByteRangeError("range not satisfiable")
        end = min(end, total_size - 1)

    return start, end


def slice_payload_bytes(data: bytes, start: int, end: int) -> bytes:
    """Return inclusive byte slice ``[start, end]`` from ``data``."""

    return data[start : end + 1]
