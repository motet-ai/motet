"""
Motet - Clipboard Read Tool

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-04-01

Description:
    Read text or binary (e.g. images) from the system clipboard via pyclip. Routed only to
    local edge workers (required capabilities: tool_execution, edge_execution, edge_clipboard).

Dependencies:
    - pyclip
    - clipboard MIME sniff helper (SDK import with runtime fallback)
    - motet.core.artifacts (optional binary artifact materialization)
    - Tool registry and protocol (err)

Usage:
    result = run({})  # default mode=auto
    result = run({"mode": "auto"})  # text if present, else image/binary as base64

Notes:
    - Maximum returned text length capped by MOTET_CLIPBOARD_MAX_CHARS (default 1_048_576).
    - Binary payloads capped by MOTET_CLIPBOARD_MAX_BINARY_BYTES (default 8_388_608).
    - Binary clipboard data can be materialized to artifact storage when execution context
      has tenant/principal identity. This keeps large payloads out of tool message text.
    - When MOTET_CLIPBOARD_BRIDGE_URL is set (motet-cli device start), uses the host bridge
      instead of in-container pyclip.
"""

from __future__ import annotations

import base64
import binascii
import os
from typing import Any, Dict, Literal, Optional, cast

from pydantic import BaseModel, Field

from ...artifacts import ArtifactKind, get_artifact_store
from ..protocol import err
from ..registry import ToolRegistry
from .clipboard_bridge_client import read_via_bridge
from .clipboard_formats import sniff_image_mime

try:  # Prefer SDK helper when available (host/dev), fallback keeps runtime containers decoupled.
    from motet_sdk.cli.clipboard_formats import sniff_image_mime as _sdk_sniff_image_mime

    sniff_image_mime = _sdk_sniff_image_mime
except ModuleNotFoundError:
    pass


class ParamsRead(BaseModel):
    """Read system clipboard (local worker only)."""

    model_config = {"extra": "forbid"}

    mode: Literal["text", "binary", "auto"] = Field(
        default="auto",
        description=(
            "text: UTF-8 string only. binary: raw clipboard bytes as base64 (images, files). "
            "auto (default): use non-empty text if available, otherwise binary."
        ),
    )


def _max_chars() -> int:
    return int(os.getenv("MOTET_CLIPBOARD_MAX_CHARS") or 1_048_576)


def _max_binary_bytes() -> int:
    return int(os.getenv("MOTET_CLIPBOARD_MAX_BINARY_BYTES") or 8_388_608)


def _binary_inline_max_bytes() -> int:
    return int(os.getenv("MOTET_CLIPBOARD_BINARY_INLINE_MAX_BYTES") or 262_144)


def _artifact_ttl_seconds() -> int:
    return int(os.getenv("MOTET_CLIPBOARD_BINARY_ARTIFACT_TTL_SECONDS") or 3_600)


def _store_binary_artifact_enabled() -> bool:
    val = (os.getenv("MOTET_CLIPBOARD_STORE_BINARY_ARTIFACT") or "1").strip().lower()
    return val not in {"0", "false", "no", "off"}


def _get_motet_context_optional() -> Any:
    """Return current MotetContext if available (when tool runs inside tool_execution)."""
    try:
        from motet.core.commands.decorator import get_motet_context

        return get_motet_context()
    except Exception:
        return None


def _store_binary_as_artifact(binary: bytes, mime: Optional[str], mode: str) -> Optional[str]:
    """
    Persist clipboard binary in artifact store when we have isolation context.

    Returns artifact_id on success; otherwise None (best-effort).
    """
    if not _store_binary_artifact_enabled() or not binary:
        return None
    motet = _get_motet_context_optional()
    if motet is None:
        return None

    tenant_id = getattr(motet, "tenant_id", None)
    principal_id = getattr(motet, "principal_id", None)
    motet_id = getattr(motet, "motet_id", None)
    if not tenant_id or not principal_id:
        return None

    metadata: Dict[str, Any] = {
        "source": "clipboard_read",
        "mode": mode,
        "mime": mime,
        "byte_length": len(binary),
        "conversation_id": getattr(motet, "conversation_id", None),
        "task_id": getattr(motet, "task_id", None),
    }
    metadata = {k: v for k, v in metadata.items() if v is not None}

    try:
        return get_artifact_store().put(
            payload=binary,
            content_type=mime or "application/octet-stream",
            metadata=metadata,
            ttl_seconds=max(30, _artifact_ttl_seconds()),
            kind=ArtifactKind.TOOL_ARTIFACT,
            tenant_id=str(tenant_id),
            principal_id=str(principal_id),
            motet_id=str(motet_id) if motet_id is not None else None,
        )
    except Exception:
        return None


def _postprocess_binary_result(res: Dict[str, Any], mode: str) -> Dict[str, Any]:
    """Optionally materialize binary result as artifact and trim inline base64 by size."""
    if res.get("kind") != "binary":
        return res

    b64 = res.get("base64")
    if not isinstance(b64, str) or not b64:
        return res

    try:
        raw = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        return res

    artifact_id = _store_binary_as_artifact(raw, res.get("mime"), mode=mode)
    inline_limit = max(0, _binary_inline_max_bytes())
    keep_inline = len(raw) <= inline_limit or not artifact_id

    out = dict(res)
    if artifact_id:
        out["artifact_id"] = artifact_id
    if keep_inline:
        out["inline_base64"] = True
    else:
        out.pop("base64", None)
        out["inline_base64"] = False
    return out


def _normalize_paste_bytes(raw: object) -> bytes:
    if raw is None:
        return b""
    if isinstance(raw, bytes):
        return raw
    if isinstance(raw, bytearray):
        return bytes(raw)
    if isinstance(raw, str):
        return raw.encode("utf-8", errors="replace")
    if isinstance(raw, memoryview):
        return raw.tobytes()
    try:
        return bytes(memoryview(cast(Any, raw)))
    except TypeError:
        return str(raw).encode("utf-8", errors="replace")


def _fmt(res: Dict[str, Any]) -> str:
    if "error" in res:
        return f"clipboard_read(error={res['error']})"
    if res.get("kind") == "binary":
        mime = res.get("mime") or "unknown"
        return (
            f"clipboard_read(ok, kind=binary, byte_length={res.get('byte_length')}, "
            f"mime={mime!r}, truncated={res.get('truncated')})"
        )
    t = (res.get("text") or "")[:48]
    return f"clipboard_read(ok, chars={res.get('chars')}, snippet={t!r})"


def _read_local(pyclip: Any, mode: str) -> Dict[str, Any]:
    cap_text = _max_chars()
    cap_bin = _max_binary_bytes()
    if mode == "text":
        text = pyclip.paste(text=True)
        if text is None:
            text = ""
        if not isinstance(text, str):
            text = str(text)
        truncated = len(text) > cap_text
        out = text[:cap_text]
        return {"text": out, "chars": len(out), "truncated": truncated}

    if mode == "binary":
        raw = _normalize_paste_bytes(pyclip.paste())
        truncated = len(raw) > cap_bin
        chunk = raw[:cap_bin]
        b64 = base64.b64encode(chunk).decode("ascii")
        return {
            "kind": "binary",
            "base64": b64,
            "byte_length": len(chunk),
            "mime": sniff_image_mime(chunk),
            "truncated": truncated,
        }

    text = pyclip.paste(text=True)
    if text is None:
        text = ""
    if not isinstance(text, str):
        text = str(text)
    if text.strip():
        truncated = len(text) > cap_text
        out = text[:cap_text]
        return {"text": out, "chars": len(out), "truncated": truncated}

    raw = _normalize_paste_bytes(pyclip.paste())
    truncated = len(raw) > cap_bin
    chunk = raw[:cap_bin]
    b64 = base64.b64encode(chunk).decode("ascii")
    return {
        "kind": "binary",
        "base64": b64,
        "byte_length": len(chunk),
        "mime": sniff_image_mime(chunk),
        "truncated": truncated,
    }


def run(params: Dict[str, Any]) -> Dict[str, Any]:
    """Synchronous clipboard read (gevent/eventlet compatible)."""
    mode_raw = params.get("mode", "auto")
    mode = mode_raw if mode_raw in ("text", "binary", "auto") else "auto"

    bridged = read_via_bridge(mode)
    if bridged is not None:
        return _postprocess_binary_result(bridged, mode)

    try:
        import pyclip  # type: ignore[import-untyped]
    except ImportError as e:
        return err(f"pyclip not available: {e}")
    try:
        return _postprocess_binary_result(_read_local(pyclip, mode), mode)
    except Exception as exc:
        return err(str(exc))


def register(registry: ToolRegistry) -> None:
    registry.register(
        name="core.clipboard_read",
        description=(
            "Read from the system clipboard (local edge worker): UTF-8 text and/or binary "
            "(e.g. copied images). Binary results can include artifact_id and optional inline base64. "
            "Use mode=auto (default, prefer text then binary), mode=text, or mode=binary. "
            "When MOTET_CLIPBOARD_BRIDGE_URL is set (motet-cli device start), "
            "uses the host bridge; otherwise pyclip in the worker (Docker needs the bridge for the host)."
        ),
        func=run,
        tool_schema=ParamsRead,
        triggers=[],
        priority=5,
        estimate_tokens=lambda _: 15,
        parse_params=None,
        observation_formatter=_fmt,
        category="clipboard",
        required_capabilities=["TOOL_EXECUTION", "EDGE_EXECUTION", "EDGE_CLIPBOARD"],
    )


__all__ = ["register", "run"]
