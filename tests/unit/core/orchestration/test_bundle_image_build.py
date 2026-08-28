"""
Motet - Deployer-side OCI Image Build Orchestrator Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

ADR-0100 §"Deployer build orchestration" — unit tests for
``motet.core.bundles.bundle_image_build``.

These tests exercise the deployer-side OCI build orchestrator in isolation
(without invoking ``docker``), using a fake ``_DockerCliBackend`` so we can
pin behavior across the four operationally meaningful states:

1. **Disabled** — ``MOTET_DEPLOYER_BUILD_ENABLED=false`` → no-op, return None.
2. **Bundle pin wins** — operator declared ``oci_image_ref`` in
   ``config/exec.yaml`` → no build, no overwrite, return None.
3. **No requirements_path** — bundle has no install step → no build, no
   overwrite, return None.
4. **Happy path** — build + push + digest resolution all succeed → catalog
   ``exec.oci_image_ref`` rewritten to ``image@sha256:...``.
5. **Build/push/inspect failures** — surface as ``BundleImageBuildError`` so
   the publish path can fail loud (ADR-0100 §rule 1: no silent runtime ``pip
   install`` fallback).
6. **Mismatched ``requirements_sha256``** — catalog vs. build disagree → fail
   loud rather than ship a catalog row whose hash doesn't match the image.

We deliberately stub the backend rather than the subprocess module so the
backend ↔ subprocess seam is also exercised by the few tests that use the real
``_DockerCliBackend`` with a fake build script.
"""

from __future__ import annotations

import stat
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Dict
from unittest.mock import patch

import pytest

from motet.core.bundles.bundle_image_build import (
    BuildOutput,
    BundleImageBuildError,
    _DockerCliBackend,
    build_and_pin_exec_image,
)


# ---------------------------------------------------------------------------
# Fakes / helpers
# ---------------------------------------------------------------------------


@dataclass
class _CompletedStub:
    """Minimal stand-in for ``subprocess.CompletedProcess`` for the
    ``_DockerCliBackend._run`` patch — the backend only ever reads
    ``returncode`` / ``stdout`` / ``stderr`` so we don't need the full type."""

    returncode: int
    stdout: str
    stderr: str


class _FakeBackend:
    """Stand-in for ``_DockerCliBackend`` that records calls and returns canned
    output. Lets us test the orchestrator's branching without a docker daemon."""

    def __init__(self, output: BuildOutput | Exception) -> None:
        self._output = output
        self.calls: list[Dict[str, str]] = []

    def build_and_push(
        self,
        *,
        context_dir: Path,
        image_ref: str,
        base_image_ref: str | None = None,
        base_image_stack: str | None = None,
    ) -> BuildOutput:
        self.calls.append(
            {
                "context_dir": str(context_dir),
                "image_ref": image_ref,
                "base_image_ref": base_image_ref or "",
                "base_image_stack": base_image_stack or "",
            }
        )
        if isinstance(self._output, Exception):
            raise self._output
        return self._output


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip the deployer-build env vars between tests so a leaked
    MOTET_DEPLOYER_BUILD_REGISTRY from the developer's shell doesn't make
    the disabled/skip tests pass for the wrong reason."""
    for key in (
        "MOTET_DEPLOYER_BUILD_ENABLED",
        "MOTET_DEPLOYER_BUILD_REGISTRY",
        "MOTET_DEPLOYER_BUILD_TAG_PREFIX",
        "MOTET_DEPLOYER_BUILD_TIMEOUT_SECONDS",
        "MOTET_DEPLOYER_PUSH_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# Skip-path tests (no build subprocess invoked)
# ---------------------------------------------------------------------------


def test_disabled_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """``MOTET_DEPLOYER_BUILD_ENABLED=false`` MUST short-circuit before
    touching backend / requirements lookup. This is the SaaS opt-out path."""
    monkeypatch.setenv("MOTET_DEPLOYER_BUILD_ENABLED", "false")
    backend = _FakeBackend(
        BuildOutput(image_ref_with_digest="x@sha256:" + "a" * 64, requirements_sha256="b" * 64)
    )

    result = build_and_pin_exec_image(
        bundle_id="acme.demo",
        bundle_version="v1",
        bundle_files={"exec/requirements.txt": b"requests==2.31.0\n"},
        exec_meta={"requirements_path": "exec/requirements.txt"},
        backend=backend,  # type: ignore[arg-type]
    )

    assert result is None
    assert backend.calls == [], "Disabled path MUST NOT invoke the backend"


def test_no_exec_meta_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bundles with no config/exec.yaml have empty exec_meta → no-op."""
    monkeypatch.setenv("MOTET_DEPLOYER_BUILD_ENABLED", "true")
    monkeypatch.setenv("MOTET_DEPLOYER_BUILD_REGISTRY", "registry.example.com/acme")
    backend = _FakeBackend(BuildOutput(image_ref_with_digest="x", requirements_sha256="y"))

    assert (
        build_and_pin_exec_image(
            bundle_id="acme.demo",
            bundle_version="v1",
            bundle_files={},
            exec_meta=None,
            backend=backend,  # type: ignore[arg-type]
        )
        is None
    )
    assert (
        build_and_pin_exec_image(
            bundle_id="acme.demo",
            bundle_version="v1",
            bundle_files={},
            exec_meta={},
            backend=backend,  # type: ignore[arg-type]
        )
        is None
    )
    assert backend.calls == []


def test_bundle_pin_always_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR-0100 §rule 7: operator-supplied ``oci_image_ref`` is sacred. The
    orchestrator MUST NOT overwrite it even when enabled with a registry
    configured."""
    monkeypatch.setenv("MOTET_DEPLOYER_BUILD_ENABLED", "true")
    monkeypatch.setenv("MOTET_DEPLOYER_BUILD_REGISTRY", "registry.example.com/acme")
    backend = _FakeBackend(
        BuildOutput(image_ref_with_digest="bad@sha256:" + "0" * 64, requirements_sha256="0" * 64)
    )

    pinned_meta = {
        "oci_image_ref": "registry.example.com/acme/demo@sha256:" + "a" * 64,
        "requirements_path": "exec/requirements.txt",
    }
    result = build_and_pin_exec_image(
        bundle_id="acme.demo",
        bundle_version="v1",
        bundle_files={"exec/requirements.txt": b"x\n"},
        exec_meta=pinned_meta,
        backend=backend,  # type: ignore[arg-type]
    )

    assert result is None
    assert backend.calls == [], "Backend MUST NOT be invoked when bundle is already pinned"


def test_no_requirements_path_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bundle whose exec block declares only base_image_stack (no
    requirements.txt) has nothing to install → stack image is sufficient."""
    monkeypatch.setenv("MOTET_DEPLOYER_BUILD_ENABLED", "true")
    monkeypatch.setenv("MOTET_DEPLOYER_BUILD_REGISTRY", "registry.example.com/acme")
    backend = _FakeBackend(BuildOutput(image_ref_with_digest="x", requirements_sha256="y"))

    result = build_and_pin_exec_image(
        bundle_id="acme.demo",
        bundle_version="v1",
        bundle_files={},
        exec_meta={"base_image_stack": "python-minimal"},
        backend=backend,  # type: ignore[arg-type]
    )

    assert result is None
    assert backend.calls == []


# ---------------------------------------------------------------------------
# Failure / safety tests
# ---------------------------------------------------------------------------


def test_enabled_without_registry_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse to run a build with no destination registry — failing loud here
    is much friendlier than a confusing ``docker push`` error later."""
    monkeypatch.setenv("MOTET_DEPLOYER_BUILD_ENABLED", "true")
    backend = _FakeBackend(BuildOutput(image_ref_with_digest="x", requirements_sha256="y"))

    with pytest.raises(BundleImageBuildError) as exc:
        build_and_pin_exec_image(
            bundle_id="acme.demo",
            bundle_version="v1",
            bundle_files={"exec/requirements.txt": b"x\n"},
            exec_meta={"requirements_path": "exec/requirements.txt"},
            backend=backend,  # type: ignore[arg-type]
        )
    assert "MOTET_DEPLOYER_BUILD_REGISTRY" in str(exc.value)
    assert backend.calls == []


def test_unsafe_requirements_path_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defense-in-depth: lint should have caught these, but if anything
    slips through the orchestrator MUST NOT pass an absolute path or a
    ``../`` traversal to the build context layout."""
    monkeypatch.setenv("MOTET_DEPLOYER_BUILD_ENABLED", "true")
    monkeypatch.setenv("MOTET_DEPLOYER_BUILD_REGISTRY", "registry.example.com/acme")
    backend = _FakeBackend(BuildOutput(image_ref_with_digest="x", requirements_sha256="y"))

    for bad in ("/etc/passwd", "../../etc/shadow", "exec/../../leak.txt"):
        with pytest.raises(BundleImageBuildError):
            build_and_pin_exec_image(
                bundle_id="acme.demo",
                bundle_version="v1",
                bundle_files={bad: b"x"},
                exec_meta={"requirements_path": bad},
                backend=backend,  # type: ignore[arg-type]
            )


def test_missing_requirements_blob_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lint normally guarantees the file exists in the bundle; the
    orchestrator should still fail loud rather than silently skip."""
    monkeypatch.setenv("MOTET_DEPLOYER_BUILD_ENABLED", "true")
    monkeypatch.setenv("MOTET_DEPLOYER_BUILD_REGISTRY", "registry.example.com/acme")
    backend = _FakeBackend(BuildOutput(image_ref_with_digest="x", requirements_sha256="y"))

    with pytest.raises(BundleImageBuildError) as exc:
        build_and_pin_exec_image(
            bundle_id="acme.demo",
            bundle_version="v1",
            bundle_files={},
            exec_meta={"requirements_path": "exec/requirements.txt"},
            backend=backend,  # type: ignore[arg-type]
        )
    assert "not present in bundle files" in str(exc.value)


def test_backend_failure_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``BundleImageBuildError`` from the backend must surface unmodified
    so publish_bundle can wrap it as a hard publish failure."""
    monkeypatch.setenv("MOTET_DEPLOYER_BUILD_ENABLED", "true")
    monkeypatch.setenv("MOTET_DEPLOYER_BUILD_REGISTRY", "registry.example.com/acme")
    err = BundleImageBuildError("docker push exploded", details={"exit_code": 1})
    backend = _FakeBackend(err)

    with pytest.raises(BundleImageBuildError) as exc:
        build_and_pin_exec_image(
            bundle_id="acme.demo",
            bundle_version="v1",
            bundle_files={"exec/requirements.txt": b"requests==2.31.0\n"},
            exec_meta={"requirements_path": "exec/requirements.txt"},
            backend=backend,  # type: ignore[arg-type]
        )
    assert "docker push exploded" in str(exc.value)


def test_requirements_sha_mismatch_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Catalog hash and build hash MUST match. If they diverge something
    has gone badly wrong (concurrent edit, disk corruption) and shipping
    the image anyway would land a catalog row whose recorded
    ``requirements_sha256`` no longer matches the image's
    ``LABEL motet.bundle.exec.requirements.sha256``."""
    monkeypatch.setenv("MOTET_DEPLOYER_BUILD_ENABLED", "true")
    monkeypatch.setenv("MOTET_DEPLOYER_BUILD_REGISTRY", "registry.example.com/acme")
    backend = _FakeBackend(
        BuildOutput(
            image_ref_with_digest="registry.example.com/acme/demo@sha256:" + "a" * 64,
            requirements_sha256="d" * 64,  # ← built from different content
        )
    )

    with pytest.raises(BundleImageBuildError) as exc:
        build_and_pin_exec_image(
            bundle_id="acme.demo",
            bundle_version="v1",
            bundle_files={"exec/requirements.txt": b"requests==2.31.0\n"},
            exec_meta={
                "requirements_path": "exec/requirements.txt",
                "requirements_sha256": "c" * 64,  # ← catalog disagrees
            },
            backend=backend,  # type: ignore[arg-type]
        )
    assert "mismatch" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_returns_pinned_exec_meta(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end through the orchestrator with a stub backend: catalog
    ``exec`` block comes back with ``oci_image_ref`` rewritten to the
    pushed image's repo-digest, and other keys are preserved."""
    monkeypatch.setenv("MOTET_DEPLOYER_BUILD_ENABLED", "true")
    monkeypatch.setenv("MOTET_DEPLOYER_BUILD_REGISTRY", "registry.example.com/acme")
    monkeypatch.setenv("MOTET_DEPLOYER_BUILD_TAG_PREFIX", "motet-bundle-exec")

    pushed = "registry.example.com/acme/motet-bundle-exec-acme.demo@sha256:" + "a" * 64
    req_sha = "c" * 64
    backend = _FakeBackend(BuildOutput(image_ref_with_digest=pushed, requirements_sha256=req_sha))

    exec_meta_in = {
        "requirements_path": "exec/requirements.txt",
        "requirements_sha256": req_sha,
        "base_image_stack": "python-minimal",
    }
    result = build_and_pin_exec_image(
        bundle_id="acme.demo",
        bundle_version="abc1234",
        bundle_files={"exec/requirements.txt": b"requests==2.31.0\n"},
        exec_meta=exec_meta_in,
        backend=backend,  # type: ignore[arg-type]
    )

    assert result is not None
    assert result["oci_image_ref"] == pushed
    # Other keys preserved verbatim.
    assert result["requirements_path"] == "exec/requirements.txt"
    assert result["requirements_sha256"] == req_sha
    assert result["base_image_stack"] == "python-minimal"
    # Input MUST NOT be mutated.
    assert "oci_image_ref" not in exec_meta_in

    # Exactly one backend invocation; image_ref is composed from registry +
    # tag prefix + bundle_id + version.
    assert len(backend.calls) == 1
    expected_pretag = "registry.example.com/acme/motet-bundle-exec-acme.demo:abc1234"
    assert backend.calls[0]["image_ref"] == expected_pretag


def test_image_ref_normalizes_unsafe_bundle_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bundle IDs and versions can contain characters that aren't OCI-tag-
    safe (``/``, uppercase, etc). The orchestrator MUST normalize them so
    ``docker build -t`` doesn't fail with a parse error."""
    monkeypatch.setenv("MOTET_DEPLOYER_BUILD_ENABLED", "true")
    monkeypatch.setenv("MOTET_DEPLOYER_BUILD_REGISTRY", "registry.example.com/acme")

    # 1-to-1 character replacement (no run-collapse): "/", "::", "!" → "-", "--", "-"
    # then trailing "-" stripped, then lowercased. We don't pin the exact form
    # because the normalizer is a defense-in-depth step and the precise
    # output isn't part of the contract — what IS part of the contract is:
    #   * lowercased
    #   * no "/", ":", "!", "+", or other tag-illegal characters in the name
    #     segment past the registry
    #   * tag is non-empty and tag-safe
    pushed = "registry.example.com/acme/anything@sha256:" + "a" * 64
    backend = _FakeBackend(BuildOutput(image_ref_with_digest=pushed, requirements_sha256="c" * 64))

    result = build_and_pin_exec_image(
        bundle_id="Org/Team::Thing!",
        bundle_version="v1.2.3+build/42",
        bundle_files={"exec/requirements.txt": b"x\n"},
        exec_meta={"requirements_path": "exec/requirements.txt"},
        backend=backend,  # type: ignore[arg-type]
    )

    assert result is not None
    composed = backend.calls[0]["image_ref"]
    name, _, tag = composed.rpartition(":")
    # Strip the registry prefix to inspect the name segment we composed.
    assert name.startswith("registry.example.com/acme/")
    name_only = name[len("registry.example.com/acme/"):]
    # Lowercased, no tag-illegal characters.
    assert name_only == name_only.lower(), name_only
    for ch in ("/", ":", "!", "+", " "):
        assert ch not in name_only, f"Unsafe char {ch!r} survived in {name_only!r}"
    # Tag MUST NOT contain "+" or "/" either (OCI tag charset).
    assert "+" not in tag and "/" not in tag, tag
    # Tag is bounded.
    assert 0 < len(tag) <= 128


# ---------------------------------------------------------------------------
# Real _DockerCliBackend wired to a fake build script (no docker required)
# ---------------------------------------------------------------------------


def _write_fake_build_script(tmp_path: Path, *, requirements_sha: str, exit_code: int = 0) -> Path:
    """Write a stand-in for ``scripts/bundle_exec_docker_build.sh`` that
    behaves like the real one (echoes ``requirements_sha256=<hex>``) without
    invoking docker."""
    script = tmp_path / "fake_build.sh"
    script.write_text(
        textwrap.dedent(
            f"""
            #!/usr/bin/env bash
            set -euo pipefail
            echo "fake build CTX=$1 IMG=$2"
            echo "requirements_sha256={requirements_sha}"
            exit {exit_code}
            """
        ).lstrip()
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


def test_docker_cli_backend_parses_sha_and_resolves_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise the real ``_DockerCliBackend`` against a fake build script
    plus a stubbed ``subprocess.run`` for ``docker push`` / ``docker
    inspect``. Verifies:

    * requirements_sha256 is parsed out of stdout
    * RepoDigests JSON parsing picks the digest matching the pushed name
    * The composed BuildOutput is correct
    """
    req_sha = "e" * 64
    script = _write_fake_build_script(tmp_path, requirements_sha=req_sha)

    backend = _DockerCliBackend(build_script=script, build_timeout_s=30, push_timeout_s=30)
    image_ref = "registry.example.com/acme/motet-bundle-exec-acme.demo:v1"
    pushed_digest = (
        "registry.example.com/acme/motet-bundle-exec-acme.demo@sha256:" + "a" * 64
    )

    real_run = backend._run
    inspect_stdout = (
        '["other.registry/something@sha256:' + "0" * 64 + '","' + pushed_digest + '"]\n'
    )

    def fake_run(argv: list[str], *, timeout_s: int, env=None):  # type: ignore[no-untyped-def]
        if argv and argv[0] == str(script):
            return real_run(argv, timeout_s=timeout_s, env=env)
        if argv[:2] == ["docker", "push"]:
            return _CompletedStub(returncode=0, stdout="pushed\n", stderr="")
        if argv[:2] == ["docker", "inspect"]:
            return _CompletedStub(returncode=0, stdout=inspect_stdout, stderr="")
        raise AssertionError(f"Unexpected subprocess call: {argv}")

    with patch.object(backend, "_run", side_effect=fake_run):
        out = backend.build_and_push(context_dir=tmp_path, image_ref=image_ref)

    assert out.image_ref_with_digest == pushed_digest
    assert out.requirements_sha256 == req_sha


def test_docker_cli_backend_build_script_failure_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-zero exit from the build script MUST raise BundleImageBuildError
    with the exit code captured for diagnostics."""
    script = _write_fake_build_script(tmp_path, requirements_sha="x" * 64, exit_code=42)
    backend = _DockerCliBackend(build_script=script, build_timeout_s=30, push_timeout_s=30)

    with pytest.raises(BundleImageBuildError) as exc:
        backend.build_and_push(
            context_dir=tmp_path,
            image_ref="registry.example.com/acme/x:v1",
        )
    assert "exit code 42" in str(exc.value)
    assert exc.value.details.get("exit_code") == 42


def test_docker_cli_backend_inspect_no_matching_digest_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If RepoDigests doesn't contain a digest matching the pushed image's
    name, the push must have silently failed — fail loud rather than ship
    an empty/incorrect digest."""
    req_sha = "f" * 64
    script = _write_fake_build_script(tmp_path, requirements_sha=req_sha)
    backend = _DockerCliBackend(build_script=script, build_timeout_s=30, push_timeout_s=30)
    image_ref = "registry.example.com/acme/wanted:v1"

    real_run = backend._run
    inspect_stdout = '["other.registry/somethingelse@sha256:' + "0" * 64 + '"]\n'

    def fake_run(argv: list[str], *, timeout_s: int, env=None):  # type: ignore[no-untyped-def]
        if argv and argv[0] == str(script):
            return real_run(argv, timeout_s=timeout_s, env=env)
        if argv[:2] == ["docker", "push"]:
            return _CompletedStub(returncode=0, stdout="", stderr="")
        if argv[:2] == ["docker", "inspect"]:
            return _CompletedStub(returncode=0, stdout=inspect_stdout, stderr="")
        raise AssertionError(f"Unexpected subprocess call: {argv}")

    with patch.object(backend, "_run", side_effect=fake_run):
        with pytest.raises(BundleImageBuildError) as exc:
            backend.build_and_push(context_dir=tmp_path, image_ref=image_ref)
    assert "RepoDigest" in str(exc.value)


# ---------------------------------------------------------------------------
# ADR-0101 §"Platform-managed image stacks" — stack registry → backend wiring
# ---------------------------------------------------------------------------


def _enable_orchestrator(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOTET_DEPLOYER_BUILD_ENABLED", "true")
    monkeypatch.setenv("MOTET_DEPLOYER_BUILD_REGISTRY", "registry.example.com/acme")


def test_known_pinned_stack_forwarded_to_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the bundle declares a stack the registry knows AND has a pinned
    ref for, the orchestrator MUST pass the resolved ref to the backend so
    the docker build uses it as the FROM line."""
    _enable_orchestrator(monkeypatch)
    monkeypatch.setenv(
        "MOTET_IMAGE_STACK_PYTHON_OFFICE",
        "registry.example.com/motet/office@sha256:" + "c" * 64,
    )
    backend = _FakeBackend(
        BuildOutput(
            image_ref_with_digest="registry.example.com/acme/x@sha256:" + "d" * 64,
            requirements_sha256="e" * 64,
        )
    )

    result = build_and_pin_exec_image(
        bundle_id="acme.demo",
        bundle_version="v1",
        bundle_files={"exec/requirements.txt": b"requests==2.31.0\n"},
        exec_meta={
            "requirements_path": "exec/requirements.txt",
            "base_image_stack": "python-office",
        },
        backend=backend,  # type: ignore[arg-type]
    )

    assert result is not None
    assert len(backend.calls) == 1
    call = backend.calls[0]
    assert call["base_image_stack"] == "python-office"
    assert call["base_image_ref"].startswith("registry.example.com/motet/office@sha256:")


def test_unknown_stack_falls_back_to_dockerfile_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unknown stack name is a lint warning, not a build blocker. The
    orchestrator MUST proceed (passing no base_image_ref) so the Dockerfile's
    BASE_IMAGE default takes effect."""
    _enable_orchestrator(monkeypatch)
    backend = _FakeBackend(
        BuildOutput(
            image_ref_with_digest="registry.example.com/acme/x@sha256:" + "1" * 64,
            requirements_sha256="2" * 64,
        )
    )

    result = build_and_pin_exec_image(
        bundle_id="acme.demo",
        bundle_version="v1",
        bundle_files={"exec/requirements.txt": b""},
        exec_meta={
            "requirements_path": "exec/requirements.txt",
            "base_image_stack": "definitely-not-real",
        },
        backend=backend,  # type: ignore[arg-type]
    )

    assert result is not None
    call = backend.calls[0]
    assert call["base_image_stack"] == "definitely-not-real"  # passed through as label
    assert call["base_image_ref"] == "", "Unknown stack must NOT pin a base image ref"


def test_known_unpinned_stack_falls_back_to_dockerfile_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A registered-but-unpinned stack (e.g. python-office before operator
    sets the env var) is *also* a soft fallback — we don't fail the build
    because the catalog can still record the bundle's intent."""
    _enable_orchestrator(monkeypatch)
    # python-office is a builtin with empty oci_image_ref by default.
    backend = _FakeBackend(
        BuildOutput(
            image_ref_with_digest="registry.example.com/acme/x@sha256:" + "3" * 64,
            requirements_sha256="4" * 64,
        )
    )

    result = build_and_pin_exec_image(
        bundle_id="acme.demo",
        bundle_version="v1",
        bundle_files={"exec/requirements.txt": b""},
        exec_meta={
            "requirements_path": "exec/requirements.txt",
            "base_image_stack": "python-office",
        },
        backend=backend,  # type: ignore[arg-type]
    )

    assert result is not None
    call = backend.calls[0]
    assert call["base_image_stack"] == "python-office"
    assert call["base_image_ref"] == ""


def test_no_stack_declared_means_no_base_image_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bundles that don't set base_image_stack get the default behavior —
    no BASE_IMAGE override, no stack label."""
    _enable_orchestrator(monkeypatch)
    backend = _FakeBackend(
        BuildOutput(
            image_ref_with_digest="registry.example.com/acme/x@sha256:" + "5" * 64,
            requirements_sha256="6" * 64,
        )
    )

    result = build_and_pin_exec_image(
        bundle_id="acme.demo",
        bundle_version="v1",
        bundle_files={"exec/requirements.txt": b""},
        exec_meta={"requirements_path": "exec/requirements.txt"},
        backend=backend,  # type: ignore[arg-type]
    )

    assert result is not None
    call = backend.calls[0]
    assert call["base_image_stack"] == ""
    assert call["base_image_ref"] == ""
