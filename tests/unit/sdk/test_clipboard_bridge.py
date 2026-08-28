"""
Motet - Unit tests for SDK clipboard bridge provider chain

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-04-01

Description:
    Verifies provider metadata, MIME sniffing extensions, and payload shaping for
    the host clipboard bridge provider chain used by local/distributed edge workers.

Dependencies:
    - pytest for assertions and monkeypatch fixtures
    - motet_sdk.cli.clipboard_bridge module under test

Usage:
    pytest tests/unit/sdk/test_clipboard_bridge.py

Notes:
    - Tests monkeypatch provider functions to avoid relying on host clipboard state.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
from pathlib import Path


def _load_clipboard_bridge_module():
    root = Path(__file__).resolve().parents[3]
    sdk_src = root / "motet-sdk" / "src"
    sys.path.insert(0, str(sdk_src))
    module_path = root / "motet-sdk" / "src" / "motet_sdk" / "cli" / "clipboard_bridge.py"
    spec = importlib.util.spec_from_file_location("motet_sdk.cli.clipboard_bridge", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


clipboard_bridge = _load_clipboard_bridge_module()


def test_sniff_binary_mime_tiff() -> None:
    data = b"II*\x00dummy"
    assert clipboard_bridge._sniff_binary_mime(data) == "image/tiff"


def test_provider_capabilities_shapes_output(monkeypatch) -> None:
    providers = [
        clipboard_bridge.ClipboardProvider(
            name="alpha",
            read_text=True,
            read_binary=False,
            write_text=True,
            write_binary=False,
        ),
        clipboard_bridge.ClipboardProvider(
            name="beta",
            read_text=False,
            read_binary=True,
            write_text=False,
            write_binary=True,
        ),
    ]
    monkeypatch.setattr(clipboard_bridge, "_build_provider_chain", lambda: providers)
    monkeypatch.setattr(clipboard_bridge, "_provider_available", lambda name: name == "alpha")
    monkeypatch.setattr(clipboard_bridge, "_platform_name", lambda: "test-os")
    monkeypatch.setenv("MOTET_CLIPBOARD_MAX_CHARS", "10")
    monkeypatch.setenv("MOTET_CLIPBOARD_MAX_BINARY_BYTES", "20")

    out = clipboard_bridge._provider_capabilities()
    assert out["platform"] == "test-os"
    assert out["max_chars"] == 10
    assert out["max_binary_bytes"] == 20
    assert out["providers"][0]["name"] == "alpha"
    assert out["providers"][0]["available"] is True
    assert out["providers"][1]["name"] == "beta"
    assert out["providers"][1]["available"] is False


def test_clipboard_get_payload_text_includes_provider(monkeypatch) -> None:
    monkeypatch.setenv("MOTET_CLIPBOARD_MAX_CHARS", "4")
    monkeypatch.setattr(
        clipboard_bridge,
        "_read_text_with_providers",
        lambda: ("abcdef", "provider_a"),
    )
    out = clipboard_bridge._clipboard_get_payload("text")
    assert out == {
        "text": "abcd",
        "chars": 4,
        "truncated": True,
        "provider": "provider_a",
    }


def test_clipboard_get_payload_binary_includes_provider(monkeypatch) -> None:
    payload = b"\x89PNG\r\n\x1a\n\x00\x00"
    monkeypatch.setenv("MOTET_CLIPBOARD_MAX_BINARY_BYTES", "8")
    monkeypatch.setattr(
        clipboard_bridge,
        "_read_binary_with_providers",
        lambda: (payload, "image/png", "provider_b"),
    )
    out = clipboard_bridge._clipboard_get_payload("binary")
    assert out["kind"] == "binary"
    assert out["provider"] == "provider_b"
    assert out["mime"] == "image/png"
    assert out["byte_length"] == 8
    assert out["truncated"] is True
    assert out["base64"] == base64.b64encode(payload[:8]).decode("ascii")


def test_clipboard_put_payload_binary_uses_provider_chain(monkeypatch) -> None:
    raw = b"\x89PNG\r\n\x1a\n\x00\x00"
    b64 = base64.b64encode(raw).decode("ascii")
    monkeypatch.setattr(
        clipboard_bridge,
        "_write_binary_with_providers",
        lambda data: ("image/png", "provider_c"),
    )
    body = json.dumps({"binary_base64": b64}).encode("utf-8")
    out = clipboard_bridge._clipboard_put_payload("application/json", body)
    assert out == {"byte_length": len(raw), "mime": "image/png", "provider": "provider_c"}
