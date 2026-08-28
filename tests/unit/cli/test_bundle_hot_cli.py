"""
Motet - Bundle Hot Deploy CLI Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
Unit tests for `motet-cli bundle hot-deploy` command behavior (Mutagen sync).

Dependencies:
- pytest: Test framework
- click.testing: CliRunner for command invocation
- motet.cli.bundle: bundle_group CLI

Usage:
pytest tests/unit/cli/test_bundle_hot_cli.py

Notes:
- API calls are monkeypatched to avoid network access.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest
from click.testing import CliRunner

from motet.cli.bundle import bundle_group


class _FakeResponse:
    """Simple response stub for api_request monkeypatch."""

    def __init__(self, payload: Dict[str, Any]) -> None:
        self._payload = payload

    def json(self) -> Dict[str, Any]:
        return self._payload


def test_bundle_hot_deploy_cli_creates_mutagen_sessions_and_posts_hot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """bundle hot-deploy creates mutagen sessions and uses remote worker path for hot deploy."""
    bundle_root = tmp_path / "bundle"
    bundle_root.mkdir(parents=True, exist_ok=True)
    (bundle_root / "manifest.yaml").write_text(
        'format_version: "1"\nname: "hello-world"\nversion: "0.1.0"\n',
        encoding="utf-8",
    )

    captured_api: Dict[str, Any] = {}
    mutagen_calls: list[list[str]] = []

    def _fake_api_request(method: str, url: str, **kwargs: Any) -> _FakeResponse:
        captured_api["method"] = method
        captured_api["url"] = url
        captured_api["kwargs"] = kwargs
        return _FakeResponse(
            {
                "deploy_job_id": "job-1",
                "bundle_id": "hello-world",
                "bundle_version": "abc123",
                "status": "complete",
            }
        )

    class _Proc:
        def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def _fake_subprocess_run(cmd: list[str], **_kwargs: Any) -> _Proc:
        mutagen_calls.append(cmd)
        return _Proc(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("motet_sdk.cli._api.api_request", _fake_api_request)
    monkeypatch.setattr("motet_sdk.cli._auth.get_api_headers", lambda: {"Authorization": "Bearer t"})
    monkeypatch.setattr("motet_sdk.cli.bundle.subprocess.run", _fake_subprocess_run)

    runner = CliRunner()
    result = runner.invoke(
        bundle_group,
        [
            "hot-deploy",
            str(bundle_root),
            "--containers",
            "cloud_worker1,cloud_worker2",
            "--remote-path",
            "/tmp/imf_dev/matt/hello-world",
            "--no-watch",
            "--api-url",
            "http://localhost:8000",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured_api["method"] == "POST"
    assert captured_api["url"] == "http://localhost:8000/api/v1/deploy/hot"
    assert captured_api["kwargs"]["json"]["bundle_path"] == "/tmp/imf_dev/matt/hello-world"
    assert any(call == ["mutagen", "version"] for call in mutagen_calls)
    create_calls = [call for call in mutagen_calls if call[:4] == ["mutagen", "sync", "create", "--name"]]
    assert len(create_calls) == 2
    assert all("_" not in call[4] for call in create_calls)
    assert any("docker://cloud_worker1/tmp/imf_dev/matt/hello-world" in call for call in create_calls)
    assert any("docker://cloud_worker2/tmp/imf_dev/matt/hello-world" in call for call in create_calls)


def test_bundle_hot_deploy_cli_requires_absolute_remote_path(tmp_path: Path) -> None:
    """bundle hot-deploy rejects non-absolute remote path."""
    bundle_root = tmp_path / "bundle"
    bundle_root.mkdir(parents=True, exist_ok=True)
    (bundle_root / "manifest.yaml").write_text(
        'format_version: "1"\nname: "hello-world"\nversion: "0.1.0"\n',
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(
        bundle_group,
        [
            "hot-deploy",
            str(bundle_root),
            "--containers",
            "cloud_worker1",
            "--remote-path",
            "relative/path",
            "--no-watch",
            "--api-url",
            "http://localhost:8000",
        ],
    )

    assert result.exit_code != 0
    assert "--remote-path must be an absolute container path" in result.output


def test_bundle_hot_deploy_cli_uses_default_containers_and_remote_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """bundle hot-deploy auto-discovers containers and derives remote path defaults."""
    bundle_root = tmp_path / "bundle"
    bundle_root.mkdir(parents=True, exist_ok=True)
    (bundle_root / "manifest.yaml").write_text(
        'format_version: "1"\nname: "hello-world"\nversion: "0.1.0"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("USER", "matt")

    api_calls: list[tuple[str, str, Dict[str, Any]]] = []
    mutagen_calls: list[list[str]] = []
    written_config: Dict[str, Any] = {}

    def _fake_api_request(method: str, url: str, **kwargs: Any) -> _FakeResponse:
        api_calls.append((method, url, kwargs))
        if url.endswith("/api/v1/workers/health"):
            return _FakeResponse(
                {
                    "worker_health": {
                        "cloud_worker1": {"healthy": True},
                        "cloud_worker2": {"healthy": False},
                        "cloud_lifecycle_management": {"healthy": True},
                    }
                }
            )
        return _FakeResponse(
            {
                "deploy_job_id": "job-2",
                "bundle_id": "hello-world",
                "bundle_version": "def456",
                "status": "complete",
            }
        )

    class _Proc:
        def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def _fake_subprocess_run(cmd: list[str], **_kwargs: Any) -> _Proc:
        mutagen_calls.append(cmd)
        return _Proc(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("motet_sdk.cli._api.api_request", _fake_api_request)
    monkeypatch.setattr("motet_sdk.cli._auth.get_api_headers", lambda: {"Authorization": "Bearer t"})
    monkeypatch.setattr("motet_sdk.cli.bundle.subprocess.run", _fake_subprocess_run)
    monkeypatch.setattr("motet_sdk.cli.bundle.get_cli_config", lambda: {})
    monkeypatch.setattr(
        "motet_sdk.cli.bundle.set_cli_config_value",
        lambda k, v: written_config.__setitem__(k, v),
    )

    runner = CliRunner()
    result = runner.invoke(
        bundle_group,
        [
            "hot-deploy",
            str(bundle_root),
            "--no-watch",
            "--api-url",
            "http://localhost:8000",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Auto-discovered containers: cloud_worker1, cloud_lifecycle_management" in result.output
    assert "Using default remote path: /tmp/imf_dev/matt/hello-world" in result.output
    assert any(
        "docker://cloud_worker1/tmp/imf_dev/matt/hello-world" in call
        for call in mutagen_calls
    )
    assert any(
        "docker://cloud_lifecycle_management/tmp/imf_dev/matt/hello-world" in call
        for call in mutagen_calls
    )
    assert any(call[1].endswith("/api/v1/workers/health") for call in api_calls)
    assert written_config["hot_deploy_discovered_containers"] == [
        "cloud_worker1",
        "cloud_lifecycle_management",
    ]
    hot_calls = [call for call in api_calls if call[1].endswith("/api/v1/deploy/hot")]
    assert hot_calls
    assert hot_calls[0][2]["json"]["bundle_path"] == "/tmp/imf_dev/matt/hello-world"


def test_bundle_hot_deploy_cli_uses_cached_discovered_containers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """bundle hot-deploy uses cached discovered containers before live discovery."""
    bundle_root = tmp_path / "bundle"
    bundle_root.mkdir(parents=True, exist_ok=True)
    (bundle_root / "manifest.yaml").write_text(
        'format_version: "1"\nname: "hello-world"\nversion: "0.1.0"\n',
        encoding="utf-8",
    )

    api_calls: list[tuple[str, str, Dict[str, Any]]] = []
    mutagen_calls: list[list[str]] = []

    def _fake_api_request(method: str, url: str, **kwargs: Any) -> _FakeResponse:
        api_calls.append((method, url, kwargs))
        return _FakeResponse(
            {
                "deploy_job_id": "job-3",
                "bundle_id": "hello-world",
                "bundle_version": "ghi789",
                "status": "complete",
            }
        )

    class _Proc:
        def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def _fake_subprocess_run(cmd: list[str], **_kwargs: Any) -> _Proc:
        mutagen_calls.append(cmd)
        return _Proc(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("motet_sdk.cli._api.api_request", _fake_api_request)
    monkeypatch.setattr("motet_sdk.cli._auth.get_api_headers", lambda: {"Authorization": "Bearer t"})
    monkeypatch.setattr("motet_sdk.cli.bundle.subprocess.run", _fake_subprocess_run)
    monkeypatch.setattr(
        "motet_sdk.cli.bundle.get_cli_config",
        lambda: {"hot_deploy_discovered_containers": ["cloud_worker1", "cloud_worker2"]},
    )
    monkeypatch.setattr("motet_sdk.cli.bundle.set_cli_config_value", lambda *_args, **_kwargs: None)

    runner = CliRunner()
    result = runner.invoke(
        bundle_group,
        [
            "hot-deploy",
            str(bundle_root),
            "--remote-path",
            "/tmp/imf_dev/matt/hello-world",
            "--no-watch",
            "--api-url",
            "http://localhost:8000",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Using cached discovered containers: cloud_worker1, cloud_worker2" in result.output
    assert not any(call[1].endswith("/api/v1/workers/health") for call in api_calls)
    assert any(
        "docker://cloud_worker1/tmp/imf_dev/matt/hello-world" in call
        for call in mutagen_calls
    )


def test_bundle_hot_deploy_cli_refreshes_stale_cached_container_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """hot-deploy refreshes cached container names that no longer exist."""
    bundle_root = tmp_path / "bundle"
    bundle_root.mkdir(parents=True, exist_ok=True)
    (bundle_root / "manifest.yaml").write_text(
        'format_version: "1"\nname: "hello-world"\nversion: "0.1.0"\n',
        encoding="utf-8",
    )

    api_calls: list[tuple[str, str, Dict[str, Any]]] = []
    subprocess_calls: list[list[str]] = []
    written_config: Dict[str, Any] = {}

    def _fake_api_request(method: str, url: str, **kwargs: Any) -> _FakeResponse:
        api_calls.append((method, url, kwargs))
        if url.endswith("/api/v1/workers/health"):
            return _FakeResponse(
                {
                    "worker_health": {
                        "cloud_worker1": {"healthy": True},
                        "cloud_lifecycle_management": {"healthy": True},
                    }
                }
            )
        return _FakeResponse(
            {
                "deploy_job_id": "job-stale-cache",
                "bundle_id": "hello-world",
                "bundle_version": "stale123",
                "status": "complete",
            }
        )

    class _Proc:
        def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def _fake_subprocess_run(cmd: list[str], **_kwargs: Any) -> _Proc:
        subprocess_calls.append(cmd)
        if cmd == ["docker", "ps", "--format", "{{.Names}}"]:
            return _Proc(
                returncode=0,
                stdout="motet_dev-worker-1-1\nmotet_dev-worker-lcm-1\n",
            )
        if cmd[:3] == ["docker", "inspect", "motet_dev-worker-1-1"]:
            return _Proc(returncode=0, stdout='["MOTET_WORKER_ID=worker1"]')
        if cmd[:3] == ["docker", "inspect", "motet_dev-worker-lcm-1"]:
            return _Proc(returncode=0, stdout='["MOTET_WORKER_ID=lifecycle_management"]')
        return _Proc(returncode=0, stdout="ok")

    monkeypatch.setattr("motet_sdk.cli._api.api_request", _fake_api_request)
    monkeypatch.setattr("motet_sdk.cli._auth.get_api_headers", lambda: {"Authorization": "Bearer t"})
    monkeypatch.setattr("motet_sdk.cli.bundle.subprocess.run", _fake_subprocess_run)
    monkeypatch.setattr(
        "motet_sdk.cli.bundle.get_cli_config",
        lambda: {"hot_deploy_discovered_containers": ["agent_worker1", "agent_lifecycle_management"]},
    )
    monkeypatch.setattr(
        "motet_sdk.cli.bundle.set_cli_config_value",
        lambda k, v: written_config.__setitem__(k, v),
    )

    runner = CliRunner()
    result = runner.invoke(
        bundle_group,
        [
            "hot-deploy",
            str(bundle_root),
            "--remote-path",
            "/tmp/imf_dev/matt/hello-world",
            "--no-watch",
            "--api-url",
            "http://localhost:8000",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Using cached discovered containers: agent_worker1, agent_lifecycle_management" in result.output
    assert "Cached discovered containers are no longer running; refreshing discovery." in result.output
    assert "Resolved Docker containers: motet_dev-worker-1-1, motet_dev-worker-lcm-1" in result.output
    assert any(call[1].endswith("/api/v1/workers/health") for call in api_calls)
    assert written_config["hot_deploy_discovered_containers"] == [
        "cloud_worker1",
        "cloud_lifecycle_management",
    ]
    create_calls = [c for c in subprocess_calls if c[:4] == ["mutagen", "sync", "create", "--name"]]
    assert len(create_calls) == 2
    assert any("docker://motet_dev-worker-1-1/tmp/imf_dev/matt/hello-world" in c for c in create_calls)
    assert any("docker://motet_dev-worker-lcm-1/tmp/imf_dev/matt/hello-world" in c for c in create_calls)


def test_bundle_hot_deploy_cli_disable_discovered_container_caching_ignores_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """hot-deploy --disable-discovered-container-caching forces live discovery."""
    bundle_root = tmp_path / "bundle"
    bundle_root.mkdir(parents=True, exist_ok=True)
    (bundle_root / "manifest.yaml").write_text(
        'format_version: "1"\nname: "hello-world"\nversion: "0.1.0"\n',
        encoding="utf-8",
    )

    api_calls: list[tuple[str, str, Dict[str, Any]]] = []
    mutagen_calls: list[list[str]] = []
    write_called = {"value": False}

    def _fake_api_request(method: str, url: str, **kwargs: Any) -> _FakeResponse:
        api_calls.append((method, url, kwargs))
        if url.endswith("/api/v1/workers/health"):
            return _FakeResponse({"worker_health": {"agent_worker9": {"healthy": True}}})
        return _FakeResponse(
            {
                "deploy_job_id": "job-4",
                "bundle_id": "hello-world",
                "bundle_version": "zzz999",
                "status": "complete",
            }
        )

    class _Proc:
        def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def _fake_subprocess_run(cmd: list[str], **_kwargs: Any) -> _Proc:
        mutagen_calls.append(cmd)
        return _Proc(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("motet_sdk.cli._api.api_request", _fake_api_request)
    monkeypatch.setattr("motet_sdk.cli._auth.get_api_headers", lambda: {"Authorization": "Bearer t"})
    monkeypatch.setattr("motet_sdk.cli.bundle.subprocess.run", _fake_subprocess_run)
    monkeypatch.setattr(
        "motet_sdk.cli.bundle.get_cli_config",
        lambda: {"hot_deploy_discovered_containers": ["cloud_worker1"]},
    )
    monkeypatch.setattr(
        "motet_sdk.cli.bundle.set_cli_config_value",
        lambda *_args, **_kwargs: write_called.__setitem__("value", True),
    )

    runner = CliRunner()
    result = runner.invoke(
        bundle_group,
        [
            "hot-deploy",
            str(bundle_root),
            "--remote-path",
            "/tmp/imf_dev/matt/hello-world",
            "--disable-discovered-container-caching",
            "--no-watch",
            "--api-url",
            "http://localhost:8000",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Using cached discovered containers" not in result.output
    assert any(call[1].endswith("/api/v1/workers/health") for call in api_calls)
    assert not write_called["value"]
    assert any(
        "docker://agent_worker9/tmp/imf_dev/matt/hello-world" in call
        for call in mutagen_calls
    )


def test_bundle_hot_deploy_cli_resolves_worker_ids_to_docker_container_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """hot-deploy resolves discovered worker IDs to real Docker container names for mutagen."""
    bundle_root = tmp_path / "bundle"
    bundle_root.mkdir(parents=True, exist_ok=True)
    (bundle_root / "manifest.yaml").write_text(
        'format_version: "1"\nname: "hello-world"\nversion: "0.1.0"\n',
        encoding="utf-8",
    )

    subprocess_calls: list[list[str]] = []

    def _fake_api_request(method: str, url: str, **kwargs: Any) -> _FakeResponse:
        if url.endswith("/api/v1/workers/health"):
            return _FakeResponse(
                {"worker_health": {"cloud_worker1": {"healthy": True}, "cloud_worker2": {"healthy": True}}}
            )
        return _FakeResponse(
            {
                "deploy_job_id": "job-5",
                "bundle_id": "hello-world",
                "bundle_version": "uvw111",
                "status": "complete",
            }
        )

    class _Proc:
        def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def _fake_subprocess_run(cmd: list[str], **_kwargs: Any) -> _Proc:
        subprocess_calls.append(cmd)
        if cmd == ["docker", "ps", "--format", "{{.Names}}"]:
            return _Proc(
                returncode=0,
                stdout="motet_dev-worker-1-1\nmotet_dev-worker-2-1\n",
            )
        if cmd[:3] == ["docker", "inspect", "motet_dev-worker-1-1"]:
            return _Proc(returncode=0, stdout='["MOTET_WORKER_ID=worker1"]')
        if cmd[:3] == ["docker", "inspect", "motet_dev-worker-2-1"]:
            return _Proc(returncode=0, stdout='["MOTET_WORKER_ID=worker2"]')
        return _Proc(returncode=0, stdout="ok")

    monkeypatch.setattr("motet_sdk.cli._api.api_request", _fake_api_request)
    monkeypatch.setattr("motet_sdk.cli._auth.get_api_headers", lambda: {"Authorization": "Bearer t"})
    monkeypatch.setattr("motet_sdk.cli.bundle.subprocess.run", _fake_subprocess_run)
    monkeypatch.setattr("motet_sdk.cli.bundle.get_cli_config", lambda: {})
    monkeypatch.setattr("motet_sdk.cli.bundle.set_cli_config_value", lambda *_args, **_kwargs: None)

    runner = CliRunner()
    result = runner.invoke(
        bundle_group,
        [
            "hot-deploy",
            str(bundle_root),
            "--remote-path",
            "/tmp/imf_dev/matt/hello-world",
            "--no-watch",
            "--api-url",
            "http://localhost:8000",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Resolved Docker containers: motet_dev-worker-1-1, motet_dev-worker-2-1" in result.output
    create_calls = [c for c in subprocess_calls if c[:4] == ["mutagen", "sync", "create", "--name"]]
    assert any("docker://motet_dev-worker-1-1/tmp/imf_dev/matt/hello-world" in c for c in create_calls)
    assert any("docker://motet_dev-worker-2-1/tmp/imf_dev/matt/hello-world" in c for c in create_calls)


def test_bundle_hot_deploy_cli_prefers_dev_container_over_test_for_same_worker_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When both dev/test containers expose same MOTET_WORKER_ID, prefer dev container."""
    bundle_root = tmp_path / "bundle"
    bundle_root.mkdir(parents=True, exist_ok=True)
    (bundle_root / "manifest.yaml").write_text(
        'format_version: "1"\nname: "hello-world"\nversion: "0.1.0"\n',
        encoding="utf-8",
    )

    subprocess_calls: list[list[str]] = []

    def _fake_api_request(method: str, url: str, **kwargs: Any) -> _FakeResponse:
        if url.endswith("/api/v1/workers/health"):
            return _FakeResponse({"worker_health": {"cloud_lifecycle_management": {"healthy": True}}})
        return _FakeResponse(
            {
                "deploy_job_id": "job-6",
                "bundle_id": "hello-world",
                "bundle_version": "aaa222",
                "status": "complete",
            }
        )

    class _Proc:
        def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def _fake_subprocess_run(cmd: list[str], **_kwargs: Any) -> _Proc:
        subprocess_calls.append(cmd)
        if cmd == ["docker", "ps", "--format", "{{.Names}}"]:
            return _Proc(returncode=0, stdout="imf-test-deployer\nimf_dev-worker-lcm-1\n")
        if cmd[:3] == ["docker", "inspect", "imf-test-deployer"]:
            return _Proc(returncode=0, stdout='["MOTET_WORKER_ID=lifecycle_management"]')
        if cmd[:3] == ["docker", "inspect", "imf_dev-worker-lcm-1"]:
            return _Proc(returncode=0, stdout='["MOTET_WORKER_ID=lifecycle_management"]')
        return _Proc(returncode=0, stdout="ok")

    monkeypatch.setattr("motet_sdk.cli._api.api_request", _fake_api_request)
    monkeypatch.setattr("motet_sdk.cli._auth.get_api_headers", lambda: {"Authorization": "Bearer t"})
    monkeypatch.setattr("motet_sdk.cli.bundle.subprocess.run", _fake_subprocess_run)
    monkeypatch.setattr("motet_sdk.cli.bundle.get_cli_config", lambda: {})
    monkeypatch.setattr("motet_sdk.cli.bundle.set_cli_config_value", lambda *_args, **_kwargs: None)

    runner = CliRunner()
    result = runner.invoke(
        bundle_group,
        [
            "hot-deploy",
            str(bundle_root),
            "--remote-path",
            "/tmp/imf_dev/matt/hello-world",
            "--no-watch",
            "--api-url",
            "http://localhost:8000",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Resolved Docker containers: imf_dev-worker-lcm-1" in result.output
    create_calls = [c for c in subprocess_calls if c[:4] == ["mutagen", "sync", "create", "--name"]]
    assert len(create_calls) == 1
    assert "docker://imf_dev-worker-lcm-1/tmp/imf_dev/matt/hello-world" in create_calls[0]


def test_bundle_hot_deploy_cli_creates_remote_path_before_mutagen_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """hot-deploy ensures remote path exists in container before mutagen sync create."""
    bundle_root = tmp_path / "bundle"
    bundle_root.mkdir(parents=True, exist_ok=True)
    (bundle_root / "manifest.yaml").write_text(
        'format_version: "1"\nname: "hello-world"\nversion: "0.1.0"\n',
        encoding="utf-8",
    )

    subprocess_calls: list[list[str]] = []

    def _fake_api_request(method: str, url: str, **kwargs: Any) -> _FakeResponse:
        if url.endswith("/api/v1/workers/health"):
            return _FakeResponse({"worker_health": {"cloud_worker1": {"healthy": True}}})
        return _FakeResponse(
            {
                "deploy_job_id": "job-7",
                "bundle_id": "hello-world",
                "bundle_version": "bbb333",
                "status": "complete",
            }
        )

    class _Proc:
        def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def _fake_subprocess_run(cmd: list[str], **_kwargs: Any) -> _Proc:
        subprocess_calls.append(cmd)
        if cmd == ["docker", "ps", "--format", "{{.Names}}"]:
            return _Proc(returncode=0, stdout="motet_dev-worker-1-1\n")
        if cmd[:3] == ["docker", "inspect", "motet_dev-worker-1-1"]:
            return _Proc(returncode=0, stdout='["MOTET_WORKER_ID=worker1"]')
        return _Proc(returncode=0, stdout="ok")

    monkeypatch.setattr("motet_sdk.cli._api.api_request", _fake_api_request)
    monkeypatch.setattr("motet_sdk.cli._auth.get_api_headers", lambda: {"Authorization": "Bearer t"})
    monkeypatch.setattr("motet_sdk.cli.bundle.subprocess.run", _fake_subprocess_run)
    monkeypatch.setattr("motet_sdk.cli.bundle.get_cli_config", lambda: {})
    monkeypatch.setattr("motet_sdk.cli.bundle.set_cli_config_value", lambda *_args, **_kwargs: None)

    runner = CliRunner()
    result = runner.invoke(
        bundle_group,
        [
            "hot-deploy",
            str(bundle_root),
            "--remote-path",
            "/tmp/imf_dev/matt/hello-world",
            "--no-watch",
            "--api-url",
            "http://localhost:8000",
        ],
    )

    assert result.exit_code == 0, result.output
    mkdir_calls = [c for c in subprocess_calls if c[:5] == ["docker", "exec", "motet_dev-worker-1-1", "sh", "-lc"]]
    assert mkdir_calls
    assert "mkdir -p /tmp/imf_dev/matt/hello-world" in mkdir_calls[0][5]
    create_calls = [c for c in subprocess_calls if c[:4] == ["mutagen", "sync", "create", "--name"]]
    assert create_calls
    assert subprocess_calls.index(mkdir_calls[0]) < subprocess_calls.index(create_calls[0])
