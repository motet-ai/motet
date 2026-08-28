"""
Motet - unit tests for MCP Docker cleanup helpers

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-13

Description:
    Tests labeling, worker/manager-id sweep (including raw manager_id), and
    HTTP sidecar reclaim by service_id or published host port.

Dependencies:
    - pytest
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest


def test_mcp_docker_container_labels_uses_worker_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOTET_WORKER_ID", "worker99")
    from motet.core.execution.mcp_docker_cleanup import mcp_docker_container_labels

    labels = mcp_docker_container_labels()
    assert labels["motet.mcp"] == "1"
    assert labels["motet.worker_id"] == "cloud_worker99"
    assert "motet.mcp.service_id" not in labels


def test_mcp_docker_container_labels_explicit_worker_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOTET_WORKER_ID", "worker99")
    from motet.core.execution.mcp_docker_cleanup import mcp_docker_container_labels

    labels = mcp_docker_container_labels("cloud_worker1", service_id="everything_http_test")
    assert labels["motet.worker_id"] == "cloud_worker1"
    assert labels["motet.mcp.service_id"] == "everything_http_test"


def test_sweep_skips_when_subprocess_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOTET_MCP_EXEC_BACKEND", "subprocess")
    monkeypatch.delenv("MOTET_EXEC_BACKEND", raising=False)
    from motet.core.execution import mcp_docker_cleanup as mod

    with patch.object(mod, "docker_request") as dr:
        mod.sweep_mcp_containers_for_worker("worker1")
    dr.assert_not_called()


def _run_sweep(mod, containers: list[dict], worker_id: str) -> list[tuple[str, str]]:
    list_body = json.dumps(containers).encode()
    calls: list[tuple[str, str]] = []

    def fake_request(sock, method, path, body=None, headers=None):
        calls.append((method, path))
        if method == "GET" and "/containers/json" in path:
            return (200, list_body)
        return (204, b"")

    with patch.object(mod, "docker_request", side_effect=fake_request):
        with patch.object(mod, "docker_socket_path", return_value=("/var/run/docker.sock", None)):
            with patch.object(mod.os.path, "exists", return_value=True):
                mod.sweep_mcp_containers_for_worker(worker_id)
    return calls


def test_sweep_lists_and_removes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOTET_MCP_EXEC_BACKEND", "docker")
    monkeypatch.delenv("MOTET_EXEC_BACKEND", raising=False)
    from motet.core.execution import mcp_docker_cleanup as mod

    cid = "abc123deadbeef00000000000000000000000000000000000000000000000000"
    calls = _run_sweep(
        mod,
        [
            {
                "Id": cid,
                "Names": ["/mcp_test"],
                "Labels": {"motet.mcp": "1", "motet.worker_id": "cloud_worker1"},
            }
        ],
        "worker1",
    )

    assert any(m == "GET" and "containers/json" in p for m, p in calls)
    assert any(m == "POST" and "/stop" in p and cid in p for m, p in calls)
    assert any(m == "DELETE" and cid in p for m, p in calls)


def test_sweep_matches_raw_manager_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Manager labels sidecars mcp-local-default; sweep must not require cloud_ prefix."""
    monkeypatch.setenv("MOTET_MCP_EXEC_BACKEND", "docker")
    monkeypatch.delenv("MOTET_EXEC_BACKEND", raising=False)
    from motet.core.execution import mcp_docker_cleanup as mod

    cid = "def456deadbeef00000000000000000000000000000000000000000000000000"
    calls = _run_sweep(
        mod,
        [
            {
                "Id": cid,
                "Names": ["/leftover_http"],
                "Labels": {"motet.mcp": "1", "motet.worker_id": "mcp-local-default"},
            }
        ],
        "mcp-local-default",
    )
    assert any(m == "DELETE" and cid in p for m, p in calls)


def test_sweep_skips_other_worker_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOTET_MCP_EXEC_BACKEND", "docker")
    monkeypatch.delenv("MOTET_EXEC_BACKEND", raising=False)
    from motet.core.execution import mcp_docker_cleanup as mod

    cid = "aaa111deadbeef00000000000000000000000000000000000000000000000000"
    calls = _run_sweep(
        mod,
        [
            {
                "Id": cid,
                "Names": ["/other"],
                "Labels": {"motet.mcp": "1", "motet.worker_id": "cloud_worker2"},
            }
        ],
        "mcp-local-default",
    )
    assert not any(m == "DELETE" for m, _p in calls)


def test_sweep_http_sidecars_by_port_and_service(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOTET_MCP_EXEC_BACKEND", "docker")
    monkeypatch.delenv("MOTET_EXEC_BACKEND", raising=False)
    from motet.core.execution import mcp_docker_cleanup as mod

    port_cid = "port000deadbeef0000000000000000000000000000000000000000000000000"
    svc_cid = "svc0000deadbeef0000000000000000000000000000000000000000000000000"
    other_cid = "oth0000deadbeef0000000000000000000000000000000000000000000000000"
    list_body = json.dumps(
        [
            {
                "Id": port_cid,
                "Names": ["/old_port"],
                "Labels": {"motet.mcp": "1", "motet.worker_id": "cloud_worker9"},
                "Ports": [{"IP": "0.0.0.0", "PrivatePort": 3301, "PublicPort": 3301, "Type": "tcp"}],
            },
            {
                "Id": svc_cid,
                "Names": ["/old_svc"],
                "Labels": {
                    "motet.mcp": "1",
                    "motet.worker_id": "other",
                    "motet.mcp.service_id": "everything_http_test",
                },
                "Ports": [],
            },
            {
                "Id": other_cid,
                "Names": ["/keep"],
                "Labels": {
                    "motet.mcp": "1",
                    "motet.worker_id": "other",
                    "motet.mcp.service_id": "weather",
                },
                "Ports": [{"PublicPort": 8080}],
            },
        ]
    ).encode()
    calls: list[tuple[str, str]] = []

    def fake_request(sock, method, path, body=None, headers=None):
        calls.append((method, path))
        if method == "GET" and "/containers/json" in path:
            return (200, list_body)
        return (204, b"")

    with patch.object(mod, "docker_request", side_effect=fake_request):
        with patch.object(mod, "docker_socket_path", return_value=("/var/run/docker.sock", None)):
            with patch.object(mod.os.path, "exists", return_value=True):
                removed = mod.sweep_mcp_http_sidecars(
                    service_id="everything_http_test",
                    host_port=3301,
                )

    assert removed == 2
    deleted = [p for m, p in calls if m == "DELETE"]
    assert any(port_cid in p for p in deleted)
    assert any(svc_cid in p for p in deleted)
    assert not any(other_cid in p for p in deleted)
