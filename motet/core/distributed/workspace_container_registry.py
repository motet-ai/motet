"""
Motet - Workspace Container Registry

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    Redis-backed routing registry for per-conversation workspace containers.
    Implements the routing primitive defined in §"The primitive":

        workspace:container:<tenant_id>:<conv_id>:<bundle_id>:<skill_name>:<image_stack>
            -> container endpoint

    Any worker reads this key to discover whether a workspace container exists
    for a given (tenant, conversation, bundle, skill, image_stack) tuple, dial
    it via docker exec, and refresh its idle TTL. The registry also tracks
    active exec counts so the idle reaper can distinguish truly idle containers
    from long-running in-flight work. The registry itself is stateless beyond
    Redis; the WorkspaceContainerManager (sibling module) owns the actual
    container lifecycle.

    The registry shape intentionally mirrors the manager-status
    registry pattern: a single Redis hash per logical entity, keyed on the
    tuple that identifies it, with a TTL refreshed on activity. This is the
    *same routing key shape* that 's PER_SESSION sandbox will use
    when the substrate is swapped from runc to Firecracker.

Dependencies:
    - pydantic: data validation for the WorkspaceContainerBinding model
    - redis: distributed key/value store for routing state and activity counters
    - motet.core.distributed.redis_manager: centralized Redis client

Usage:
    from motet.core.distributed.workspace_container_registry import (
        WorkspaceContainerRegistry,
        WorkspaceContainerBinding,
    )

    registry = WorkspaceContainerRegistry()

    # Bind a freshly-created container to its (tenant, conv, bundle, skill, stack) identity
    registry.bind(
        WorkspaceContainerBinding(
            tenant_id="tenant-a",
            conversation_id="conv-1",
            bundle_id="acme",
            skill_name="pdf",
            image_stack="python-minimal",
            container_id="abcdef123456",
            image="python:3.11-slim",
            mode="cold",
            worker_attribution="cloud_worker1",
        )
    )

    # Any worker can look the binding up later
    binding = registry.lookup(
        tenant_id="tenant-a",
        conversation_id="conv-1",
        bundle_id="acme",
        skill_name="pdf",
        image_stack="python-minimal",
    )

    # Refresh idle TTL on each successful exec
    registry.touch(
        tenant_id="tenant-a",
        conversation_id="conv-1",
        bundle_id="acme",
        skill_name="pdf",
        image_stack="python-minimal",
    )

    # Listing per tenant powers the ops dashboard
    bindings = registry.list_for_tenant("tenant-a")

Notes:
    - **Cross-tenant guard:** the routing key MUST start
      with ``tenant_id``. The registry never serves a binding from one
      tenant to a request whose context says otherwise. This is the
      structural protection that lets ship without depending on
      's per-tenant worker pools.
    - **TTL semantics:** the Redis entry TTL is ``MOTET_WORKSPACE_CONTAINER_IDLE_TTL_SECONDS``
      (default 1800). Each successful ``touch()`` refreshes the TTL. When
      Redis evicts a stale entry, the next call observes a missing
      binding and the manager creates a fresh container. The actual
      container reaper (kills the docker container) lives in the
      WorkspaceContainerManager; the registry only governs *routing*
      visibility.
    - **No worker affinity:** ``worker_attribution`` is observability-only,
      mirroring the §R3 worker_id-as-tag treatment. Workers
      MUST NOT cache bindings across calls.
    - **Active exec safety:** ``active_execs`` is incremented before
      ``docker exec start`` begins and decremented when the exec call
      finishes, allowing reapers on any worker to skip bindings with live
      in-flight work.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Literal, Optional, cast

import redis
import structlog
from pydantic import BaseModel, Field

from .redis_manager import get_sync_redis_client
from .tenant_keys import (
    delete_candidate_keys,
    family_scan_patterns,
    first_existing_key,
    hgetall_first,
    smembers_union,
    tenant_key,
)

logger = structlog.get_logger(__name__)


def _workspace_container_env(name: str) -> Optional[str]:
    """Read a workspace-container env var by suffix."""
    return os.getenv(f"MOTET_WORKSPACE_CONTAINER_{name}")


WorkspaceContainerMode = Literal["cold", "warm"]
DEFAULT_WORKSPACE_SCOPE_BUNDLE_ID = "__manual__"
DEFAULT_WORKSPACE_SCOPE_SKILL_NAME = "__manual__"


class WorkspaceContainerBinding(BaseModel):
    """Routing entry for a single per-workspace container.

    The (tenant_id, conversation_id, bundle_id, skill_name, image_stack)
    tuple is the *identity* of a workspace container. Two runners from the
    same skill invoked in the same conversation that resolve to the same
    image_stack share one container; different skills or image_stacks each
    get their own.
    """

    tenant_id: str = Field(..., description="Owning tenant; first segment of the routing key")
    conversation_id: str = Field(..., description="Conversation identity; defines workspace lifetime")
    bundle_id: str = Field(
        default=DEFAULT_WORKSPACE_SCOPE_BUNDLE_ID,
        description="Owning bundle for runner-scoped conversation workspaces",
    )
    skill_name: str = Field(
        default=DEFAULT_WORKSPACE_SCOPE_SKILL_NAME,
        description="Owning skill for runner-scoped conversation workspaces",
    )
    image_stack: str = Field(
        ...,
        description=(
            "Platform image stack id. One container per "
            "(conversation, bundle, skill, image_stack); different stacks get "
            "different containers."
        ),
    )

    container_id: str = Field(
        ...,
        description="Substrate-level container id (Docker container id, kata sandbox id, etc.)",
    )
    image: str = Field(
        ...,
        description="Resolved OCI image reference the container was created from",
    )
    mode: WorkspaceContainerMode = Field(
        default="cold",
        description=(
            "One declaration, two modes. 'cold' = docker exec per call; "
            "'warm' = long-lived in-container process (Slice B)."
        ),
    )
    endpoint: Optional[str] = Field(
        default=None,
        description=(
            "Optional dial endpoint for stateful in-container RPC servers. "
            "None for cold-mode containers (dispatch is plain `docker exec`)."
        ),
    )

    created_at: float = Field(default_factory=time.time, description="Unix epoch seconds")
    last_active_at: float = Field(
        default_factory=time.time,
        description="Unix epoch seconds; refreshed by touch() on each successful dispatch",
    )
    active_execs: int = Field(
        default=0,
        ge=0,
        description="Distributed in-flight exec count used by the idle reaper",
    )

    worker_attribution: Optional[str] = Field(
        default=None,
        description=(
            "Observability-only worker_id of the worker that created the container "
            ". Workers MUST NOT use this for "
            "routing decisions; any worker may dispatch to any container."
        ),
    )

    metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Substrate-specific metadata (e.g. storage-opt, runtime, exposed ports)",
    )

    def to_redis_mapping(self) -> Dict[str, str]:
        """Serialize to a flat mapping suitable for HSET."""
        data = self.model_dump(mode="json")
        return {
            k: json.dumps(v) if isinstance(v, (dict, list)) else ("" if v is None else str(v))
            for k, v in data.items()
        }

    @classmethod
    def from_redis_mapping(cls, mapping: Dict[Any, Any]) -> "WorkspaceContainerBinding":
        """Reconstruct a binding from an HGETALL result.

        Handles bytes-vs-str keys and JSON-encoded ``metadata``.
        """
        parsed: Dict[str, Any] = {}
        for raw_key, raw_val in mapping.items():
            key = raw_key.decode("utf-8") if isinstance(raw_key, bytes) else raw_key
            value = raw_val.decode("utf-8") if isinstance(raw_val, bytes) else raw_val

            if key == "metadata":
                try:
                    parsed[key] = json.loads(value) if value else {}
                except (json.JSONDecodeError, ValueError):
                    parsed[key] = {}
                continue

            if key in {"created_at", "last_active_at"}:
                try:
                    parsed[key] = float(value)
                except (TypeError, ValueError):
                    parsed[key] = 0.0
                continue

            if key == "active_execs":
                try:
                    parsed[key] = max(int(value), 0)
                except (TypeError, ValueError):
                    parsed[key] = 0
                continue

            if key == "endpoint" and value == "":
                parsed[key] = None
                continue

            if key == "worker_attribution" and value == "":
                parsed[key] = None
                continue

            parsed[key] = value

        return cls(**parsed)


class WorkspaceContainerRegistry:
    """Redis-backed routing registry for per-conversation workspace containers.

    See module docstring for the ADR-0106 design contract.
    """

    KEY_PREFIX = "workspace:container"
    DEFAULT_IDLE_TTL_SECONDS = 1800  # 30 minutes; matches ADR-0106 default
    TENANT_INDEX_PREFIX = "workspace:container:index:tenant"

    def __init__(self, idle_ttl_seconds: Optional[int] = None) -> None:
        """Initialize the registry.

        Args:
            idle_ttl_seconds: Override for the Redis-entry TTL. When None, the
                value of ``MOTET_WORKSPACE_CONTAINER_IDLE_TTL_SECONDS`` is used,
                falling back to ``DEFAULT_IDLE_TTL_SECONDS``. Operators set this
                at deploy time per ADR-0106 §rule 5 (quota and TTL are
                platform-enforced).
        """
        self.redis = cast(redis.Redis, get_sync_redis_client("workspace_container_registry"))
        self._idle_ttl_seconds = self._resolve_idle_ttl(idle_ttl_seconds)

    @staticmethod
    def _resolve_idle_ttl(explicit: Optional[int]) -> int:
        if explicit is not None:
            return max(int(explicit), 1)
        env_raw = (_workspace_container_env("IDLE_TTL_SECONDS") or "").strip()
        if env_raw:
            try:
                parsed = int(env_raw)
                if parsed > 0:
                    return parsed
            except ValueError:
                logger.warning(
                    "workspace_container_registry.invalid_ttl_env",
                    raw=env_raw,
                    fallback=WorkspaceContainerRegistry.DEFAULT_IDLE_TTL_SECONDS,
                )
        return WorkspaceContainerRegistry.DEFAULT_IDLE_TTL_SECONDS

    @property
    def idle_ttl_seconds(self) -> int:
        """Effective Redis-entry idle TTL (seconds)."""
        return self._idle_ttl_seconds

    @staticmethod
    def _key_part(value: str) -> str:
        """Encode a routing-key segment while keeping common slugs readable."""
        return str(value or "").replace("%", "%25").replace(":", "%3A")

    def _routing_key(
        self,
        tenant_id: str,
        conversation_id: str,
        bundle_id: str,
        skill_name: str,
        image_stack: str,
    ) -> str:
        """Build the canonical routing key.

        Keep the tenant first for the cross-tenant guard, then scope the
        workspace by conversation and runner owner so two skills using the same
        image stack do not accidentally share /scratch.
        """
        return ":".join(
            [
                self.KEY_PREFIX,
                self._key_part(tenant_id),
                self._key_part(conversation_id),
                self._key_part(bundle_id),
                self._key_part(skill_name),
                self._key_part(image_stack),
            ]
        )

    def _routing_keys(
        self,
        tenant_id: str,
        conversation_id: str,
        bundle_id: str,
        skill_name: str,
        image_stack: str,
    ) -> tuple[str, ...]:
        return (tenant_key(
            tenant_id,
            self._routing_key(tenant_id, conversation_id, bundle_id, skill_name, image_stack),
        ),)

    def _write_routing_key(
        self,
        tenant_id: str,
        conversation_id: str,
        bundle_id: str,
        skill_name: str,
        image_stack: str,
    ) -> str:
        return tenant_key(
            tenant_id,
            self._routing_key(tenant_id, conversation_id, bundle_id, skill_name, image_stack),
        )

    def _resolve_routing_key(
        self,
        tenant_id: str,
        conversation_id: str,
        bundle_id: str,
        skill_name: str,
        image_stack: str,
    ) -> Optional[str]:
        return first_existing_key(
            self.redis,
            self._routing_keys(tenant_id, conversation_id, bundle_id, skill_name, image_stack),
        )

    def _tenant_index_logical(self, tenant_id: str) -> str:
        return f"{self.TENANT_INDEX_PREFIX}:{tenant_id}"

    def _tenant_index_key(self, tenant_id: str) -> str:
        return tenant_key(tenant_id, self._tenant_index_logical(tenant_id))

    def _tenant_index_keys(self, tenant_id: str) -> tuple[str, ...]:
        return (tenant_key(tenant_id, self._tenant_index_logical(tenant_id)),)

    def bind(self, binding: WorkspaceContainerBinding) -> None:
        """Publish a (tenant, conv, bundle, skill, image_stack) → container binding.

        Overwrites any existing binding with the same identity. Callers MUST
        hold the per-identity lifecycle lock when calling this — the registry
        does not serialize concurrent creators (the WorkspaceContainerManager
        owns that contract).
        """
        key = self._write_routing_key(
            binding.tenant_id,
            binding.conversation_id,
            binding.bundle_id,
            binding.skill_name,
            binding.image_stack,
        )
        index_key = self._tenant_index_key(binding.tenant_id)

        binding.last_active_at = time.time()
        mapping = binding.to_redis_mapping()

        pipe = self.redis.pipeline()
        pipe.delete(key)
        pipe.hset(key, mapping=mapping)
        pipe.expire(key, self._idle_ttl_seconds)
        pipe.sadd(index_key, key)
        pipe.expire(index_key, self._idle_ttl_seconds * 4)
        pipe.execute()

        logger.info(
            "workspace_container_registry.bind",
            tenant_id=binding.tenant_id,
            conversation_id=binding.conversation_id,
            bundle_id=binding.bundle_id,
            skill_name=binding.skill_name,
            image_stack=binding.image_stack,
            container_id=binding.container_id[:12],
            mode=binding.mode,
            ttl_seconds=self._idle_ttl_seconds,
        )

    def lookup(
        self,
        *,
        tenant_id: str,
        conversation_id: str,
        image_stack: str,
        bundle_id: str = DEFAULT_WORKSPACE_SCOPE_BUNDLE_ID,
        skill_name: str = DEFAULT_WORKSPACE_SCOPE_SKILL_NAME,
    ) -> Optional[WorkspaceContainerBinding]:
        """Look up a binding. Returns None if no binding exists.

        Per ADR-0106 §rule 4, callers MUST NOT cache results across calls;
        always re-read the registry.
        """
        keys = self._routing_keys(tenant_id, conversation_id, bundle_id, skill_name, image_stack)
        raw = cast(Dict[Any, Any], hgetall_first(self.redis, keys))
        if not raw:
            return None
        try:
            binding = WorkspaceContainerBinding.from_redis_mapping(raw)
        except Exception as exc:
            logger.error(
                "workspace_container_registry.corrupt_binding",
                key=key,
                error=str(exc),
                exc_info=True,
            )
            return None

        if binding.tenant_id != tenant_id:
            logger.error(
                "workspace_container_registry.cross_tenant_violation",
                key_tenant=binding.tenant_id,
                requested_tenant=tenant_id,
                conversation_id=conversation_id,
                bundle_id=bundle_id,
                skill_name=skill_name,
                image_stack=image_stack,
            )
            return None

        return binding

    def touch(
        self,
        *,
        tenant_id: str,
        conversation_id: str,
        image_stack: str,
        bundle_id: str = DEFAULT_WORKSPACE_SCOPE_BUNDLE_ID,
        skill_name: str = DEFAULT_WORKSPACE_SCOPE_SKILL_NAME,
    ) -> bool:
        """Refresh the idle TTL after a successful dispatch.

        Returns True when an existing entry was refreshed, False otherwise
        (no-op when the key has been evicted or never existed).
        """
        key = self._resolve_routing_key(tenant_id, conversation_id, bundle_id, skill_name, image_stack)
        index_key = self._tenant_index_key(tenant_id)
        if not key:
            return False
        now = time.time()
        pipe = self.redis.pipeline()
        pipe.hset(key, "last_active_at", str(now))
        pipe.expire(key, self._idle_ttl_seconds)
        pipe.expire(index_key, self._idle_ttl_seconds * 4)
        pipe.execute()
        return True

    def begin_activity(
        self,
        *,
        tenant_id: str,
        conversation_id: str,
        image_stack: str,
        bundle_id: str = DEFAULT_WORKSPACE_SCOPE_BUNDLE_ID,
        skill_name: str = DEFAULT_WORKSPACE_SCOPE_SKILL_NAME,
    ) -> bool:
        """Mark a binding as actively executing and refresh its TTLs."""

        key = self._resolve_routing_key(tenant_id, conversation_id, bundle_id, skill_name, image_stack)
        index_key = self._tenant_index_key(tenant_id)
        if not key:
            return False
        now = time.time()
        pipe = self.redis.pipeline()
        pipe.hincrby(key, "active_execs", 1)
        pipe.hset(key, "last_active_at", str(now))
        pipe.expire(key, self._idle_ttl_seconds)
        pipe.expire(index_key, self._idle_ttl_seconds * 4)
        pipe.execute()
        return True

    def end_activity(
        self,
        *,
        tenant_id: str,
        conversation_id: str,
        image_stack: str,
        bundle_id: str = DEFAULT_WORKSPACE_SCOPE_BUNDLE_ID,
        skill_name: str = DEFAULT_WORKSPACE_SCOPE_SKILL_NAME,
    ) -> bool:
        """Decrement the in-flight exec count for a binding."""

        key = self._resolve_routing_key(tenant_id, conversation_id, bundle_id, skill_name, image_stack)
        if not key:
            return False
        remaining = int(cast(Any, self.redis).hincrby(key, "active_execs", -1))
        if remaining < 0:
            cast(Any, self.redis).hset(key, "active_execs", "0")
        return True

    def unbind(
        self,
        *,
        tenant_id: str,
        conversation_id: str,
        image_stack: str,
        bundle_id: str = DEFAULT_WORKSPACE_SCOPE_BUNDLE_ID,
        skill_name: str = DEFAULT_WORKSPACE_SCOPE_SKILL_NAME,
    ) -> bool:
        """Remove a binding.

        The WorkspaceContainerManager calls this after killing the container
        (idle reaper, OOM, manual delete, conversation close). Returns True
        if a binding was removed, False if no binding existed.
        """
        keys = self._routing_keys(tenant_id, conversation_id, bundle_id, skill_name, image_stack)
        deleted = bool(delete_candidate_keys(self.redis, keys))
        for index_key in self._tenant_index_keys(tenant_id):
            for member in keys:
                cast(Any, self.redis).srem(index_key, member)

        if deleted:
            logger.info(
                "workspace_container_registry.unbind",
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                bundle_id=bundle_id,
                skill_name=skill_name,
                image_stack=image_stack,
            )
        return deleted

    def list_for_tenant(self, tenant_id: str) -> List[WorkspaceContainerBinding]:
        """All current bindings for a tenant.

        Powers the ops dashboard "Workspace Containers" panel (ADR-0106 Slice C).
        Includes a self-heal pass: if the tenant index references a key
        that has been TTL-evicted, the index entry is cleaned up.
        """
        index_keys = self._tenant_index_keys(tenant_id)
        raw_members = smembers_union(self.redis, index_keys)

        bindings: List[WorkspaceContainerBinding] = []
        stale_members: List[str] = []
        for member in raw_members:
            data = cast(Dict[Any, Any], cast(Any, self.redis).hgetall(member))
            if not data:
                stale_members.append(member)
                continue
            try:
                binding = WorkspaceContainerBinding.from_redis_mapping(data)
            except Exception as exc:
                logger.error(
                    "workspace_container_registry.corrupt_binding",
                    key=member,
                    error=str(exc),
                    exc_info=True,
                )
                continue
            if binding.tenant_id != tenant_id:
                logger.warning(
                    "workspace_container_registry.tenant_index_mismatch",
                    index_tenant=tenant_id,
                    binding_tenant=binding.tenant_id,
                    key=member,
                )
                stale_members.append(member)
                continue
            bindings.append(binding)

        if stale_members:
            for index_key in index_keys:
                cast(Any, self.redis).srem(index_key, *stale_members)

        return bindings

    def list_all(self) -> List[WorkspaceContainerBinding]:
        """All current bindings across all tenants.

        Used by the idle reaper sweep. O(N) over all routing keys; acceptable
        in steady state because the keys are bounded by per-tenant caps and
        TTL eviction.
        """
        bindings: List[WorkspaceContainerBinding] = []
        seen: set[str] = set()
        for pattern in family_scan_patterns(f"{self.KEY_PREFIX}:"):
            for raw_key in cast(Any, self.redis).scan_iter(match=pattern):
                key = raw_key.decode("utf-8") if isinstance(raw_key, bytes) else raw_key
                if key in seen:
                    continue
                seen.add(key)
                if "workspace:container:index:tenant:" in key:
                    continue
                data = cast(Dict[Any, Any], cast(Any, self.redis).hgetall(key))
                if not data:
                    continue
                try:
                    bindings.append(WorkspaceContainerBinding.from_redis_mapping(data))
                except Exception as exc:
                    logger.error(
                        "workspace_container_registry.corrupt_binding",
                        key=key,
                        error=str(exc),
                        exc_info=True,
                    )
        return bindings

    def count_for_tenant(self, tenant_id: str) -> int:
        """Cheap cardinality lookup for per-tenant cap enforcement."""
        return len(self.list_for_tenant(tenant_id))


__all__ = [
    "WorkspaceContainerBinding",
    "WorkspaceContainerMode",
    "WorkspaceContainerRegistry",
    "DEFAULT_WORKSPACE_SCOPE_BUNDLE_ID",
    "DEFAULT_WORKSPACE_SCOPE_SKILL_NAME",
]
