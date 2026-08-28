"""
Motet - Clipboard binary format helpers (runtime)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-04-01

Description:
    Runtime-local image / binary MIME detection for clipboard payloads. This mirrors the SDK helper
    used by host-side CLI utilities so runtime containers do not require `motet_sdk` to be importable.

Dependencies:
    - stdlib only

Usage:
    from motet.core.tools.builtin.clipboard_formats import sniff_image_mime
    mime = sniff_image_mime(blob)

Notes:
    - Keep logic aligned with `motet_sdk.cli.clipboard_formats.sniff_image_mime`.
"""

from __future__ import annotations


def sniff_image_mime(data: bytes) -> str | None:
    """Return a MIME type if ``data`` looks like a common raster image, else None."""
    if len(data) < 8:
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if len(data) >= 2 and data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if len(data) >= 6 and data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


__all__ = ["sniff_image_mime"]
