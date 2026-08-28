"""
Motet - Deployer-side OCI image build orchestration for bundle exec artifacts

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Background
----------
§"Normative rules" originally read "**In-worker OCI build is forbidden.**"
That rule is *correct for runtime workers* (which interpret LLM tool output and
run tenant code, so adding ``docker build`` to their capability surface would
collapse the Tier B/D isolation story). It is overstated for the **deployer
worker** — a platform-owned worker class with the ``DEPLOYMENT`` capability that
runs ``deploy``/``validate``/``publish``/``rollback``/``undeploy`` commands and
specifically does **not** dispatch tenant tool calls.

This module implements the "deployer-as-build-orchestrator" position
(documented as the 2026-04-21 amendment to): when explicitly enabled,
the deployer worker MAY build a bundle exec image at publish time, push it to
the configured registry, and write the resulting ``image@sha256:...`` ref into
the catalog ``exec`` block — so the runtime workers continue to "pull pinned
image, run argv" with no build capability at all.

Guardrails (intentionally conservative)
---------------------------------------
1. **Off by default at the call site, opt-in via env.** ``MOTET_DEPLOYER_BUILD_ENABLED``
   gates the entire path. (The current default is ``true`` for early adopters;
   that default is expected to flip to ``false`` once SaaS deployments become
   common — operators should opt in consciously.)
2. **Bundle pin always wins.** If the bundle's ``config/exec.yaml`` already
   declares ``oci_image_ref``, this module is a no-op. Operators retain the
"build elsewhere, point Motet at the digest" workflow that §rule 7
   originally mandated.
3. **Allowed-Dockerfile only.** We invoke the in-repo
   ``docker/images/bundle-exec-from-requirements/Dockerfile`` via the
   ``scripts/bundle_exec_docker_build.sh`` wrapper. Bundle-supplied Dockerfiles
   are NEVER executed — that would re-open the build-time tenant-code-escape
   the original ADR rule was guarding against.
4. **Pin by digest at the catalog.** After ``docker push`` we resolve the
   pushed digest via ``docker inspect`` and store ``image@sha256:...`` in the
   catalog. The mutable ``image:tag`` form is build/push convenience only and
   MUST NOT end up in the catalog row.
5. **Build failures are publish failures.** A failed build raises
   ``BundleImageBuildError`` which the publish path surfaces cleanly; we
   never half-publish a bundle whose declared dependencies could not be baked.
6. **Bounded subprocess.** Build/push subprocesses run with strict timeouts and
   captured-output caps so a misbehaving registry can't wedge the worker.

Future work (out of scope for v1)
---------------------------------
- BuildKit / kaniko (rootless, no host docker socket required) is the obvious
  next hardening step for SaaS deployments. The ``BuildBackend`` indirection
  in this module is intentionally narrow — swapping the docker-CLI backend for
  a BuildKit gRPC backend should not require touching the publish pipeline.
- Image scanning / signing (Trivy, cosign) hooks at the build-output stage.
- Future hardening of the stack ↔ digest resolution: today the registry
  resolves ``base_image_stack`` to whatever ref the operator pinned in
  ``MOTET_IMAGE_STACK_*`` (recommended ``image@sha256:...`` but not enforced
  here). A follow-up should validate the resolved ref is digest-pinned at
  registry-load time, and surface unpinned platform stacks in the ops UI as
  a "needs operator action" status.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import structlog

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Configuration surface (env-driven)
# ---------------------------------------------------------------------------

ENV_ENABLED = "MOTET_DEPLOYER_BUILD_ENABLED"
ENV_REGISTRY = "MOTET_DEPLOYER_BUILD_REGISTRY"
ENV_TAG_PREFIX = "MOTET_DEPLOYER_BUILD_TAG_PREFIX"
ENV_BUILD_TIMEOUT = "MOTET_DEPLOYER_BUILD_TIMEOUT_SECONDS"
ENV_PUSH_TIMEOUT = "MOTET_DEPLOYER_PUSH_TIMEOUT_SECONDS"

DEFAULT_BUILD_TIMEOUT_SECONDS = 600
DEFAULT_PUSH_TIMEOUT_SECONDS = 300
SUBPROCESS_OUTPUT_CAP_BYTES = 16 * 1024


def _enabled() -> bool:
    """Whether the deployer-side build orchestrator is active.

    Defaults to ``true`` to make early-stage Motet installs work without a
    separate CI pipeline. Operators standing up SaaS-shaped deployments should
    set ``MOTET_DEPLOYER_BUILD_ENABLED=false`` and front the build with their
    own CI (the original ADR-0100 §rule 7 story).
    """
    raw = os.environ.get(ENV_ENABLED, "true").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _registry() -> Optional[str]:
    raw = os.environ.get(ENV_REGISTRY, "").strip().rstrip("/")
    return raw or None


def _tag_prefix() -> str:
    return os.environ.get(ENV_TAG_PREFIX, "motet-bundle-exec").strip() or "motet-bundle-exec"


def _build_timeout() -> int:
    try:
        return max(1, int(os.environ.get(ENV_BUILD_TIMEOUT, str(DEFAULT_BUILD_TIMEOUT_SECONDS))))
    except ValueError:
        return DEFAULT_BUILD_TIMEOUT_SECONDS


def _push_timeout() -> int:
    try:
        return max(1, int(os.environ.get(ENV_PUSH_TIMEOUT, str(DEFAULT_PUSH_TIMEOUT_SECONDS))))
    except ValueError:
        return DEFAULT_PUSH_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class BundleImageBuildError(Exception):
    """Raised when the deployer-side build orchestrator cannot produce a pinned
    ``image@sha256:...`` ref. Callers in ``deploy.publish_bundle`` SHOULD wrap
    this in :class:`BundleDeployError` so it surfaces through the standard
    ADR-0029 error envelope. Half-publishing a bundle whose declared
    dependencies couldn't be baked would silently fall back to runtime
    ``pip install`` (banned by ADR-0100 §rule 1), so the failure must be hard.

    The optional ``details`` mapping mirrors the convention used by
    :class:`motet.core.bundles.deploy.BundleDeployError` so the
    wrapping site can pass it straight through.
    """

    def __init__(self, message: str, *, details: Optional[Dict[str, object]] = None) -> None:
        super().__init__(message)
        self.details = dict(details or {})


# ---------------------------------------------------------------------------
# Backend abstraction (so a future BuildKit/kaniko swap doesn't ripple)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BuildOutput:
    image_ref_with_digest: str  # canonical "registry/name@sha256:..."
    requirements_sha256: str    # echoed back from the build script for cross-check


class _DockerCliBackend:
    """Default backend: shells out to ``docker build``/``docker push`` via the
    in-repo ``scripts/bundle_exec_docker_build.sh``. Requires a reachable
    docker engine (host socket mount or DinD); see ADR-0100 §"Deployer build
    orchestration" for the security posture this implies."""

    def __init__(self, build_script: Path, build_timeout_s: int, push_timeout_s: int) -> None:
        self.build_script = build_script
        self.build_timeout_s = build_timeout_s
        self.push_timeout_s = push_timeout_s

    def _run(
        self,
        argv: list[str],
        *,
        timeout_s: int,
        env: Optional[Dict[str, str]] = None,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
                env=env,
            )
        except subprocess.TimeoutExpired as e:
            raise BundleImageBuildError(
                f"Subprocess {argv[0]!r} exceeded {timeout_s}s timeout",
                details={"argv": argv, "timeout_seconds": timeout_s, "stderr": str(e)[:SUBPROCESS_OUTPUT_CAP_BYTES]},
            ) from e
        except FileNotFoundError as e:
            raise BundleImageBuildError(
                f"Required binary not found on PATH: {argv[0]!r}",
                details={"argv": argv, "error": str(e)},
            ) from e

    def build_and_push(
        self,
        *,
        context_dir: Path,
        image_ref: str,
        base_image_ref: Optional[str] = None,
        base_image_stack: Optional[str] = None,
    ) -> BuildOutput:
        # --- build -----------------------------------------------------------
        build_cmd = [str(self.build_script), str(context_dir), image_ref]
        # ADR-0101 §"Platform-managed image stacks": when the caller resolved
        # the bundle's declared ``base_image_stack`` against the registry,
        # forward the chosen base image and stack name to the build script via
        # env vars (rather than positional argv, to keep the script's
        # backward-compatible 2-arg signature).
        env = dict(os.environ)
        if base_image_ref:
            env["MOTET_BUILD_BASE_IMAGE"] = base_image_ref
        if base_image_stack:
            env["MOTET_BUILD_BASE_IMAGE_STACK"] = base_image_stack
        logger.info(
            "deployer_build_start",
            argv=build_cmd,
            image_ref=image_ref,
            base_image_ref=base_image_ref,
            base_image_stack=base_image_stack,
        )
        proc = self._run(build_cmd, timeout_s=self.build_timeout_s, env=env)
        stdout_tail = (proc.stdout or "")[-SUBPROCESS_OUTPUT_CAP_BYTES:]
        stderr_tail = (proc.stderr or "")[-SUBPROCESS_OUTPUT_CAP_BYTES:]
        if proc.returncode != 0:
            raise BundleImageBuildError(
                f"Build script failed with exit code {proc.returncode}",
                details={
                    "image_ref": image_ref,
                    "exit_code": proc.returncode,
                    "stdout_tail": stdout_tail,
                    "stderr_tail": stderr_tail,
                },
            )

        # The build script echoes ``requirements_sha256=<hex>``; capture for
        # cross-check against the value Motet computes from bundle bytes.
        m = re.search(r"requirements_sha256=([0-9a-f]{64})", stdout_tail)
        if not m:
            raise BundleImageBuildError(
                "Build script did not emit requirements_sha256=<hex>",
                details={"image_ref": image_ref, "stdout_tail": stdout_tail},
            )
        req_sha = m.group(1)

        # --- push ------------------------------------------------------------
        push_cmd = ["docker", "push", image_ref]
        logger.info("deployer_push_start", argv=push_cmd)
        proc = self._run(push_cmd, timeout_s=self.push_timeout_s)
        if proc.returncode != 0:
            raise BundleImageBuildError(
                f"docker push {image_ref!r} failed with exit code {proc.returncode}",
                details={
                    "exit_code": proc.returncode,
                    "stdout_tail": (proc.stdout or "")[-SUBPROCESS_OUTPUT_CAP_BYTES:],
                    "stderr_tail": (proc.stderr or "")[-SUBPROCESS_OUTPUT_CAP_BYTES:],
                },
            )

        # --- resolve digest --------------------------------------------------
        # ``RepoDigests`` is populated *after* a successful push; pre-push it
        # is empty even though the local image exists. We pick the digest that
        # belongs to the registry/name we just pushed to (a single image can
        # have multiple repo-digest entries when retagged across registries).
        inspect_cmd = ["docker", "inspect", "--format={{json .RepoDigests}}", image_ref]
        proc = self._run(inspect_cmd, timeout_s=30)
        if proc.returncode != 0:
            raise BundleImageBuildError(
                f"docker inspect for digest resolution failed (exit {proc.returncode})",
                details={
                    "image_ref": image_ref,
                    "stderr_tail": (proc.stderr or "")[-SUBPROCESS_OUTPUT_CAP_BYTES:],
                },
            )
        # Output looks like: ["registry.example/foo@sha256:abcd...", ...]
        import json as _json
        try:
            repo_digests = _json.loads((proc.stdout or "").strip() or "[]")
        except Exception as e:
            raise BundleImageBuildError(
                f"Could not parse RepoDigests JSON from docker inspect: {e}",
                details={"image_ref": image_ref, "stdout": proc.stdout},
            ) from e
        # Match by the "name" portion of the ref we pushed (strip ":tag").
        name_only = image_ref.rsplit(":", 1)[0] if ":" in image_ref else image_ref
        matched = next(
            (d for d in (repo_digests or []) if isinstance(d, str) and d.startswith(name_only + "@sha256:")),
            None,
        )
        if not matched:
            raise BundleImageBuildError(
                "docker inspect returned no RepoDigest for the pushed image — "
                "push may have silently failed despite exit-0",
                details={"image_ref": image_ref, "repo_digests": repo_digests},
            )

        return BuildOutput(image_ref_with_digest=matched, requirements_sha256=req_sha)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def _project_root() -> Path:
    """Locate the repo root so we can reach ``scripts/bundle_exec_docker_build.sh``.

    This module lives at ``motet/core/bundles/bundle_image_build.py``, so the
    project root is three parents up. The index tracks this file's directory
    depth — moving this module to a different nesting level silently resolves
    to the wrong root, surfacing as "Build script not found".
    """
    return Path(__file__).resolve().parents[3]


def _normalize_tag_segment(s: str) -> str:
    """Make a string OCI-tag-safe (alnum, ``_``, ``.``, ``-``; max 128 chars)."""
    safe = re.sub(r"[^A-Za-z0-9_.\-]", "-", s).strip("-.")
    return (safe or "untagged")[:128]


def _resolve_image_ref(*, registry: str, bundle_id: str, bundle_version: str) -> str:
    """Compose ``{registry}/{prefix}-{bundle_id}:{version}`` (tag form, pre-digest)."""
    name = _normalize_tag_segment(f"{_tag_prefix()}-{bundle_id}").lower()
    tag = _normalize_tag_segment(bundle_version) or "latest"
    return f"{registry}/{name}:{tag}"


def build_and_pin_exec_image(
    *,
    bundle_id: str,
    bundle_version: str,
    bundle_files: Dict[str, bytes],
    exec_meta: Optional[Dict[str, Any]],
    backend: Optional[_DockerCliBackend] = None,
) -> Optional[Dict[str, Any]]:
    """Build the bundle exec image (if applicable) and return an updated
    ``exec_meta`` dict with ``oci_image_ref`` set to ``image@sha256:...``.

    Returns ``None`` (i.e. *no change*) when:

    - The orchestrator is disabled (``MOTET_DEPLOYER_BUILD_ENABLED=false``).
    - The bundle did not declare ``config/exec.yaml`` (``exec_meta`` is empty).
    - The bundle declared ``oci_image_ref`` itself — operator pin always wins
      (ADR-0100 §rule 7).
    - The bundle did not declare ``requirements_path`` — there is nothing to
      install, so the platform image stack (resolved by the ADR-0101 stack
      registry) is sufficient on its own.

    When the bundle declares ``base_image_stack`` and the registry knows
    about it with a pinned ref, that ref is used as the Dockerfile FROM
    line. Unknown or unpinned stacks log a warning and fall back to the
    Dockerfile default (``python:3.11-slim``); we don't fail the build,
    because the validate-time lint already surfaces the misconfiguration
    and producing *some* image is better than blocking the publish.

    Raises :class:`BundleImageBuildError` when the build/push/digest-resolution
    pipeline fails. Callers MUST treat this as a publish failure (do not fall
    back to runtime ``pip install``).
    """
    if not _enabled():
        logger.debug("deployer_build_skipped_disabled", bundle_id=bundle_id)
        return None

    if not exec_meta:
        logger.debug("deployer_build_skipped_no_exec_meta", bundle_id=bundle_id)
        return None

    if exec_meta.get("oci_image_ref"):
        logger.info(
            "deployer_build_skipped_bundle_pinned",
            bundle_id=bundle_id,
            oci_image_ref=exec_meta["oci_image_ref"],
        )
        return None

    requirements_path = exec_meta.get("requirements_path")
    if not requirements_path:
        logger.debug(
            "deployer_build_skipped_no_requirements",
            bundle_id=bundle_id,
        )
        return None

    # Defensive — lint should have caught these, but we don't want a build
    # crash to look like an arbitrary subprocess failure.
    if requirements_path.startswith("/") or ".." in requirements_path.split("/"):
        raise BundleImageBuildError(
            "Unsafe requirements_path rejected at build time",
            details={"bundle_id": bundle_id, "requirements_path": requirements_path},
        )
    req_blob = bundle_files.get(requirements_path)
    if req_blob is None:
        raise BundleImageBuildError(
            f"requirements_path {requirements_path!r} not present in bundle files",
            details={"bundle_id": bundle_id, "requirements_path": requirements_path},
        )

    registry = _registry()
    if not registry:
        raise BundleImageBuildError(
            f"Deployer build is enabled but {ENV_REGISTRY} is not set — refusing "
            "to build an image with no destination registry",
            details={"bundle_id": bundle_id, "env_var": ENV_REGISTRY},
        )

    image_ref = _resolve_image_ref(
        registry=registry,
        bundle_id=bundle_id,
        bundle_version=bundle_version,
    )

    project_root = _project_root()
    build_script = project_root / "scripts" / "bundle_exec_docker_build.sh"
    if not build_script.exists():
        raise BundleImageBuildError(
            f"Build script not found at expected path: {build_script}",
            details={"bundle_id": bundle_id, "build_script": str(build_script)},
        )

    if backend is None:
        backend = _DockerCliBackend(
            build_script=build_script,
            build_timeout_s=_build_timeout(),
            push_timeout_s=_push_timeout(),
        )

    # ADR-0101 §"Platform-managed image stacks": resolve the bundle's declared
    # stack against the platform registry. Unknown / unpinned → log and let
    # the Dockerfile default (python:3.11-slim) take over. The lint at
    # validate time has already raised a warning for the unknown case.
    declared_stack = str(exec_meta.get("base_image_stack") or "").strip() or None
    runtime_capabilities = exec_meta.get("runtime_capabilities")
    base_image_ref: Optional[str] = None
    if declared_stack is not None:
        try:
            from motet.core.execution.image_stacks import resolve_image_stack

            stack = resolve_image_stack(declared_stack)
            if stack is None:
                logger.warning(
                    "deployer_build_unknown_stack",
                    bundle_id=bundle_id,
                    base_image_stack=declared_stack,
                    fallback="dockerfile_default",
                )
            elif not stack.is_pinned:
                logger.warning(
                    "deployer_build_unpinned_stack",
                    bundle_id=bundle_id,
                    base_image_stack=declared_stack,
                    fallback="dockerfile_default",
                )
            else:
                base_image_ref = stack.oci_image_ref
        except Exception as e:  # registry import/load shouldn't block builds
            logger.warning(
                "deployer_build_stack_resolve_failed",
                bundle_id=bundle_id,
                base_image_stack=declared_stack,
                error=str(e),
            )
    elif isinstance(runtime_capabilities, list) and runtime_capabilities:
        try:
            from motet.core.execution.image_stacks import resolve_image_stack_for_capabilities

            resolution = resolve_image_stack_for_capabilities([str(c) for c in runtime_capabilities])
            if resolution.matched and resolution.stack is not None:
                base_image_ref = resolution.stack.oci_image_ref
                declared_stack = resolution.stack.name
                logger.info(
                    "deployer_build_stack_resolved_from_capabilities",
                    bundle_id=bundle_id,
                    image_stack=resolution.stack.name,
                    runtime_capabilities=runtime_capabilities,
                )
            else:
                logger.warning(
                    "deployer_build_capability_stack_unresolved",
                    bundle_id=bundle_id,
                    runtime_capabilities=runtime_capabilities,
                    missing_capabilities=resolution.missing_capabilities,
                    fallback="dockerfile_default",
                )
        except Exception as e:
            logger.warning(
                "deployer_build_capability_stack_resolve_failed",
                bundle_id=bundle_id,
                runtime_capabilities=runtime_capabilities,
                error=str(e),
                fallback="dockerfile_default",
            )

    # Materialize requirements.txt at the root of an isolated build context.
    # The wrapper script expects requirements.txt at ``${CTX}/requirements.txt``
    # (see ``scripts/bundle_exec_docker_build.sh``). We do NOT copy other bundle
    # files — the in-repo Dockerfile only needs requirements.txt, and limiting
    # the context shrinks attack surface (no stray bundle scripts ending up
    # in the build cache).
    with tempfile.TemporaryDirectory(prefix=f"motet-build-{_normalize_tag_segment(bundle_id)}-") as tmp:
        ctx = Path(tmp)
        (ctx / "requirements.txt").write_bytes(req_blob)
        try:
            output = backend.build_and_push(
                context_dir=ctx,
                image_ref=image_ref,
                base_image_ref=base_image_ref,
                base_image_stack=declared_stack,
            )
        finally:
            # Best-effort local cleanup — keep the registry copy, drop the
            # local layer cache entry to keep the deployer-worker disk bounded.
            shutil.rmtree(ctx, ignore_errors=True)

    # Cross-check the digest hash the build computed against what Motet
    # computed at validate-time. If they diverge something is very wrong
    # (concurrent edit, disk corruption, etc.) — fail loud rather than
    # silently shipping an image whose hash doesn't match the catalog.
    expected_sha = exec_meta.get("requirements_sha256")
    if expected_sha and expected_sha != output.requirements_sha256:
        raise BundleImageBuildError(
            "requirements_sha256 mismatch between catalog and built image",
            details={
                "bundle_id": bundle_id,
                "catalog_sha256": expected_sha,
                "build_sha256": output.requirements_sha256,
            },
        )

    updated = dict(exec_meta)
    updated["oci_image_ref"] = output.image_ref_with_digest
    logger.info(
        "deployer_build_complete",
        bundle_id=bundle_id,
        bundle_version=bundle_version,
        oci_image_ref=output.image_ref_with_digest,
        requirements_sha256=output.requirements_sha256,
    )
    return updated
