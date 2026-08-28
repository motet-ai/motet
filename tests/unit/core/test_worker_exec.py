"""Tests for core.worker_exec and subprocess execution backend."""

from __future__ import annotations

import http.client
import json
import sys

import pytest

from motet.core.execution import ExecutionRequest, docker_client, run_execution
from motet.core.commands.capabilities import WorkerCapability
from motet.core.commands.builtin.tool import _infer_tool_capabilities
from motet.core.commands.command_data_classes import ToolExecutionData
from motet.core.tools.builtin.worker_exec import run as worker_run


def test_worker_exec_infer_capabilities() -> None:
    caps = _infer_tool_capabilities(
        ToolExecutionData(
            tool_name="core.worker_exec",
            parameters={"argv": ["true"]},
        )
    )
    assert WorkerCapability.WORKER_SHELL_EXEC in caps
    assert WorkerCapability.TOOL_EXECUTION in caps


def test_worker_exec_requires_allowlist(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.delenv("MOTET_WORKER_EXEC_CWD_ALLOWLIST", raising=False)
    r = worker_run({"argv": [sys.executable, "-c", "pass"]})
    assert "error" in r
    assert "MOTET_WORKER_EXEC_CWD_ALLOWLIST" in r["error"]
    meta = r.get("meta") or {}
    assert meta.get("backend") == "subprocess"
    assert "worker_id" not in meta


def test_worker_exec_runs_with_allowlist(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    d = tmp_path / "allowed"
    d.mkdir()
    monkeypatch.setenv("MOTET_WORKER_EXEC_CWD_ALLOWLIST", str(d))
    r = worker_run({"argv": [sys.executable, "-c", "print('hi')"]})
    assert r.get("returncode") == 0
    assert "hi" in (r.get("stdout") or "")
    assert r.get("backend") == "subprocess"
    effective = r.get("effective_cwd") or ""
    assert effective.startswith(str(d.resolve()))
    assert r.get("cwd_generated") is True
    assert "worker_id" not in r


def test_worker_exec_ignores_provided_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Caller-provided cwd is ignored; tool always generates allowlisted cwd."""
    root = tmp_path / "allowed"
    root.mkdir()
    monkeypatch.setenv("MOTET_WORKER_EXEC_CWD_ALLOWLIST", str(root))
    provided = str(tmp_path / "outside")
    r = worker_run({"argv": [sys.executable, "-c", "print('ok')"], "cwd": provided})
    assert r.get("returncode") == 0
    assert r.get("cwd_generated") is True
    effective = r.get("effective_cwd") or ""
    assert effective.startswith(str(root))
    assert effective != provided
    assert "ok" in (r.get("stdout") or "")


def test_worker_exec_generates_cwd_when_omitted(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    monkeypatch.setenv("MOTET_WORKER_EXEC_CWD_ALLOWLIST", str(root))
    r = worker_run({"argv": [sys.executable, "-c", "print('gen')"]})
    assert r.get("returncode") == 0
    assert r.get("cwd_generated") is True
    effective = r.get("effective_cwd") or ""
    assert effective.startswith(str(root))
    assert "runs" in effective
    assert "gen" in (r.get("stdout") or "")


def test_worker_exec_reuses_allowlist_root_when_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """ADR-0122: app-builder worker runs at the clone root (no runs/ subdir)."""
    root = tmp_path / "clone"
    root.mkdir()
    monkeypatch.setenv("MOTET_WORKER_EXEC_CWD_ALLOWLIST", str(root))
    monkeypatch.setenv("MOTET_WORKER_EXEC_REUSE_ALLOWLIST_AS_CWD", "1")
    r = worker_run({"argv": [sys.executable, "-c", "import os; print(os.getcwd())"]})
    assert r.get("returncode") == 0
    assert r.get("cwd_generated") is False
    assert (r.get("effective_cwd") or "") == str(root.resolve())
    assert str(root.resolve()) in (r.get("stdout") or "")


def test_worker_exec_generated_cwds_are_unique(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    monkeypatch.setenv("MOTET_WORKER_EXEC_CWD_ALLOWLIST", str(root))
    r1 = worker_run({"argv": [sys.executable, "-c", "print('a')"]})
    r2 = worker_run({"argv": [sys.executable, "-c", "print('b')"]})
    assert r1.get("returncode") == 0
    assert r2.get("returncode") == 0
    assert r1.get("effective_cwd") != r2.get("effective_cwd")


def test_worker_exec_stages_bundle_and_normalizes_script_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    allow_root = tmp_path / "allowed"
    allow_root.mkdir()
    plugin_root = tmp_path / "bundles"
    script_dir = plugin_root / "basic-skill-example" / "skills" / "basic-script-skill" / "scripts"
    script_dir.mkdir(parents=True)
    script = script_dir / "echo_payload.py"
    script.write_text("print('bundle-staged-ok')\n", encoding="utf-8")

    monkeypatch.setenv("MOTET_WORKER_EXEC_CWD_ALLOWLIST", str(allow_root))
    monkeypatch.setenv("MOTET_PLUGIN_ROOT", str(plugin_root))

    r = worker_run(
        {
            "bundle_id": "basic-skill-example",
            "argv": [sys.executable, "/work/skills/basic-script-skill/scripts/echo_payload.py"],
        }
    )
    assert r.get("returncode") == 0
    assert "bundle-staged-ok" in (r.get("stdout") or "")
    assert r.get("bundle_id") == "basic-skill-example"


def test_worker_exec_normalizes_absolute_path_missing_skills_segment(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Host paths that skip ``skills/`` under the bundle root still run after staging."""
    allow_root = tmp_path / "allowed"
    allow_root.mkdir()
    plugin_root = tmp_path / "bundles"
    script_dir = plugin_root / "basic-skill-example" / "skills" / "basic-script-skill" / "scripts"
    script_dir.mkdir(parents=True)
    script = script_dir / "echo_payload.py"
    script.write_text("print('missing-skills-seg-ok')\n", encoding="utf-8")

    monkeypatch.setenv("MOTET_WORKER_EXEC_CWD_ALLOWLIST", str(allow_root))
    monkeypatch.setenv("MOTET_PLUGIN_ROOT", str(plugin_root))

    bad_abs = str(
        plugin_root / "basic-skill-example" / "basic-script-skill" / "scripts" / "echo_payload.py"
    )
    r = worker_run(
        {
            "bundle_id": "basic-skill-example",
            "argv": [sys.executable, bad_abs],
        }
    )
    assert r.get("returncode") == 0
    assert "missing-skills-seg-ok" in (r.get("stdout") or "")


def test_worker_exec_coerces_skill_shaped_bundle_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Skill id passed as bundle_id should stage from the bundle directory slug."""
    allow_root = tmp_path / "allowed"
    allow_root.mkdir()
    plugin_root = tmp_path / "bundles"
    script_dir = plugin_root / "basic-skill-example" / "skills" / "basic-script-skill" / "scripts"
    script_dir.mkdir(parents=True)
    script = script_dir / "echo_payload.py"
    script.write_text("print('coerced-bundle-ok')\n", encoding="utf-8")

    monkeypatch.setenv("MOTET_WORKER_EXEC_CWD_ALLOWLIST", str(allow_root))
    monkeypatch.setenv("MOTET_PLUGIN_ROOT", str(plugin_root))

    r = worker_run(
        {
            "bundle_id": "basic-skill-example.basic-script-skill",
            "argv": [sys.executable, "skills/basic-script-skill/scripts/echo_payload.py"],
        }
    )
    assert r.get("returncode") == 0
    assert "coerced-bundle-ok" in (r.get("stdout") or "")


def test_worker_exec_resolves_skill_md_relative_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """SKILL.md-relative paths (e.g. scripts/echo_payload.py) are resolved under skills/*/."""
    allow_root = tmp_path / "allowed"
    allow_root.mkdir()
    plugin_root = tmp_path / "bundles"
    script_dir = plugin_root / "basic-skill-example" / "skills" / "basic-script-skill" / "scripts"
    script_dir.mkdir(parents=True)
    script = script_dir / "echo_payload.py"
    script.write_text("print('skill-md-relative-ok')\n", encoding="utf-8")

    monkeypatch.setenv("MOTET_WORKER_EXEC_CWD_ALLOWLIST", str(allow_root))
    monkeypatch.setenv("MOTET_PLUGIN_ROOT", str(plugin_root))

    r = worker_run(
        {
            "bundle_id": "basic-skill-example",
            "argv": [sys.executable, "scripts/echo_payload.py"],
        }
    )
    assert r.get("returncode") == 0
    assert "skill-md-relative-ok" in (r.get("stdout") or "")


def test_worker_exec_rejects_skills_path_without_bundle_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """skills/ argv without bundle_id must fail early instead of running against empty cwd."""
    allow_root = tmp_path / "allowed"
    allow_root.mkdir()
    monkeypatch.setenv("MOTET_WORKER_EXEC_CWD_ALLOWLIST", str(allow_root))

    r = worker_run(
        {
            "argv": [sys.executable, "skills/basic-script-skill/scripts/echo_payload.py"],
        }
    )
    assert "error" in r
    assert "bundle_id" in (r.get("error") or "")


def test_worker_exec_bundle_id_requires_deployed_bundle(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    allow_root = tmp_path / "allowed"
    allow_root.mkdir()
    monkeypatch.setenv("MOTET_WORKER_EXEC_CWD_ALLOWLIST", str(allow_root))
    monkeypatch.setenv("MOTET_PLUGIN_ROOT", str(tmp_path / "bundles"))

    r = worker_run(
        {
            "bundle_id": "missing-bundle",
            "argv": [sys.executable, "skills/basic-script-skill/scripts/echo_payload.py"],
        }
    )
    assert "error" in r
    assert "bundle files not found" in (r.get("error") or "")


def test_run_execution_unknown_backend(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("MOTET_EXEC_BACKEND", "nonexistent")
    d = tmp_path / "x"
    d.mkdir()
    monkeypatch.setenv("MOTET_WORKER_EXEC_CWD_ALLOWLIST", str(tmp_path))
    res = run_execution(ExecutionRequest(argv=["true"], cwd=str(d)))
    assert res.error
    assert "unknown" in res.error.lower()


def test_worker_exec_includes_troubleshooting_docker_mocked(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    root = tmp_path / "r"
    root.mkdir()
    monkeypatch.setenv("MOTET_EXEC_BACKEND", "docker")
    monkeypatch.setenv("MOTET_WORKER_EXEC_CWD_ALLOWLIST", str(root))
    monkeypatch.setenv("MOTET_WORKER_EXEC_DOCKER_IMAGE", "alpine:3.19")

    cid = "aaaabbbbccccddddeeeeffffaaaabbbbccccddddeeeeffffaaaabbbb"

    def _fake_request(sock_path, method, path, body=None, headers=None):
        if method == "POST" and path.endswith("/containers/create"):
            return http.client.CREATED, json.dumps({"Id": cid}).encode()
        if method == "POST" and "/start" in path:
            return http.client.NO_CONTENT, b""
        if method == "POST" and "/wait" in path:
            return http.client.OK, json.dumps({"StatusCode": 42}).encode()
        if method == "GET" and "/logs" in path:
            return http.client.OK, b""
        if method == "DELETE":
            return http.client.NO_CONTENT, b""
        return 500, b"{}"

    monkeypatch.setattr(docker_client, "docker_request", _fake_request)
    r = worker_run({"argv": ["/bin/sh", "-c", "exit 0"]})
    assert r.get("returncode") == 42
    assert r.get("backend") == "docker"
    assert r.get("backend_ref") == cid[:12]
    assert r.get("oci_image_ref") == "alpine:3.19"
    assert r.get("engine_runtime") is None
    assert "worker_id" not in r


def test_worker_exec_docker_error_includes_meta(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    monkeypatch.setenv("MOTET_EXEC_BACKEND", "docker")
    monkeypatch.setenv("MOTET_WORKER_EXEC_CWD_ALLOWLIST", str(root))
    monkeypatch.setenv("MOTET_WORKER_EXEC_DOCKER_IMAGE", "python:3.11-slim")

    def _fake_request(sock_path, method, path, body=None, headers=None):
        if method == "POST" and path.endswith("/containers/create"):
            return 500, json.dumps({"message": "boom"}).encode()
        return 500, b"{}"

    monkeypatch.setattr(docker_client, "docker_request", _fake_request)
    r = worker_run({"argv": ["true"]})
    assert "error" in r
    meta = r.get("meta") or {}
    assert meta.get("backend") == "docker"
    assert meta.get("oci_image_ref") == "python:3.11-slim"
    assert "worker_id" not in meta

