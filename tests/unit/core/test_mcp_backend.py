"""Tests for MOTET_MCP_EXEC_BACKEND resolution."""

from __future__ import annotations

import pytest

from motet.core.execution.mcp_backend import mcp_exec_backend, mcp_exec_uses_docker


def test_mcp_explicit_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MOTET_MCP_EXEC_BACKEND", raising=False)
    monkeypatch.delenv("MOTET_EXEC_BACKEND", raising=False)
    monkeypatch.setenv("MOTET_MCP_EXEC_BACKEND", "subprocess")
    monkeypatch.setenv("MOTET_EXEC_BACKEND", "docker")
    assert mcp_exec_backend() == "subprocess"
    assert not mcp_exec_uses_docker()


def test_mcp_explicit_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOTET_MCP_EXEC_BACKEND", "docker")
    monkeypatch.setenv("MOTET_EXEC_BACKEND", "subprocess")
    assert mcp_exec_backend() == "docker"
    assert mcp_exec_uses_docker()


def test_mcp_inherits_worker_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MOTET_MCP_EXEC_BACKEND", raising=False)
    monkeypatch.setenv("MOTET_EXEC_BACKEND", "docker")
    assert mcp_exec_backend() == "docker"


def test_mcp_inherits_kata_fc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MOTET_MCP_EXEC_BACKEND", raising=False)
    monkeypatch.setenv("MOTET_EXEC_BACKEND", "kata-fc")
    assert mcp_exec_backend() == "docker"
    assert mcp_exec_uses_docker()


def test_mcp_default_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MOTET_MCP_EXEC_BACKEND", raising=False)
    monkeypatch.delenv("MOTET_EXEC_BACKEND", raising=False)
    assert mcp_exec_backend() == "subprocess"
