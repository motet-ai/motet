"""Tests for env passed to Docker MCP container create."""

from motet.core.tools.mcp_motet.proxy.mcp_docker_stdio import _container_env_for_docker_create


def test_container_env_omits_path() -> None:
    out = _container_env_for_docker_create({"PATH": "/usr/bin", "FOO": "1"})
    assert out == {"FOO": "1"}


def test_container_env_empty_ok() -> None:
    assert _container_env_for_docker_create({}) == {}
    assert _container_env_for_docker_create(None) == {}


