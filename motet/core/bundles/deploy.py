"""
Motet - Bundle Deployment Commands

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    Distributed commands for the bundle-based deployment pipeline.
    All commands in this module require WorkerCapability.DEPLOYMENT and run
    exclusively on the dedicated deployer worker (worker-lcm
    Docker service with DEPLOYMENT capability added to its queue).

    Command hierarchy:
    - core.deploy_bundle:   Top-level orchestrator (fetch → validate → publish → reload)
    - core.deploy_bundle_upload: Deploy from uploaded zip (no git; lint → publish → reload)
    - core.validate_bundle_upload: Validate (lint-only) uploaded zip; streams SSE lint events
    - core.validate_bundle: Fetch from git + run lint gate; streams SSE lint events
    - core.publish_bundle:  Write signed artifact to shared store; dispatch reload to workers
    - core.hot_deploy_bundle: Dev-only shared-path hot deploy (no artifact publish)
    - core.undeploy_bundle: Dispatch unload_bundle to targeted AI workers; cancel schedules
    - core.rollback_bundle: Re-publish a stored prior artifact (skip fetch + lint)
    - core.propagate_bundle: Retry reload on failed/skipped workers for already-published bundle

    The AI-worker commands (core.reload_bundle, core.unload_bundle) live in bundle_reload.py.

    Catalog extraction includes ``command_capabilities`` (per-command required
    capability values from ``@motet.command``) so API dispatch can apply
    CapabilityFilter for edge-bound bundle commands, and ``command_descriptions``
    (decorator ``description=`` or first docstring line) so the manage/API
    command list can show discovery prose without loading bundles in-process.
    ``command_schemas`` (Pydantic JSON Schema) are harvested from AI-worker
    reload acks after import succeeds and merged into the Redis catalog —
    deployer AST extract cannot produce full schemas, and worker-lcm often
    lacks bundle third-party deps.
    Bundle ``config/surfaces.yaml`` entries are validated at publish time and
    registered into the surfaces catalog with register_if_absent (existing
    surfaces are left unchanged).

Dependencies:
    - motet.core.commands.decorator / motet: @motet.command, get_motet_context
    - motet.core.commands.distributed: WorkerCapability
    - BundleDeployError: Local exception class for structured deploy failures
    - motet.core.distributed.worker_readiness: Worker heartbeat / live-worker detection
    - pydantic: Data validation
    - motet.core.bundles.bundle_lint: AST/YAML/skills/exec lint helpers (issue #158)
    - hashlib: Source fingerprint and bundle artifact hashing
    - tarfile: Bundle artifact packaging (tar.gz in memory)

Usage:
    from motet.core.bundles.deploy import (
        deploy_bundle, validate_bundle, publish_bundle,
        undeploy_bundle, rollback_bundle, propagate_bundle,
        DeployBundleData, BundleTargeting, BundleDeployStatus,
    )

Notes:
    - Lint/AST helpers extracted to bundle_lint.py (issue #158).
    - All commands require WorkerCapability.DEPLOYMENT.
    - Artifact store backend is selected by MOTET_BUNDLE_STORE=redis (default) | s3.
    - Artifact size cap: 50 MB warning, 100 MB hard limit at publish time.
    - Artifact retention: last MOTET_BUNDLE_ARTIFACT_RETENTION (default 5) versions per bundle.
    - Bundle registry stored under Redis keys bundle:{bundle_id}:registry (hash).
    - Latest-version pointer: bundle:{bundle_id}:latest (string).
    - Deploy history stored under bundle:{bundle_id}:history (list, JSON entries).
    - Content catalog bundle:{bundle_id}:catalog may include ``exec`` from optional config/exec.yaml (Phase 3).
"""

from __future__ import annotations

import ast
import hashlib
import base64
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from motet import motet
from motet.core.workers.concurrency_primitives import worker_run_subprocess

import structlog
from pydantic import BaseModel, Field

from motet.core.commands.base_command_data import BaseCommandData
from motet.core.commands.decorator import get_motet_context
from motet.core.commands.capabilities import WorkerCapability

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Config constants
# ---------------------------------------------------------------------------

ARTIFACT_RETENTION: int = int(os.getenv("MOTET_BUNDLE_ARTIFACT_RETENTION", "5"))
ARTIFACT_STORE_BACKEND: str = os.getenv("MOTET_BUNDLE_STORE", "redis")
BUNDLE_ARTIFACT_TTL: int = 7 * 24 * 3600  # 7 days in seconds
BUNDLE_SIZE_WARN_BYTES: int = 50 * 1024 * 1024   # 50 MB
BUNDLE_SIZE_LIMIT_BYTES: int = 100 * 1024 * 1024  # 100 MB
WORKER_HEARTBEAT_STALE_SECS: int = int(os.getenv("MOTET_WORKER_HEARTBEAT_STALE_SECS", "60"))
RESERVED_BUNDLE_NAMES: set[str] = {"core", "admin", "motet_admin", "motet-admin", "internal", "reserved", "system"}

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class BundleDeployError(Exception):
    """
    Raised by distributed deploy commands (validate_bundle, publish_bundle, etc.)
    to signal a user-visible failure with optional structured details.

    The decorator's _extract_error_details() method automatically surfaces any
    instance attributes (including `details`) into the ADR-0029 error envelope,
    making them available to callers via motet.do() / _extract_data().

    Args:
        message: Human-readable description of the failure.
        details: Optional dict of structured diagnostic data (e.g. lint_errors).
    """

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.details: Dict[str, Any] = details or {}


# ---------------------------------------------------------------------------
# Enums and data models
# ---------------------------------------------------------------------------


class BundleDeployStatus(str, Enum):
    """Overall deploy status values for a bundle deploy job."""
    PUBLISHING = "publishing"
    PROPAGATING = "propagating"
    COMPLETE = "complete"
    NO_CHANGE = "no_change"
    DEGRADED = "degraded"
    FAILED = "failed"


class BundleTargeting(BaseModel):
    """
    Bundle targeting selector — controls which workers load the bundle
    (worker_tags) and which request contexts can see it (motet_ids, tenant_ids).
    """
    worker_ids: List[str] = Field(
        default_factory=list,
        description="Specific worker IDs to target; empty = match by tags or all",
        json_schema_extra={"example": ["agent-worker-1"]},
    )
    worker_tags: List[str] = Field(
        default_factory=list,
        description="Workers must have all listed capability tags to receive this bundle",
        json_schema_extra={"example": ["gpu"]},
    )
    motet_ids: List[str] = Field(
        default_factory=list,
        description="Bundle artifacts visible only within these motet IDs; empty = global",
        json_schema_extra={"example": ["sales", "sales-demo"]},
    )
    tenant_ids: List[str] = Field(
        default_factory=list,
        description="Bundle artifacts visible only for these tenant IDs; empty = all tenants",
        json_schema_extra={"example": []},
    )


class DeployBundleData(BaseCommandData):
    """
    Input data for core.deploy_bundle.

    The bundle_id is NOT supplied by the caller — it is read from the manifest
    'name' field by core.validate_bundle and propagated from there.

    The deployment conversation_id (prefix 'deploy:<uuid>') is passed as the
    command's execution context conversation_id by the API, not as a data field.
    It propagates automatically to all child commands via motet.do() / motet.apply().
    CI/CD callers omit conversation_id (set it to "") — no persona is invoked.
    """

    repo_url: str = Field(..., description="Git repository URL", json_schema_extra={"example": "https://github.com/org/repo"})
    branch: str = Field(
        ...,
        description="Branch, tag, or commit SHA to deploy (prefer tags/SHAs for reproducibility)",
        json_schema_extra={"example": "main"},
    )
    path: str = Field(
        ...,
        description="Path within repo conforming to worker install format",
        json_schema_extra={"example": "extensions/sales"},
    )
    targeting: Optional[BundleTargeting] = Field(
        None,
        description="Worker/motet/tenant selector; None = global (all workers, all motets)",
    )
    repo_creds_path: Optional[str] = Field(
        None,
        description="Vault path for private repo credentials (SSH key or token)",
        json_schema_extra={"example": "vault://deploy/github-token"},
    )


class HotDeployBundleData(BaseCommandData):
    """
    Input data for core.hot_deploy_bundle (dev-only local path hot reload).

    This command is intended for local Docker development where workers share a
    mounted bundle source path. It does not publish artifacts or create rollback
    history; it dispatches hot_reload_bundle directly to target workers.
    """

    bundle_path: str = Field(
        ...,
        description="Shared local filesystem path to bundle root (as seen by workers)",
        json_schema_extra={"example": "/app/motet-sdk/examples/bundles/hello-world"},
    )
    targeting: Optional[BundleTargeting] = Field(
        None,
        description="Worker/motet/tenant selector; None = global",
    )
    lint: bool = Field(
        default=False,
        description="Run lint gate before dispatching worker hot reload (default false for speed)",
    )


class ValidateBundleData(BaseCommandData):
    """Input data for core.validate_bundle (called by deploy_bundle and validate-only endpoint)."""

    repo_url: str = Field(..., description="Git repository URL")
    branch: str = Field(..., description="Branch, tag, or commit SHA")
    path: str = Field(..., description="Path within repo")
    targeting: Optional[BundleTargeting] = Field(None)
    repo_creds_path: Optional[str] = Field(None)


class PublishBundleData(BaseCommandData):
    """Input data for core.publish_bundle (called internally by deploy_bundle)."""

    bundle_id: str = Field(..., description="Bundle slug (from manifest 'name')")
    bundle_version: str = Field(..., description="Git tree SHA (content fingerprint)")
    bundle_ref: str = Field(..., description="Resolved git commit SHA")
    source_fingerprint: str = Field(..., description="sha256(repo_url + '#' + path)")
    artifact_b64: str = Field(..., description="Base64-encoded tar.gz artifact bytes")
    targeting: Optional[BundleTargeting] = Field(None)
    manifest_version: Optional[str] = Field(None, description="Semver from manifest (display only)")
    catalog: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Bundle content catalog extracted by validate_bundle (commands, tools, workflows, agents, mcp_servers, model_ids)",
    )


class UndeployBundleData(BaseCommandData):
    """Input data for core.undeploy_bundle."""

    bundle_id: str = Field(..., description="Bundle slug (manifest name)")


class RollbackBundleData(BaseCommandData):
    """Input data for core.rollback_bundle."""

    bundle_id: str = Field(..., description="Bundle slug")
    bundle_version: str = Field(..., description="Git tree SHA of the version to restore")


class PropagateBundleData(BaseCommandData):
    """Input data for core.propagate_bundle (retry failed/skipped workers)."""

    bundle_id: str = Field(..., description="Bundle slug")


class DeployBundleUploadData(BaseCommandData):
    """Input data for core.deploy_bundle_upload (deploy from uploaded zip)."""

    zip_b64: str = Field(..., description="Base64-encoded zip of the bundle directory (manifest.yaml at root)")
    targeting: Optional[BundleTargeting] = Field(None, description="Worker/motet/tenant selector")


class ValidateBundleUploadData(BaseCommandData):
    """Input data for core.validate_bundle_upload (lint-only for uploaded zip; streams SSE)."""

    zip_b64: str = Field(..., description="Base64-encoded zip of the bundle directory (manifest.yaml at root)")



# Lint helpers live in bundle_lint.py (issue #158); re-exported for call sites.
from motet.core.bundles.bundle_lint import (  # noqa: E402
    DANGEROUS_IMPORTS,
    LintError,
    THREADING_ANTIPATTERNS,
    _ADHOC_IDENTITY_RE,
    _COMMON_LOCAL_IMPORTS,
    _ENV_REQUIRE_DIGEST_PINNED_PUBLISH,
    _EXEC_CONFIG_ALLOWED_KEYS,
    _HOST_ABSOLUTE_PATH_RE,
    _MARKDOWN_LINK_RE,
    _RUNTIME_CAPABILITY_RE,
    _SCRIPT_REF_RE,
    _STD_LIB_MODULES,
    _SYSTEM_PRINCIPAL_RE,
    _THREADING_RE,
    _collect_lint_errors,
    _emit_lint_failure_events,
    _enrich_exec_meta_requirements_sha,
    _fatal_lint_errors,
    _get_decorator_qualified_names,
    _is_external_markdown_link,
    _is_motet_tool,
    _is_skill_runners_file,
    _is_skill_script_usage_file,
    _line_for_offset,
    _lint_bundle,
    _lint_exec_bundle_paths,
    _lint_exec_config_file,
    _lint_python_file,
    _lint_reserved_bundle_name,
    _lint_runner_script_paths,
    _lint_runners_file,
    _lint_script_usage_file,
    _lint_script_usage_paths,
    _lint_skill_markdown_file,
    _lint_skill_portability,
    _lint_yaml_file,
    _manifest_file_name,
    _normalize_exec_config_block,
    _normalize_runtime_capabilities,
    _safe_exec_requirements_relative,
    _safe_skill_relative_path,
    _third_party_imports,
)

# ---------------------------------------------------------------------------
# Publish digest pinning (ADR-0100) — stays with deploy/publish path
# ---------------------------------------------------------------------------
def _digest_pinning_enforced() -> bool:
    raw = os.environ.get(_ENV_REQUIRE_DIGEST_PINNED_PUBLISH, "false").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _is_oci_ref_digest_pinned(ref: str) -> bool:
    """Return True iff ``ref`` is of the form ``name@sha256:<64hex>`` (ADR-0100 §rule 2).

    Empty / whitespace-only / unset refs return True (caller decides whether
    absence is allowed — e.g. a tier-only bundle has no concrete ref to pin).
    """
    ref = (ref or "").strip()
    if not ref:
        return True
    if "@sha256:" not in ref:
        return False
    digest = ref.split("@sha256:", 1)[1]
    return len(digest) == 64 and all(c in "0123456789abcdef" for c in digest.lower())


def _enforce_publish_digest_pinning(bundle_id: str, exec_meta: Dict[str, Any]) -> None:
    """Hard-fail publish when enforcement is on and ``exec.oci_image_ref`` is
    set to a mutable tag. Called *after* the deployer build hook so a deployer
    rewrite of a bundle-supplied mutable tag still passes — this only catches
    operators who:

    1. Bundle-pinned with a mutable tag (deployer build is a no-op when bundle
       declares any ``oci_image_ref``).
    2. Have the deployer build disabled and pre-pinned the catalog row out of band.

    A bundle that declared no ``oci_image_ref`` at all (e.g. tier-only) is
    NOT rejected here — the worker_exec / merge path handles the empty case
    according to the worker's own backend rules.
    """
    if not _digest_pinning_enforced():
        return
    ref = (exec_meta or {}).get("oci_image_ref", "") or ""
    ref = ref.strip()
    if not ref:
        return
    if _is_oci_ref_digest_pinned(ref):
        return
    raise BundleDeployError(
        "Publish rejected: catalog.exec.oci_image_ref must be digest-pinned "
        "(name@sha256:<64hex>) when MOTET_REQUIRE_DIGEST_PINNED_PUBLISH=true. "
        "Either pin the bundle to a digest or enable the deployer-side build "
        "(MOTET_DEPLOYER_BUILD_ENABLED=true) so Motet pins it for you.",
        details={
            "bundle_id": bundle_id,
            "oci_image_ref": ref,
            "rule": "ADR-0100 §rule 2",
        },
    )

# ---------------------------------------------------------------------------
# Git fetch helpers
# ---------------------------------------------------------------------------

def _resolve_ref_and_tree_sha(
    repo_url: str,
    ref: str,
    path: str,
    credentials: Optional[str] = None,
) -> Tuple[str, str]:
    """
    Resolve a git ref to (bundle_ref, bundle_version) without fetching file content.

    bundle_ref    = resolved commit SHA
    bundle_version = git tree SHA for the bundle path at that commit

    Uses a shallow clone + ls-tree for efficiency.
    Returns ("", "") if the lightweight check is unavailable.

    For file:// URLs the commit SHA is read directly from .git/HEAD and
    .git/refs/ without spawning a subprocess — subprocess.run(git ...) deadlocks
    under gevent pools.
    """
    # --- file:// fast-path: read .git metadata directly, no subprocess ---
    if repo_url.startswith("file://"):
        try:
            repo_dir = Path(repo_url[len("file://"):])
            git_dir = repo_dir / ".git"
            head_file = git_dir / "HEAD"
            if not head_file.exists():
                return "", ""
            head_content = head_file.read_text().strip()
            if head_content.startswith("ref: "):
                # Symbolic ref — resolve to commit SHA
                ref_path = git_dir / head_content[len("ref: "):]
                commit_sha = ref_path.read_text().strip() if ref_path.exists() else ""
            else:
                # Detached HEAD — content is already the SHA
                commit_sha = head_content
            return commit_sha, commit_sha
        except Exception as e:
            logger.warning("git_resolve_file_url_error", repo_url=repo_url, error=str(e))
            return "", ""

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            env = _git_env(credentials)
            # Shallow fetch just the ref — uses gevent.subprocess on gevent workers
            worker_run_subprocess(
                ["git", "clone", "--depth=1", "--branch", ref, "--no-checkout", repo_url, tmpdir],
                capture_output=True, check=True, env=env, timeout=60,
            )
            # Get commit SHA
            commit_result = worker_run_subprocess(
                ["git", "rev-parse", "HEAD"],
                capture_output=True, text=True, check=True, cwd=tmpdir, env=env, timeout=10,
            )
            bundle_ref = commit_result.stdout.strip()
            # Get tree SHA for the path
            tree_result = worker_run_subprocess(
                ["git", "ls-tree", "HEAD", path or "."],
                capture_output=True, text=True, check=False, cwd=tmpdir, env=env, timeout=10,
            )
            if tree_result.returncode == 0 and tree_result.stdout.strip():
                # Format: <mode> <type> <sha>\t<path>
                parts = tree_result.stdout.strip().split()
                if len(parts) >= 3:
                    bundle_version = parts[2]
                    return bundle_ref, bundle_version
            # Fall back: use commit SHA as tree proxy
            return bundle_ref, bundle_ref
    except subprocess.TimeoutExpired:
        logger.warning("git_resolve_timeout", repo_url=repo_url, ref=ref)
        return "", ""
    except subprocess.CalledProcessError as e:
        logger.warning("git_resolve_failed", repo_url=repo_url, ref=ref, stderr=e.stderr)
        return "", ""
    except Exception as e:
        logger.warning("git_resolve_error", repo_url=repo_url, ref=ref, error=str(e))
        return "", ""


def _fetch_bundle_files(
    repo_url: str,
    ref: str,
    path: str,
    credentials: Optional[str] = None,
) -> Dict[str, bytes]:
    """
    Fetch bundle files from a git repo (via clone) or local directory (file:// fallback).
    Returns {relative_path: bytes} dict.

    For file:// URLs, if git is unavailable, falls back to reading the local directory
    directly. This supports development workflows where workers have the workspace
    volume-mounted but git is not installed in the container.
    """
    # For file:// URLs always read from the local filesystem — never spawn a subprocess.
    # subprocess.run(git ...) deadlocks under gevent pools; direct filesystem reads
    # are safe and efficient for volume-mounted development repos.
    if repo_url.startswith("file://"):
        local_path = repo_url[len("file://"):]
        local_bundle_root = Path(local_path) / path if path and path != "." else Path(local_path)
        if local_bundle_root.exists() and local_bundle_root.is_dir():
            logger.info(
                "fetch_bundle_files_local",
                repo_url=repo_url,
                path=path,
                local_root=str(local_bundle_root),
            )
            files: Dict[str, bytes] = {}
            for file_path in local_bundle_root.rglob("*"):
                if file_path.is_file() and ".git" not in file_path.parts and "__pycache__" not in file_path.parts:
                    rel = str(file_path.relative_to(local_bundle_root))
                    files[rel] = file_path.read_bytes()
            return files
        raise RuntimeError(f"file:// bundle path does not exist or is not a directory: {local_bundle_root}")

    env = _git_env(credentials)
    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            # worker_run_subprocess uses gevent.subprocess on gevent pools to avoid
            # blocking the event loop while waiting for git clone to complete.
            worker_run_subprocess(
                ["git", "clone", "--depth=1", "--branch", ref, repo_url, tmpdir],
                capture_output=True, check=True, env=env, timeout=120,
            )
        except FileNotFoundError as e:
            raise RuntimeError(f"git is not installed: {e}") from e
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"git clone failed: {e.stderr.decode('utf-8', errors='replace')[:500]}") from e
        except subprocess.TimeoutExpired as e:
            raise RuntimeError("git clone timed out (120s)") from e

        bundle_root = Path(tmpdir) / path
        if not bundle_root.exists():
            raise RuntimeError(f"Bundle path '{path}' does not exist in repo")

        files_tmp: Dict[str, bytes] = {}
        for file_path in bundle_root.rglob("*"):
            if file_path.is_file() and ".git" not in file_path.parts:
                rel = str(file_path.relative_to(bundle_root))
                files_tmp[rel] = file_path.read_bytes()
        return files_tmp


def _git_env(credentials: Optional[str]) -> Dict[str, str]:
    """Build environment dict for git subprocess calls."""
    env = dict(os.environ)
    if credentials:
        # Support 'https://token@github.com' style credential injection
        env["GIT_ASKPASS"] = "echo"
        env["GIT_TERMINAL_PROMPT"] = "0"
    return env


# ---------------------------------------------------------------------------
# Manifest helpers
# ---------------------------------------------------------------------------

def _parse_manifest(bundle_files: Dict[str, bytes]) -> Dict[str, Any]:
    """Parse manifest.yaml or bundle.json from the bundle file set."""
    for name in ("manifest.yaml", "manifest.yml", "bundle.json"):
        if name in bundle_files:
            content = bundle_files[name].decode("utf-8", errors="replace")
            try:
                if name.endswith(".json"):
                    return json.loads(content)
                import yaml  # type: ignore[import]
                return yaml.safe_load(content) or {}
            except Exception as e:
                raise ValueError(f"Failed to parse {name}: {e}") from e
    raise ValueError("Bundle is missing manifest.yaml (or bundle.json)")


def _validate_bundle_name(name: str) -> None:
    """Validate the manifest 'name' slug."""
    if not name:
        raise ValueError("Manifest 'name' field is required")
    if not re.match(r'^[a-z0-9][a-z0-9\-]*$', name):
        raise ValueError(
            f"Manifest 'name' must be lowercase alphanumeric with hyphens (got '{name}'). "
            "No dots (dots are namespace separators)."
        )
    if name in RESERVED_BUNDLE_NAMES:
        raise ValueError(
            f"Manifest 'name' '{name}' is reserved and cannot be used for bundle deployments. "
            "Choose a different bundle name."
        )


def _raise_manifest_validation_error(
    bundle_files: Dict[str, bytes],
    bundle_id: str,
    error: ValueError,
    *,
    motet: Optional[Any] = None,
) -> None:
    """Normalize manifest validation failures and optionally stream lint events."""
    reserved_name_errors = _lint_reserved_bundle_name(bundle_files)
    if reserved_name_errors:
        if motet is not None:
            _emit_lint_failure_events(motet, bundle_id, reserved_name_errors)
        raise BundleDeployError(
            "Bundle lint failed",
            details={
                "lint_errors": [err.model_dump() for err in reserved_name_errors],
                "validation_error": str(error),
            },
        ) from error
    raise BundleDeployError(str(error), details={}) from error


def _parse_and_validate_manifest(
    bundle_files: Dict[str, bytes],
    *,
    motet: Optional[Any] = None,
) -> Tuple[Dict[str, Any], str]:
    """Parse manifest and validate bundle name + agent/surface config schema."""
    try:
        manifest = _parse_manifest(bundle_files)
    except ValueError as e:
        raise BundleDeployError(str(e), details={}) from e

    bundle_id = str(manifest.get("name", "") or "")
    try:
        _validate_bundle_name(bundle_id)
        _extract_bundle_agent_ids(bundle_id, bundle_files, strict=True)
        _extract_bundle_surfaces(bundle_id, bundle_files, strict=True)
    except ValueError as e:
        _raise_manifest_validation_error(bundle_files, bundle_id, e, motet=motet)

    return manifest, bundle_id


def _extract_bundle_agents_configs(
    bundle_id: str,
    bundle_files: Dict[str, bytes],
    *,
    strict: bool,
) -> List[Dict[str, Any]]:
    """
    Extract validated agent configs from config/agents.yaml.

    Supports either:
    - root mapping with `agents: [...]`
    - top-level list `[...]`

    In strict mode, malformed YAML/schema raises ValueError.
    In non-strict mode, parse errors are logged and an empty list is returned.
    """
    path = None
    for candidate in ("config/agents.yaml", "config/agents.yml", "agents/agents.yaml", "agents/agents.yml"):
        if candidate in bundle_files:
            path = candidate
            break
    if path is None:
        return []

    try:
        import yaml  # type: ignore[import]
        from motet.core.agents import AgentConfig

        content = bundle_files[path].decode("utf-8", errors="replace")
        raw = yaml.safe_load(content) or {}
        entries = raw.get("agents") if isinstance(raw, dict) else raw
        if entries is None:
            return []
        if not isinstance(entries, list):
            raise ValueError(f"{path} must define a list of agents")

        configs: List[Dict[str, Any]] = []
        for i, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise ValueError(f"{path} entry #{i} must be an object")
            candidate_cfg = dict(entry)
            candidate_cfg["bundle_id"] = bundle_id
            cfg = AgentConfig(**candidate_cfg)
            configs.append(cfg.model_dump())
        return sorted(configs, key=lambda c: str(c.get("agent_id", "")))
    except Exception as e:
        if strict:
            raise ValueError(f"Invalid bundle agent config in {path}: {e}") from e
        logger.warning("bundle_agent_catalog_extract_failed", bundle_id=bundle_id, path=path, error=str(e))
        return []


def _extract_bundle_agent_ids(
    bundle_id: str,
    bundle_files: Dict[str, bytes],
    *,
    strict: bool,
) -> List[str]:
    """Extract fully-qualified agent IDs from validated bundle agent configs."""
    return [
        f"{bundle_id}.{str(cfg.get('agent_id', '')).strip()}"
        for cfg in _extract_bundle_agents_configs(bundle_id, bundle_files, strict=strict)
        if str(cfg.get("agent_id", "")).strip()
    ]


def _extract_bundle_surfaces(
    bundle_id: str,
    bundle_files: Dict[str, bytes],
    *,
    strict: bool,
) -> List[Dict[str, Any]]:
    """
    Extract surface catalog entries from config/surfaces.yaml.

    Supports either:
    - root mapping with ``surfaces: [...]``
    - top-level list ``[...]``

    Each entry requires ``id`` (stable surface slug). Optional ``display_name``
    and ``description``. IDs are global (not bundle-namespaced).
    """
    path = None
    for candidate in (
        "config/surfaces.yaml",
        "config/surfaces.yml",
        "surfaces/surfaces.yaml",
        "surfaces/surfaces.yml",
    ):
        if candidate in bundle_files:
            path = candidate
            break
    if path is None:
        return []

    try:
        import yaml  # type: ignore[import]
        from motet.core.surfaces import validate_surface_id

        content = bundle_files[path].decode("utf-8", errors="replace")
        raw = yaml.safe_load(content) or {}
        entries = raw.get("surfaces") if isinstance(raw, dict) else raw
        if entries is None:
            return []
        if not isinstance(entries, list):
            raise ValueError(f"{path} must define a list of surfaces")

        surfaces: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for i, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise ValueError(f"{path} entry #{i} must be an object")
            raw_id = entry.get("id") or entry.get("surface_id")
            if not isinstance(raw_id, str) or not raw_id.strip():
                raise ValueError(f"{path} entry #{i} requires id")
            sid = validate_surface_id(raw_id)
            if sid in seen:
                raise ValueError(f"{path} duplicate surface id: {sid}")
            seen.add(sid)
            display_name = entry.get("display_name") or entry.get("name") or sid
            description = entry.get("description")
            surfaces.append(
                {
                    "id": sid,
                    "display_name": str(display_name).strip() or sid,
                    "description": (
                        str(description).strip()
                        if isinstance(description, str) and description.strip()
                        else None
                    ),
                    "bundle_id": bundle_id,
                }
            )
        return sorted(surfaces, key=lambda s: str(s.get("id", "")))
    except Exception as e:
        if strict:
            raise ValueError(f"Invalid bundle surface config in {path}: {e}") from e
        logger.warning(
            "bundle_surface_catalog_extract_failed",
            bundle_id=bundle_id,
            path=path,
            error=str(e),
        )
        return []


def _register_bundle_surfaces(
    bundle_id: str,
    surfaces: List[Dict[str, Any]],
) -> Dict[str, int]:
    """
    Register bundle-declared surfaces into the Redis surfaces catalog.

    Existing surfaces are left unchanged (no-op). Returns counts
    ``{"created": N, "skipped": M}``.
    """
    if not surfaces:
        return {"created": 0, "skipped": 0}
    from motet.core.surfaces import SurfaceRegistry

    registry = SurfaceRegistry()
    created = 0
    skipped = 0
    for entry in surfaces:
        sid = str(entry.get("id") or "").strip()
        if not sid:
            continue
        was_created, _record = registry.register_if_absent(
            surface_id=sid,
            display_name=entry.get("display_name"),
            description=entry.get("description"),
            created_by=f"bundle:{bundle_id}",
        )
        if was_created:
            created += 1
            logger.info(
                "bundle_surface_registered",
                bundle_id=bundle_id,
                surface_id=sid,
            )
        else:
            skipped += 1
            logger.info(
                "bundle_surface_already_exists",
                bundle_id=bundle_id,
                surface_id=sid,
            )
    return {"created": created, "skipped": skipped}


# ---------------------------------------------------------------------------
# Source fingerprint
# ---------------------------------------------------------------------------

def _compute_source_fingerprint(repo_url: str, path: str) -> str:
    """sha256(repo_url + '#' + path) — used for namespace ownership validation."""
    raw = f"{repo_url}#{path}".encode()
    return hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------------------
# Artifact packaging / extraction
# ---------------------------------------------------------------------------

def _package_artifact(bundle_files: Dict[str, bytes]) -> bytes:
    """Pack all bundle files into an in-memory tar.gz archive."""
    bundle_files = _drop_path_prefix_conflicts(bundle_files)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel_path, content in sorted(bundle_files.items()):
            info = tarfile.TarInfo(name=rel_path)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def _artifact_to_bundle_files(artifact_bytes: bytes) -> Dict[str, bytes]:
    """In-memory inverse of :func:`_package_artifact` — returns ``{rel_path: bytes}``.

    Used by :func:`publish_bundle` to feed the deployer-side OCI image
    builder (ADR-0100 §"Deployer build orchestration") without paying the cost
    of round-tripping the artifact to a tempdir on disk.
    """
    files: Dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(artifact_bytes), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            if member.name.startswith("/") or ".." in member.name.split("/"):
                continue
            f = tar.extractfile(member)
            if f is None:
                continue
            try:
                files[member.name] = f.read()
            finally:
                f.close()
    return files


def _unpack_artifact(artifact_bytes: bytes, dest_dir: Path) -> None:
    """Extract a tar.gz artifact into dest_dir."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(artifact_bytes), mode="r:gz") as tar:
        # Security: filter out absolute paths and '..' components.
        members = [
            m
            for m in tar.getmembers()
            if m.isfile() and not m.name.startswith("/") and ".." not in m.name.split("/")
        ]
        member_names = {m.name.rstrip("/") for m in members}
        for member in members:
            member_name = member.name.rstrip("/")
            if _path_has_descendant(member_name, member_names):
                logger.warning(
                    "bundle_artifact_skipping_prefix_conflict",
                    path=member_name,
                    reason="archive also contains files below this path",
                )
                continue
            tar.extract(member, path=str(dest_dir))


def _path_has_descendant(path: str, all_paths: Set[str]) -> bool:
    """Return True when an archive file path also appears as a directory prefix."""
    prefix = path.rstrip("/") + "/"
    return any(candidate.startswith(prefix) for candidate in all_paths)


def _drop_path_prefix_conflicts(bundle_files: Dict[str, bytes]) -> Dict[str, bytes]:
    """Drop file entries that conflict with descendant paths.

    Some zip creators include directory symlinks or link placeholders as regular
    file entries while also including the resolved files beneath that path. A
    real filesystem cannot contain both, so keep the concrete descendant files.
    """
    paths = set(bundle_files)
    return {
        path: content
        for path, content in bundle_files.items()
        if not _path_has_descendant(path, paths)
    }


def _zip_to_bundle_files(zip_bytes: bytes) -> Dict[str, bytes]:
    """
    Extract a zip archive to a bundle_files dict (relative_path -> bytes).
    If the zip has a single top-level directory, treat that as the bundle root
    so that manifest.yaml is at the top level of the returned dict.
    """
    bundle_files: Dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
        names = [n for n in zf.namelist() if not n.startswith("/") and ".." not in n and not n.endswith("/")]
        if not names:
            raise ValueError("Zip archive is empty or contains no files")
        # Check for single top-level directory (e.g. "calculator/manifest.yaml" -> root = "calculator/")
        parts = [n.split("/") for n in names]
        first_parts = [p[0] for p in parts if len(p) > 1]
        if first_parts and all(len(p) > 1 for p in parts) and len(set(p[0] for p in parts)) == 1:
            prefix = parts[0][0] + "/"
            prefix_len = len(prefix)
            for n in names:
                if n.startswith(prefix) and not n == prefix:
                    rel = n[prefix_len:]
                    bundle_files[rel] = zf.read(n)
        else:
            # Preserve full relative path so commands/, tools/, workflows/ structure is kept
            for n in names:
                rel = n
                if rel:
                    bundle_files[rel] = zf.read(n)
    bundle_files = _drop_path_prefix_conflicts(bundle_files)
    if "manifest.yaml" not in bundle_files and "manifest.yml" not in bundle_files and "bundle.json" not in bundle_files:
        raise ValueError("Zip must contain manifest.yaml (or manifest.yml / bundle.json) at the bundle root")
    return bundle_files


# ---------------------------------------------------------------------------
# Artifact store abstraction (Redis backend — V1)
# ---------------------------------------------------------------------------

def _artifact_key(bundle_id: str, bundle_version: str) -> str:
    return f"bundle:{bundle_id}:artifact:{bundle_version}"


def _store_artifact(redis_client: Any, bundle_id: str, bundle_version: str, artifact_bytes: bytes) -> None:
    """Write artifact bytes to Redis with TTL.

    Artifacts are base64-encoded before storage because the shared Redis client
    uses decode_responses=True and cannot store raw binary data.
    """
    import base64
    key = _artifact_key(bundle_id, bundle_version)
    redis_client.set(key, base64.b64encode(artifact_bytes).decode("ascii"), ex=BUNDLE_ARTIFACT_TTL)
    # Update latest pointer
    redis_client.set(f"bundle:{bundle_id}:latest", bundle_version, ex=BUNDLE_ARTIFACT_TTL)
    # Append to version list for retention management
    versions_key = f"bundle:{bundle_id}:versions"
    redis_client.rpush(versions_key, bundle_version)
    redis_client.expire(versions_key, BUNDLE_ARTIFACT_TTL * 2)
    _evict_old_artifacts(redis_client, bundle_id)


def _fetch_artifact(redis_client: Any, bundle_id: str, bundle_version: str) -> Optional[bytes]:
    """Fetch artifact bytes from Redis (base64-encoded at rest, decoded on retrieval)."""
    import base64
    raw = redis_client.get(_artifact_key(bundle_id, bundle_version))
    if raw is None:
        return None
    if isinstance(raw, bytes):
        return raw  # already raw bytes (client without decode_responses)
    return base64.b64decode(raw)


def _evict_old_artifacts(redis_client: Any, bundle_id: str) -> None:
    """Retain only the last ARTIFACT_RETENTION versions; delete older artifacts."""
    versions_key = f"bundle:{bundle_id}:versions"
    all_versions = redis_client.lrange(versions_key, 0, -1)
    if len(all_versions) > ARTIFACT_RETENTION:
        to_evict = all_versions[: len(all_versions) - ARTIFACT_RETENTION]
        for v in to_evict:
            version_str = v.decode() if isinstance(v, bytes) else v
            redis_client.delete(_artifact_key(bundle_id, version_str))
        # Trim the list
        redis_client.ltrim(versions_key, len(all_versions) - ARTIFACT_RETENTION, -1)


# ---------------------------------------------------------------------------
# Bundle registry helpers (Redis hash per bundle_id)
# ---------------------------------------------------------------------------

def _registry_key(bundle_id: str) -> str:
    return f"bundle:{bundle_id}:registry"


def _record_deploy_metadata(
    redis_client: Any,
    bundle_id: str,
    bundle_version: str,
    bundle_ref: str,
    source_fingerprint: str,
    manifest_version: Optional[str],
    targeting: Optional[BundleTargeting],
    deploy_job_id: str,
) -> None:
    """Persist bundle registry entry and append to deploy history."""
    key = _registry_key(bundle_id)
    entry = {
        "bundle_id": bundle_id,
        "bundle_version": bundle_version,
        "bundle_ref": bundle_ref,
        "source_fingerprint": source_fingerprint,
        "manifest_version": manifest_version or "",
        "targeting": targeting.model_dump() if targeting else {},
        "deploy_job_id": deploy_job_id,
        "deployed_at": time.time(),
        "status": BundleDeployStatus.COMPLETE.value,
    }
    redis_client.hset(key, mapping={k: json.dumps(v) if isinstance(v, (dict, list)) else str(v) for k, v in entry.items()})
    redis_client.expire(key, 365 * 24 * 3600)  # 1 year

    # History list
    history_key = f"bundle:{bundle_id}:history"
    redis_client.rpush(history_key, json.dumps(entry))
    redis_client.ltrim(history_key, -50, -1)  # keep last 50 entries
    redis_client.expire(history_key, 365 * 24 * 3600)


def _get_registry_entry(redis_client: Any, bundle_id: str) -> Optional[Dict[str, Any]]:
    """Fetch the current registry entry for a bundle."""
    key = _registry_key(bundle_id)
    raw = redis_client.hgetall(key)
    if not raw:
        return None
    return {
        (k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
        for k, v in raw.items()
    }


def _validate_namespace_ownership(
    redis_client: Any,
    bundle_id: str,
    source_fingerprint: str,
) -> None:
    """
    Ensure the bundle_id namespace is either unclaimed or owned by this source.
    Raises RuntimeError if claimed by a different source.
    """
    entry = _get_registry_entry(redis_client, bundle_id)
    if not entry:
        return  # first deploy — namespace is free
    stored_fp = entry.get("source_fingerprint", "")
    if stored_fp and stored_fp != source_fingerprint:
        raise RuntimeError(
            f"Bundle namespace '{bundle_id}' is already claimed by a different repo/path. "
            "Contact an admin to transfer ownership."
        )


def _lookup_bundle_id_by_source(redis_client: Any, source_fingerprint: str) -> Optional[str]:
    """
    Reverse-lookup: given a source fingerprint, find the registered bundle_id.
    Scans bundle registry keys (acceptable for deploy frequency).
    """
    cursor = 0
    while True:
        cursor, keys = redis_client.scan(cursor, match="bundle:*:registry", count=100)
        for key in keys:
            raw = redis_client.hgetall(key)
            if not raw:
                continue
            fp = raw.get(b"source_fingerprint", b"")
            if isinstance(fp, bytes):
                fp = fp.decode()
            if fp == source_fingerprint:
                bid = raw.get(b"bundle_id", b"")
                return bid.decode() if isinstance(bid, bytes) else bid
        if cursor == 0:
            break
    return None


def _get_bundle_history(redis_client: Any, bundle_id: str) -> List[Dict[str, Any]]:
    """Return deploy history entries for a bundle (most recent last)."""
    history_key = f"bundle:{bundle_id}:history"
    raw = redis_client.lrange(history_key, 0, -1)
    entries = []
    for item in raw:
        try:
            entries.append(json.loads(item.decode() if isinstance(item, bytes) else item))
        except Exception:
            pass  # skip malformed history entry
    return entries


def _list_all_bundles(redis_client: Any) -> List[Dict[str, Any]]:
    """List all registered bundles (scans bundle:*:registry keys)."""
    bundles = []
    cursor = 0
    while True:
        cursor, keys = redis_client.scan(cursor, match="bundle:*:registry", count=100)
        for key in keys:
            raw = redis_client.hgetall(key)
            if raw:
                entry = {
                    (k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
                    for k, v in raw.items()
                }
                bundles.append(entry)
        if cursor == 0:
            break
    return bundles


# ---------------------------------------------------------------------------
# Bundle content catalog helpers (ADR-0071 §5 Bundle content catalog)
# ---------------------------------------------------------------------------


def _catalog_key(bundle_id: str) -> str:
    return f"bundle:{bundle_id}:catalog"


def _worker_state_key(bundle_id: str) -> str:
    return f"bundle:{bundle_id}:worker_state"


def _extract_decorator_required_capabilities(decorator: Any) -> List[str]:
    """Extract required_capabilities from a @motet.command / @distributed_command AST node.

    Supports ``WorkerCapability.EDGE_EXECUTION`` attributes and string constants.
    Bare variable references are skipped (their value is unknown statically;
    guessing from the name would emit bogus capabilities). Returns lowercase
    capability values for CapabilityFilter matching.
    """
    if not isinstance(decorator, ast.Call):
        return []
    caps: List[str] = []
    for kw in decorator.keywords or []:
        if kw.arg != "required_capabilities":
            continue
        if not isinstance(kw.value, (ast.List, ast.Tuple)):
            continue
        for elt in kw.value.elts:
            if isinstance(elt, ast.Attribute) and isinstance(elt.attr, str):
                # WorkerCapability.EDGE_EXECUTION -> edge_execution
                caps.append(elt.attr.lower())
            elif isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                caps.append(elt.value.strip().lower())
    # Preserve order, drop empties/dupes
    seen: set[str] = set()
    out: List[str] = []
    for c in caps:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _extract_decorator_description(decorator: Any) -> Optional[str]:
    """Extract a string ``description=`` kwarg from a command decorator AST node."""
    if not isinstance(decorator, ast.Call):
        return None
    for kw in decorator.keywords or []:
        if kw.arg != "description":
            continue
        if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            text = kw.value.value.strip()
            return text or None
    return None


def _first_docstring_line(node: Any) -> Optional[str]:
    """Return the first non-empty line of a function's docstring, if any."""
    try:
        doc = ast.get_docstring(node)
    except Exception:
        return None
    if not doc:
        return None
    for line in str(doc).strip().splitlines():
        text = line.strip()
        if text:
            return text
    return None


def _extract_command_description(node: Any, decorator: Any) -> Optional[str]:
    """
    Prefer explicit ``description=`` on the decorator; else first docstring line.

    Mirrors runtime CommandRegistration.description derivation so the Redis
    catalog matches what workers index for ``core.help``.
    """
    explicit = _extract_decorator_description(decorator)
    if explicit:
        return explicit
    return _first_docstring_line(node)


def _extract_bundle_catalog(bundle_id: str, bundle_files: Dict[str, bytes]) -> Dict[str, Any]:
    """
    Extract the bundle content catalog from bundle files via AST / YAML analysis.

    Called by validate_bundle after the lint pass (files already in memory).
    Returns a dict with commands, tools, workflows, agents, agent_configs,
    surfaces, mcp_servers, model_ids, skills lists, and optional exec
    (from config/exec.yaml). Most names are namespaced with the bundle_id
    prefix (e.g. 'hello-world.hello_world'); surface ids stay global.
    """
    yaml: Any = None
    try:
        import yaml  # type: ignore[import]
        _yaml_available = True
    except ImportError:
        _yaml_available = False

    commands: List[str] = []
    command_capabilities: Dict[str, List[str]] = {}
    command_descriptions: Dict[str, str] = {}
    tools: List[str] = []
    workflows: List[str] = []
    agents: List[str] = []
    agent_configs: List[Dict[str, Any]] = []
    surfaces: List[Dict[str, Any]] = []
    mcp_servers: List[str] = []
    model_ids: List[str] = []
    skills: List[Dict[str, Any]] = []
    exec_meta: Optional[Dict[str, Any]] = None

    # config/agents.yaml / surfaces.yaml are validated in strict mode in validate
    # commands; catalog extraction is tolerant to avoid hiding other metadata.
    agent_configs = _extract_bundle_agents_configs(bundle_id, bundle_files, strict=False)
    agents = [
        f"{bundle_id}.{str(cfg.get('agent_id', '')).strip()}"
        for cfg in agent_configs
        if str(cfg.get("agent_id", "")).strip()
    ]
    surfaces = _extract_bundle_surfaces(bundle_id, bundle_files, strict=False)

    for file_path, content_bytes in bundle_files.items():
        try:
            content = content_bytes.decode("utf-8", errors="replace")
        except Exception:
            continue  # skip binary / non-decodable files

        # commands/*.py — find @motet.command (or @distributed_command alias) decorated functions (ADR-0089)
        if file_path.startswith("commands/") and file_path.endswith(".py"):
            try:
                tree = ast.parse(content, filename=file_path)
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        is_command = False
                        command_dec: Any = None
                        for dec in node.decorator_list:
                            if isinstance(dec, ast.Name):
                                if dec.id == "distributed_command":
                                    is_command = True
                                    command_dec = dec
                                    break
                            elif isinstance(dec, ast.Call):
                                func = dec.func
                                if isinstance(func, ast.Name):
                                    if func.id == "distributed_command":
                                        is_command = True
                                        command_dec = dec
                                        break
                                elif isinstance(func, ast.Attribute):
                                    if getattr(func.value, "id", None) == "motet" and func.attr == "command":
                                        is_command = True
                                        command_dec = dec
                                        break
                            elif isinstance(dec, ast.Attribute):
                                if dec.attr == "command" and getattr(dec.value, "id", None) == "motet":
                                    is_command = True
                                    command_dec = dec
                                    break
                        if is_command:
                            qualified = f"{bundle_id}.{node.name}"
                            commands.append(qualified)
                            caps = _extract_decorator_required_capabilities(command_dec)
                            if caps:
                                command_capabilities[qualified] = caps
                            desc = _extract_command_description(node, command_dec)
                            if desc:
                                command_descriptions[qualified] = desc
            except SyntaxError:
                pass  # lint already caught syntax errors

        # tools/*.py — find tool registrations.
        # Supports: @tool / @register_tool / @motet_tool / @motet.tool (ADR-0089), or registry.register(...)
        elif file_path.startswith("tools/") and file_path.endswith(".py"):
            try:
                tree = ast.parse(content, filename=file_path)
                for node in ast.walk(tree):
                    # Pattern 1: decorator-based tool registration
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        is_tool = False
                        for dec in node.decorator_list:
                            if isinstance(dec, ast.Name):
                                if dec.id in ("tool", "register_tool", "motet_tool"):
                                    is_tool = True
                                    break
                            elif isinstance(dec, ast.Call):
                                func = dec.func
                                if isinstance(func, ast.Name):
                                    if func.id in ("tool", "register_tool", "motet_tool"):
                                        is_tool = True
                                        break
                                elif isinstance(func, ast.Attribute):
                                    if getattr(func.value, "id", None) == "motet" and func.attr == "tool":
                                        is_tool = True
                                        break
                            elif isinstance(dec, ast.Attribute):
                                if dec.attr == "tool" and getattr(dec.value, "id", None) == "motet":
                                    is_tool = True
                                    break
                        if is_tool:
                            resolved_tool_name = node.name
                            # ADR-0089: support explicit decorator name override
                            # e.g. @motet.tool(name="custom_name")
                            for dec in node.decorator_list:
                                if not isinstance(dec, ast.Call):
                                    continue
                                func = dec.func
                                is_matching_decorator = False
                                if isinstance(func, ast.Name):
                                    is_matching_decorator = func.id in ("tool", "register_tool", "motet_tool")
                                elif isinstance(func, ast.Attribute):
                                    is_matching_decorator = (
                                        getattr(func.value, "id", None) == "motet" and func.attr == "tool"
                                    )
                                if not is_matching_decorator:
                                    continue
                                for kw in dec.keywords or []:
                                    if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                                        if isinstance(kw.value.value, str) and kw.value.value.strip():
                                            resolved_tool_name = kw.value.value.strip()
                                break
                            tools.append(f"{bundle_id}.{resolved_tool_name}")
                    # Pattern 2: registry.register("tool_name", ...) call at module level
                    elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                        call = node.value
                        # Match <anything>.register(...) or register(...)
                        func = call.func
                        func_name = None
                        if isinstance(func, ast.Attribute) and func.attr == "register":
                            func_name = "register"
                        elif isinstance(func, ast.Name) and func.id == "register":
                            func_name = "register"
                        if func_name and call.args and isinstance(call.args[0], ast.Constant):
                            tool_name = call.args[0].value
                            if isinstance(tool_name, str) and tool_name.startswith(f"{bundle_id}."):
                                tools.append(tool_name)
            except SyntaxError:
                pass

        # workflows/*.yaml — collect workflow_id values (namespaced by bundle_id)
        elif (
            (file_path.startswith("workflows/") or file_path.startswith("workflow/"))
            and (file_path.endswith(".yaml") or file_path.endswith(".yml"))
            and _yaml_available
        ):
            try:
                wf_data = yaml.safe_load(content) or {}
                original_id = wf_data.get("workflow_id") or file_path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
                workflows.append(f"{bundle_id}.{original_id}")
            except Exception:
                pass  # skip malformed workflow YAML

        # config/mcp.yaml — collect server IDs
        elif file_path in ("config/mcp.yaml", "config/mcp.yml", "mcp/mcp.yaml") and _yaml_available:
            try:
                mcp_data = yaml.safe_load(content) or {}
                for server_id in mcp_data.get("servers", {}).keys():
                    mcp_servers.append(f"{bundle_id}.{server_id}")
            except Exception:
                pass  # skip malformed MCP YAML

        # config/models.yaml — collect model profile IDs
        elif file_path in ("config/models.yaml", "config/models.yml", "models/models.yaml") and _yaml_available:
            try:
                models_data = yaml.safe_load(content) or {}
                for model_profile_id in models_data.get("profiles", {}).keys():
                    model_ids.append(f"{bundle_id}.{model_profile_id}")
            except Exception:
                pass  # skip malformed models YAML

        # config/exec.yaml — OCI image / digest hints for worker_exec (Phase 3)
        elif file_path in ("config/exec.yaml", "config/exec.yml") and _yaml_available:
            try:
                raw_exec = yaml.safe_load(content) or {}
                if isinstance(raw_exec, dict):
                    norm = _normalize_exec_config_block(raw_exec)
                    if norm:
                        exec_meta = norm
            except Exception:
                pass

        # skills/<name>/SKILL.md — ADR-0073 catalog metadata (full load on worker)
        elif file_path.startswith("skills/") and file_path.endswith("/SKILL.md"):
            try:
                from motet.core.skills.parser import parse_skill_markdown_text

                doc = parse_skill_markdown_text(content, source_hint=file_path)
                parts = file_path.split("/")
                dir_name = parts[-2] if len(parts) >= 2 else ""
                skill_key = f"{bundle_id}.{doc.name}"
                skills.append(
                    {
                        "id": skill_key,
                        "name": doc.name,
                        "description": doc.description,
                        "path": "/".join(parts[:-1]) + "/",
                        "dir": dir_name,
                        "dir_matches_name": dir_name == doc.name,
                    }
                )
            except Exception:
                pass  # lint/validate should catch broken SKILL.md earlier

    skills_sorted = sorted(skills, key=lambda s: str(s.get("id", "")))

    out: Dict[str, Any] = {
        "bundle_id": bundle_id,
        "commands": sorted(commands),
        "command_capabilities": {
            k: command_capabilities[k] for k in sorted(command_capabilities)
        },
        "command_descriptions": {
            k: command_descriptions[k] for k in sorted(command_descriptions)
        },
        # Populated after worker reload acks (see _merge_command_schemas_into_catalog).
        "command_schemas": {},
        "tools": sorted(tools),
        "workflows": sorted(workflows),
        "agents": sorted(agents),
        "agent_configs": agent_configs,
        "surfaces": surfaces,
        "mcp_servers": sorted(mcp_servers),
        "model_ids": sorted(model_ids),
        "skills": skills_sorted,
    }
    if exec_meta:
        out["exec"] = _enrich_exec_meta_requirements_sha(exec_meta, bundle_files)
    return out


def _merge_command_schemas_into_catalog(
    catalog: Dict[str, Any],
    reload_results: List[Any],
) -> Dict[str, Any]:
    """
    Merge ``command_schemas`` from worker reload/hot_reload acks into a catalog dict.

    First successful schema per command_type wins; malformed acks are skipped.
    """
    merged = dict(catalog or {})
    schemas: Dict[str, Any] = {}
    existing = merged.get("command_schemas") or {}
    if isinstance(existing, dict):
        schemas.update({k: v for k, v in existing.items() if isinstance(v, dict)})

    for result in reload_results or []:
        if not isinstance(result, dict) or result.get("_error"):
            continue
        chunk = result.get("command_schemas") or {}
        if not isinstance(chunk, dict):
            continue
        for command_type, schema in chunk.items():
            if not isinstance(command_type, str) or not command_type.strip():
                continue
            if isinstance(schema, dict) and command_type not in schemas:
                schemas[command_type] = schema

    merged["command_schemas"] = {
        k: schemas[k] for k in sorted(schemas.keys())
    }
    return merged


def _store_catalog(
    redis_client: Any,
    bundle_id: str,
    bundle_version: str,
    catalog: Dict[str, Any],
    targeting: Optional[BundleTargeting] = None,
) -> None:
    """Store bundle content catalog in Redis (JSON string, 1-year TTL)."""
    import datetime

    catalog_with_meta = {
        **catalog,
        "bundle_id": bundle_id,
        "bundle_version": bundle_version,
        "targeting": targeting.model_dump() if targeting else {},
        "extracted_at": datetime.datetime.utcnow().isoformat() + "Z",
    }
    redis_client.set(_catalog_key(bundle_id), json.dumps(catalog_with_meta), ex=365 * 24 * 3600)


def _get_catalog(redis_client: Any, bundle_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve bundle content catalog from Redis."""
    raw = redis_client.get(_catalog_key(bundle_id))
    if not raw:
        return None
    try:
        return json.loads(raw.decode() if isinstance(raw, bytes) else raw)
    except Exception:
        return None


def _store_worker_state(
    redis_client: Any,
    bundle_id: str,
    worker_id: str,
    registered_commands: List[str],
    registered_tools: List[str],
) -> None:
    """Store per-worker loaded state in Redis hash (keyed by worker_id)."""
    import datetime

    state = json.dumps({
        "commands": registered_commands,
        "tools": registered_tools,
        "loaded_at": datetime.datetime.utcnow().isoformat() + "Z",
    })
    redis_client.hset(_worker_state_key(bundle_id), worker_id, state)
    redis_client.expire(_worker_state_key(bundle_id), 365 * 24 * 3600)


def _get_worker_state(redis_client: Any, bundle_id: str) -> Dict[str, Any]:
    """Retrieve per-worker loaded state from Redis hash."""
    raw = redis_client.hgetall(_worker_state_key(bundle_id))
    result: Dict[str, Any] = {}
    for worker_id, state_json in raw.items():
        wid = worker_id.decode() if isinstance(worker_id, bytes) else worker_id
        state_str = state_json.decode() if isinstance(state_json, bytes) else state_json
        try:
            result[wid] = json.loads(state_str)
        except Exception:
            result[wid] = {}
    return result


def _list_all_catalogs(redis_client: Any) -> Dict[str, Dict[str, Any]]:
    """Scan all bundle:*:catalog keys and return a mapping of bundle_id → catalog."""
    catalogs: Dict[str, Dict[str, Any]] = {}
    cursor = 0
    while True:
        cursor, keys = redis_client.scan(cursor, match="bundle:*:catalog", count=100)
        for key in keys:
            raw = redis_client.get(key)
            if raw:
                try:
                    key_str = key.decode() if isinstance(key, bytes) else key
                    # key format: "bundle:{bundle_id}:catalog"
                    parts = key_str.split(":", 2)
                    default_bid = parts[1] if len(parts) >= 2 else None
                    data = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
                    bid = data.get("bundle_id") or default_bid
                    if bid:
                        if "bundle_id" not in data:
                            data["bundle_id"] = bid
                        catalogs[bid] = data
                except Exception:
                    pass  # skip malformed catalog entry
        if cursor == 0:
            break
    return catalogs


# ---------------------------------------------------------------------------
# Live-worker detection
# ---------------------------------------------------------------------------

def _hot_workers_stale_after_restart(
    redis_client: Any,
    bundle_id: str,
    live_workers: List[str],
) -> bool:
    """Return True if any live worker likely lost in-memory hot-bundle state.

    After ``docker restart``, Redis ``worker_state.loaded_at`` is still present
    but the worker's ``startup_time`` is newer — so a content-hash ``no_change``
    short-circuit would leave commands unregistered (#125). When readiness
    data is unavailable, returns False so unit tests / degraded detection keep
    the hash short-circuit (startup catch-up is the durable fix).
    """
    if not live_workers:
        return False
    try:
        from motet.core.distributed.worker_readiness import WorkerReadinessService

        svc = WorkerReadinessService()
    except Exception as e:
        logger.debug(
            "hot_worker_staleness_check_unavailable",
            bundle_id=bundle_id,
            error=str(e),
        )
        return False

    from datetime import datetime, timezone

    worker_state = _get_worker_state(redis_client, bundle_id)
    for worker_id in live_workers:
        state = worker_state.get(worker_id) or {}
        loaded_at_raw = state.get("loaded_at")
        if not loaded_at_raw:
            logger.info(
                "hot_worker_missing_loaded_at",
                bundle_id=bundle_id,
                worker_id=worker_id,
            )
            return True
        info = svc.get_worker_info(worker_id)
        startup_time = float(getattr(info, "startup_time", 0) or 0) if info else 0.0
        if startup_time <= 0:
            continue
        try:
            loaded_at = datetime.fromisoformat(
                str(loaded_at_raw).replace("Z", "+00:00")
            )
            if loaded_at.tzinfo is None:
                loaded_at = loaded_at.replace(tzinfo=timezone.utc)
            loaded_ts = loaded_at.timestamp()
        except Exception:
            return True
        if startup_time > loaded_ts:
            logger.info(
                "hot_worker_stale_after_restart",
                bundle_id=bundle_id,
                worker_id=worker_id,
                startup_time=startup_time,
                loaded_at=loaded_at_raw,
            )
            return True
    return False


def _resolve_live_targeted_workers(redis_client: Any, targeting: Optional[BundleTargeting]) -> List[str]:
    """
    Return list of currently live worker IDs that match the bundle targeting.

    Uses the worker readiness heartbeat keys (worker:readiness:{worker_id}).
    A worker is considered live if its heartbeat was seen within WORKER_HEARTBEAT_STALE_SECS.
    """
    try:
        from motet.core.distributed.worker_readiness import WorkerReadinessService
        svc = WorkerReadinessService()
        # get_ready_workers() returns List[str] (worker IDs)
        ready_ids: List[str] = svc.get_ready_workers()
    except Exception as e:
        logger.warning("live_worker_detection_failed", error=str(e))
        return []

    matched = []
    for worker_id in ready_ids:
        # Need WorkerInfo to skip DEPLOYMENT workers and to filter by targeting.worker_tags
        worker_info = svc.get_worker_info(worker_id)
        worker_tags = set(worker_info.capabilities) if worker_info else set()

        # Always skip DEPLOYMENT workers — they are orchestrators, not AI workers.
        # This applies even when targeting is omitted.
        if "deployment" in worker_tags:
            continue

        if targeting is None:
            matched.append(worker_id)
            continue

        # Worker ID filter
        if targeting.worker_ids and worker_id not in targeting.worker_ids:
            continue
        # Worker tag filter (worker must have ALL required tags)
        if targeting.worker_tags and not all(t in worker_tags for t in targeting.worker_tags):
            continue
        matched.append(worker_id)
    return matched


def _read_bundle_files_local(root: Path) -> Dict[str, bytes]:
    """Read local bundle files into {relative_path: bytes}."""
    files: Dict[str, bytes] = {}
    for fp in root.rglob("*"):
        if fp.is_file() and ".git" not in fp.parts and "__pycache__" not in fp.parts:
            rel = str(fp.relative_to(root))
            files[rel] = fp.read_bytes()
    return files


def _compute_bundle_version_local(bundle_files: Dict[str, bytes]) -> str:
    """Compute deterministic content hash for a local bundle source tree."""
    digest = hashlib.sha256()
    for rel_path in sorted(bundle_files.keys()):
        digest.update(rel_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(bundle_files[rel_path])
        digest.update(b"\0")
    return digest.hexdigest()


def _is_hot_deploy_enabled() -> bool:
    """Return True if dev-only hot deploy mode is enabled via env flag."""
    value = os.getenv("MOTET_ENABLE_HOT_DEPLOY", "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _record_hot_deploy_metadata(
    redis_client: Any,
    bundle_id: str,
    bundle_version: str,
    source_fingerprint: str,
    targeting: Optional[BundleTargeting],
    deploy_job_id: str,
    status: str,
) -> None:
    """
    Store current hot deploy state without artifact/latest/history writes.

    This keeps status and catalog visibility for local testing while avoiding
    polluting production artifact-retention and rollback metadata.
    """
    registry = {
        "bundle_id": bundle_id,
        "bundle_version": bundle_version,
        "bundle_ref": "",
        "source_fingerprint": source_fingerprint,
        "manifest_version": "",
        "targeting": json.dumps(targeting.model_dump() if targeting else {}),
        "deploy_job_id": deploy_job_id,
        "status": status,
        "deployed_at": str(int(time.time())),
        "mode": "hot",
    }
    redis_client.hset(_registry_key(bundle_id), mapping=registry)
    redis_client.expire(_registry_key(bundle_id), 365 * 24 * 3600)


# ---------------------------------------------------------------------------
# Deployer worker commands
# ---------------------------------------------------------------------------

@motet.command(
    description="Deploy a bundle from git: validate, publish artifact, and reload on targeted workers.",
    timeout_seconds=300,
    required_capabilities=[WorkerCapability.DEPLOYMENT],
)
def deploy_bundle(data: DeployBundleData) -> Dict[str, Any]:
    """
    Top-level bundle deployment orchestrator (ADR-0071).

    Calls core.validate_bundle (fetch + lint) then core.publish_bundle
    (artifact store + worker reload dispatch). Returns deploy job metadata
    including bundle_id, bundle_version, and per-worker ack status.
    """
    motet = get_motet_context()


    logger.info(
        "deploy_bundle_start",
        repo_url=data.repo_url,
        branch=data.branch,
        path=data.path,
    )

    # Step 1: validate (fetch + lint + no-op detection).
    # conversation_id propagates automatically to child commands via motet.do().
    validated = motet.do(
        validate_bundle,
        data=ValidateBundleData(
            repo_url=data.repo_url,
            branch=data.branch,
            path=data.path,
            targeting=data.targeting,
            repo_creds_path=data.repo_creds_path,
        ),
    )

    # No-op: content unchanged
    if validated.get("deploy_status") == BundleDeployStatus.NO_CHANGE.value:
        return validated

    # Step 2: publish (artifact store + worker reload)
    # validate_bundle returns artifact_b64 (base64-encoded) for JSON-safe transport
    result = motet.do(
        publish_bundle,
        data=PublishBundleData(
            bundle_id=validated["bundle_id"],
            bundle_version=validated["bundle_version"],
            bundle_ref=validated["bundle_ref"],
            source_fingerprint=validated["source_fingerprint"],
            artifact_b64=validated["artifact_b64"],
            targeting=data.targeting,
            manifest_version=validated.get("manifest_version"),
            catalog=validated.get("catalog"),
        ),
    )

    logger.info(
        "deploy_bundle_complete",
        bundle_id=validated["bundle_id"],
        bundle_version=validated["bundle_version"],
        deploy_status=result.get("deploy_status"),
        acked_workers=result.get("acked_workers"),
        failed_workers=result.get("failed_workers"),
    )
    return result


@motet.command(
    description="Dev-only: hot-deploy a bundle from a shared local filesystem path without the full publish pipeline.",
    timeout_seconds=120,
    required_capabilities=[WorkerCapability.DEPLOYMENT],
)
def hot_deploy_bundle(data: HotDeployBundleData) -> Dict[str, Any]:
    """
    Dev-only local hot deploy from a shared filesystem path.

    Reads the local bundle source, optionally lints, computes a local content
    hash, and dispatches core.hot_reload_bundle to live targeted workers.
    """
    if not _is_hot_deploy_enabled():
        raise BundleDeployError(
            "Hot deploy mode is disabled. Set MOTET_ENABLE_HOT_DEPLOY=true in local environments.",
            details={"env_var": "MOTET_ENABLE_HOT_DEPLOY"},
        )

    motet = get_motet_context()
    redis_client = motet.redis
    if redis_client is None:
        raise BundleDeployError("Redis client unavailable", details={})
    bundle_root = Path(data.bundle_path).resolve()
    if not bundle_root.exists() or not bundle_root.is_dir():
        raise BundleDeployError(
            f"Hot deploy bundle path does not exist or is not a directory: {bundle_root}",
            details={"bundle_path": str(bundle_root)},
        )

    bundle_files = _read_bundle_files_local(bundle_root)
    if not bundle_files:
        raise BundleDeployError("Bundle path is empty — no files found", details={"bundle_path": str(bundle_root)})

    manifest, bundle_id = _parse_and_validate_manifest(bundle_files)

    if data.lint:
        lint_passed, all_errors = _lint_bundle(bundle_files)
        fatal_errors = [e for e in all_errors if e.severity == "error"]
        if not lint_passed:
            raise BundleDeployError(
                "Bundle lint failed",
                details={"lint_errors": [e.model_dump() for e in fatal_errors]},
            )

    bundle_version = _compute_bundle_version_local(bundle_files)
    source_fingerprint = f"hot:{bundle_root}"
    existing_entry = _get_registry_entry(redis_client, bundle_id)
    live_workers = _resolve_live_targeted_workers(redis_client, data.targeting)
    if (
        existing_entry
        and existing_entry.get("bundle_version") == bundle_version
        and not _hot_workers_stale_after_restart(redis_client, bundle_id, live_workers)
    ):
        return {
            "deploy_status": BundleDeployStatus.NO_CHANGE.value,
            "bundle_id": bundle_id,
            "bundle_version": bundle_version,
            "bundle_path": str(bundle_root),
            "acked_workers": [],
            "failed_workers": [],
            "skipped_workers": [],
            "catalog": _get_catalog(redis_client, bundle_id) or {},
            "worker_state": _get_worker_state(redis_client, bundle_id),
        }

    catalog = _extract_bundle_catalog(bundle_id, bundle_files)
    if not live_workers:
        raise BundleDeployError(
            "No live targeted workers available for hot deploy",
            details={"bundle_id": bundle_id, "bundle_path": str(bundle_root)},
        )

    from motet.core.bundles.bundle_reload import hot_reload_bundle

    acked: List[str] = []
    failed: List[str] = []
    reload_inputs = [
        {
            "bundle_id": bundle_id,
            "bundle_version": bundle_version,
            "bundle_path": str(bundle_root),
            "targeting": data.targeting.model_dump() if data.targeting else None,
            "target_worker_id": worker_id,
        }
        for worker_id in live_workers
    ]
    reload_results: List[Any] = []
    try:
        results = motet.apply(hot_reload_bundle, inputs=reload_inputs)
        reload_results = list(results or [])
        for i, result in enumerate(reload_results):
            worker_id = live_workers[i] if i < len(live_workers) else f"worker-{i}"
            if isinstance(result, dict) and result.get("_error"):
                failed.append(worker_id)
            else:
                acked.append(worker_id)
                _store_worker_state(
                    redis_client,
                    bundle_id,
                    worker_id,
                    registered_commands=result.get("registered_commands", []) if isinstance(result, dict) else [],
                    registered_tools=result.get("registered_tools", []) if isinstance(result, dict) else [],
                )
    except Exception as e:
        logger.error("hot_deploy_bundle_dispatch_failed", error=str(e), exc_info=True)
        failed = list(live_workers)

    status = BundleDeployStatus.COMPLETE.value if not failed else (BundleDeployStatus.DEGRADED.value if acked else BundleDeployStatus.FAILED.value)
    catalog = _merge_command_schemas_into_catalog(catalog, reload_results)
    _store_catalog(redis_client, bundle_id, bundle_version, catalog, targeting=data.targeting)
    # Surfaces: also registered on each worker via reload; register here so the
    # shared catalog is updated even if a worker reload path is skipped.
    try:
        _register_bundle_surfaces(bundle_id, list(catalog.get("surfaces") or []))
    except Exception as e:
        logger.error(
            "hot_deploy_bundle_surfaces_register_failed",
            bundle_id=bundle_id,
            error=str(e),
            exc_info=True,
        )
        raise BundleDeployError(
            f"Failed to register bundle surfaces: {e}",
            details={"bundle_id": bundle_id},
        ) from e
    _record_hot_deploy_metadata(
        redis_client=redis_client,
        bundle_id=bundle_id,
        bundle_version=bundle_version,
        source_fingerprint=source_fingerprint,
        targeting=data.targeting,
        deploy_job_id=motet.command_id,
        status=status,
    )

    return {
        "deploy_status": status,
        "bundle_id": bundle_id,
        "bundle_version": bundle_version,
        "bundle_path": str(bundle_root),
        "acked_workers": acked,
        "failed_workers": failed,
        "skipped_workers": [],
        "catalog": _get_catalog(redis_client, bundle_id) or {},
        "worker_state": _get_worker_state(redis_client, bundle_id),
    }


UPLOAD_SOURCE_FINGERPRINT = "upload"


@motet.command(
    description="Deploy a bundle from an uploaded zip (no git fetch): validate, publish, and reload workers.",
    timeout_seconds=180,
    required_capabilities=[WorkerCapability.DEPLOYMENT],
)
def deploy_bundle_upload(data: DeployBundleUploadData) -> Dict[str, Any]:
    """
    Deploy a bundle from an uploaded zip (no git fetch).
    Extracts zip → lint → package as tar.gz → publish_bundle.
    Use for local dev or when the deployer worker cannot reach the repo (e.g. file:// in Docker).
    """
    motet = get_motet_context()
    redis_client = motet.redis
    if redis_client is None:
        raise BundleDeployError("Redis client unavailable", details={})

    zip_bytes = base64.b64decode(data.zip_b64)
    if len(zip_bytes) > BUNDLE_SIZE_LIMIT_BYTES:
        raise BundleDeployError(
            f"Upload exceeds 100MB limit ({len(zip_bytes) // (1024 * 1024)} MB).",
            details={"upload_kb": len(zip_bytes) // 1024},
        )

    try:
        bundle_files = _zip_to_bundle_files(zip_bytes)
    except ValueError as e:
        raise BundleDeployError(str(e), details={}) from e

    manifest, bundle_id = _parse_and_validate_manifest(bundle_files)

    try:
        _validate_namespace_ownership(redis_client, bundle_id, UPLOAD_SOURCE_FINGERPRINT)
    except RuntimeError as e:
        raise BundleDeployError(str(e), details={}) from e

    # Lint gate (same as validate_bundle, no stream events for upload)
    all_errors = _collect_lint_errors(bundle_files)
    fatal_errors = _fatal_lint_errors(all_errors)
    if fatal_errors:
        raise BundleDeployError(
            "Bundle lint failed",
            details={"lint_errors": [e.model_dump() for e in fatal_errors]},
        )

    artifact_bytes = _package_artifact(bundle_files)
    if len(artifact_bytes) > BUNDLE_SIZE_LIMIT_BYTES:
        raise BundleDeployError(
            f"Bundle artifact exceeds 100MB limit ({len(artifact_bytes) // (1024 * 1024)} MB).",
            details={"artifact_kb": len(artifact_bytes) // 1024},
        )
    bundle_version = hashlib.sha256(artifact_bytes).hexdigest()
    catalog = _extract_bundle_catalog(bundle_id, bundle_files)

    return motet.do(
        publish_bundle,
        data=PublishBundleData(
            bundle_id=bundle_id,
            bundle_version=bundle_version,
            bundle_ref="",
            source_fingerprint=UPLOAD_SOURCE_FINGERPRINT,
            artifact_b64=base64.b64encode(artifact_bytes).decode(),
            targeting=data.targeting,
            manifest_version=manifest.get("version"),
            catalog=catalog,
        ),
    )


@motet.command(
    description="Lint-validate an uploaded bundle zip without publishing or reloading workers.",
    timeout_seconds=120,
    required_capabilities=[WorkerCapability.DEPLOYMENT],
)
def validate_bundle_upload(data: ValidateBundleUploadData) -> Dict[str, Any]:
    """
    Validate (lint-only) an uploaded zip without deploying.
    Streams lint_file, lint_error, lint_complete via motet.stream_event() for SSE.
    Use POST /api/v1/deploy/validate-upload with multipart zip.
    """
    motet = get_motet_context()
    redis_client = motet.redis
    if redis_client is None:
        raise BundleDeployError("Redis client unavailable", details={})

    zip_bytes = base64.b64decode(data.zip_b64)
    if len(zip_bytes) > BUNDLE_SIZE_LIMIT_BYTES:
        raise BundleDeployError(
            f"Upload exceeds 100MB limit ({len(zip_bytes) // (1024 * 1024)} MB).",
            details={"upload_kb": len(zip_bytes) // 1024},
        )

    try:
        bundle_files = _zip_to_bundle_files(zip_bytes)
    except ValueError as e:
        raise BundleDeployError(str(e), details={}) from e

    manifest, bundle_id = _parse_and_validate_manifest(bundle_files, motet=motet)

    try:
        _validate_namespace_ownership(redis_client, bundle_id, UPLOAD_SOURCE_FINGERPRINT)
    except RuntimeError as e:
        raise BundleDeployError(str(e), details={}) from e

    # Lint gate with stream events (same shape as validate_bundle)
    all_errors = _collect_lint_errors(bundle_files, motet=motet)
    fatal_errors = _fatal_lint_errors(all_errors)
    lint_passed = len(fatal_errors) == 0
    motet.stream_event(
        "lint_complete",
        passed=lint_passed,
        bundle_id=bundle_id,
        errors=[e.model_dump() for e in all_errors],
    )

    if not lint_passed:
        raise BundleDeployError(
            "Bundle lint failed",
            details={"lint_errors": [e.model_dump() for e in fatal_errors]},
        )

    return {
        "bundle_id": bundle_id,
        "passed": True,
        "lint_errors": [e.model_dump() for e in all_errors],
    }


@motet.command(
    description="Fetch a git bundle path and run the lint/validate gate without deploying.",
    timeout_seconds=120,
    required_capabilities=[WorkerCapability.DEPLOYMENT],
)
def validate_bundle(data: ValidateBundleData) -> Dict[str, Any]:
    """
    Fetch the git repo path and run the lint gate (ADR-0071 §3).

    Used by:
    - core.deploy_bundle (via motet.do) before publishing
    - POST /api/v1/deploy/validate directly (validate-only, no artifact publish)

    Streams lint progress via motet.stream_event() to the task stream:
    lint_file, lint_error, lint_complete events — consumed by the SSE endpoint.

    Returns validated bundle metadata + packaged artifact bytes (not stored here).
    Raises BundleDeployError if lint fails (fatal errors found).
    """
    motet = get_motet_context()

    redis_client = motet.redis
    if redis_client is None:
        raise BundleDeployError("Redis client unavailable", details={})
    creds: Optional[str] = None
    if data.repo_creds_path:
        try:
            creds = motet.vault.get_secret(data.repo_creds_path)
        except Exception as e:
            logger.warning("vault_creds_fetch_failed", path=data.repo_creds_path, error=str(e))

    # --- Lightweight no-op check (resolve ref → tree SHA before downloading) ---
    source_fingerprint = _compute_source_fingerprint(data.repo_url, data.path)
    existing_bundle_id = _lookup_bundle_id_by_source(redis_client, source_fingerprint)

    bundle_ref, bundle_version = _resolve_ref_and_tree_sha(
        data.repo_url, data.branch, data.path, credentials=creds
    )

    if bundle_ref and bundle_version and existing_bundle_id:
        current_version = redis_client.get(f"bundle:{existing_bundle_id}:latest")
        if current_version:
            current_str = current_version.decode() if isinstance(current_version, bytes) else current_version
            if current_str == bundle_version:
                logger.info("validate_bundle_no_change", bundle_id=existing_bundle_id, bundle_version=bundle_version)
                motet.stream_event("lint_complete", passed=True, no_change=True, bundle_id=existing_bundle_id, bundle_version=bundle_version)
                return {
                    "deploy_status": BundleDeployStatus.NO_CHANGE.value,
                    "bundle_id": existing_bundle_id,
                    "bundle_version": bundle_version,
                    "bundle_ref": bundle_ref,
                    "source_fingerprint": source_fingerprint,
                }

    # --- Full fetch ---
    logger.info("validate_bundle_fetch_start", repo_url=data.repo_url, branch=data.branch, path=data.path)
    try:
        bundle_files = _fetch_bundle_files(data.repo_url, data.branch, data.path, credentials=creds)
    except RuntimeError as e:
        raise BundleDeployError(f"Bundle fetch failed: {e}", details={"error": str(e)}) from e

    if not bundle_files:
        raise BundleDeployError("Bundle path is empty — no files found", details={})

    # --- Parse + validate manifest ---
    manifest, bundle_id = _parse_and_validate_manifest(bundle_files, motet=motet)

    # --- Namespace ownership check ---
    try:
        _validate_namespace_ownership(redis_client, bundle_id, source_fingerprint)
    except RuntimeError as e:
        raise BundleDeployError(str(e), details={}) from e

    # --- Lint gate ---
    logger.info("validate_bundle_lint_start", bundle_id=bundle_id, file_count=len(bundle_files))
    all_errors = _collect_lint_errors(bundle_files, motet=motet)
    fatal_errors = _fatal_lint_errors(all_errors)
    lint_passed = len(fatal_errors) == 0
    motet.stream_event(
        "lint_complete",
        passed=lint_passed,
        bundle_id=bundle_id,
        errors=[e.model_dump() for e in all_errors],
    )

    if not lint_passed:
        raise BundleDeployError(
            "Bundle lint failed",
            details={"lint_errors": [e.model_dump() for e in fatal_errors]},
        )

    # --- Package artifact (size check) ---
    artifact_bytes = _package_artifact(bundle_files)
    artifact_size = len(artifact_bytes)
    if artifact_size > BUNDLE_SIZE_LIMIT_BYTES:
        raise BundleDeployError(
            f"Bundle artifact exceeds 100MB limit ({artifact_size // (1024 * 1024)} MB). "
            "Bundles are code and config only — move large assets to Motet Artifacts (ADR-0061).",
            details={"artifact_kb": artifact_size // 1024},
        )
    if artifact_size > BUNDLE_SIZE_WARN_BYTES:
        logger.warning(
            "validate_bundle_size_warning",
            bundle_id=bundle_id,
            artifact_kb=artifact_size // 1024,
            message="Bundle exceeds 50MB soft limit — consider moving large assets to Motet Artifacts",
        )

    # --- Extract content catalog (commands, tools, workflows, agents, MCP servers, model IDs) ---
    resolved_version = bundle_version or hashlib.sha256(artifact_bytes).hexdigest()[:16]
    catalog = _extract_bundle_catalog(bundle_id, bundle_files)

    logger.info(
        "validate_bundle_success",
        bundle_id=bundle_id,
        bundle_version=resolved_version,
        artifact_kb=artifact_size // 1024,
        lint_errors=len(all_errors),
        lint_warnings=len([e for e in all_errors if e.severity == "warning"]),
        catalog_commands=catalog["commands"],
    )

    return {
        "bundle_id": bundle_id,
        "bundle_version": resolved_version,
        "bundle_ref": bundle_ref,
        "source_fingerprint": source_fingerprint,
        "manifest_version": manifest.get("version"),
        # Base64-encode so the result can be JSON-serialized through the distributed command chain
        "artifact_b64": base64.b64encode(artifact_bytes).decode(),
        "lint_errors": [e.model_dump() for e in all_errors],
        "catalog": catalog,
    }


@motet.command(
    description="Write a signed bundle artifact to the shared store and dispatch reload on targeted workers.",
    timeout_seconds=180,
    required_capabilities=[WorkerCapability.DEPLOYMENT],
)
def publish_bundle(data: PublishBundleData) -> Dict[str, Any]:
    """
    Write signed artifact to the shared store and dispatch core.reload_bundle
    to each live targeted AI worker (ADR-0071 §2 publish step).

    Returns aggregated ack/fail/skip worker lists and overall deploy status.
    """
    motet = get_motet_context()
    import base64

    redis_client = motet.redis
    if redis_client is None:
        raise BundleDeployError("Redis client unavailable", details={})

    # --- Decode and size-check artifact ---
    artifact_bytes = base64.b64decode(data.artifact_b64)
    if len(artifact_bytes) > BUNDLE_SIZE_LIMIT_BYTES:
        raise BundleDeployError(
            "Bundle artifact exceeds 100MB hard limit",
            details={"artifact_kb": len(artifact_bytes) // 1024},
        )

    # --- Write to artifact store ---
    _store_artifact(redis_client, data.bundle_id, data.bundle_version, artifact_bytes)
    logger.info(
        "publish_bundle_artifact_stored",
        bundle_id=data.bundle_id,
        bundle_version=data.bundle_version,
        artifact_kb=len(artifact_bytes) // 1024,
    )

    # --- Record deploy metadata ---
    _record_deploy_metadata(
        redis_client,
        bundle_id=data.bundle_id,
        bundle_version=data.bundle_version,
        bundle_ref=data.bundle_ref,
        source_fingerprint=data.source_fingerprint,
        manifest_version=data.manifest_version,
        targeting=data.targeting,
        deploy_job_id=motet.command_id,
    )

    # --- ADR-0100: optionally build & pin the bundle exec image ---
    # When MOTET_DEPLOYER_BUILD_ENABLED=true (the early-stage default) and the
    # bundle declared config/exec.yaml *without* a pre-pinned ``oci_image_ref``,
    # the deployer worker builds the exec image from the bundle's
    # requirements.txt, pushes it to MOTET_DEPLOYER_BUILD_REGISTRY, and rewrites
    # the catalog ``exec.oci_image_ref`` to ``image@sha256:...`` so the
    # downstream worker_exec path pulls a digest-pinned image (ADR-0100 §rule 2).
    #
    # This is a *deployer*-only capability: ADR-0100 §"In-worker OCI build is
    # forbidden" still binds runtime workers. Build failures are publish
    # failures — we never half-publish a bundle whose declared dependencies
    # could not be baked, because that would silently regress to runtime
    # ``pip install`` (ADR-0100 §rule 1).
    catalog_to_store: Optional[Dict[str, Any]] = data.catalog
    if data.catalog:
        # Imported outside the try on purpose: the handler below catches
        # BundleImageBuildError, so the name must be bound before the try is
        # entered. Importing it inside means an ImportError leaves the name
        # unbound, and evaluating the except clause raises UnboundLocalError
        # over the top of the real failure.
        from motet.core.bundles.bundle_image_build import (
            BundleImageBuildError,
            build_and_pin_exec_image,
        )

        try:
            exec_meta = data.catalog.get("exec") or {}
            bundle_files = _artifact_to_bundle_files(artifact_bytes) if exec_meta else {}
            updated_exec = build_and_pin_exec_image(
                bundle_id=data.bundle_id,
                bundle_version=data.bundle_version,
                bundle_files=bundle_files,
                exec_meta=exec_meta,
            )
            if updated_exec is not None:
                # Don't mutate the input model — make a shallow copy of catalog
                # and replace the exec block.
                catalog_to_store = dict(data.catalog)
                catalog_to_store["exec"] = updated_exec
                logger.info(
                    "publish_bundle_exec_image_pinned",
                    bundle_id=data.bundle_id,
                    bundle_version=data.bundle_version,
                    oci_image_ref=updated_exec.get("oci_image_ref"),
                )
        except BundleImageBuildError as e:
            # Re-wrap so the standard ADR-0029 error envelope picks up details.
            logger.error(
                "publish_bundle_exec_image_build_failed",
                bundle_id=data.bundle_id,
                bundle_version=data.bundle_version,
                error=str(e),
                details=getattr(e, "details", {}),
            )
            raise BundleDeployError(
                f"Bundle exec image build failed: {e}",
                details={
                    "bundle_id": data.bundle_id,
                    "bundle_version": data.bundle_version,
                    **getattr(e, "details", {}),
                },
            ) from e

    # --- ADR-0100 §rule 2 enforcement (opt-in via MOTET_REQUIRE_DIGEST_PINNED_PUBLISH) ---
    # Runs *after* the deployer build hook so a deployer-rewritten mutable tag
    # passes; only operator-pre-pinned mutable refs (or bundle-supplied mutable
    # refs with deployer build disabled) are caught here.
    if catalog_to_store:
        _enforce_publish_digest_pinning(
            data.bundle_id,
            catalog_to_store.get("exec") or {},
        )

    # --- Store content catalog (schemas filled after reload acks below) ---
    if catalog_to_store:
        _store_catalog(
            redis_client,
            data.bundle_id,
            data.bundle_version,
            catalog_to_store,
            targeting=data.targeting,
        )
        logger.info(
            "publish_bundle_catalog_stored",
            bundle_id=data.bundle_id,
            commands=catalog_to_store.get("commands", []),
            agents=catalog_to_store.get("agents", []),
            surfaces=[
                s.get("id")
                for s in (catalog_to_store.get("surfaces") or [])
                if isinstance(s, dict)
            ],
        )

        # Register bundle-declared surfaces into the shared surfaces catalog.
        # Existing ids are no-ops (metadata not overwritten).
        try:
            surface_stats = _register_bundle_surfaces(
                data.bundle_id,
                list(catalog_to_store.get("surfaces") or []),
            )
            logger.info(
                "publish_bundle_surfaces_registered",
                bundle_id=data.bundle_id,
                **surface_stats,
            )
        except Exception as e:
            logger.error(
                "publish_bundle_surfaces_register_failed",
                bundle_id=data.bundle_id,
                error=str(e),
                exc_info=True,
            )
            raise BundleDeployError(
                f"Failed to register bundle surfaces: {e}",
                details={"bundle_id": data.bundle_id},
            ) from e

    # --- Dispatch reload to live targeted workers ---
    from motet.core.bundles.bundle_reload import reload_bundle, ReloadBundleData

    live_workers = _resolve_live_targeted_workers(redis_client, data.targeting)
    logger.info("publish_bundle_dispatch_reload", bundle_id=data.bundle_id, live_workers=live_workers)

    acked: List[str] = []
    failed: List[str] = []
    reload_results: List[Any] = []

    if live_workers:
        # conversation_id propagates automatically via motet.apply() execution context.
        # One reload per worker with target_worker_id so routing sends each to the intended worker
        # (avoids duplicate reloads on the same worker when map routes by capability only).
        reload_inputs = [
            {
                "bundle_id": data.bundle_id,
                "bundle_version": data.bundle_version,
                "targeting": data.targeting.model_dump() if data.targeting else None,
                "target_worker_id": worker_id,
            }
            for worker_id in live_workers
        ]
        try:
            results = motet.apply(reload_bundle, inputs=reload_inputs)
            reload_results = list(results or [])
            for i, result in enumerate(reload_results):
                worker_id = live_workers[i] if i < len(live_workers) else f"worker-{i}"
                if isinstance(result, dict) and result.get("_error"):
                    failed.append(worker_id)
                    logger.warning("publish_bundle_worker_reload_failed", worker_id=worker_id, error=result.get("message"))
                else:
                    acked.append(worker_id)
                    # Store per-worker loaded state from the ack report
                    _store_worker_state(
                        redis_client,
                        data.bundle_id,
                        worker_id,
                        registered_commands=result.get("registered_commands", []) if isinstance(result, dict) else [],
                        registered_tools=result.get("registered_tools", []) if isinstance(result, dict) else [],
                    )
        except Exception as e:
            logger.error("publish_bundle_reload_dispatch_failed", error=str(e), exc_info=True)
            failed = live_workers

    # Merge worker-harvested JSON schemas into the catalog after successful imports.
    if catalog_to_store is not None:
        catalog_to_store = _merge_command_schemas_into_catalog(
            catalog_to_store, reload_results
        )
        _store_catalog(
            redis_client,
            data.bundle_id,
            data.bundle_version,
            catalog_to_store,
            targeting=data.targeting,
        )
        logger.info(
            "publish_bundle_command_schemas_merged",
            bundle_id=data.bundle_id,
            schema_count=len(catalog_to_store.get("command_schemas") or {}),
        )

    # Compute overall status
    if len(failed) == 0:
        overall_status = BundleDeployStatus.COMPLETE.value
    elif len(acked) > 0:
        overall_status = BundleDeployStatus.DEGRADED.value
    else:
        overall_status = BundleDeployStatus.FAILED.value

    return {
        "deploy_status": overall_status,
        "bundle_id": data.bundle_id,
        "bundle_version": data.bundle_version,
        "bundle_ref": data.bundle_ref,
        "acked_workers": acked,
        "failed_workers": failed,
        "skipped_workers": [],
        "catalog": data.catalog or {},
        "worker_state": _get_worker_state(redis_client, data.bundle_id),
    }


@motet.command(
    description="Undeploy a bundle cluster-wide: unload from targeted workers and cancel related schedules.",
    timeout_seconds=180,
    required_capabilities=[WorkerCapability.DEPLOYMENT],
)
def undeploy_bundle(data: UndeployBundleData) -> Dict[str, Any]:
    """
    Unload a bundle from all targeted AI workers and cancel associated schedules (ADR-0071 §6).

    Dispatches core.unload_bundle to each live worker, then invalidates any
    schedules that referenced this bundle_id.
    """
    motet = get_motet_context()
    from motet.core.bundles.bundle_reload import unload_bundle, UnloadBundleData as UData

    redis_client = motet.redis
    if redis_client is None:
        raise RuntimeError("Redis client unavailable for undeploy")
    entry = _get_registry_entry(redis_client, data.bundle_id)
    if not entry:
        raise RuntimeError(f"Bundle '{data.bundle_id}' not found in registry")

    targeting_raw = entry.get("targeting")
    targeting: Optional[BundleTargeting] = None
    if targeting_raw:
        try:
            targeting_dict = json.loads(targeting_raw) if isinstance(targeting_raw, str) else targeting_raw
            targeting = BundleTargeting(**targeting_dict)
        except Exception:
            pass  # targeting parse best-effort; continue without

    live_workers = _resolve_live_targeted_workers(redis_client, targeting)
    acked: List[str] = []
    failed: List[str] = []

    if live_workers:
        # One unload per worker with target_worker_id so routing sends each to the
        # intended worker (mirrors the publish_bundle reload dispatch pattern).
        unload_inputs = [
            {"bundle_id": data.bundle_id, "target_worker_id": worker_id}
            for worker_id in live_workers
        ]
        try:
            results = motet.apply(unload_bundle, inputs=unload_inputs)
            for i, result in enumerate(results):
                worker_id = live_workers[i] if i < len(live_workers) else f"worker-{i}"
                if isinstance(result, dict) and result.get("_error"):
                    failed.append(worker_id)
                else:
                    acked.append(worker_id)
        except Exception as e:
            logger.error("undeploy_bundle_dispatch_failed", error=str(e), exc_info=True)
            failed = live_workers

    # Cancel schedules referencing this bundle_id
    _cancel_bundle_schedules(data.bundle_id)

    # Remove all bundle keys for this bundle_id
    redis_client.delete(_registry_key(data.bundle_id))
    redis_client.delete(_catalog_key(data.bundle_id))
    redis_client.delete(_worker_state_key(data.bundle_id))
    redis_client.delete(f"bundle:{data.bundle_id}:latest")
    redis_client.delete(f"bundle:{data.bundle_id}:history")
    redis_client.delete(f"bundle:{data.bundle_id}:versions")

    logger.info(
        "undeploy_bundle_complete",
        bundle_id=data.bundle_id,
        acked_workers=acked,
        failed_workers=failed,
    )
    return {
        "deploy_status": "undeployed" if not failed else "partial",
        "bundle_id": data.bundle_id,
        "acked_workers": acked,
        "failed_workers": failed,
    }


@motet.command(
    description="Roll back to a prior stored bundle artifact version without re-fetching or re-linting from git.",
    timeout_seconds=180,
    required_capabilities=[WorkerCapability.DEPLOYMENT],
)
def rollback_bundle(data: RollbackBundleData) -> Dict[str, Any]:
    """
    Re-deploy a prior stored artifact without re-fetching or re-linting (ADR-0071 §Considerations).

    Reads the artifact for the specified bundle_version from the shared store
    and dispatches core.reload_bundle to live targeted workers.
    """
    motet = get_motet_context()
    import base64

    logger.info(
        "rollback_bundle_start",
        bundle_id=data.bundle_id,
        bundle_version=data.bundle_version,
    )
    redis_client = motet.redis
    if redis_client is None:
        raise BundleDeployError("Redis client unavailable", details={})
    artifact_bytes = _fetch_artifact(redis_client, data.bundle_id, data.bundle_version)
    if not artifact_bytes:
        raise BundleDeployError(
            f"Artifact for bundle '{data.bundle_id}' version '{data.bundle_version}' not found in store. "
            "It may have been evicted (retention window). Try re-deploying from git.",
            details={"bundle_id": data.bundle_id, "bundle_version": data.bundle_version},
        )

    entry = _get_registry_entry(redis_client, data.bundle_id)
    targeting: Optional[BundleTargeting] = None
    if entry and entry.get("targeting"):
        try:
            targeting = BundleTargeting(**json.loads(entry["targeting"]))
        except Exception:
            pass  # targeting parse best-effort; continue without

    # conversation_id from the execution context flows through motet.do() automatically
    result = motet.do(
        publish_bundle,
        data=PublishBundleData(
            bundle_id=data.bundle_id,
            bundle_version=data.bundle_version,
            bundle_ref=entry.get("bundle_ref", "") if entry else "",
            source_fingerprint=entry.get("source_fingerprint", "") if entry else "",
            artifact_b64=base64.b64encode(artifact_bytes).decode(),
            targeting=targeting,
            manifest_version=entry.get("manifest_version") if entry else None,
        ),
    )
    logger.info(
        "rollback_bundle_outcome",
        bundle_id=data.bundle_id,
        bundle_version=data.bundle_version,
        deploy_status=result.get("deploy_status"),
        acked_workers=result.get("acked_workers"),
        failed_workers=result.get("failed_workers"),
    )
    return result


@motet.command(
    description="Retry bundle reload on live targeted workers for an already-published bundle version.",
    timeout_seconds=180,
    required_capabilities=[WorkerCapability.DEPLOYMENT],
)
def propagate_bundle(data: PropagateBundleData) -> Dict[str, Any]:
    """
    Retry reload on live targeted workers for an already-published bundle
    without re-fetching or re-linting (ADR-0071 §2 propagate endpoint).

    Artifact-backed bundles use ``core.reload_bundle``. Hot-mode bundles
    (no Redis artifact) use ``core.hot_reload_bundle`` from the
    ``hot:<path>`` fingerprint so propagate recovers after worker restarts
    (#125).
    """
    motet = get_motet_context()

    from motet.core.bundles.bundle_reload import (
        hot_reload_bundle,
        reload_bundle,
    )

    redis_client = motet.redis
    if redis_client is None:
        raise BundleDeployError("Redis client unavailable", details={})
    entry = _get_registry_entry(redis_client, data.bundle_id)
    if not entry:
        raise BundleDeployError(f"Bundle '{data.bundle_id}' not found in registry", details={})

    bundle_version = entry.get("bundle_version", "")
    fingerprint = entry.get("source_fingerprint") or ""
    if isinstance(fingerprint, bytes):
        fingerprint = fingerprint.decode()
    mode = entry.get("mode") or ""
    is_hot = mode == "hot" or (
        isinstance(fingerprint, str) and fingerprint.startswith("hot:")
    )

    targeting: Optional[BundleTargeting] = None
    if entry.get("targeting"):
        try:
            targeting_raw = entry["targeting"]
            if isinstance(targeting_raw, str):
                targeting = BundleTargeting(**json.loads(targeting_raw))
            elif isinstance(targeting_raw, dict):
                targeting = BundleTargeting(**targeting_raw)
        except Exception:
            pass  # targeting parse best-effort; continue without

    live_workers = _resolve_live_targeted_workers(redis_client, targeting)
    acked: List[str] = []
    failed: List[str] = []

    if not live_workers:
        logger.warning(
            "propagate_bundle_no_live_workers",
            bundle_id=data.bundle_id,
            bundle_version=bundle_version,
            mode="hot" if is_hot else "artifact",
        )
        return {
            "deploy_status": BundleDeployStatus.FAILED.value,
            "bundle_id": data.bundle_id,
            "bundle_version": bundle_version,
            "acked_workers": [],
            "failed_workers": [],
            "note": "No live targeted workers available for propagate",
        }

    if is_hot:
        if not isinstance(fingerprint, str) or not fingerprint.startswith("hot:"):
            raise BundleDeployError(
                f"Hot bundle '{data.bundle_id}' is missing a hot: source fingerprint",
                details={
                    "bundle_id": data.bundle_id,
                    "bundle_version": bundle_version,
                    "source_fingerprint": fingerprint,
                },
            )
        bundle_path = fingerprint[len("hot:") :]
        reload_inputs = [
            {
                "bundle_id": data.bundle_id,
                "bundle_version": bundle_version,
                "bundle_path": bundle_path,
                "targeting": targeting.model_dump() if targeting else None,
                "target_worker_id": worker_id,
            }
            for worker_id in live_workers
        ]
        reload_cmd = hot_reload_bundle
    else:
        artifact_bytes = _fetch_artifact(redis_client, data.bundle_id, bundle_version)
        if not artifact_bytes:
            raise BundleDeployError(
                f"Artifact for '{data.bundle_id}' version '{bundle_version}' not available",
                details={"bundle_id": data.bundle_id, "bundle_version": bundle_version},
            )
        reload_inputs = [
            {
                "bundle_id": data.bundle_id,
                "bundle_version": bundle_version,
                "targeting": targeting.model_dump() if targeting else None,
                "target_worker_id": worker_id,
            }
            for worker_id in live_workers
        ]
        reload_cmd = reload_bundle

    reload_results: List[Any] = []
    try:
        results = motet.apply(reload_cmd, inputs=reload_inputs)
        reload_results = list(results or [])
        for i, result in enumerate(reload_results):
            worker_id = live_workers[i] if i < len(live_workers) else f"worker-{i}"
            if isinstance(result, dict) and result.get("_error"):
                failed.append(worker_id)
            else:
                acked.append(worker_id)
                if isinstance(result, dict):
                    _store_worker_state(
                        redis_client,
                        data.bundle_id,
                        worker_id,
                        registered_commands=result.get("registered_commands", []),
                        registered_tools=result.get("registered_tools", []),
                    )
    except Exception as e:
        logger.error("propagate_bundle_reload_failed", error=str(e), exc_info=True)
        failed = list(live_workers)

    # Refresh catalog schemas from worker acks (covers pre-schema catalogs).
    existing_catalog = _get_catalog(redis_client, data.bundle_id) or {
        "commands": [],
        "command_schemas": {},
    }
    updated_catalog = _merge_command_schemas_into_catalog(
        existing_catalog, reload_results
    )
    if updated_catalog.get("command_schemas") != existing_catalog.get("command_schemas"):
        _store_catalog(
            redis_client,
            data.bundle_id,
            bundle_version,
            updated_catalog,
            targeting=targeting,
        )

    status = (
        BundleDeployStatus.COMPLETE.value
        if not failed
        else (
            BundleDeployStatus.DEGRADED.value
            if acked
            else BundleDeployStatus.FAILED.value
        )
    )
    return {
        "deploy_status": status,
        "bundle_id": data.bundle_id,
        "bundle_version": bundle_version,
        "acked_workers": acked,
        "failed_workers": failed,
        "mode": "hot" if is_hot else "artifact",
    }


# ---------------------------------------------------------------------------
# Schedule cancellation helper
# ---------------------------------------------------------------------------

def _cancel_bundle_schedules(bundle_id: str) -> None:
    """Cancel/invalidate all active schedules that reference bundle_id."""
    try:
        from motet.core.orchestration.scheduling.manager import ScheduledCommandManager
        manager = ScheduledCommandManager()
        all_schedules = manager.list_schedules()
        for sched in all_schedules:
            # ScheduleMetadata stores command payload in metadata["original_command_data"]
            cmd_data = (sched.metadata or {}).get("original_command_data") if hasattr(sched, "metadata") else {}
            sched_bundle = getattr(sched, "bundle_id", None) or (cmd_data.get("bundle_id") if isinstance(cmd_data, dict) else None)
            if sched_bundle == bundle_id:
                try:
                    manager.delete_schedule(sched.schedule_id)
                    logger.info("bundle_schedule_cancelled", schedule_id=sched.schedule_id, bundle_id=bundle_id)
                except Exception as e:
                    logger.warning("bundle_schedule_cancel_failed", schedule_id=getattr(sched, "schedule_id", "?"), error=str(e))
    except Exception as e:
        logger.warning("cancel_bundle_schedules_failed", bundle_id=bundle_id, error=str(e))
