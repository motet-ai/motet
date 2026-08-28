"""Tests for clipboard HTTP bridge client (worker side)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from motet.core.tools.builtin.clipboard_bridge_client import read_via_bridge, write_via_bridge


def test_read_via_bridge_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MOTET_CLIPBOARD_BRIDGE_URL", raising=False)
    assert read_via_bridge() is None


def test_read_via_bridge_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOTET_CLIPBOARD_BRIDGE_URL", "http://host.docker.internal:9999")
    monkeypatch.setenv("MOTET_CLIPBOARD_BRIDGE_TOKEN", "secret")
    body = json.dumps(
        {"text": "hello", "chars": 5, "truncated": False}, ensure_ascii=False
    ).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.read.return_value = body
    ctx = MagicMock()
    ctx.__enter__.return_value = mock_resp
    ctx.__exit__.return_value = None
    with patch(
        "motet.core.tools.builtin.clipboard_bridge_client.urllib.request.urlopen",
        return_value=ctx,
    ):
        r = read_via_bridge()
    assert r == {"text": "hello", "chars": 5, "truncated": False}


def test_write_via_bridge_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOTET_CLIPBOARD_BRIDGE_URL", "http://host.docker.internal:8888")
    monkeypatch.setenv("MOTET_CLIPBOARD_BRIDGE_TOKEN", "t2")
    body = json.dumps({"chars": 3}).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.read.return_value = body
    ctx = MagicMock()
    ctx.__enter__.return_value = mock_resp
    ctx.__exit__.return_value = None
    with patch(
        "motet.core.tools.builtin.clipboard_bridge_client.urllib.request.urlopen",
        return_value=ctx,
    ):
        r = write_via_bridge(text="abc")
    assert r == {"chars": 3}


def test_read_via_bridge_binary_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOTET_CLIPBOARD_BRIDGE_URL", "http://host.docker.internal:9999")
    monkeypatch.setenv("MOTET_CLIPBOARD_BRIDGE_TOKEN", "secret")
    body = json.dumps(
        {
            "kind": "binary",
            "base64": "QQ==",
            "byte_length": 1,
            "mime": None,
            "truncated": False,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.read.return_value = body
    ctx = MagicMock()
    ctx.__enter__.return_value = mock_resp
    ctx.__exit__.return_value = None
    with patch(
        "motet.core.tools.builtin.clipboard_bridge_client.urllib.request.urlopen",
        return_value=ctx,
    ) as mock_open:
        r = read_via_bridge("binary")
    assert r == {
        "kind": "binary",
        "base64": "QQ==",
        "byte_length": 1,
        "mime": None,
        "truncated": False,
    }
    req = mock_open.call_args[0][0]
    assert "mode=binary" in req.full_url


def test_write_via_bridge_binary_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOTET_CLIPBOARD_BRIDGE_URL", "http://host.docker.internal:8888")
    monkeypatch.setenv("MOTET_CLIPBOARD_BRIDGE_TOKEN", "t2")
    body = json.dumps({"byte_length": 5, "mime": "image/png"}).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.read.return_value = body
    ctx = MagicMock()
    ctx.__enter__.return_value = mock_resp
    ctx.__exit__.return_value = None
    with patch(
        "motet.core.tools.builtin.clipboard_bridge_client.urllib.request.urlopen",
        return_value=ctx,
    ) as mock_open:
        r = write_via_bridge(binary_base64="aGVsbG8=")
    assert r == {"byte_length": 5, "mime": "image/png"}
    req = mock_open.call_args[0][0]
    assert req.get_header("Content-type").startswith("application/json")
