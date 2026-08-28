"""
Motet - Local CLI Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-27

Description:
Unit tests for `motet-cli local` stack management commands.

Dependencies:
- pytest: Test framework
- click.testing: CliRunner
- motet.cli.local: local_group
"""

from __future__ import annotations

import os
from typing import Any, Dict, List

import pytest
from click.testing import CliRunner

from motet_sdk.cli.local import (
    DEFAULT_IMAGE_REGISTRY,
    _ensure_image_pin_env,
    _ensure_local_tls_certs,
    _tls_material_present,
    local_group,
)


class _Proc:
    """Simple subprocess result stub."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _Resp:
    """Simple HTTP response stub."""

    def __init__(self, payload: Dict[str, Any]) -> None:
        self._payload = payload

    def json(self) -> Dict[str, Any]:
        return self._payload


def test_local_up_build_and_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """local up forwards build/profile flags to compose."""
    calls: List[List[str]] = []
    ensured = {"called": False}

    def _fake_run(cmd: List[str], **_kwargs: Any) -> _Proc:
        calls.append(cmd)
        if cmd == ["docker", "compose", "version"]:
            return _Proc(returncode=0, stdout="Docker Compose version v2")
        return _Proc(returncode=0, stdout="started")

    monkeypatch.setattr("motet_sdk.cli.local.subprocess.run", _fake_run)
    monkeypatch.setattr(
        "motet_sdk.cli.local._ensure_local_tls_certs",
        lambda: ensured.__setitem__("called", True),
    )

    runner = CliRunner()
    result = runner.invoke(local_group, ["up", "--build", "--profile", "distributed"])

    assert result.exit_code == 0, result.output
    assert ensured["called"] is True
    up_calls = [call for call in calls if len(call) > 5 and call[0:2] == ["docker", "compose"] and "up" in call]
    assert up_calls
    assert "--build" in up_calls[0]
    assert "--profile" in up_calls[0]


def test_ensure_image_pin_env_sets_product_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset image pins default to ghcr.io/motet-ai and v{package version}."""
    monkeypatch.delenv("MOTET_IMAGE_REGISTRY", raising=False)
    monkeypatch.delenv("MOTET_IMAGE_TAG", raising=False)
    monkeypatch.setattr("motet_sdk.cli.local.get_version", lambda: "0.1.0")

    _ensure_image_pin_env()

    assert os.environ["MOTET_IMAGE_REGISTRY"] == DEFAULT_IMAGE_REGISTRY
    assert os.environ["MOTET_IMAGE_TAG"] == "v0.1.0"


def test_ensure_image_pin_env_keeps_explicit_pins(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit registry or tag is not overwritten."""
    monkeypatch.setenv("MOTET_IMAGE_REGISTRY", "ghcr.io/example/eval")
    monkeypatch.setenv("MOTET_IMAGE_TAG", "v9.9.9")

    _ensure_image_pin_env()

    assert os.environ["MOTET_IMAGE_REGISTRY"] == "ghcr.io/example/eval"
    assert os.environ["MOTET_IMAGE_TAG"] == "v9.9.9"


def test_local_down_defaults_remove_orphans(monkeypatch: pytest.MonkeyPatch) -> None:
    """local down includes --remove-orphans by default."""
    calls: List[List[str]] = []

    def _fake_run(cmd: List[str], **_kwargs: Any) -> _Proc:
        calls.append(cmd)
        if cmd == ["docker", "compose", "version"]:
            return _Proc(returncode=0)
        return _Proc(returncode=0, stdout="stopped")

    monkeypatch.setattr("motet_sdk.cli.local.subprocess.run", _fake_run)

    runner = CliRunner()
    result = runner.invoke(local_group, ["down"])

    assert result.exit_code == 0, result.output
    down_calls = [c for c in calls if "down" in c]
    assert down_calls
    assert "--remove-orphans" in down_calls[0]


def test_local_restart_runs_down_then_up(monkeypatch: pytest.MonkeyPatch) -> None:
    """local restart runs compose restart (single command)."""
    calls: List[List[str]] = []

    def _fake_run(cmd: List[str], **_kwargs: Any) -> _Proc:
        calls.append(cmd)
        if cmd == ["docker", "compose", "version"]:
            return _Proc(returncode=0)
        return _Proc(returncode=0, stdout="ok")

    monkeypatch.setattr("motet_sdk.cli.local.subprocess.run", _fake_run)

    runner = CliRunner()
    result = runner.invoke(local_group, ["restart"])

    assert result.exit_code == 0, result.output
    restart_calls = [c for c in calls if "restart" in c]
    assert len(restart_calls) >= 1


def test_local_status_shows_readiness_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    """local status prints compose ps output and readiness summary."""
    def _fake_run(cmd: List[str], **_kwargs: Any) -> _Proc:
        if cmd == ["docker", "compose", "version"]:
            return _Proc(returncode=0)
        return _Proc(returncode=0, stdout="NAME STATUS")

    monkeypatch.setattr("motet_sdk.cli.local.subprocess.run", _fake_run)
    monkeypatch.setattr(
        "motet_sdk.cli.local.api_request",
        lambda *_args, **_kwargs: _Resp(
            {
                "system_stats": {"total_workers": 3, "ready_workers": 3},
                "workers": {"cloud_lifecycle_management": {}, "cloud_worker1": {}, "cloud_worker2": {}},
            }
        ),
    )
    monkeypatch.setattr("motet_sdk.cli.local.get_api_headers", lambda: {"Authorization": "Bearer t"})

    runner = CliRunner()
    result = runner.invoke(local_group, ["status", "--api-url", "http://localhost:8000"])

    assert result.exit_code == 0, result.output
    assert "Readiness: workers ready 3/3; lifecycle worker present" in result.output


def test_local_doctor_validates_docker_and_compose(monkeypatch: pytest.MonkeyPatch) -> None:
    """local doctor succeeds when docker/compose/readiness checks pass."""
    def _fake_run(cmd: List[str], **_kwargs: Any) -> _Proc:
        if cmd == ["docker", "info"]:
            return _Proc(returncode=0, stdout="ok")
        if cmd == ["docker", "compose", "version"]:
            return _Proc(returncode=0, stdout="ok")
        if "ps" in cmd and "--format" in cmd and "json" in cmd:
            return _Proc(returncode=0, stdout="[]")
        return _Proc(returncode=0, stdout="ok")

    monkeypatch.setattr("motet_sdk.cli.local.subprocess.run", _fake_run)
    monkeypatch.setattr(
        "motet_sdk.cli.local.api_request",
        lambda *_args, **_kwargs: _Resp({"system_stats": {"total_workers": 1, "ready_workers": 1}, "workers": {}}),
    )
    monkeypatch.setattr("motet_sdk.cli.local.get_api_headers", lambda: {"Authorization": "Bearer t"})

    runner = CliRunner()
    result = runner.invoke(local_group, ["doctor", "--api-url", "http://localhost:8000"])

    assert result.exit_code == 0, result.output
    assert "OK: Docker daemon reachable" in result.output
    assert "Compose stack query succeeded" in result.output


def test_local_manage_print_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """local manage --print-only does not attempt to open browser."""
    opened = {"called": False}

    monkeypatch.setattr("motet_sdk.cli.local._open_url", lambda _url: opened.__setitem__("called", True))

    runner = CliRunner()
    result = runner.invoke(local_group, ["manage", "--print-only"])

    assert result.exit_code == 0, result.output
    assert "Manage UI: http://localhost:8000/manage" in result.output
    assert opened["called"] is False


def test_local_manage_wait_calls_health_then_opens(monkeypatch: pytest.MonkeyPatch) -> None:
    """local manage --wait checks health before opening browser."""
    calls: List[str] = []

    monkeypatch.setattr("motet_sdk.cli.local._wait_for_manage_ready", lambda _api_url, _timeout: calls.append("wait"))
    monkeypatch.setattr("motet_sdk.cli.local._open_url", lambda _url: calls.append("open"))

    runner = CliRunner()
    result = runner.invoke(
        local_group,
        ["manage", "--wait", "--timeout", "5", "--api-url", "http://localhost:8000"],
    )

    assert result.exit_code == 0, result.output
    assert calls == ["wait", "open"]
    assert "Opened browser." in result.output


def test_tls_material_present_requires_server_cert_and_key(tmp_path: Any) -> None:
    """redis-tls needs both redis.crt and redis.key."""
    tls_dir = tmp_path / "tls"
    tls_dir.mkdir()
    assert _tls_material_present(tmp_path) is False
    (tls_dir / "redis.crt").write_text("cert\n", encoding="utf-8")
    assert _tls_material_present(tmp_path) is False
    (tls_dir / "redis.key").write_text("key\n", encoding="utf-8")
    assert _tls_material_present(tmp_path) is True


def test_ensure_local_tls_certs_skips_when_present(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Do not regenerate certificates when tls/ already has a server pair."""
    tls_dir = tmp_path / "tls"
    tls_dir.mkdir()
    (tls_dir / "redis.crt").write_text("cert\n", encoding="utf-8")
    (tls_dir / "redis.key").write_text("key\n", encoding="utf-8")
    calls: List[List[str]] = []

    def _fake_run(cmd: List[str], **_kwargs: Any) -> _Proc:
        calls.append(cmd)
        return _Proc(returncode=0)

    monkeypatch.setattr("motet_sdk.cli.local.subprocess.run", _fake_run)
    _ensure_local_tls_certs(tmp_path)
    assert calls == []


def test_ensure_local_tls_certs_runs_generate_script(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clean clone uses docker/redis/generate-tls-certs.sh when bash is available."""
    script = tmp_path / "docker" / "redis" / "generate-tls-certs.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/bash\n", encoding="utf-8")

    def _fake_run(cmd: List[str], **_kwargs: Any) -> _Proc:
        tls_dir = tmp_path / "tls"
        tls_dir.mkdir(exist_ok=True)
        (tls_dir / "redis.crt").write_text("cert\n", encoding="utf-8")
        (tls_dir / "redis.key").write_text("key\n", encoding="utf-8")
        return _Proc(returncode=0, stdout="ok")

    monkeypatch.setattr("motet_sdk.cli.local.subprocess.run", _fake_run)
    monkeypatch.setattr("motet_sdk.cli.local.shutil.which", lambda name: "/bin/bash" if name == "bash" else None)
    _ensure_local_tls_certs(tmp_path)
    assert _tls_material_present(tmp_path) is True


def test_ensure_local_tls_certs_falls_back_to_openssl(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the generate script, host openssl writes a minimal server pair."""
    openssl_calls: List[List[str]] = []

    def _fake_which(name: str) -> str | None:
        if name == "openssl":
            return "/usr/bin/openssl"
        return None

    def _fake_run(cmd: List[str], **_kwargs: Any) -> _Proc:
        openssl_calls.append(cmd)
        tls_dir = tmp_path / "tls"
        tls_dir.mkdir(exist_ok=True)
        (tls_dir / "redis.crt").write_text("cert\n", encoding="utf-8")
        (tls_dir / "redis.key").write_text("key\n", encoding="utf-8")
        return _Proc(returncode=0)

    monkeypatch.setattr("motet_sdk.cli.local.shutil.which", _fake_which)
    monkeypatch.setattr("motet_sdk.cli.local.subprocess.run", _fake_run)
    _ensure_local_tls_certs(tmp_path)
    assert openssl_calls
    assert openssl_calls[0][0] == "/usr/bin/openssl"
    assert _tls_material_present(tmp_path) is True
    assert (tmp_path / "tls" / "ca.crt").is_file()

