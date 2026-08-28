"""
Motet - Clipboard bridge HTTP client (container -> host)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-03-31

Description:
    When MOTET_CLIPBOARD_BRIDGE_URL and MOTET_CLIPBOARD_BRIDGE_TOKEN are set (by motet-cli
    device start), clipboard tools use this loopback bridge instead of in-container pyclip.

Dependencies:
    - stdlib json, urllib

Notes:
    - Uses stdlib only so the worker does not require httpx for clipboard.
    - GET supports ``mode=text|binary|auto``; PUT may send JSON for ``binary_base64``.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

from ..protocol import err


def _base_url() -> str:
    return (os.getenv("MOTET_CLIPBOARD_BRIDGE_URL") or "").strip().rstrip("/")


def _token() -> str:
    return (os.getenv("MOTET_CLIPBOARD_BRIDGE_TOKEN") or "").strip()


def read_via_bridge(mode: str = "text") -> Optional[Dict[str, Any]]:
    """
    If bridge env is configured, return the tool result dict or an err-shaped dict.
    If not configured, return None so caller can fall back to pyclip.

    Args:
        mode: ``text`` (UTF-8 string), ``binary`` (base64 + mime sniff), or ``auto``
            (prefer non-empty text, else binary).
    """
    base = _base_url()
    if not base:
        return None
    tok = _token()
    if not tok:
        return err(
            "MOTET_CLIPBOARD_BRIDGE_URL is set but MOTET_CLIPBOARD_BRIDGE_TOKEN is missing"
        )

    m = (mode or "text").strip().lower()
    if m not in ("text", "binary", "auto"):
        m = "text"
    url = f"{base}/?mode={urllib.parse.quote(m, safe='')}"
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {tok}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:500]
        return err(f"clipboard bridge HTTP {e.code}: {body}")
    except urllib.error.URLError as e:
        return err(f"clipboard bridge connection failed: {e}")
    except Exception as exc:
        return err(str(exc))

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return err("clipboard bridge returned non-JSON body")

    if not isinstance(data, dict):
        return err("clipboard bridge returned invalid payload")
    if data.get("kind") == "binary":
        return {
            "kind": "binary",
            "base64": str(data.get("base64") or ""),
            "byte_length": int(data.get("byte_length", 0)),
            "mime": data.get("mime"),
            "truncated": bool(data.get("truncated", False)),
        }
    text = data.get("text", "")
    if text is None:
        text = ""
    if not isinstance(text, str):
        text = str(text)
    return {
        "text": text,
        "chars": int(data.get("chars", len(text))),
        "truncated": bool(data.get("truncated", False)),
    }


def write_via_bridge(
    *,
    text: Optional[str] = None,
    binary_base64: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """PUT to bridge. Pass exactly one of ``text`` or ``binary_base64``. None = bridge disabled."""
    base = _base_url()
    if not base:
        return None
    tok = _token()
    if not tok:
        return err(
            "MOTET_CLIPBOARD_BRIDGE_URL is set but MOTET_CLIPBOARD_BRIDGE_TOKEN is missing"
        )

    url = f"{base}/"
    if binary_base64 is not None:
        body_obj: Dict[str, Any] = {"binary_base64": binary_base64}
        body = json.dumps(body_obj, ensure_ascii=False).encode("utf-8")
        ctype = "application/json; charset=utf-8"
    elif text is not None:
        body = text.encode("utf-8")
        ctype = "text/plain; charset=utf-8"
    else:
        return err("write_via_bridge requires text or binary_base64")

    req = urllib.request.Request(url, data=body, method="PUT")
    req.add_header("Authorization", f"Bearer {tok}")
    req.add_header("Content-Type", ctype)
    req.add_header("Content-Length", str(len(body)))
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8", errors="replace")[:500]
        return err(f"clipboard bridge HTTP {e.code}: {msg}")
    except urllib.error.URLError as e:
        return err(f"clipboard bridge connection failed: {e}")
    except Exception as exc:
        return err(str(exc))

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return err("clipboard bridge returned non-JSON body")

    if not isinstance(data, dict):
        return err("clipboard bridge returned invalid payload")
    if "byte_length" in data:
        out: Dict[str, Any] = {"byte_length": int(data.get("byte_length", 0))}
        if data.get("mime") is not None:
            out["mime"] = data.get("mime")
        return out
    return {"chars": int(data.get("chars", len(text or "")))}


__all__ = ["read_via_bridge", "write_via_bridge"]
