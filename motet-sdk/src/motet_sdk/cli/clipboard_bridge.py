"""
Motet - Host clipboard bridge for Docker local workers

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-04-01

Description:
    Loopback HTTP server (127.0.0.1) that reads/writes the host clipboard.
    Uses a provider chain for robust cross-platform behavior:
    native platform providers first (for binary clipboard reliability), then pyclip fallback.
    Started by ``motet-cli device start`` on the host; the worker container calls it using
    MOTET_CLIPBOARD_BRIDGE_URL (e.g. http://host.docker.internal:PORT).

Dependencies:
    - stdlib: http.server, os, signal, base64, subprocess, platform, urllib.parse
    - pyclip (fallback provider in host Python env)
    - macOS optional: PyObjC/AppKit for native pasteboard access
    - macOS optional: pngpaste CLI for binary image reads

Usage:
    MOTET_CLIPBOARD_BRIDGE_TOKEN=secret python -m motet_sdk.cli.clipboard_bridge

Notes:
    - First line of stdout is the bound TCP port (for the parent process).
    - GET ``/?mode=text|binary|auto`` reads text, raw bytes (base64 + image sniff), or text-then-binary.
    - GET ``/capabilities`` returns provider availability and read/write support details.
    - PUT ``text/plain`` body or ``application/json`` with ``text`` or ``binary_base64`` (images etc.).
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

from motet_sdk.cli.clipboard_formats import sniff_image_mime

_TOKEN = ""
_MAX_CHARS = 1_048_576
_MAX_BINARY = 8_388_608
_SUBPROCESS_TIMEOUT_SECONDS = 2.0


def _max_chars() -> int:
    return int(os.getenv("MOTET_CLIPBOARD_MAX_CHARS") or _MAX_CHARS)


def _max_binary_bytes() -> int:
    return int(os.getenv("MOTET_CLIPBOARD_MAX_BINARY_BYTES") or _MAX_BINARY)


def _sniff_binary_mime(data: bytes) -> str | None:
    """Detect common image MIME types; includes TIFF/BMP in addition to shared helper."""
    known = sniff_image_mime(data)
    if known:
        return known
    if len(data) >= 4 and data[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    if len(data) >= 2 and data[:2] == b"BM":
        return "image/bmp"
    return None


@dataclass(frozen=True)
class ClipboardProvider:
    """Provider implementation for platform clipboard operations."""

    name: str
    read_text: bool
    read_binary: bool
    write_text: bool
    write_binary: bool


def _platform_name() -> str:
    return platform.system().strip().lower()


def _run_command(cmd: list[str]) -> bytes:
    proc = subprocess.run(
        cmd,
        capture_output=True,
        timeout=_SUBPROCESS_TIMEOUT_SECONDS,
        check=False,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()[:200]
        raise RuntimeError(
            f"{cmd[0]} exited {proc.returncode}" + (f": {stderr}" if stderr else "")
        )
    return proc.stdout


def _build_provider_chain() -> list[ClipboardProvider]:
    plat = _platform_name()
    providers: list[ClipboardProvider] = []
    if plat == "darwin":
        providers.append(
            ClipboardProvider(
                name="macos_appkit",
                read_text=True,
                read_binary=True,
                write_text=True,
                write_binary=True,
            )
        )
        providers.append(
            ClipboardProvider(
                name="macos_pngpaste",
                read_text=False,
                read_binary=True,
                write_text=False,
                write_binary=False,
            )
        )
    providers.append(
        ClipboardProvider(
            name="pyclip",
            read_text=True,
            read_binary=True,
            write_text=True,
            write_binary=True,
        )
    )
    return providers


def _macos_appkit_read_text() -> str:
    from AppKit import NSPasteboard, NSPasteboardTypeString  # type: ignore[import-untyped]

    pb = NSPasteboard.generalPasteboard()
    text = pb.stringForType_(NSPasteboardTypeString)
    if text is None:
        return ""
    return str(text)


def _macos_appkit_read_binary() -> tuple[bytes, str | None]:
    from AppKit import NSPasteboard  # type: ignore[import-untyped]

    pb = NSPasteboard.generalPasteboard()
    preferred_types = (
        ("public.png", "image/png"),
        ("public.jpeg", "image/jpeg"),
        ("com.compuserve.gif", "image/gif"),
        ("org.webmproject.webp", "image/webp"),
        ("public.tiff", "image/tiff"),
        ("com.microsoft.bmp", "image/bmp"),
    )
    for pb_type, hint_mime in preferred_types:
        data_obj = pb.dataForType_(pb_type)
        if data_obj is None:
            continue
        raw = bytes(data_obj)
        if not raw:
            continue
        return raw, (_sniff_binary_mime(raw) or hint_mime)
    return b"", None


def _macos_appkit_write_text(text: str) -> None:
    from AppKit import NSPasteboard, NSPasteboardTypeString  # type: ignore[import-untyped]

    pb = NSPasteboard.generalPasteboard()
    pb.clearContents()
    ok = pb.setString_forType_(text, NSPasteboardTypeString)
    if not ok:
        raise RuntimeError("NSPasteboard setString_forType failed")


def _macos_appkit_write_binary(raw: bytes) -> str | None:
    from AppKit import NSPasteboard  # type: ignore[import-untyped]
    from Foundation import NSData  # type: ignore[import-untyped]

    mime = _sniff_binary_mime(raw)
    pb_type = {
        "image/png": "public.png",
        "image/jpeg": "public.jpeg",
        "image/gif": "com.compuserve.gif",
        "image/webp": "org.webmproject.webp",
        "image/tiff": "public.tiff",
        "image/bmp": "com.microsoft.bmp",
    }.get(mime or "", "public.data")
    pb = NSPasteboard.generalPasteboard()
    pb.clearContents()
    ns_data = NSData.dataWithBytes_length_(raw, len(raw))
    ok = pb.setData_forType_(ns_data, pb_type)
    if not ok:
        raise RuntimeError("NSPasteboard setData_forType failed")
    return mime


def _macos_pngpaste_read_binary() -> tuple[bytes, str | None]:
    out = _run_command(["pngpaste", "-"])
    return out, (_sniff_binary_mime(out) or "image/png")


def _pyclip_read_text() -> str:
    import pyclip  # type: ignore[import-untyped]

    text = pyclip.paste(text=True)
    if text is None:
        return ""
    if not isinstance(text, str):
        return str(text)
    return text


def _pyclip_read_binary() -> tuple[bytes, str | None]:
    import pyclip  # type: ignore[import-untyped]

    raw = _normalize_paste_bytes(pyclip.paste())
    return raw, _sniff_binary_mime(raw)


def _pyclip_write_text(text: str) -> None:
    import pyclip  # type: ignore[import-untyped]

    pyclip.copy(text)


def _pyclip_write_binary(raw: bytes) -> str | None:
    import pyclip  # type: ignore[import-untyped]

    pyclip.copy(raw)
    return _sniff_binary_mime(raw)


def _provider_capabilities() -> dict[str, Any]:
    providers = []
    for p in _build_provider_chain():
        available = _provider_available(p.name)
        providers.append(
            {
                "name": p.name,
                "available": available,
                "read_text": p.read_text,
                "read_binary": p.read_binary,
                "write_text": p.write_text,
                "write_binary": p.write_binary,
            }
        )
    return {
        "platform": _platform_name(),
        "max_chars": _max_chars(),
        "max_binary_bytes": _max_binary_bytes(),
        "providers": providers,
    }


def _provider_available(provider_name: str) -> bool:
    try:
        if provider_name == "macos_appkit":
            if _platform_name() != "darwin":
                return False
            import AppKit  # type: ignore[import-untyped]  # noqa: F401

            return True
        if provider_name == "macos_pngpaste":
            if _platform_name() != "darwin":
                return False
            return shutil.which("pngpaste") is not None
        if provider_name == "pyclip":
            import pyclip  # type: ignore[import-untyped]  # noqa: F401

            return True
        return False
    except Exception:
        return False


def _read_text_with_providers() -> tuple[str, str]:
    errors: list[str] = []
    for p in _build_provider_chain():
        if not p.read_text or not _provider_available(p.name):
            continue
        try:
            if p.name == "macos_appkit":
                return _macos_appkit_read_text(), p.name
            if p.name == "pyclip":
                return _pyclip_read_text(), p.name
        except Exception as exc:
            errors.append(f"{p.name}: {exc}")
    if errors:
        raise RuntimeError("; ".join(errors))
    raise RuntimeError("no available text clipboard provider")


def _read_binary_with_providers() -> tuple[bytes, str | None, str]:
    errors: list[str] = []
    for p in _build_provider_chain():
        if not p.read_binary or not _provider_available(p.name):
            continue
        try:
            if p.name == "macos_appkit":
                raw, mime = _macos_appkit_read_binary()
                if not raw:
                    continue
                return raw, mime, p.name
            if p.name == "macos_pngpaste":
                raw, mime = _macos_pngpaste_read_binary()
                if not raw:
                    continue
                return raw, mime, p.name
            if p.name == "pyclip":
                raw, mime = _pyclip_read_binary()
                if not raw:
                    continue
                return raw, mime, p.name
        except Exception as exc:
            errors.append(f"{p.name}: {exc}")
    if errors:
        raise RuntimeError("; ".join(errors))
    raise RuntimeError("no available binary clipboard provider")


def _write_text_with_providers(text: str) -> str:
    errors: list[str] = []
    for p in _build_provider_chain():
        if not p.write_text or not _provider_available(p.name):
            continue
        try:
            if p.name == "macos_appkit":
                _macos_appkit_write_text(text)
                return p.name
            if p.name == "pyclip":
                _pyclip_write_text(text)
                return p.name
        except Exception as exc:
            errors.append(f"{p.name}: {exc}")
    if errors:
        raise RuntimeError("; ".join(errors))
    raise RuntimeError("no available clipboard write-text provider")


def _write_binary_with_providers(raw: bytes) -> tuple[str | None, str]:
    errors: list[str] = []
    for p in _build_provider_chain():
        if not p.write_binary or not _provider_available(p.name):
            continue
        try:
            if p.name == "macos_appkit":
                return _macos_appkit_write_binary(raw), p.name
            if p.name == "pyclip":
                return _pyclip_write_binary(raw), p.name
        except Exception as exc:
            errors.append(f"{p.name}: {exc}")
    if errors:
        raise RuntimeError("; ".join(errors))
    raise RuntimeError("no available clipboard write-binary provider")


class ClipboardBridgeHandler(BaseHTTPRequestHandler):
    """Minimal authenticated clipboard REST handler."""

    server_version = "MotetClipboardBridge/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        """Disable stderr access logging (avoid leaking paths)."""
        return

    def _unauthorized(self) -> None:
        self.send_response(401)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"unauthorized")

    def _bad(self, code: int, msg: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(msg.encode("utf-8"))

    def _auth_ok(self) -> bool:
        global _TOKEN
        expected = f"Bearer {_TOKEN}".strip()
        auth = (self.headers.get("Authorization") or "").strip()
        if auth == expected:
            return True
        # Alternate header for clients that struggle with Bearer on GET
        alt = (self.headers.get("X-Motet-Clipboard-Token") or "").strip()
        return bool(_TOKEN) and alt == _TOKEN

    def _read_mode(self) -> str:
        parsed = urlparse(self.path)
        if parsed.path.rstrip("/") not in ("", "/"):
            return "__bad_path__"
        qs = parse_qs(parsed.query)
        raw = (qs.get("mode") or ["text"])[0].strip().lower()
        if raw in ("text", "binary", "auto"):
            return raw
        return "text"

    def do_GET(self) -> None:
        if not self._auth_ok():
            self._unauthorized()
            return
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path == "/capabilities":
            body = json.dumps(_provider_capabilities(), ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        mode = self._read_mode()
        if mode == "__bad_path__":
            self._bad(404, "not found")
            return
        try:
            payload = _clipboard_get_payload(mode)
        except Exception as e:
            self._bad(500, str(e))
            return
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_write_body(self) -> None:
        if not self._auth_ok():
            self._unauthorized()
            return
        length_raw = self.headers.get("Content-Length")
        if length_raw is None:
            self._bad(411, "Content-Length required")
            return
        try:
            length = int(length_raw)
        except ValueError:
            self._bad(400, "invalid Content-Length")
            return
        max_body = max(_max_chars(), _max_binary_bytes() * 2)
        if length > max_body:
            self._bad(413, f"body too large: {length} > {max_body}")
            return
        data = self.rfile.read(length)
        ctype = (self.headers.get("Content-Type") or "text/plain").split(";")[0].strip().lower()
        try:
            meta = _clipboard_put_payload(ctype, data)
        except ValueError as ve:
            self._bad(400, str(ve))
            return
        except Exception as e:
            self._bad(500, str(e))
            return
        out = json.dumps(meta, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_PUT(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.rstrip("/") not in ("", "/"):
            self._bad(404, "not found")
            return
        self._read_write_body()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.rstrip("/") not in ("", "/"):
            self._bad(404, "not found")
            return
        self._read_write_body()


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


def _clipboard_get_payload(mode: str) -> dict[str, Any]:
    cap_text = _max_chars()
    cap_bin = _max_binary_bytes()
    if mode == "text":
        text, provider = _read_text_with_providers()
        truncated = len(text) > cap_text
        out = text[:cap_text]
        return {"text": out, "chars": len(out), "truncated": truncated, "provider": provider}

    if mode == "binary":
        raw, mime, provider = _read_binary_with_providers()
        truncated = len(raw) > cap_bin
        chunk = raw[:cap_bin]
        b64 = base64.b64encode(chunk).decode("ascii")
        return {
            "kind": "binary",
            "base64": b64,
            "byte_length": len(chunk),
            "mime": mime or _sniff_binary_mime(chunk),
            "truncated": truncated,
            "provider": provider,
        }

    # auto
    text, text_provider = _read_text_with_providers()
    if text.strip():
        truncated = len(text) > cap_text
        out = text[:cap_text]
        return {
            "text": out,
            "chars": len(out),
            "truncated": truncated,
            "provider": text_provider,
        }

    raw, mime, provider = _read_binary_with_providers()
    truncated = len(raw) > cap_bin
    chunk = raw[:cap_bin]
    b64 = base64.b64encode(chunk).decode("ascii")
    return {
        "kind": "binary",
        "base64": b64,
        "byte_length": len(chunk),
        "mime": mime or _sniff_binary_mime(chunk),
        "truncated": truncated,
        "provider": provider,
    }


def _clipboard_put_payload(ctype: str, data: bytes) -> dict[str, Any]:
    if ctype == "application/json":
        try:
            obj = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ValueError("invalid JSON body") from e
        if not isinstance(obj, dict):
            raise ValueError("JSON body must be an object")
        b64 = obj.get("binary_base64")
        if b64 is not None:
            if not isinstance(b64, str):
                raise ValueError("binary_base64 must be a string")
            raw = base64.b64decode(b64, validate=True)
            cap = _max_binary_bytes()
            if len(raw) > cap:
                raise ValueError(f"decoded binary too large: {len(raw)} > {cap}")
            mime, provider = _write_binary_with_providers(raw)
            return {"byte_length": len(raw), "mime": mime, "provider": provider}

        text_val = obj.get("text")
        if text_val is None:
            raise ValueError("JSON must include text or binary_base64")
        if not isinstance(text_val, str):
            text_val = str(text_val)
        cap = _max_chars()
        if len(text_val) > cap:
            raise ValueError(f"text too long: {len(text_val)} > {cap}")
        provider = _write_text_with_providers(text_val)
        return {"chars": len(text_val), "provider": provider}

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ValueError("body must be UTF-8 for text/plain") from e
    cap = _max_chars()
    if len(text) > cap:
        raise ValueError(f"text too long: {len(text)} > {cap}")
    provider = _write_text_with_providers(text)
    return {"chars": len(text), "provider": provider}


def main() -> None:
    global _TOKEN
    raw_token = os.getenv("MOTET_CLIPBOARD_BRIDGE_TOKEN", "").strip()
    if not raw_token:
        print("MOTET_CLIPBOARD_BRIDGE_TOKEN missing", file=sys.stderr)
        sys.exit(2)
    _TOKEN = raw_token

    httpd = HTTPServer(("127.0.0.1", 0), ClipboardBridgeHandler)
    port = httpd.server_address[1]
    print(port, flush=True)

    def _stop(*_args: object) -> None:
        httpd.shutdown()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
