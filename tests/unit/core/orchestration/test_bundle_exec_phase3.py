"""Phase 3 bundle exec catalog: config/exec.yaml, Redis merge for worker_exec."""

from __future__ import annotations

import json

from motet.core.execution.bundle_exec import merge_exec_catalog_into_request
from motet.core.execution.models import ExecutionRequest
from motet.core.bundles.deploy import (
    _extract_bundle_catalog,
    _lint_exec_bundle_paths,
    _lint_exec_config_file,
    _safe_exec_requirements_relative,
)


def test_extract_bundle_catalog_includes_exec_block() -> None:
    files = {
        "config/exec.yaml": (
            b"oci_image_ref: python:3.11-slim\n"
            b"base_image_stack: python-minimal\n"
            b'exec_artifact_digest: "sha256:abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"\n'
        ),
    }
    cat = _extract_bundle_catalog("acme.demo", files)
    assert cat.get("exec") == {
        "oci_image_ref": "python:3.11-slim",
        "base_image_stack": "python-minimal",
        "exec_artifact_digest": "sha256:abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
    }


def test_lint_exec_unknown_key_is_warning() -> None:
    errs = _lint_exec_config_file(
        "config/exec.yaml",
        "oci_image_ref: alpine\nunknown_thing: oops\n",
    )
    assert any(e.severity == "warning" and "unknown_thing" in e.message for e in errs)
    assert not any(e.severity == "error" for e in errs)


def test_lint_exec_non_mapping_error() -> None:
    errs = _lint_exec_config_file("config/exec.yaml", "[1, 2]\n")
    assert any(e.severity == "error" for e in errs)


def test_lint_exec_unknown_base_image_stack_is_warning() -> None:
    """ADR-0101 §"Platform-managed image stacks": setting base_image_stack
    to a name the registry doesn't know is a warning (operator may be
    rolling out a new stack), not an error."""
    errs = _lint_exec_config_file(
        "config/exec.yaml",
        "base_image_stack: not-a-real-stack\n",
    )
    assert any(
        e.severity == "warning" and "not-a-real-stack" in e.message and "MOTET_IMAGE_STACK_" in e.message
        for e in errs
    )
    assert not any(e.severity == "error" for e in errs)


def test_lint_exec_known_base_image_stack_is_silent() -> None:
    """A builtin stack name MUST NOT trigger the unknown-stack warning."""
    errs = _lint_exec_config_file(
        "config/exec.yaml",
        "base_image_stack: python-minimal\n",
    )
    # No warning about base_image_stack specifically; an "unknown_thing"-style
    # message would only appear if we accidentally rejected the key itself.
    assert not any(
        "base_image_stack" in e.message and e.severity == "warning"
        for e in errs
    )


def test_safe_exec_requirements_relative_rejects_parent() -> None:
    assert _safe_exec_requirements_relative("../secrets.txt") is None
    assert _safe_exec_requirements_relative("/etc/passwd") is None
    assert _safe_exec_requirements_relative("exec/req.txt") == "exec/req.txt"


def test_extract_exec_adds_requirements_sha256() -> None:
    files = {
        "config/exec.yaml": b"requirements_path: exec/deps.txt\n",
        "exec/deps.txt": b"requests==2.31.0\n",
    }
    cat = _extract_bundle_catalog("acme.demo", files)
    exec_block = cat.get("exec") or {}
    assert exec_block.get("requirements_path") == "exec/deps.txt"
    assert exec_block.get("requirements_sha256") == (
        "1d277ef3981a3e49b02912a0f03fe1ab563539d7e4e1b5c1e6404a57b19d883f"
    )


def test_lint_exec_bundle_paths_missing_file() -> None:
    files = {
        "config/exec.yaml": b"requirements_path: missing.txt\n",
    }
    errs = _lint_exec_bundle_paths(files)
    assert any("does not exist" in e.message for e in errs)
    assert any(e.severity == "error" for e in errs)


class _FakeRedis:
    def __init__(self, catalog: dict | None) -> None:
        self._catalog = catalog

    def get(self, key: str):  # noqa: ANN401
        if self._catalog is None:
            return None
        return json.dumps(self._catalog)


def test_merge_exec_catalog_fills_oci_image_ref() -> None:
    redis = _FakeRedis(
        {
            "bundle_id": "acme.demo",
            "exec": {"oci_image_ref": "my/img:tag"},
        }
    )
    req = ExecutionRequest(argv=["/bin/true"], cwd="/work", bundle_id="acme.demo")
    out = merge_exec_catalog_into_request(req, redis_client=redis)
    assert out.oci_image_ref == "my/img:tag"


def test_merge_exec_respects_explicit_request_image() -> None:
    redis = _FakeRedis({"exec": {"oci_image_ref": "from/catalog:1"}})
    req = ExecutionRequest(
        argv=["/bin/true"],
        cwd="/work",
        bundle_id="b",
        oci_image_ref="explicit:digest",
    )
    out = merge_exec_catalog_into_request(req, redis_client=redis)
    assert out.oci_image_ref == "explicit:digest"


def test_merge_no_bundle_id_noop() -> None:
    redis = _FakeRedis({"exec": {"oci_image_ref": "x"}})
    req = ExecutionRequest(argv=["true"], cwd="/work")
    out = merge_exec_catalog_into_request(req, redis_client=redis)
    assert out.oci_image_ref is None
