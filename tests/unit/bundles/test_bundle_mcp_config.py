"""
Motet - Bundle MCP config enqueue unit tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-13

Description:
    A bundle with ``config/mcp.yaml`` (or ``mcp/mcp.yaml``) must enqueue
    namespaced register/unregister commands to the sibling MCP manager.
    No Redis I/O — enqueue is mocked.

Dependencies:
    - pytest
    - motet.core.bundles.bundle_reload

Usage:
    pytest tests/unit/bundles/test_bundle_mcp_config.py
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Tuple
from unittest.mock import patch

from motet.core.bundles.bundle_reload import (
    _load_bundle_config,
    _merge_mcp_config,
    _unregister_bundle_mcp,
)

def _write_mcp_yaml(bundle_dir: Path, *, rel: str, body: str) -> Path:
    path = bundle_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _services_yaml() -> str:
    return (
        "services:\n"
        "  - service_id: weather\n"
        "    transport: stdio\n"
        "    command: npx\n"
        "    args:\n"
        "      - -y\n"
        "      - \"@timlukahorstmann/mcp-weather\"\n"
    )


def _servers_yaml() -> str:
    return (
        "servers:\n"
        "  docs:\n"
        "    transport: http\n"
        "    start_server: false\n"
        "    base_url: https://mcp.example.com/mcp\n"
    )


def test_merge_services_list_enqueues_namespaced_register(tmp_path: Path) -> None:
    mcp_path = _write_mcp_yaml(tmp_path, rel="config/mcp.yaml", body=_services_yaml())
    captured: List[Tuple[str, Dict[str, Any]]] = []

    def _enqueue(manager_id: str, command: Dict[str, Any]) -> str:
        captured.append((manager_id, command))
        return "1-0"

    with (
        patch(
            "motet.core.tools.mcp_motet.manager.control_commands.resolve_mcp_manager_id",
            return_value="mcp-local-default",
        ),
        patch(
            "motet.core.tools.mcp_motet.manager.control_commands.enqueue_mcp_control_command",
            side_effect=_enqueue,
        ),
    ):
        _merge_mcp_config("demo-bundle", mcp_path)

    assert len(captured) == 1
    manager_id, command = captured[0]
    assert manager_id == "mcp-local-default"
    assert command["op"] == "register"
    assert command["service_id"] == "demo-bundle.weather"
    assert command["config"]["command"] == "npx"
    assert command["config"]["transport"] == "stdio"


def test_merge_servers_dict_enqueues_register(tmp_path: Path) -> None:
    mcp_path = _write_mcp_yaml(tmp_path, rel="config/mcp.yaml", body=_servers_yaml())
    captured: List[Dict[str, Any]] = []

    with (
        patch(
            "motet.core.tools.mcp_motet.manager.control_commands.resolve_mcp_manager_id",
            return_value="mcp-x",
        ),
        patch(
            "motet.core.tools.mcp_motet.manager.control_commands.enqueue_mcp_control_command",
            side_effect=lambda _mid, command: captured.append(command) or "1-0",
        ),
    ):
        _merge_mcp_config("docs-pack", mcp_path)

    assert [c["service_id"] for c in captured] == ["docs-pack.docs"]
    assert captured[0]["op"] == "register"
    assert captured[0]["config"]["base_url"] == "https://mcp.example.com/mcp"


def test_merge_skips_when_manager_id_missing(tmp_path: Path) -> None:
    mcp_path = _write_mcp_yaml(tmp_path, rel="config/mcp.yaml", body=_services_yaml())
    with (
        patch(
            "motet.core.tools.mcp_motet.manager.control_commands.resolve_mcp_manager_id",
            return_value=None,
        ),
        patch(
            "motet.core.tools.mcp_motet.manager.control_commands.enqueue_mcp_control_command",
        ) as enqueue,
    ):
        _merge_mcp_config("demo-bundle", mcp_path)
    enqueue.assert_not_called()


def test_unregister_enqueues_namespaced_ids(tmp_path: Path) -> None:
    _write_mcp_yaml(tmp_path, rel="config/mcp.yaml", body=_services_yaml())
    captured: List[Dict[str, Any]] = []

    with (
        patch(
            "motet.core.tools.mcp_motet.manager.control_commands.resolve_mcp_manager_id",
            return_value="mcp-local-default",
        ),
        patch(
            "motet.core.tools.mcp_motet.manager.control_commands.enqueue_mcp_control_command",
            side_effect=lambda _mid, command: captured.append(command) or "1-0",
        ),
    ):
        _unregister_bundle_mcp("demo-bundle", tmp_path)

    assert captured == [{"op": "unregister", "service_id": "demo-bundle.weather"}]


def test_load_bundle_config_reads_mcp_yaml(tmp_path: Path) -> None:
    _write_mcp_yaml(tmp_path, rel="config/mcp.yaml", body=_services_yaml())
    captured: List[str] = []

    with (
        patch(
            "motet.core.tools.mcp_motet.manager.control_commands.resolve_mcp_manager_id",
            return_value="mcp-local-default",
        ),
        patch(
            "motet.core.tools.mcp_motet.manager.control_commands.enqueue_mcp_control_command",
            side_effect=lambda _mid, command: captured.append(command["service_id"]) or "1-0",
        ),
    ):
        _load_bundle_config("demo-bundle", tmp_path, targeting=None)

    assert captured == ["demo-bundle.weather"]


def test_load_bundle_config_reads_mcp_dir_fallback(tmp_path: Path) -> None:
    _write_mcp_yaml(tmp_path, rel="mcp/mcp.yaml", body=_servers_yaml())
    captured: List[str] = []

    with (
        patch(
            "motet.core.tools.mcp_motet.manager.control_commands.resolve_mcp_manager_id",
            return_value="mcp-local-default",
        ),
        patch(
            "motet.core.tools.mcp_motet.manager.control_commands.enqueue_mcp_control_command",
            side_effect=lambda _mid, command: captured.append(command["op"] + ":" + command["service_id"])
            or "1-0",
        ),
    ):
        _load_bundle_config("alt-pack", tmp_path, targeting=None)
        _unregister_bundle_mcp("alt-pack", tmp_path)

    assert captured == ["register:alt-pack.docs", "unregister:alt-pack.docs"]
