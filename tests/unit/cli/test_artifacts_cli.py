"""
Motet - Artifacts CLI Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-06

Description:
Unit tests for `motet-cli artifacts` commands that wrap artifact indexing
management endpoints.

Dependencies:
- pytest: Test framework
- click.testing: CliRunner
- motet.cli.artifacts: artifacts_group

Usage:
pytest tests/unit/cli/test_artifacts_cli.py
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest
from click.testing import CliRunner

from motet.cli.artifacts import artifacts_group


class _Resp:
    """Simple response stub with JSON payload."""

    def __init__(self, payload: Dict[str, Any], body: bytes = b"payload") -> None:
        self._payload = payload
        self._body = body

    def json(self) -> Dict[str, Any]:
        return self._payload

    def iter_content(self, chunk_size: int = 8192):  # noqa: ANN201
        yield self._body


def _capture_api(monkeypatch: pytest.MonkeyPatch, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Patch artifact CLI HTTP helpers and capture requests."""

    calls: List[Dict[str, Any]] = []

    def fake_api_request(method: str, url: str, **kwargs: Any) -> _Resp:
        calls.append({"method": method, "url": url, **kwargs})
        return _Resp(payload)

    monkeypatch.setattr("motet_sdk.cli.artifacts.get_api_headers", lambda: {"Authorization": "Bearer t"})
    monkeypatch.setattr("motet_sdk.cli.artifacts.api_request", fake_api_request)
    return calls


def test_artifacts_ls_passes_scope_params(monkeypatch: pytest.MonkeyPatch) -> None:
    """ls forwards admin scope overrides supported by the Artifacts API."""

    calls = _capture_api(
        monkeypatch,
        {
            "items": [
                {
                    "id": "artifact-1",
                    "kind": "user_upload",
                    "content_type": "text/plain",
                    "bytes": 12,
                    "created_at": 0,
                }
            ]
        },
    )
    runner = CliRunner()

    result = runner.invoke(
        artifacts_group,
        [
            "ls",
            "--kind",
            "user_upload",
            "--tenant-id",
            "tenant-1",
            "--motet-id",
            "motet-1",
            "--api-url",
            "http://localhost:8000",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "artifact-1" in result.output
    assert calls[0]["method"] == "GET"
    assert calls[0]["url"] == "http://localhost:8000/api/v1/artifacts"
    assert calls[0]["params"]["kind"] == "user_upload"
    assert calls[0]["params"]["tenant_id"] == "tenant-1"
    assert calls[0]["params"]["motet_id"] == "motet-1"


def test_artifacts_ls_passes_source_artifact_id_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    """ls forwards source_artifact_id for derived-artifact discovery."""

    calls = _capture_api(
        monkeypatch,
        {
            "items": [
                {
                    "id": "poster-1",
                    "kind": "derived_video_poster",
                    "content_type": "image/jpeg",
                    "bytes": 4096,
                    "created_at": 0,
                    "source_artifact_id": "video-1",
                }
            ]
        },
    )
    runner = CliRunner()

    result = runner.invoke(
        artifacts_group,
        [
            "ls",
            "--source-artifact-id",
            "video-1",
            "--kind",
            "derived_video_poster",
            "--api-url",
            "http://localhost:8000",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "poster-1" in result.output
    assert calls[0]["params"]["source_artifact_id"] == "video-1"
    assert calls[0]["params"]["kind"] == "derived_video_poster"


def test_artifacts_info_passes_scope_params(monkeypatch: pytest.MonkeyPatch) -> None:
    """info forwards admin scope overrides to metadata lookup."""

    calls = _capture_api(monkeypatch, {"id": "artifact-1", "metadata": {}})
    runner = CliRunner()

    result = runner.invoke(
        artifacts_group,
        ["info", "artifact-1", "--tenant-id", "tenant-1", "--api-url", "http://localhost:8000"],
    )

    assert result.exit_code == 0, result.output
    assert '"id": "artifact-1"' in result.output
    assert calls[0]["method"] == "GET"
    assert calls[0]["url"] == "http://localhost:8000/api/v1/artifacts/artifact-1/metadata"
    assert calls[0]["params"] == {"tenant_id": "tenant-1"}


def test_artifacts_rm_passes_scope_params(monkeypatch: pytest.MonkeyPatch) -> None:
    """rm forwards admin scope overrides to delete."""

    calls = _capture_api(monkeypatch, {"status": "deleted", "artifact_id": "artifact-1"})
    runner = CliRunner()

    result = runner.invoke(
        artifacts_group,
        ["rm", "artifact-1", "--motet-id", "motet-1", "--api-url", "http://localhost:8000"],
    )

    assert result.exit_code == 0, result.output
    assert "Artifact artifact-1 deleted." in result.output
    assert calls[0]["method"] == "DELETE"
    assert calls[0]["url"] == "http://localhost:8000/api/v1/artifacts/artifact-1"
    assert calls[0]["params"] == {"motet_id": "motet-1"}


def test_artifacts_get_passes_scope_params(monkeypatch: pytest.MonkeyPatch) -> None:
    """get forwards admin scope overrides to download."""

    calls = _capture_api(monkeypatch, {})
    runner = CliRunner()

    with runner.isolated_filesystem():
        result = runner.invoke(
            artifacts_group,
            [
                "get",
                "artifact-1",
                "--out",
                "artifact.txt",
                "--tenant-id",
                "tenant-1",
                "--api-url",
                "http://localhost:8000",
            ],
        )

    assert result.exit_code == 0, result.output
    assert "Downloaded artifact artifact-1 to artifact.txt" in result.output
    assert calls[0]["method"] == "GET"
    assert calls[0]["url"] == "http://localhost:8000/api/v1/artifacts/artifact-1/download"
    assert calls[0]["params"] == {"tenant_id": "tenant-1"}
    assert calls[0]["stream"] is True


def test_artifacts_indexing_status_calls_bulk_status_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """indexing-status sends repeated artifact_id query params."""

    calls = _capture_api(monkeypatch, {"items": [{"artifact_id": "a1", "summary": "indexed"}]})
    runner = CliRunner()

    result = runner.invoke(
        artifacts_group,
        ["indexing-status", "a1", "a2", "--tenant-id", "tenant-1", "--api-url", "http://localhost:8000"],
    )

    assert result.exit_code == 0, result.output
    assert '"summary": "indexed"' in result.output
    assert calls[0]["method"] == "GET"
    assert calls[0]["url"] == "http://localhost:8000/api/v1/artifacts/indexing-status"
    assert calls[0]["params"] == [
        ("artifact_id", "a1"),
        ("artifact_id", "a2"),
        ("tenant_id", "tenant-1"),
    ]


def test_artifacts_reindex_queues_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """reindex defaults to queued async behavior."""

    calls = _capture_api(monkeypatch, {"task_id": "task-1", "status": "queued"})
    runner = CliRunner()

    result = runner.invoke(
        artifacts_group,
        ["reindex", "artifact-1", "--api-url", "http://localhost:8000"],
    )

    assert result.exit_code == 0, result.output
    assert '"status": "queued"' in result.output
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"] == "http://localhost:8000/api/v1/artifacts/artifact-1/reindex"
    assert calls[0]["params"] == {}
    assert calls[0]["timeout"] == 30


def test_artifacts_reindex_wait_passes_wait_param(monkeypatch: pytest.MonkeyPatch) -> None:
    """reindex --wait uses synchronous API mode and longer timeout."""

    calls = _capture_api(monkeypatch, {"task_id": "task-1", "status": "success"})
    runner = CliRunner()

    result = runner.invoke(
        artifacts_group,
        ["reindex", "artifact-1", "--wait", "--strategy", "json_pointer", "--motet-id", "motet-1", "--api-url", "http://localhost:8000"],
    )

    assert result.exit_code == 0, result.output
    assert calls[0]["params"] == {"motet_id": "motet-1", "wait": "true", "strategy_id": "json_pointer"}
    assert calls[0]["timeout"] == 300


def test_artifacts_reindex_task_calls_task_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """reindex-task fetches task state by task id."""

    calls = _capture_api(monkeypatch, {"task_id": "task-1", "status": "running"})
    runner = CliRunner()

    result = runner.invoke(
        artifacts_group,
        ["reindex-task", "task-1", "--api-url", "http://localhost:8000"],
    )

    assert result.exit_code == 0, result.output
    assert '"status": "running"' in result.output
    assert calls[0]["method"] == "GET"
    assert calls[0]["url"] == "http://localhost:8000/api/v1/artifacts/reindex-tasks/task-1"


def test_artifacts_indexing_policy_requires_enabled_or_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """indexing-policy requires an explicit desired policy state."""

    _capture_api(monkeypatch, {})
    runner = CliRunner()

    result = runner.invoke(
        artifacts_group,
        ["indexing-policy", "artifact-1", "--api-url", "http://localhost:8000"],
    )

    assert result.exit_code != 0
    assert "Pass either --enabled or --disabled" in result.output


def test_artifacts_indexing_policy_patches_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """indexing-policy sends the durable eligibility flag."""

    calls = _capture_api(monkeypatch, {"artifact_id": "artifact-1", "indexing_enabled": False})
    runner = CliRunner()

    result = runner.invoke(
        artifacts_group,
        ["indexing-policy", "artifact-1", "--disabled", "--api-url", "http://localhost:8000"],
    )

    assert result.exit_code == 0, result.output
    assert '"indexing_enabled": false' in result.output
    assert calls[0]["method"] == "PATCH"
    assert calls[0]["url"] == "http://localhost:8000/api/v1/artifacts/artifact-1/indexing-policy"
    assert calls[0]["json"] == {"indexing_enabled": False}


def test_artifacts_metadata_patches_fields_and_tags(monkeypatch: pytest.MonkeyPatch) -> None:
    """metadata merges key=value fields and artifact tags."""

    calls = _capture_api(monkeypatch, {"id": "artifact-1", "metadata": {"source": "memo"}})
    runner = CliRunner()

    result = runner.invoke(
        artifacts_group,
        [
            "metadata",
            "artifact-1",
            "--set",
            "source=memo",
            "--tag",
            "jersey",
            "--tag",
            "signed",
            "--tenant-id",
            "tenant-1",
            "--api-url",
            "http://localhost:8000",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls[0]["method"] == "PATCH"
    assert calls[0]["url"] == "http://localhost:8000/api/v1/artifacts/artifact-1/metadata"
    assert calls[0]["params"] == {"tenant_id": "tenant-1"}
    assert calls[0]["json"] == {
        "metadata": {"source": "memo"},
        "artifact_tags": ["jersey", "signed"],
        "merge_artifact_tags": True,
    }


def test_artifacts_metadata_requires_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    """metadata refuses empty patches."""

    _capture_api(monkeypatch, {})
    runner = CliRunner()

    result = runner.invoke(
        artifacts_group,
        ["metadata", "artifact-1", "--api-url", "http://localhost:8000"],
    )

    assert result.exit_code != 0
    assert "Provide at least one of --set, --set-json, or --tag" in result.output


def test_artifacts_rm_all_aborts_without_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    """rm-all aborts when the confirmation prompt is declined."""

    calls = _capture_api(monkeypatch, {})
    runner = CliRunner()

    result = runner.invoke(
        artifacts_group,
        ["rm-all", "--api-url", "http://localhost:8000"],
        input="n\n",
    )

    assert result.exit_code != 0
    assert "Delete ALL artifacts in the resolved scope?" in result.output
    assert calls == []


def test_artifacts_rm_all_calls_bulk_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    """rm-all deletes all artifacts in scope with --yes or confirmed prompt."""

    calls = _capture_api(monkeypatch, {"deleted_count": 3, "failed_count": 0})
    runner = CliRunner()

    result = runner.invoke(
        artifacts_group,
        ["rm-all", "--yes", "--motet-id", "motet-1", "--api-url", "http://localhost:8000"],
    )

    assert result.exit_code == 0, result.output
    assert '"deleted_count": 3' in result.output
    assert calls[0]["method"] == "DELETE"
    assert calls[0]["url"] == "http://localhost:8000/api/v1/artifacts"
    assert calls[0]["params"] == {"motet_id": "motet-1"}


def test_artifacts_rm_all_accepts_interactive_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    """rm-all proceeds when the confirmation prompt is accepted."""

    calls = _capture_api(monkeypatch, {"deleted_count": 1, "failed_count": 0})
    runner = CliRunner()

    result = runner.invoke(
        artifacts_group,
        ["rm-all", "--api-url", "http://localhost:8000"],
        input="y\n",
    )

    assert result.exit_code == 0, result.output
    assert calls[0]["method"] == "DELETE"
    assert calls[0]["url"] == "http://localhost:8000/api/v1/artifacts"
