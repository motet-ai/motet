"""
Motet - Workspace Container Manager

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Lifecycle owner for per-conversation workspace containers introduced by
    ("Per-Workspace Container on the Existing Tier").

    Slice A scope:
    * ``mode: cold`` — ``docker exec`` argv dispatch into a long-lived
        container (``dispatch``). Each call is a fresh process; only
        ``/scratch`` and the container's filesystem persist between calls.

    Slice B scope (this module):
    * ``mode: warm`` — ship the warm supervisor + the runner author's
        skill module into the container, run the supervisor as a
        long-lived background process, and dispatch each call as a
        ``handle(params)`` round-trip via ``dispatch_warm``. Module-level
        globals (loaded models, cached connections, counters) survive
        between calls within a workspace.

    Out of scope for Slice B (handled in later slices):
    * Disk-quota enforcement via storage-opt or sized volumes (Slice C)
    * Ops UI panel + workspace containers API (Slice C)

    The user-visible contract — "calls within a conversation share /scratch
    and (optionally) running processes" — is implemented in this module by
    creating one container per (tenant, conversation, bundle, skill, image_stack)
    tuple and dispatching each runner invocation as a `docker exec` into that
    container. Cold dispatch may materialize manager-owned input files before exec, and the
    registry is updated with distributed in-flight activity markers so the
    idle reaper does not kill a container mid-exec.

Dependencies:
    - motet.core.distributed.workspace_container_registry: Routing primitive
    - motet.core.distributed.redis_manager: Distributed lock helpers
    - motet.core.execution.docker_client: Engine API wrappers
    - motet.core.execution.models: ExecutionRequest / ExecutionResult contract

Usage:
    from motet.core.execution.workspace_container_manager import (
        WorkspaceContainerManager,
        get_workspace_container_manager,
        is_workspace_container_enabled,
    )

    manager = get_workspace_container_manager()
    result = manager.dispatch(
        request,
        tenant_id="tenant-a",
        conversation_id="conv-1",
        image_stack="python-minimal",
        mode="cold",
    )

Notes:
    - **Tier B substrate (runc/Docker).** This manager intentionally lives in
      Tier B per. Phase 2 will swap the substrate to
      Firecracker / Kata-fc behind the same routing key shape; the
      ``runners.yaml`` author contract does not change.
    - **No worker affinity.** Any worker may dispatch to any workspace
      container by reading the registry. The
      ``worker_attribution`` field on a binding is observability only.
    - **Best-effort reaping.** ``reap_idle()`` is a sweep that walks the
      registry, kills containers idle for longer than the configured TTL,
      and unbinds them. It is safe to call from a Celery beat task or a
      background loop. Slice C wires it into a beat schedule.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from importlib import resources
from typing import Any, Dict, List, Optional

import structlog

from motet.core.distributed.redis_manager import acquire_distributed_lock_sync
from motet.core.distributed.workspace_container_registry import (
    DEFAULT_WORKSPACE_SCOPE_BUNDLE_ID,
    DEFAULT_WORKSPACE_SCOPE_SKILL_NAME,
    WorkspaceContainerBinding,
    WorkspaceContainerMode,
    WorkspaceContainerRegistry,
)

from . import docker_client
from .capture import truncate_output_pair
from .models import ExecutionInputFile, ExecutionRequest, ExecutionResult

logger = structlog.get_logger(__name__)

_SCRATCH_DIR_DEFAULT = "/scratch"
_SLEEPER_ARGV = ["sh", "-c", "trap : TERM INT; tail -f /dev/null & wait"]
_BACKEND_LABEL = "workspace-container"

# ADR-0106 Slice B (stateful mode) container layout. These paths are
# manager-owned: runner authors NEVER need to know they exist; they
# write a skill module that defines ``handle(params)`` and the manager
# stages it into the container at module-load time.
_WARM_DIR = "/motet"
_WARM_SUPERVISOR_PATH = f"{_WARM_DIR}/_warm_supervisor.py"
_WARM_CLIENT_PATH = f"{_WARM_DIR}/_warm_client.py"
_WARM_SKILL_MODULE_PATH = f"{_WARM_DIR}/skill_module.py"
_WARM_SOCKET_PATH = f"{_WARM_DIR}/warm.sock"
_WARM_MARKER_PATH = f"{_WARM_DIR}/.bootstrapped"
_WARM_BOOTSTRAP_TIMEOUT_SECONDS = 15.0
_WARM_BOOTSTRAP_POLL_INTERVAL = 0.25


def _workspace_container_env(name: str) -> Optional[str]:
    """Read a workspace-container env var by suffix."""
    return os.getenv(f"MOTET_WORKSPACE_CONTAINER_{name}")


def is_workspace_container_enabled() -> bool:
    """Master kill-switch from ADR-0106 §Configuration.

    When False, callers MUST silently downgrade to hermetic per-call
    execution. The check lives here (not in ``WorkspaceContainerManager``
    constructor) so that tests can flip the env var at runtime without
    rebuilding the manager.
    """
    raw = (_workspace_container_env("ENABLED") or "true").strip().lower()
    return raw not in ("0", "false", "no", "off")


def is_stateful_mode_enabled() -> bool:
    """Operator gate for ``lifetime: stateful`` from ADR-0106 §Configuration.

    When False, ``run_stateful_in_workspace`` MUST downgrade ``lifetime: stateful``
    declarations to ``lifetime: workspace`` (same per-workspace container; loses
    in-process state, keeps ``/scratch``). Independent of the master
    kill-switch ``MOTET_WORKSPACE_CONTAINER_ENABLED``: an operator can
    keep workspace containers and turn off stateful without touching either knob's
    sibling.
    """
    raw = (os.getenv("MOTET_WORKSPACE_STATEFUL_MODE_ENABLED") or "true").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _max_per_tenant() -> int:
    raw = (_workspace_container_env("MAX_PER_TENANT") or "100").strip()
    try:
        v = int(raw)
        if v > 0:
            return v
    except ValueError:
        logger.warning("workspace_container_manager.invalid_max_per_tenant", raw=raw)
    return 100


def _scratch_dir() -> str:
    raw = (_workspace_container_env("SCRATCH_DIR") or _SCRATCH_DIR_DEFAULT).strip()
    return raw or _SCRATCH_DIR_DEFAULT


def _default_image() -> str:
    return (
        _workspace_container_env("DEFAULT_IMAGE")
        or os.getenv("MOTET_WORKER_EXEC_DOCKER_IMAGE")
        or "python:3.11-slim"
    ).strip()


def _network_mode_default() -> str:
    raw = (_workspace_container_env("NETWORK") or "default").strip()
    return raw or "default"


def _warm_bootstrap_timeout_seconds() -> float:
    raw = (_workspace_container_env("WARM_BOOTSTRAP_TIMEOUT") or "").strip()
    if not raw:
        return _WARM_BOOTSTRAP_TIMEOUT_SECONDS
    try:
        v = float(raw)
        if v > 0:
            return v
    except ValueError:
        pass
    return _WARM_BOOTSTRAP_TIMEOUT_SECONDS


def _warm_runtime_source(filename: str) -> bytes:
    """Read a packaged warm-runtime asset (supervisor / client) as bytes.

    The warm supervisor and warm client live alongside ``runners.py`` in
    ``motet.core.skills`` so they are versioned with the rest of the
    runner code; the manager loads them via ``importlib.resources`` so
    we work whether Motet is installed as a wheel or run from a checkout.
    """
    return resources.files("motet.core.skills").joinpath(filename).read_bytes()


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class WarmBootstrapPlan:
    """Everything the manager needs to bootstrap a warm workspace container.

    The author-supplied ``script_source`` is the *exact* bytes that end up
    at ``/motet/skill_module.py`` inside the container — the runtime
    keeps a SHA-256 of it on the binding so a redeploy that changes the
    skill source forces a clean container rather than silently keeping
    stale globals.
    """

    script_source: bytes
    script_logical_name: str
    script_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        # frozen=True forbids assignment; use object.__setattr__ for the
        # derived field. Keeping script_sha256 derived (vs caller-supplied)
        # eliminates the chance of a mismatched digest sneaking in.
        object.__setattr__(self, "script_sha256", _sha256_hex(self.script_source))


class WorkspaceContainerManager:
    """Owns the lifecycle of per-conversation workspace containers.

    Designed as a singleton per worker process: the global instance is
    obtained via :func:`get_workspace_container_manager`. Multiple workers
    share routing through the Redis-backed registry, so the singleton
    invariant is per-process, not global.
    """

    LOCK_CLIENT = "workspace_container_manager"
    LOCK_TTL_SECONDS = 60

    def __init__(
        self,
        *,
        registry: Optional[WorkspaceContainerRegistry] = None,
        worker_id: Optional[str] = None,
    ) -> None:
        self._registry = registry or WorkspaceContainerRegistry()
        self._worker_id = worker_id or os.getenv("MOTET_WORKER_ID") or os.getenv("HOSTNAME")
        self._lock_per_id_client = self.LOCK_CLIENT

    @property
    def registry(self) -> WorkspaceContainerRegistry:
        return self._registry

    def dispatch(
        self,
        request: ExecutionRequest,
        *,
        tenant_id: str,
        conversation_id: str,
        image_stack: str,
        bundle_id: str = DEFAULT_WORKSPACE_SCOPE_BUNDLE_ID,
        skill_name: str = DEFAULT_WORKSPACE_SCOPE_SKILL_NAME,
        mode: WorkspaceContainerMode = "cold",
        oci_image_ref: Optional[str] = None,
    ) -> ExecutionResult:
        """Resolve workspace container, run argv via docker exec, return result.

        ADR-0106 §"Reference flow": resolve binding → exec into container →
        refresh TTL. Lazy-creates a fresh container on the first call for
        a given (tenant, conversation_id, bundle_id, skill_name, image_stack)
        tuple.

        Args:
            request: Argv + cwd + timeout per the canonical execution
                contract. ``cwd`` is mapped to ``MOTET_WORKSPACE_CONTAINER_SCRATCH_DIR``
                (default ``/scratch``) inside the container.
            tenant_id: Tenant identity. MUST match the request context;
                the manager does not infer it.
            conversation_id: Conversation identity. Defines workspace lifetime.
            image_stack: Platform image stack (ADR-0101 Slice A) used as one
                routing-key dimension.
            bundle_id: Bundle scope for runner-owned conversation workspaces.
            skill_name: Skill scope for runner-owned conversation workspaces.
            mode: ``"cold"`` for argv per-call dispatch. Stateful-mode callers
                MUST use :meth:`dispatch_warm` instead — the dispatch
                contracts differ enough that funneling them through a
                single method would be more confusing than helpful.
            oci_image_ref: Optional pinned image ref. Falls back to
                ``MOTET_WORKSPACE_CONTAINER_DEFAULT_IMAGE`` then
                ``MOTET_WORKER_EXEC_DOCKER_IMAGE`` then ``python:3.11-slim``.

        Returns:
            ExecutionResult with ``backend == "workspace-container"``.
        """
        if mode != "cold":
            return ExecutionResult(
                exit_code=-1,
                backend=_BACKEND_LABEL,
                error=(
                    f"WorkspaceContainerManager.dispatch only supports mode='cold'; "
                    "use dispatch_warm() for stateful-mode (ADR-0106 Slice B) calls"
                ),
            )

        sock_path, sock_err = docker_client.docker_socket_path()
        if sock_err:
            return ExecutionResult(exit_code=-1, backend=_BACKEND_LABEL, error=sock_err)
        assert sock_path is not None

        if not os.path.exists(sock_path):
            return ExecutionResult(
                exit_code=-1,
                backend=_BACKEND_LABEL,
                error=f"Docker unix socket not found at {sock_path!r}",
            )

        prefix = docker_client.api_prefix()

        binding, create_err = self._get_or_create_binding(
            sock_path=sock_path,
            prefix=prefix,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            image_stack=image_stack,
            bundle_id=bundle_id,
            skill_name=skill_name,
            mode=mode,
            oci_image_ref=oci_image_ref,
            warm_plan=None,
        )
        if create_err:
            return ExecutionResult(
                exit_code=-1,
                backend=_BACKEND_LABEL,
                error=create_err,
            )
        assert binding is not None

        return self._exec_in_container(
            sock_path=sock_path,
            prefix=prefix,
            binding=binding,
            request=request,
        )

    def dispatch_warm(
        self,
        *,
        tenant_id: str,
        conversation_id: str,
        image_stack: str,
        oci_image_ref: Optional[str],
        warm_plan: WarmBootstrapPlan,
        params: Dict[str, Any],
        bundle_id: str = DEFAULT_WORKSPACE_SCOPE_BUNDLE_ID,
        skill_name: str = DEFAULT_WORKSPACE_SCOPE_SKILL_NAME,
        timeout_seconds: Optional[int] = None,
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """ADR-0106 Slice B: dispatch ``handle(params)`` into the warm container.

        On the first call for a given (tenant, conversation, bundle, skill,
        image_stack) tuple, the manager:

          1. Lazily creates the workspace container.
          2. Bootstraps it with the warm supervisor + the runner author's
             skill module (shipped via ``docker put archive``).
          3. Starts the supervisor as a long-lived background process via
             ``docker exec -d``.
          4. Polls the in-container marker file until the supervisor is
             accepting connections (default timeout 15s).

        On every subsequent call, only steps 5 and 6 run:

          5. Encode the JSON request and ``docker exec`` the warm client
             inside the container, which connects to the supervisor's
             UNIX socket and exchanges one request/response.
          6. Demux the client's stdout, parse the supervisor's envelope,
             refresh the routing-key TTL.

        Returns the supervisor envelope augmented with manager-side
        metadata (``container_id``, ``oci_image_ref``, ``timed_out``,
        and a manager-emitted ``ok=false`` envelope on transport
        failures so callers always get the same shape). Errors do
        **not** raise — the result envelope's ``ok`` field is the
        success signal.
        """
        envelope_id = request_id or uuid.uuid4().hex

        sock_path, sock_err = docker_client.docker_socket_path()
        if sock_err:
            return _warm_transport_error(envelope_id, sock_err)
        assert sock_path is not None
        if not os.path.exists(sock_path):
            return _warm_transport_error(
                envelope_id, f"Docker unix socket not found at {sock_path!r}"
            )

        prefix = docker_client.api_prefix()

        binding, create_err = self._get_or_create_binding(
            sock_path=sock_path,
            prefix=prefix,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            image_stack=image_stack,
            bundle_id=bundle_id,
            skill_name=skill_name,
            mode="warm",
            oci_image_ref=oci_image_ref,
            warm_plan=warm_plan,
        )
        if create_err:
            return _warm_transport_error(envelope_id, create_err)
        assert binding is not None

        return self._dispatch_warm_in_container(
            sock_path=sock_path,
            prefix=prefix,
            binding=binding,
            warm_plan=warm_plan,
            params=params,
            timeout_seconds=timeout_seconds,
            envelope_id=envelope_id,
        )

    def release(
        self,
        *,
        tenant_id: str,
        conversation_id: str,
        image_stack: str,
        bundle_id: str = DEFAULT_WORKSPACE_SCOPE_BUNDLE_ID,
        skill_name: str = DEFAULT_WORKSPACE_SCOPE_SKILL_NAME,
    ) -> bool:
        """Explicitly destroy a workspace container and unbind its routing entry.

        Idempotent. Returns True if a container was killed, False if there
        was nothing to release.
        """
        binding = self._registry.lookup(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            image_stack=image_stack,
            bundle_id=bundle_id,
            skill_name=skill_name,
        )
        if binding is None:
            return False

        sock_path, sock_err = docker_client.docker_socket_path()
        if sock_err or sock_path is None:
            logger.warning(
                "workspace_container_manager.release_no_socket",
                error=sock_err,
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                image_stack=image_stack,
                bundle_id=bundle_id,
                skill_name=skill_name,
            )
            self._registry.unbind(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                bundle_id=bundle_id,
                skill_name=skill_name,
                image_stack=image_stack,
            )
            return True

        prefix = docker_client.api_prefix()
        try:
            docker_client.docker_remove_container(sock_path, prefix, binding.container_id)
        except Exception as exc:
            logger.warning(
                "workspace_container_manager.release_remove_failed",
                container_id=binding.container_id[:12],
                error=str(exc),
                exc_info=True,
            )

        self._registry.unbind(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            image_stack=image_stack,
            bundle_id=bundle_id,
            skill_name=skill_name,
        )
        return True

    def reap_idle(self) -> Dict[str, int]:
        """Sweep all bindings; remove containers whose idle age exceeds TTL.

        Designed to be called from a Celery beat task (Slice C wires this
        in; Slice A leaves it callable). Returns a small report:

            { "scanned": int, "reaped_idle": int, "reaped_dead": int }

        Idle containers are killed and their bindings removed; bindings
        whose container has already disappeared from the daemon are also
        unbound (so the next call lazily recreates).
        """
        ttl = self._registry.idle_ttl_seconds
        now = time.time()
        report = {"scanned": 0, "reaped_idle": 0, "reaped_dead": 0}

        sock_path, sock_err = docker_client.docker_socket_path()
        if sock_err or sock_path is None:
            logger.warning("workspace_container_manager.reap_no_socket", error=sock_err)
            return report
        prefix = docker_client.api_prefix()

        for binding in self._registry.list_all():
            report["scanned"] += 1
            idle_for = now - binding.last_active_at

            if binding.active_execs > 0:
                logger.debug(
                    "workspace_container_manager.reap_skip_active_exec",
                    container_id=binding.container_id[:12],
                    tenant_id=binding.tenant_id,
                    conversation_id=binding.conversation_id,
                    bundle_id=binding.bundle_id,
                    skill_name=binding.skill_name,
                    image_stack=binding.image_stack,
                    active_execs=binding.active_execs,
                    idle_seconds=int(idle_for),
                )
                continue

            container_alive = docker_client.docker_container_running(
                sock_path, prefix, binding.container_id
            )
            if not container_alive:
                self._registry.unbind(
                    tenant_id=binding.tenant_id,
                    conversation_id=binding.conversation_id,
                    bundle_id=binding.bundle_id,
                    skill_name=binding.skill_name,
                    image_stack=binding.image_stack,
                )
                report["reaped_dead"] += 1
                continue

            if idle_for >= ttl:
                logger.info(
                    "workspace_container_manager.reap_idle",
                    container_id=binding.container_id[:12],
                    tenant_id=binding.tenant_id,
                    conversation_id=binding.conversation_id,
                    bundle_id=binding.bundle_id,
                    skill_name=binding.skill_name,
                    image_stack=binding.image_stack,
                    idle_seconds=int(idle_for),
                )
                docker_client.docker_remove_container(
                    sock_path, prefix, binding.container_id
                )
                self._registry.unbind(
                    tenant_id=binding.tenant_id,
                    conversation_id=binding.conversation_id,
                    bundle_id=binding.bundle_id,
                    skill_name=binding.skill_name,
                    image_stack=binding.image_stack,
                )
                report["reaped_idle"] += 1

        return report

    def _get_or_create_binding(
        self,
        *,
        sock_path: str,
        prefix: str,
        tenant_id: str,
        conversation_id: str,
        image_stack: str,
        bundle_id: str,
        skill_name: str,
        mode: WorkspaceContainerMode,
        oci_image_ref: Optional[str],
        warm_plan: Optional[WarmBootstrapPlan],
    ) -> tuple[Optional[WorkspaceContainerBinding], Optional[str]]:
        existing = self._registry.lookup(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            image_stack=image_stack,
            bundle_id=bundle_id,
            skill_name=skill_name,
        )

        replace_reason: Optional[str] = None
        if existing is not None:
            running = docker_client.docker_container_running(
                sock_path, prefix, existing.container_id
            )
            if not running:
                replace_reason = "dead_container"
            elif existing.mode != mode:
                replace_reason = "mode_changed"
            elif (
                warm_plan is not None
                and (existing.metadata or {}).get("script_sha256")
                != warm_plan.script_sha256
            ):
                # The author's skill module changed (e.g. bundle redeploy
                # bumped the source). Per ADR-0106 §rule 6, container is
                # the unit of workspace reuse — replace it so we never run stale
                # globals from the prior version.
                replace_reason = "script_changed"
            else:
                return existing, None

            logger.info(
                "workspace_container_manager.binding_replaced",
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                bundle_id=bundle_id,
                skill_name=skill_name,
                image_stack=image_stack,
                stale_container_id=existing.container_id[:12],
                reason=replace_reason,
            )
            try:
                docker_client.docker_remove_container(
                    sock_path, prefix, existing.container_id
                )
            except Exception:
                # Best-effort cleanup; the dead/old container will get
                # garbage collected by the next reaper sweep regardless.
                pass
            self._registry.unbind(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                bundle_id=bundle_id,
                skill_name=skill_name,
                image_stack=image_stack,
            )

        return self._create_locked(
            sock_path=sock_path,
            prefix=prefix,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            image_stack=image_stack,
            bundle_id=bundle_id,
            skill_name=skill_name,
            mode=mode,
            oci_image_ref=oci_image_ref,
            warm_plan=warm_plan,
        )

    def _create_locked(
        self,
        *,
        sock_path: str,
        prefix: str,
        tenant_id: str,
        conversation_id: str,
        image_stack: str,
        bundle_id: str,
        skill_name: str,
        mode: WorkspaceContainerMode,
        oci_image_ref: Optional[str],
        warm_plan: Optional[WarmBootstrapPlan],
    ) -> tuple[Optional[WorkspaceContainerBinding], Optional[str]]:
        lock_key = (
            f"lock:workspace_container:{tenant_id}:{conversation_id}:"
            f"{bundle_id}:{skill_name}:{image_stack}"
        )
        lock = acquire_distributed_lock_sync(
            self._lock_per_id_client, lock_key, ttl_seconds=self.LOCK_TTL_SECONDS
        )
        if lock is None:
            return None, (
                f"another worker is currently creating a workspace container for "
                f"({tenant_id}, {conversation_id}, {bundle_id}, {skill_name}, "
                f"{image_stack}); retry shortly"
            )

        try:
            existing = self._registry.lookup(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                image_stack=image_stack,
                bundle_id=bundle_id,
                skill_name=skill_name,
            )
            if (
                existing is not None
                and existing.mode == mode
                and docker_client.docker_container_running(
                    sock_path, prefix, existing.container_id
                )
                and (
                    warm_plan is None
                    or (existing.metadata or {}).get("script_sha256")
                    == warm_plan.script_sha256
                )
            ):
                return existing, None
            if existing is not None:
                self._registry.unbind(
                    tenant_id=tenant_id,
                    conversation_id=conversation_id,
                    bundle_id=bundle_id,
                    skill_name=skill_name,
                    image_stack=image_stack,
                )

            cap_err = self._enforce_tenant_cap(
                sock_path=sock_path,
                prefix=prefix,
                tenant_id=tenant_id,
            )
            if cap_err:
                return None, cap_err

            return self._create_container(
                sock_path=sock_path,
                prefix=prefix,
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                image_stack=image_stack,
                bundle_id=bundle_id,
                skill_name=skill_name,
                mode=mode,
                oci_image_ref=oci_image_ref,
                warm_plan=warm_plan,
            )
        finally:
            try:
                lock.release_sync()
            except Exception:
                logger.warning(
                    "workspace_container_manager.lock_release_failed",
                    lock_key=lock_key,
                )

    def _enforce_tenant_cap(
        self, *, sock_path: str, prefix: str, tenant_id: str
    ) -> Optional[str]:
        cap = _max_per_tenant()
        bindings = self._registry.list_for_tenant(tenant_id)
        if len(bindings) < cap:
            return None

        # Per ADR-0106 §Configuration: reaper enforces by killing oldest-idle
        # when at cap. Do an in-line eviction of the oldest by last_active_at.
        bindings.sort(key=lambda b: b.last_active_at)
        evicted = bindings[0]
        logger.info(
            "workspace_container_manager.tenant_cap_eviction",
            tenant_id=tenant_id,
            cap=cap,
            evicted_container_id=evicted.container_id[:12],
            evicted_conversation_id=evicted.conversation_id,
        )
        docker_client.docker_remove_container(sock_path, prefix, evicted.container_id)
        self._registry.unbind(
            tenant_id=evicted.tenant_id,
            conversation_id=evicted.conversation_id,
            bundle_id=evicted.bundle_id,
            skill_name=evicted.skill_name,
            image_stack=evicted.image_stack,
        )

        bindings_after = self._registry.list_for_tenant(tenant_id)
        if len(bindings_after) >= cap:
            return (
                f"tenant {tenant_id!r} at workspace container cap ({cap}); "
                "eviction did not free a slot"
            )
        return None

    def _create_container(
        self,
        *,
        sock_path: str,
        prefix: str,
        tenant_id: str,
        conversation_id: str,
        image_stack: str,
        bundle_id: str,
        skill_name: str,
        mode: WorkspaceContainerMode,
        oci_image_ref: Optional[str],
        warm_plan: Optional[WarmBootstrapPlan],
    ) -> tuple[Optional[WorkspaceContainerBinding], Optional[str]]:
        image = (oci_image_ref or "").strip() or _default_image()
        scratch_dir = _scratch_dir()
        runtime = docker_client.docker_engine_container_runtime(for_mcp=False)

        host_cfg: Dict[str, Any] = {
            "AutoRemove": False,
            "NetworkMode": _network_mode_default(),
        }
        if runtime:
            host_cfg["Runtime"] = runtime

        labels = {
            "motet.workspace_container": "true",
            "motet.tenant_id": tenant_id,
            "motet.conversation_id": conversation_id,
            "motet.bundle_id": bundle_id,
            "motet.skill_name": skill_name,
            "motet.image_stack": image_stack,
            "motet.mode": mode,
        }
        if self._worker_id:
            labels["motet.created_by_worker"] = self._worker_id

        name_token = uuid.uuid4().hex[:10]
        # Container name is observability-only; routing is via the registry.
        container_name = f"motet-workspace-{tenant_id[:12]}-{name_token}"

        create_body: Dict[str, Any] = {
            "Image": image,
            "Cmd": list(_SLEEPER_ARGV),
            "WorkingDir": scratch_dir,
            "AttachStdout": False,
            "AttachStderr": False,
            "Tty": False,
            "OpenStdin": False,
            "Labels": labels,
            "HostConfig": host_cfg,
        }

        create_path = f"{prefix}/containers/create?name={container_name}"
        body_bytes = json.dumps(create_body).encode("utf-8")

        status, data = docker_client.docker_request(
            sock_path, "POST", create_path, body=body_bytes
        )
        if (
            status != http.client.CREATED
            and docker_client.auto_pull_enabled()
            and docker_client.create_failed_missing_image(status, data)
        ):
            pulled, pull_err = docker_client.docker_pull_image(sock_path, prefix, image)
            if not pulled:
                return None, f"{docker_client.daemon_error(status, data)} (auto-pull: {pull_err})"
            status, data = docker_client.docker_request(
                sock_path, "POST", create_path, body=body_bytes
            )

        if status != http.client.CREATED:
            return None, docker_client.daemon_error(status, data)

        try:
            created = json.loads(data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None, f"invalid create response: {data[:300]!r}"

        cid = created.get("Id")
        if not isinstance(cid, str) or not cid:
            return None, f"create response missing Id: {created!r}"

        # Pre-create the scratch dir on the in-container default rootfs so the
        # first exec doesn't trip on a missing WorkingDir. The sleeper argv
        # runs `sh`, but we invoke `mkdir -p` via a one-shot exec so we don't
        # depend on the image entrypoint having shell semantics that create
        # the dir.
        st_start, body_start = docker_client.docker_request(
            sock_path, "POST", f"{prefix}/containers/{cid}/start"
        )
        if st_start not in (http.client.NO_CONTENT, http.client.OK):
            err_msg = docker_client.daemon_error(st_start, body_start)
            docker_client.docker_remove_container(sock_path, prefix, cid)
            return None, err_msg

        # Pre-create both /scratch (user-managed) and /motet (manager-owned
        # stateful-mode area) in a single exec. /motet always exists on stateful
        # containers because warm bootstrap puts files under it; on cold
        # containers an empty /motet is harmless.
        mkdir_cmd = (
            f"mkdir -p {scratch_dir} {_WARM_DIR} && "
            f"chmod 0700 {scratch_dir} && chmod 0700 {_WARM_DIR}"
        )
        mkdir_status, mkdir_body = docker_client.docker_exec_create(
            sock_path,
            prefix,
            cid,
            cmd=["sh", "-c", mkdir_cmd],
            workdir="/",
        )
        if mkdir_status == http.client.CREATED:
            try:
                exec_id = json.loads(mkdir_body.decode("utf-8")).get("Id")
                if isinstance(exec_id, str) and exec_id:
                    docker_client.docker_exec_start(sock_path, prefix, exec_id)
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass

        metadata: Dict[str, str] = {
            "container_name": container_name,
            "engine_runtime": runtime or "",
            "scratch_dir": scratch_dir,
        }
        if warm_plan is not None:
            bootstrap_err = self._bootstrap_warm(
                sock_path=sock_path,
                prefix=prefix,
                container_id=cid,
                warm_plan=warm_plan,
            )
            if bootstrap_err:
                # Bootstrap failure is fatal for this dispatch — tear down
                # the half-built container so the next call retries cleanly
                # rather than reusing a container with no live supervisor.
                try:
                    docker_client.docker_remove_container(sock_path, prefix, cid)
                except Exception:
                    pass
                return None, bootstrap_err
            metadata["warm_supervisor_socket"] = _WARM_SOCKET_PATH
            metadata["script_sha256"] = warm_plan.script_sha256
            metadata["script_logical_name"] = warm_plan.script_logical_name

        binding = WorkspaceContainerBinding(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            bundle_id=bundle_id,
            skill_name=skill_name,
            image_stack=image_stack,
            container_id=cid,
            image=image,
            mode=mode,
            worker_attribution=self._worker_id,
            metadata=metadata,
        )
        self._registry.bind(binding)
        logger.info(
            "workspace_container_manager.container_created",
            container_id=cid[:12],
            container_name=container_name,
            image=image,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            bundle_id=bundle_id,
            skill_name=skill_name,
            image_stack=image_stack,
            mode=mode,
            warm_bootstrap=warm_plan is not None,
        )
        return binding, None

    def _exec_in_container(
        self,
        *,
        sock_path: str,
        prefix: str,
        binding: WorkspaceContainerBinding,
        request: ExecutionRequest,
    ) -> ExecutionResult:
        scratch_dir = (binding.metadata or {}).get("scratch_dir") or _scratch_dir()
        timeout_s = (
            request.timeout_seconds
            if request.timeout_seconds is not None
            else int(os.getenv("MOTET_WORKER_EXEC_DEFAULT_TIMEOUT", "120"))
        )

        stage_err = self._materialize_input_files(
            sock_path=sock_path,
            prefix=prefix,
            binding=binding,
            input_files=request.input_files,
        )
        if stage_err:
            return ExecutionResult(
                exit_code=-1,
                backend=_BACKEND_LABEL,
                backend_ref=binding.container_id[:12],
                oci_image_ref=binding.image,
                engine_runtime=(binding.metadata or {}).get("engine_runtime") or None,
                error=stage_err,
            )

        env: Dict[str, str] = {}
        if request.tenant_id:
            env["MOTET_TENANT_ID"] = request.tenant_id
        if request.bundle_id:
            env["MOTET_BUNDLE_ID"] = request.bundle_id
        if request.correlation_id:
            env["MOTET_CORRELATION_ID"] = request.correlation_id

        st, body = docker_client.docker_exec_create(
            sock_path,
            prefix,
            binding.container_id,
            cmd=list(request.argv),
            workdir=scratch_dir,
            env=env or None,
        )
        if st != http.client.CREATED:
            return ExecutionResult(
                exit_code=-1,
                backend=_BACKEND_LABEL,
                backend_ref=binding.container_id[:12],
                oci_image_ref=binding.image,
                engine_runtime=(binding.metadata or {}).get("engine_runtime") or None,
                error=docker_client.daemon_error(st, body),
            )

        try:
            exec_id = json.loads(body.decode("utf-8")).get("Id")
        except (json.JSONDecodeError, UnicodeDecodeError):
            exec_id = None
        if not isinstance(exec_id, str) or not exec_id:
            return ExecutionResult(
                exit_code=-1,
                backend=_BACKEND_LABEL,
                backend_ref=binding.container_id[:12],
                error=f"docker exec create returned no Id: {body[:300]!r}",
            )

        self._registry.begin_activity(
            tenant_id=binding.tenant_id,
            conversation_id=binding.conversation_id,
            bundle_id=binding.bundle_id,
            skill_name=binding.skill_name,
            image_stack=binding.image_stack,
        )

        result_holder: Dict[str, Any] = {}
        done = threading.Event()

        def _run() -> None:
            try:
                st_start, body_start = docker_client.docker_exec_start(
                    sock_path, prefix, exec_id
                )
                result_holder["status"] = st_start
                result_holder["body"] = body_start
            except Exception as exc:
                result_holder["error"] = str(exc)
            finally:
                self._registry.end_activity(
                    tenant_id=binding.tenant_id,
                    conversation_id=binding.conversation_id,
                    bundle_id=binding.bundle_id,
                    skill_name=binding.skill_name,
                    image_stack=binding.image_stack,
                )
                done.set()

        th = threading.Thread(target=_run, name="workspace-container-exec", daemon=True)
        th.start()
        timed_out = not done.wait(timeout=float(timeout_s))

        if timed_out:
            # docker exec doesn't expose a kill API on the exec instance;
            # the process inside the container will keep running until the
            # container is killed. For Slice A, we surface the timeout and
            # let the caller decide whether to escalate to release().
            return ExecutionResult(
                exit_code=-1,
                backend=_BACKEND_LABEL,
                backend_ref=binding.container_id[:12],
                oci_image_ref=binding.image,
                engine_runtime=(binding.metadata or {}).get("engine_runtime") or None,
                timed_out=True,
            )

        if "error" in result_holder:
            return ExecutionResult(
                exit_code=-1,
                backend=_BACKEND_LABEL,
                backend_ref=binding.container_id[:12],
                oci_image_ref=binding.image,
                engine_runtime=(binding.metadata or {}).get("engine_runtime") or None,
                error=result_holder["error"],
            )

        st_start = result_holder.get("status")
        body_start = result_holder.get("body") or b""
        if st_start != http.client.OK:
            return ExecutionResult(
                exit_code=-1,
                backend=_BACKEND_LABEL,
                backend_ref=binding.container_id[:12],
                oci_image_ref=binding.image,
                engine_runtime=(binding.metadata or {}).get("engine_runtime") or None,
                error=docker_client.daemon_error(int(st_start or 0), body_start),
            )

        stdout_b, stderr_b = docker_client.demux_docker_stream(body_start)

        st_inspect, body_inspect = docker_client.docker_exec_inspect(
            sock_path, prefix, exec_id
        )
        exit_code = -1
        if st_inspect == http.client.OK:
            try:
                payload = json.loads(body_inspect.decode("utf-8"))
                if isinstance(payload, dict) and isinstance(payload.get("ExitCode"), int):
                    exit_code = int(payload["ExitCode"])
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass

        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")
        stdout, stderr, otrunc, etrunc = truncate_output_pair(
            stdout, stderr, request.max_output_bytes
        )

        self._registry.touch(
            tenant_id=binding.tenant_id,
            conversation_id=binding.conversation_id,
            bundle_id=binding.bundle_id,
            skill_name=binding.skill_name,
            image_stack=binding.image_stack,
        )

        return ExecutionResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            timed_out=False,
            stdout_truncated=otrunc,
            stderr_truncated=etrunc,
            backend=_BACKEND_LABEL,
            backend_ref=binding.container_id[:12],
            oci_image_ref=binding.image,
            engine_runtime=(binding.metadata or {}).get("engine_runtime") or None,
        )

    def _materialize_input_files(
        self,
        *,
        sock_path: str,
        prefix: str,
        binding: WorkspaceContainerBinding,
        input_files: List[ExecutionInputFile],
    ) -> Optional[str]:
        """Ship manager-owned input files into an existing workspace container."""

        if not input_files:
            return None

        archive_entries = []
        for idx, input_file in enumerate(input_files):
            path = getattr(input_file, "path", None)
            content = getattr(input_file, "content", None)
            mode = getattr(input_file, "mode", 0o600)
            if not isinstance(path, str) or not path.startswith("/"):
                return (
                    "workspace input files must use absolute container paths; "
                    f"entry {idx} had {path!r}"
                )
            if not isinstance(content, (bytes, bytearray)):
                return f"workspace input file {path!r} did not contain bytes"
            archive_entries.append((path.lstrip("/"), bytes(content), int(mode)))

        try:
            tar_bytes = docker_client.build_tar_archive(archive_entries)
        except Exception as exc:
            return f"workspace input file staging: failed to build tar archive: {exc}"

        st, body = docker_client.docker_put_archive(
            sock_path,
            prefix,
            binding.container_id,
            target_path="/",
            tar_bytes=tar_bytes,
        )
        if st != http.client.OK:
            return (
                "workspace input file staging: put-archive failed: "
                + docker_client.daemon_error(st, body)
            )
        return None

    def list_for_tenant(self, tenant_id: str) -> List[WorkspaceContainerBinding]:
        """Convenience pass-through for the ops dashboard (Slice C uses this)."""
        return self._registry.list_for_tenant(tenant_id)

    # ------------------------------------------------------------------
    # ADR-0106 Slice B: stateful-mode bootstrap + dispatch
    # ------------------------------------------------------------------

    def _bootstrap_warm(
        self,
        *,
        sock_path: str,
        prefix: str,
        container_id: str,
        warm_plan: WarmBootstrapPlan,
    ) -> Optional[str]:
        """Ship the supervisor + skill module into ``container_id`` and start it.

        Returns ``None`` on success or an operator-readable error message.
        Idempotency on a fresh container is guaranteed by callers (we only
        bootstrap on the create path); the marker-file check still runs so
        a stuck supervisor surfaces as a timeout rather than a silent hang.
        """
        try:
            supervisor_src = _warm_runtime_source("_warm_supervisor.py")
            client_src = _warm_runtime_source("_warm_client.py")
        except Exception as exc:
            return f"warm bootstrap: failed to read packaged supervisor/client: {exc}"

        # We strip the leading "/motet/" from each member name because
        # ``docker put archive`` extracts members *relative to the
        # ``path`` query parameter* on the URL.
        archive_entries = [
            ("_warm_supervisor.py", supervisor_src, 0o600),
            ("_warm_client.py", client_src, 0o600),
            ("skill_module.py", warm_plan.script_source, 0o600),
        ]
        try:
            tar_bytes = docker_client.build_tar_archive(archive_entries)
        except Exception as exc:
            return f"warm bootstrap: failed to build tar archive: {exc}"

        st, body = docker_client.docker_put_archive(
            sock_path,
            prefix,
            container_id,
            target_path=_WARM_DIR,
            tar_bytes=tar_bytes,
        )
        if st != http.client.OK:
            return (
                "warm bootstrap: put-archive failed: "
                + docker_client.daemon_error(st, body)
            )

        # Start the supervisor in the background. Detach=True so the exec
        # call returns immediately; the supervisor keeps running inside
        # the container until the container is removed.
        start_cmd = [
            "python3",
            "-u",
            _WARM_SUPERVISOR_PATH,
            "--module",
            _WARM_SKILL_MODULE_PATH,
            "--socket",
            _WARM_SOCKET_PATH,
            "--marker",
            _WARM_MARKER_PATH,
        ]
        st_ec, body_ec = docker_client.docker_exec_create(
            sock_path,
            prefix,
            container_id,
            cmd=start_cmd,
            workdir=_WARM_DIR,
        )
        if st_ec != http.client.CREATED:
            return (
                "warm bootstrap: exec-create for supervisor failed: "
                + docker_client.daemon_error(st_ec, body_ec)
            )
        try:
            sup_exec_id = json.loads(body_ec.decode("utf-8")).get("Id")
        except (json.JSONDecodeError, UnicodeDecodeError):
            sup_exec_id = None
        if not isinstance(sup_exec_id, str) or not sup_exec_id:
            return f"warm bootstrap: exec-create returned no Id: {body_ec[:300]!r}"

        st_es, body_es = docker_client.docker_exec_start(
            sock_path, prefix, sup_exec_id, detach=True
        )
        if st_es not in (http.client.OK, http.client.NO_CONTENT):
            return (
                "warm bootstrap: exec-start (detached) for supervisor failed: "
                + docker_client.daemon_error(st_es, body_es)
            )

        ready, marker_err = self._wait_for_warm_marker(
            sock_path=sock_path,
            prefix=prefix,
            container_id=container_id,
            timeout_seconds=_warm_bootstrap_timeout_seconds(),
        )
        if not ready:
            return marker_err or (
                "warm bootstrap: supervisor did not become ready before timeout"
            )
        return None

    def _wait_for_warm_marker(
        self,
        *,
        sock_path: str,
        prefix: str,
        container_id: str,
        timeout_seconds: float,
    ) -> tuple[bool, Optional[str]]:
        """Poll inside the container for the supervisor's bootstrap marker.

        Each poll is a tiny ``test -f`` exec; we check the exec instance's
        ExitCode to decide ready vs not-ready. The poll interval is short
        (0.25s) because the supervisor's binding step is sub-second on
        every realistic image.
        """
        deadline = time.monotonic() + timeout_seconds
        check_argv = ["sh", "-c", f"test -f {_WARM_MARKER_PATH}"]

        while time.monotonic() < deadline:
            st_ec, body_ec = docker_client.docker_exec_create(
                sock_path,
                prefix,
                container_id,
                cmd=check_argv,
                workdir="/",
            )
            if st_ec != http.client.CREATED:
                # Container went away under us; surface the error rather
                # than spinning until timeout.
                return False, (
                    "warm bootstrap: marker-check exec-create failed: "
                    + docker_client.daemon_error(st_ec, body_ec)
                )
            try:
                exec_id = json.loads(body_ec.decode("utf-8")).get("Id")
            except (json.JSONDecodeError, UnicodeDecodeError):
                exec_id = None
            if isinstance(exec_id, str) and exec_id:
                docker_client.docker_exec_start(sock_path, prefix, exec_id)
                st_in, body_in = docker_client.docker_exec_inspect(
                    sock_path, prefix, exec_id
                )
                if st_in == http.client.OK:
                    try:
                        payload = json.loads(body_in.decode("utf-8"))
                        if (
                            isinstance(payload, dict)
                            and payload.get("ExitCode") == 0
                        ):
                            return True, None
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        pass
            time.sleep(_WARM_BOOTSTRAP_POLL_INTERVAL)

        return False, (
            "warm bootstrap: supervisor did not write "
            f"{_WARM_MARKER_PATH} within {timeout_seconds:.1f}s"
        )

    def _dispatch_warm_in_container(
        self,
        *,
        sock_path: str,
        prefix: str,
        binding: WorkspaceContainerBinding,
        warm_plan: WarmBootstrapPlan,
        params: Dict[str, Any],
        timeout_seconds: Optional[int],
        envelope_id: str,
    ) -> Dict[str, Any]:
        request_payload = json.dumps({"id": envelope_id, "params": params}).encode(
            "utf-8"
        )
        request_b64 = base64.b64encode(request_payload).decode("ascii")

        timeout_s = (
            timeout_seconds
            if timeout_seconds is not None
            else int(os.getenv("MOTET_WORKER_EXEC_DEFAULT_TIMEOUT", "120"))
        )

        env = {
            "MOTET_WARM_REQUEST_B64": request_b64,
            "MOTET_WARM_SOCKET": _WARM_SOCKET_PATH,
        }

        st, body = docker_client.docker_exec_create(
            sock_path,
            prefix,
            binding.container_id,
            cmd=["python3", _WARM_CLIENT_PATH],
            workdir=_WARM_DIR,
            env=env,
        )
        if st != http.client.CREATED:
            return _warm_transport_error(
                envelope_id,
                "warm dispatch: exec-create failed: "
                + docker_client.daemon_error(st, body),
                container_id=binding.container_id,
                oci_image_ref=binding.image,
            )

        try:
            exec_id = json.loads(body.decode("utf-8")).get("Id")
        except (json.JSONDecodeError, UnicodeDecodeError):
            exec_id = None
        if not isinstance(exec_id, str) or not exec_id:
            return _warm_transport_error(
                envelope_id,
                f"warm dispatch: exec-create returned no Id: {body[:300]!r}",
                container_id=binding.container_id,
                oci_image_ref=binding.image,
            )

        self._registry.begin_activity(
            tenant_id=binding.tenant_id,
            conversation_id=binding.conversation_id,
            bundle_id=binding.bundle_id,
            skill_name=binding.skill_name,
            image_stack=binding.image_stack,
        )

        result_holder: Dict[str, Any] = {}
        done = threading.Event()

        def _run() -> None:
            try:
                st_start, body_start = docker_client.docker_exec_start(
                    sock_path, prefix, exec_id
                )
                result_holder["status"] = st_start
                result_holder["body"] = body_start
            except Exception as exc:
                result_holder["error"] = str(exc)
            finally:
                self._registry.end_activity(
                    tenant_id=binding.tenant_id,
                    conversation_id=binding.conversation_id,
                    bundle_id=binding.bundle_id,
                    skill_name=binding.skill_name,
                    image_stack=binding.image_stack,
                )
                done.set()

        th = threading.Thread(
            target=_run, name="workspace-container-stateful-exec", daemon=True
        )
        th.start()
        timed_out = not done.wait(timeout=float(timeout_s))

        if timed_out:
            envelope = _warm_transport_error(
                envelope_id,
                f"warm dispatch timed out after {timeout_s}s",
                container_id=binding.container_id,
                oci_image_ref=binding.image,
            )
            envelope["timed_out"] = True
            return envelope

        if "error" in result_holder:
            return _warm_transport_error(
                envelope_id,
                f"warm dispatch: exec-start raised: {result_holder['error']}",
                container_id=binding.container_id,
                oci_image_ref=binding.image,
            )

        st_start = result_holder.get("status")
        body_start = result_holder.get("body") or b""
        if st_start != http.client.OK:
            return _warm_transport_error(
                envelope_id,
                "warm dispatch: exec-start failed: "
                + docker_client.daemon_error(int(st_start or 0), body_start),
                container_id=binding.container_id,
                oci_image_ref=binding.image,
            )

        stdout_b, stderr_b = docker_client.demux_docker_stream(body_start)

        # Inspect the client's exit code only for observability — the
        # source of truth is the supervisor envelope on stdout.
        st_inspect, body_inspect = docker_client.docker_exec_inspect(
            sock_path, prefix, exec_id
        )
        client_exit_code: Optional[int] = None
        if st_inspect == http.client.OK:
            try:
                payload = json.loads(body_inspect.decode("utf-8"))
                if isinstance(payload, dict) and isinstance(payload.get("ExitCode"), int):
                    client_exit_code = int(payload["ExitCode"])
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass

        envelope = _parse_warm_envelope(
            stdout_b, stderr_b, fallback_id=envelope_id
        )

        envelope.setdefault("container_id", binding.container_id)
        envelope.setdefault("oci_image_ref", binding.image)
        envelope["workspace_image_stack"] = binding.image_stack
        envelope["workspace_conversation_id"] = binding.conversation_id
        envelope["workspace_bundle_id"] = binding.bundle_id
        envelope["workspace_skill_name"] = binding.skill_name
        envelope["workspace_mode"] = "stateful"
        envelope["script_sha256"] = warm_plan.script_sha256
        if client_exit_code is not None:
            envelope.setdefault("client_exit_code", client_exit_code)

        self._registry.touch(
            tenant_id=binding.tenant_id,
            conversation_id=binding.conversation_id,
            bundle_id=binding.bundle_id,
            skill_name=binding.skill_name,
            image_stack=binding.image_stack,
        )
        return envelope


def _warm_transport_error(
    envelope_id: str,
    message: str,
    *,
    container_id: Optional[str] = None,
    oci_image_ref: Optional[str] = None,
) -> Dict[str, Any]:
    """Manager-side error envelope shaped like the supervisor's response.

    Callers always get the same dict shape whether the failure was the
    supervisor saying ``ok: false`` or the manager failing to even reach
    the supervisor; the ``transport_error`` flag distinguishes the two
    so observability filters can separate infra problems from runner
    bugs.
    """
    envelope: Dict[str, Any] = {
        "id": envelope_id,
        "ok": False,
        "error": message,
        "traceback": "",
        "stdout": "",
        "stderr": "",
        "transport_error": True,
        "workspace_mode": "stateful",
    }
    if container_id is not None:
        envelope["container_id"] = container_id
    if oci_image_ref is not None:
        envelope["oci_image_ref"] = oci_image_ref
    return envelope


def _parse_warm_envelope(
    stdout_b: bytes, stderr_b: bytes, *, fallback_id: str
) -> Dict[str, Any]:
    """Parse the supervisor's JSON envelope from the warm client's stdout.

    Stderr from the client (rare; mostly noisy crashes) is preserved on
    the envelope so operators can see what went wrong without re-running
    with debug logging.
    """
    stdout = stdout_b.decode("utf-8", errors="replace").strip()
    stderr = stderr_b.decode("utf-8", errors="replace")
    if not stdout:
        return {
            "id": fallback_id,
            "ok": False,
            "error": "warm dispatch: empty response from supervisor client",
            "traceback": "",
            "stdout": "",
            "stderr": stderr,
            "transport_error": True,
            "workspace_mode": "stateful",
        }
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return {
            "id": fallback_id,
            "ok": False,
            "error": f"warm dispatch: response is not JSON: {exc}",
            "traceback": "",
            "stdout": stdout,
            "stderr": stderr,
            "transport_error": True,
            "workspace_mode": "stateful",
        }

    if not isinstance(envelope, dict):
        return {
            "id": fallback_id,
            "ok": False,
            "error": f"warm dispatch: response is not a JSON object: {type(envelope).__name__}",
            "traceback": "",
            "stdout": stdout,
            "stderr": stderr,
            "transport_error": True,
            "workspace_mode": "stateful",
        }

    if stderr and not envelope.get("stderr"):
        # Supervisor-captured stderr is preferred; we only surface the
        # client process's stderr when the envelope didn't carry any.
        envelope["stderr"] = stderr
    return envelope


_singleton_lock = threading.Lock()
_singleton: Optional[WorkspaceContainerManager] = None


def get_workspace_container_manager() -> WorkspaceContainerManager:
    """Process-singleton accessor.

    Multiple workers in the same process share one manager; the routing
    primitive is Redis so cross-process correctness does not depend on the
    singleton.
    """
    global _singleton
    if _singleton is not None:
        return _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = WorkspaceContainerManager()
        return _singleton


def reset_workspace_container_manager_for_tests() -> None:
    """Test-only: drop the cached singleton so the next call rebuilds it.

    Production code MUST NOT call this. Tests that override env vars
    before constructing the manager need a fresh instance.
    """
    global _singleton
    with _singleton_lock:
        _singleton = None


__all__ = [
    "WorkspaceContainerManager",
    "WarmBootstrapPlan",
    "get_workspace_container_manager",
    "is_workspace_container_enabled",
    "is_stateful_mode_enabled",
    "reset_workspace_container_manager_for_tests",
]
