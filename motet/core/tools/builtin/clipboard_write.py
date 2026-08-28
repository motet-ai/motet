"""
Motet - Clipboard Write Tool

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-04-01

Description:
    Write text or binary (e.g. images) to the system clipboard via pyclip. Routed only to local
    edge workers (required capabilities: tool_execution, edge_execution, edge_clipboard).

Dependencies:
    - pyclip
    - clipboard MIME sniff helper (SDK import with runtime fallback)
    - Tool registry and protocol (err)

Usage:
    result = run({"text": "hello"})
    result = run({"binary_base64": "..."})  # PNG/JPEG etc.

Notes:
    - Text size capped by MOTET_CLIPBOARD_MAX_CHARS (default 1_048_576).
    - Decoded binary capped by MOTET_CLIPBOARD_MAX_BINARY_BYTES (default 8_388_608).
"""

from __future__ import annotations

import base64
import binascii
import os
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field, model_validator

from ..protocol import err
from ..registry import ToolRegistry
from .clipboard_bridge_client import write_via_bridge
from .clipboard_formats import sniff_image_mime

try:  # Prefer SDK helper when available (host/dev), fallback keeps runtime containers decoupled.
    from motet_sdk.cli.clipboard_formats import sniff_image_mime as _sdk_sniff_image_mime

    sniff_image_mime = _sdk_sniff_image_mime
except ModuleNotFoundError:
    pass


class ParamsWrite(BaseModel):
    text: Optional[str] = Field(
        None,
        description="UTF-8 plain text to place on the clipboard (mutually exclusive with binary_base64).",
    )
    binary_base64: Optional[str] = Field(
        None,
        description=(
            "Base64-encoded bytes for non-text clipboard content (e.g. image/png). "
            "Mutually exclusive with text."
        ),
    )

    @model_validator(mode="after")
    def _one_payload(self) -> ParamsWrite:
        if self.text is not None and self.binary_base64 is not None:
            raise ValueError("Provide only one of text or binary_base64")
        if self.text is None and self.binary_base64 is None:
            raise ValueError("Provide text or binary_base64")
        return self


def _max_chars() -> int:
    return int(os.getenv("MOTET_CLIPBOARD_MAX_CHARS") or 1_048_576)


def _max_binary_bytes() -> int:
    return int(os.getenv("MOTET_CLIPBOARD_MAX_BINARY_BYTES") or 8_388_608)


def _fmt(res: Dict[str, Any]) -> str:
    if "error" in res:
        return f"clipboard_write(error={res['error']})"
    if "byte_length" in res:
        return (
            f"clipboard_write(ok, byte_length={res.get('byte_length')}, "
            f"mime={res.get('mime')!r})"
        )
    return f"clipboard_write(ok, chars={res.get('chars')})"


def run(params: Dict[str, Any]) -> Dict[str, Any]:
    """Synchronous clipboard write (gevent/eventlet compatible)."""
    text = params.get("text")
    b64 = params.get("binary_base64")

    if text is not None and b64 is not None:
        return err("provide only one of text or binary_base64")
    if text is None and not b64:
        return err("text or binary_base64 is required")
    if text is not None and not isinstance(text, str):
        return err("text must be a string")
    if b64 is not None and not isinstance(b64, str):
        return err("binary_base64 must be a string")

    if b64 is not None:
        try:
            raw = base64.b64decode(b64, validate=True)
        except binascii.Error:
            return err("invalid base64 in binary_base64")
        cap_b = _max_binary_bytes()
        if len(raw) > cap_b:
            return err(f"decoded binary too long: {len(raw)} > {cap_b}")

        bridged = write_via_bridge(binary_base64=b64)
        if bridged is not None:
            return bridged

        try:
            import pyclip  # type: ignore[import-untyped]
        except ImportError as e:
            return err(f"pyclip not available: {e}")
        try:
            pyclip.copy(raw)
            return {"byte_length": len(raw), "mime": sniff_image_mime(raw)}
        except Exception as exc:
            return err(str(exc))

    cap = _max_chars()
    assert text is not None
    if len(text) > cap:
        return err(f"text too long: {len(text)} > {cap}")

    bridged = write_via_bridge(text=text)
    if bridged is not None:
        return bridged

    try:
        import pyclip  # type: ignore[import-untyped]
    except ImportError as e:
        return err(f"pyclip not available: {e}")
    try:
        pyclip.copy(text)
        return {"chars": len(text)}
    except Exception as exc:
        return err(str(exc))


def register(registry: ToolRegistry) -> None:
    registry.register(
        name="core.clipboard_write",
        description=(
            "Write to the system clipboard (local edge worker): either UTF-8 ``text`` or "
            "``binary_base64`` (e.g. PNG/JPEG bytes). When MOTET_CLIPBOARD_BRIDGE_URL is set, "
            "uses the host bridge; else pyclip in-process."
        ),
        func=run,
        tool_schema=ParamsWrite,
        triggers=[],
        priority=5,
        estimate_tokens=lambda _: 15,
        parse_params=None,
        observation_formatter=_fmt,
        category="clipboard",
        required_capabilities=["TOOL_EXECUTION", "EDGE_EXECUTION", "EDGE_CLIPBOARD"],
    )


__all__ = ["register", "run"]
