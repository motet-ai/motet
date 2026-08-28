"""Unit tests for core.clipboard_read / core.clipboard_write."""

from __future__ import annotations

import base64
from types import SimpleNamespace
from unittest.mock import patch

from motet.core.commands.capabilities import WorkerCapability
from motet.core.commands.builtin.tool import _infer_tool_capabilities
from motet.core.commands.command_data_classes import ToolExecutionData
from motet.core.tools.builtin.clipboard_read import run as run_read
from motet.core.tools.builtin.clipboard_write import run as run_write


def test_clipboard_read_run() -> None:
    with patch("pyclip.paste", return_value="abc"):
        r = run_read({})
    assert r.get("text") == "abc"
    assert r.get("chars") == 3
    assert not r.get("truncated")


def test_clipboard_read_binary_mode() -> None:
    png_stub = b"\x89PNG\r\n\x1a\n\x00\x00"
    with patch("pyclip.paste", side_effect=[png_stub]):
        r = run_read({"mode": "binary"})
    assert r.get("kind") == "binary"
    assert r.get("byte_length") == len(png_stub)
    assert r.get("base64") == base64.b64encode(png_stub).decode("ascii")
    assert r.get("mime") == "image/png"


def test_clipboard_read_auto_prefers_text() -> None:
    with patch("pyclip.paste", return_value="hello"):
        r = run_read({"mode": "auto"})
    assert r.get("text") == "hello"


def test_clipboard_read_auto_falls_back_binary() -> None:
    png_stub = b"\x89PNG\r\n\x1a\n"
    with patch("pyclip.paste", side_effect=["", png_stub]):
        r = run_read({"mode": "auto"})
    assert r.get("kind") == "binary"
    assert r.get("mime") == "image/png"


def test_clipboard_read_binary_materializes_artifact_and_trims_inline(
    monkeypatch,
) -> None:
    png_stub = b"\x89PNG\r\n\x1a\n\x00\x01"
    fake_store = SimpleNamespace()
    fake_store.put = lambda **_: "art-123"  # type: ignore[assignment]
    fake_motet = SimpleNamespace(
        tenant_id="tenant-1",
        principal_id="user-1",
        motet_id="motet-1",
        conversation_id="conv-1",
        task_id="task-1",
    )
    monkeypatch.setenv("MOTET_CLIPBOARD_BINARY_INLINE_MAX_BYTES", "1")
    with (
        patch("pyclip.paste", return_value=png_stub),
        patch(
            "motet.core.tools.builtin.clipboard_read._get_motet_context_optional",
            return_value=fake_motet,
        ),
        patch(
            "motet.core.tools.builtin.clipboard_read.get_artifact_store",
            return_value=fake_store,
        ),
    ):
        r = run_read({"mode": "binary"})
    assert r.get("kind") == "binary"
    assert r.get("artifact_id") == "art-123"
    assert r.get("inline_base64") is False
    assert "base64" not in r


def test_clipboard_write_run() -> None:
    with patch("pyclip.copy") as cp:
        r = run_write({"text": "hello"})
    assert "error" not in r
    assert r.get("chars") == 5
    cp.assert_called_once_with("hello")


def test_clipboard_write_binary_run() -> None:
    raw = b"\x89PNG\r\n\x1a\n\x00"
    b64 = base64.b64encode(raw).decode("ascii")
    with patch("pyclip.copy") as cp:
        r = run_write({"binary_base64": b64})
    assert "error" not in r
    assert r.get("byte_length") == len(raw)
    assert r.get("mime") == "image/png"
    cp.assert_called_once_with(raw)


def test_clipboard_write_requires_payload() -> None:
    r = run_write({})
    assert "error" in r


def test_clipboard_write_rejects_both_text_and_binary() -> None:
    r = run_write({"text": "a", "binary_base64": "QQ=="})
    assert "error" in r


def test_clipboard_tools_infer_edge_capabilities() -> None:
    read_caps = _infer_tool_capabilities(
        ToolExecutionData(tool_name="core.clipboard_read", parameters={})
    )
    assert WorkerCapability.EDGE_CLIPBOARD in read_caps
    assert WorkerCapability.EDGE_EXECUTION in read_caps
    assert WorkerCapability.TOOL_EXECUTION in read_caps

    write_caps = _infer_tool_capabilities(
        ToolExecutionData(tool_name="core.clipboard_write", parameters={"text": "x"})
    )
    assert write_caps == read_caps
