"""
Motet - Device CLI Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-09

Description:
    Unit tests for device CLI compose/image discovery after the local→edge
    rename (#197), and readiness deregister cleanup on device stop.

Dependencies:
    - pytest: Test framework
    - click.testing: CliRunner
    - motet_sdk.cli.device: compose discovery helpers and device_group

Usage:
    pytest tests/unit/cli/test_device_cli.py -q

Notes:
    - Does not exercise Docker Compose runtime; discovery only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest
from click.testing import CliRunner

from motet_sdk.cli.device import (
    _best_effort_remote_cleanup,
    _get_local_compose_file,
    _get_local_compose_override,
    device_group,
)


def test_get_compose_file_prefers_edge_worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    edge = tmp_path / "docker-compose.edge-worker.yml"
    legacy = tmp_path / "docker-compose.local-worker.yml"
    edge.write_text("services: {}\n", encoding="utf-8")
    legacy.write_text("services: {}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MOTET_DEVICE_COMPOSE_FILE", raising=False)

    assert _get_local_compose_file() == edge.resolve()


def test_get_compose_file_falls_back_to_legacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    legacy = tmp_path / "docker-compose.local-worker.yml"
    legacy.write_text("services: {}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MOTET_DEVICE_COMPOSE_FILE", raising=False)

    assert _get_local_compose_file() == legacy.resolve()
    err = capsys.readouterr().err
    assert "deprecated docker-compose.local-worker.yml" in err


def test_get_compose_override_prefers_edge_name(tmp_path: Path) -> None:
    compose = tmp_path / "docker-compose.edge-worker.yml"
    edge_override = tmp_path / "docker-compose.edge-worker.override.yml"
    legacy_override = tmp_path / "docker-compose.local-worker.override.yml"
    compose.write_text("services: {}\n", encoding="utf-8")
    edge_override.write_text("services: {}\n", encoding="utf-8")
    legacy_override.write_text("services: {}\n", encoding="utf-8")

    assert _get_local_compose_override(compose) == edge_override


def test_device_build_defaults_point_at_edge_image() -> None:
    runner = CliRunner()
    result = runner.invoke(device_group, ["build", "--help"])
    assert result.exit_code == 0, result.output
    # Click soft-wraps long defaults (may insert a break after a hyphen).
    compact = "".join(result.output.split())
    assert "motet-edge-worker:latest" in compact
    assert "docker/images/edge-worker/Dockerfile" in compact
    assert "motet-local-worker" not in compact


class _FakeResp:
    def __init__(self, payload: Dict[str, Any], content: bytes = b"{}") -> None:
        self._payload = payload
        self.content = content if content is not None else b"{}"

    def json(self) -> Dict[str, Any]:
        return self._payload


def test_best_effort_remote_cleanup_uses_device_deregister(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: List[Dict[str, Any]] = []

    def fake_api_request(method: str, url: str, **kwargs: Any) -> _FakeResp:
        calls.append({"method": method, "url": url, "headers": kwargs.get("headers")})
        if url.endswith("/deregister"):
            return _FakeResp(
                {"worker_id": "edge_ab12cd34", "removed": True},
                content=b'{"removed":true}',
            )
        if url.endswith("/workers/health"):
            return _FakeResp({"worker_health": {}})
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr("motet_sdk.cli.device.api_request", fake_api_request)

    _best_effort_remote_cleanup(
        profile_payload={
            "worker_id": "edge_ab12cd34",
            "device_token": "ld_secret",
        },
        api_url="http://localhost:8000",
        worker_cleanup_candidates=["edge_ab12cd34", "cloud_edge_ab12cd34"],
    )

    deregisters = [c for c in calls if c["url"].endswith("/deregister")]
    assert len(deregisters) == 2
    assert all(c["method"] == "POST" for c in deregisters)
    assert all(
        c["headers"]["Authorization"] == "Bearer ld_secret" for c in deregisters
    )
    assert all("/terminate" not in c["url"] for c in calls)
    out = capsys.readouterr().out
    assert "Deregistered edge worker" in out


def test_best_effort_remote_cleanup_skips_non_edge(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: List[str] = []

    def fake_api_request(method: str, url: str, **kwargs: Any) -> _FakeResp:
        calls.append(url)
        if url.endswith("/workers/health"):
            return _FakeResp({"worker_health": {}})
        raise AssertionError(f"unexpected deregister of non-edge: {url}")

    monkeypatch.setattr("motet_sdk.cli.device.api_request", fake_api_request)

    _best_effort_remote_cleanup(
        profile_payload={"worker_id": "edge_ab12cd34", "device_token": "ld_x"},
        api_url="http://localhost:8000",
        worker_cleanup_candidates=["cloud_hostname_only"],
    )

    assert calls == ["http://localhost:8000/api/v1/workers/health"]
    assert "Skipping non-edge cleanup candidate" in capsys.readouterr().out
