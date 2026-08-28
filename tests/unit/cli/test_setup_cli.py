"""
Motet - Setup CLI Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
Unit tests for `motet-cli setup` configuration behavior.

Dependencies:
- pytest: Test framework
- click.testing: CliRunner
- motet.cli.setup: setup_group
"""

from __future__ import annotations

from typing import Any, Dict

import pytest
from click.testing import CliRunner

from motet.cli.setup import setup_group


def test_setup_set_saves_api_and_workspace_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    """setup set persists API URL and workspace host/container mapping."""
    written: Dict[str, Any] = {}

    def _fake_set(key: str, value: Any) -> None:
        written[key] = value

    monkeypatch.setattr("motet_sdk.cli.setup.set_cli_config_value", _fake_set)

    runner = CliRunner()
    result = runner.invoke(
        setup_group,
        [
            "set",
            "--api-url",
            "http://localhost:8000",
            "--workspace-host-root",
            "/Users/matt/projects/imf",
            "--workspace-container-root",
            "/app",
        ],
    )

    assert result.exit_code == 0, result.output
    assert written["api_url"] == "http://localhost:8000"
    assert written["workspace_host_root"] == "/Users/matt/projects/imf"
    assert written["workspace_container_root"] == "/app"


def test_setup_set_requires_both_workspace_mapping_flags() -> None:
    """setup set rejects partial workspace mapping options."""
    runner = CliRunner()
    result = runner.invoke(
        setup_group,
        [
            "set",
            "--workspace-host-root",
            "/Users/matt/projects/imf",
        ],
    )

    assert result.exit_code != 0
    assert "Set both --workspace-host-root and --workspace-container-root together." in result.output


def test_setup_doctor_passes_with_verified_mount(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """setup doctor exits 0 when mapping is valid and docker mount is verified."""
    monkeypatch.setattr(
        "motet_sdk.cli.setup.get_cli_config",
        lambda: {
            "workspace_host_root": str(tmp_path),
            "workspace_container_root": "/app",
            "api_url": "http://localhost:8000",
        },
    )
    monkeypatch.setattr(
        "motet_sdk.cli.setup._docker_mount_check",
        lambda _h, _c: (True, f"Verified mount on container 'imf-worker1': {tmp_path} -> /app"),
    )

    runner = CliRunner()
    result = runner.invoke(setup_group, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "Motet CLI setup doctor" in result.output
    assert "Verified mount on container" in result.output


def test_setup_doctor_fails_when_mount_not_found(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """setup doctor exits non-zero when docker mount verification fails."""
    monkeypatch.setattr(
        "motet_sdk.cli.setup.get_cli_config",
        lambda: {
            "workspace_host_root": str(tmp_path),
            "workspace_container_root": "/app",
        },
    )
    monkeypatch.setattr(
        "motet_sdk.cli.setup._docker_mount_check",
        lambda _h, _c: (False, f"No running container exposes mount {tmp_path} -> /app"),
    )

    runner = CliRunner()
    result = runner.invoke(setup_group, ["doctor"])

    assert result.exit_code != 0
    assert "No running container exposes mount" in result.output


def test_setup_doctor_uses_inferred_defaults_when_mapping_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """setup doctor uses inferred defaults (workspace root -> /app) when config mapping is absent."""
    monkeypatch.setattr("motet_sdk.cli.setup.get_cli_config", lambda: {})
    monkeypatch.setattr(
        "motet_sdk.cli.setup.infer_default_workspace_mapping",
        lambda: (str(tmp_path), "/app"),
    )
    monkeypatch.setattr(
        "motet_sdk.cli.setup._docker_mount_check",
        lambda _h, _c: (True, f"Verified mount on container 'motet-api': {tmp_path} -> /app"),
    )

    runner = CliRunner()
    result = runner.invoke(setup_group, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "Using inferred defaults for this workspace" in result.output
    assert f"Workspace host root: {tmp_path}" in result.output
    assert "Workspace container root: /app" in result.output
