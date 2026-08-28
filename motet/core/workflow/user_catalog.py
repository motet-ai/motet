"""
Motet - User Workflow Catalog (Redis durability)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-20

Description:
    Durable store for builder/API-authored ``user.*`` workflow definitions.
    Complements in-process ``WorkflowRegistry`` so registrations survive worker
    restarts and can be hydrated on every worker. Parallel to bundle catalogs
    (which store ids only; definitions live in artifacts) — this store holds
    the full serialized workflow dict.

    Issue #234: catalog keys are tenant-prefixed so ``~{tenant_id}:*`` matches.
    Shared workers may hydrate every tenant into one process registry; list,
    discovery, and invoke filter fail-closed on the caller tenant. Redis is
    the source of truth so two tenants may share ``user.<owner>.<local>``.
    Function-discovery docs for ``user.*`` are ``workflow:{tenant_id}:{id}``
    and are indexed from this catalog, not from the one-slot in-process
    registry.

Dependencies:
    - motet.core.distributed.redis_manager: sync Redis client + structured set/get
    - motet.core.distributed.tenant_keys: tenant_key
    - motet.core.workflow: Workflow.from_dict / to_dict
    - motet.core.registry: RegistryScope / ScopeGrant for discovery visibility

Usage:
    from motet.core.workflow.user_catalog import (
        persist_user_workflow,
        delete_user_workflow,
        load_user_workflows_into_registry,
        list_visible_workflows,
        resolve_visible_workflow,
        user_workflow_visible_to_tenant,
        assert_user_workflow_invokable,
    )

    persist_user_workflow(workflow)
    n = load_user_workflows_into_registry()

Notes:
    - Keys: ``{tenant_id}:user_wf:{workflow_id}`` (JSON definition),
      ``{tenant_id}:user_wf:index`` (SET of ids),
      ``{tenant_id}:user_wf:rev`` (INCR). Live keys only.
    - Fan-out to live workers is handled by ``core.sync_user_workflow``.
    - Invoke / export of ``user.*`` requires a matching caller tenant.
    - Search docs: ``workflow:{tenant_id}:{workflow_id}``. Callable names stay
      ``workflow_{workflow_id}``. Personal (principal) ids are not required.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import structlog

from motet.core.distributed.tenant_keys import tenant_key

logger = structlog.get_logger(__name__)

USER_WF_FAMILY = "user_wf:"
USER_WF_INDEX_LOGICAL = "user_wf:index"
USER_WF_REV_LOGICAL = "user_wf:rev"
CATALOG_SERVICE = "user_workflow_catalog"
_UNSCOPED_TENANT = "__unscoped__"


def user_workflow_definition_key(tenant_id: str, workflow_id: str) -> str:
    """Return ``{tenant_id}:user_wf:{workflow_id}``."""
    return tenant_key(tenant_id, f"{USER_WF_FAMILY}{workflow_id}")


def user_workflow_ids_key(tenant_id: str) -> str:
    """Return ``{tenant_id}:user_wf:index``."""
    return tenant_key(tenant_id, USER_WF_INDEX_LOGICAL)


def user_workflow_rev_key(tenant_id: str) -> str:
    """Return ``{tenant_id}:user_wf:rev``."""
    return tenant_key(tenant_id, USER_WF_REV_LOGICAL)


def user_workflow_discovery_doc_id(tenant_id: str, workflow_id: str) -> str:
    """Return the tenant-qualified function-discovery id for a ``user.*`` workflow."""
    tid = (tenant_id or "").strip()
    wid = (workflow_id or "").strip()
    if not tid or not wid:
        raise ValueError("tenant_id and workflow_id are required for user workflow discovery")
    return f"workflow:{tid}:{wid}"


def leftover_user_workflow_discovery_doc_id(workflow_id: str) -> str:
    """Pre-tenant FD id ``workflow:{workflow_id}`` (dropped on reindex/remove)."""
    return f"workflow:{(workflow_id or '').strip()}"


def iter_catalog_user_workflows(
    client: Any = None,
) -> List[Tuple[str, str, Dict[str, Any]]]:
    """Return ``(tenant_id, workflow_id, payload)`` for every catalog row. Fail-soft."""
    rows: List[Tuple[str, str, Dict[str, Any]]] = []
    try:
        for tenant_id in _known_catalog_tenant_ids(client):
            for workflow_id in list_user_workflow_ids(tenant_id):
                raw = fetch_user_workflow_dict(workflow_id, tenant_id=tenant_id)
                if isinstance(raw, dict):
                    rows.append((tenant_id, workflow_id, raw))
    except Exception as exc:
        logger.warning(
            "user_workflow_catalog_iter_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )
    return rows


def catalog_user_workflow_discovery_doc_ids(client: Any = None) -> List[str]:
    """Return ``workflow:{tenant}:{id}`` for every catalog row. Fail-soft."""
    return [
        user_workflow_discovery_doc_id(tenant_id, workflow_id)
        for tenant_id, workflow_id, _payload in iter_catalog_user_workflows(client)
    ]


def _sync_user_workflow_discovery(op: str, workflow_id: str, tenant_id: str) -> None:
    """Best-effort FD index update from the current worker's discovery store."""
    tid = (tenant_id or "").strip()
    wid = (workflow_id or "").strip()
    if not tid or not wid:
        return
    store = None
    try:
        from motet.core.commands.decorator import get_motet_context

        ctx = get_motet_context()
        store = getattr(ctx, "function_discovery_store", None) if ctx is not None else None
    except Exception:
        store = None
    if store is None:
        return
    try:
        op_norm = (op or "").strip().lower()
        if op_norm == "unregister":
            store.remove_user_workflow(wid, tid)
        else:
            store.index_user_workflow(wid, tid)
    except Exception as exc:
        logger.warning(
            "user_workflow_discovery_sync_failed",
            op=op,
            workflow_id=wid,
            tenant_id=tid,
            error=str(exc),
            error_type=type(exc).__name__,
        )


def _workflow_tenant(workflow: Any) -> str:
    meta = getattr(workflow, "metadata", None)
    if isinstance(workflow, dict):
        meta = workflow.get("metadata")
    if not isinstance(meta, dict):
        return ""
    return str(meta.get("tenant_id") or "").strip()


def is_user_workflow_id(workflow_id: str) -> bool:
    """True for builder/API ``user.*`` catalog ids."""
    return str(workflow_id or "").startswith("user.")


def caller_tenant_id(motet: Any = None) -> str:
    """Caller tenant from *motet* or the current MotetContext, else empty."""
    if motet is not None:
        return str(getattr(motet, "tenant_id", "") or "").strip()
    try:
        from motet.core.commands.decorator import get_motet_context

        ctx = get_motet_context()
        return str(getattr(ctx, "tenant_id", "") or "").strip()
    except Exception:
        return ""


def user_workflow_visible_to_tenant(workflow: Any, tenant_id: Optional[str]) -> bool:
    """
    Core/bundle workflows are always visible. ``user.*`` is visible only when
    metadata ``tenant_id`` matches the caller. Missing tenant on either side
    is fail-closed.
    """
    wid = str(getattr(workflow, "workflow_id", "") or "")
    if isinstance(workflow, dict):
        wid = str(workflow.get("workflow_id") or "")
    if not is_user_workflow_id(wid):
        return True
    wf_tenant = _workflow_tenant(workflow)
    caller = str(tenant_id or "").strip()
    if not wf_tenant or not caller:
        return False
    return wf_tenant == caller


def scope_for_user_workflow(workflow: Any) -> Any:
    """Registry/FD scope so discovery matches the owning tenant only."""
    from motet.core.registry import RegistryScope, ScopeGrant

    tid = _workflow_tenant(workflow) or _UNSCOPED_TENANT
    return RegistryScope(namespace="user", grants=[ScopeGrant(tenant_id=tid)])


def persist_user_workflow(workflow: Any, *, tenant_id: Optional[str] = None) -> str:
    """Write a workflow definition to the tenant catalog and add it to the id set."""
    from motet.core.distributed.redis_manager import (
        get_sync_redis_client,
        store_structured_data_sync,
    )

    workflow_id = str(workflow.workflow_id)
    tid = (tenant_id or _workflow_tenant(workflow) or "").strip()
    if not tid:
        raise ValueError("tenant_id is required to persist a user workflow")
    payload = workflow.to_dict() if hasattr(workflow, "to_dict") else dict(workflow)
    meta = dict(payload.get("metadata") or {})
    if not str(meta.get("tenant_id") or "").strip():
        meta["tenant_id"] = tid
        payload["metadata"] = meta
    key = user_workflow_definition_key(tid, workflow_id)
    store_structured_data_sync(
        CATALOG_SERVICE,
        key,
        payload,
        format_type="json_string",
    )
    client = get_sync_redis_client(CATALOG_SERVICE)
    client.sadd(user_workflow_ids_key(tid), workflow_id)
    client.incr(user_workflow_rev_key(tid))
    logger.info(
        "user_workflow_persisted",
        workflow_id=workflow_id,
        tenant_id=tid,
        key=key,
    )
    _sync_user_workflow_discovery("register", workflow_id, tid)
    return key


def delete_user_workflow(workflow_id: str, *, tenant_id: Optional[str] = None) -> bool:
    """Remove a workflow definition from the tenant catalog."""
    from motet.core.distributed.redis_manager import get_sync_redis_client

    wid = (workflow_id or "").strip()
    if not wid:
        return False
    client = get_sync_redis_client(CATALOG_SERVICE)
    tid = (tenant_id or "").strip()
    deleted = False
    if not tid:
        logger.info("user_workflow_deleted", workflow_id=wid, deleted=False)
        return False
    deleted = bool(client.delete(user_workflow_definition_key(tid, wid)))
    client.srem(user_workflow_ids_key(tid), wid)
    client.incr(user_workflow_rev_key(tid))
    logger.info(
        "user_workflow_deleted",
        workflow_id=wid,
        tenant_id=tid or None,
        deleted=deleted,
    )
    if deleted:
        _sync_user_workflow_discovery("unregister", wid, tid)
    return deleted


def fetch_user_workflow_dict(
    workflow_id: str,
    *,
    tenant_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Load one workflow definition dict from Redis, or None.

    *tenant_id* is required. Missing tenant returns None (fail-closed).
    """
    from motet.core.distributed.redis_manager import retrieve_structured_data_sync

    wid = (workflow_id or "").strip()
    if not wid:
        return None
    tid = (tenant_id or "").strip()
    if not tid:
        return None
    key = user_workflow_definition_key(tid, wid)
    try:
        data = retrieve_structured_data_sync(
            CATALOG_SERVICE,
            key,
            format_type="json_string",
        )
    except Exception as exc:
        logger.warning(
            "user_workflow_fetch_failed",
            workflow_id=wid,
            key=key,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return None
    return data if isinstance(data, dict) else None


def list_user_workflow_ids(tenant_id: Optional[str] = None) -> List[str]:
    """Return durable user workflow ids for one tenant.

    *tenant_id* is required. Missing tenant returns an empty list.
    """
    from motet.core.distributed.redis_manager import get_sync_redis_client
    from motet.core.distributed.tenant_keys import smembers_union

    client = get_sync_redis_client(CATALOG_SERVICE)
    tid = (tenant_id or "").strip()
    if not tid:
        return []
    return sorted(smembers_union(client, user_workflow_ids_key(tid)))


def list_user_workflows_for_tenant(tenant_id: str) -> List[Any]:
    """Load ``user.*`` Workflow objects for *tenant_id* from Redis."""
    from motet.core.workflow import Workflow

    tid = (tenant_id or "").strip()
    if not tid:
        return []
    loaded: List[Any] = []
    for workflow_id in list_user_workflow_ids(tid):
        raw = fetch_user_workflow_dict(workflow_id, tenant_id=tid)
        if not raw:
            continue
        try:
            loaded.append(Workflow.from_dict(raw))
        except Exception as exc:
            logger.warning(
                "user_workflow_tenant_list_failed",
                workflow_id=workflow_id,
                tenant_id=tid,
                error=str(exc),
                error_type=type(exc).__name__,
            )
    return loaded


def resolve_user_workflow_for_tenant(workflow_id: str, tenant_id: str) -> Optional[Any]:
    """Return the catalog definition for *tenant_id*, or None."""
    raw = fetch_user_workflow_dict(workflow_id, tenant_id=tenant_id)
    if not raw:
        return None
    from motet.core.workflow import Workflow

    return Workflow.from_dict(raw)


def list_visible_workflows(tenant_id: Optional[str] = None) -> List[Any]:
    """
    Workflows the caller may list: in-process core/bundle plus this tenant's
    ``user.*`` catalog rows. Other tenants' ``user.*`` are omitted. Redis
    wins when the same id is in both the registry and the catalog.
    """
    from motet.core.workflow import WorkflowRegistry

    tid = str(tenant_id or "").strip()
    by_id: Dict[str, Any] = {}
    for workflow in WorkflowRegistry.list_all():
        if user_workflow_visible_to_tenant(workflow, tid):
            by_id[str(workflow.workflow_id)] = workflow
    if tid:
        for extra in list_user_workflows_for_tenant(tid):
            by_id[str(extra.workflow_id)] = extra
    return list(by_id.values())


def resolve_visible_workflow(
    workflow_id: str,
    tenant_id: Optional[str],
    *,
    allow_catalog: bool = True,
) -> Optional[Any]:
    """
    Resolve one workflow for builder register/unregister/export.

    Uses the in-process registry when the row is visible to *tenant_id*.
    When the caller has a tenant, other tenants' ``user.*`` copies are
    ignored. With no caller tenant the in-process slot is kept (persist=False
    register/export on the same worker). Then, if *allow_catalog*, loads this
    tenant's Redis definition.
    """
    from motet.core.workflow import Workflow, WorkflowRegistry

    wid = (workflow_id or "").strip()
    if not wid:
        return None
    tid = str(tenant_id or "").strip()
    existing = WorkflowRegistry.get(wid)
    if existing is not None and is_user_workflow_id(wid) and tid:
        if not user_workflow_visible_to_tenant(existing, tid):
            existing = None
    if existing is not None:
        return existing
    if not allow_catalog:
        return None
    raw = fetch_user_workflow_dict(wid, tenant_id=tid or None)
    if not raw:
        return None
    try:
        return Workflow.from_dict(raw)
    except Exception as exc:
        logger.warning(
            "user_workflow_resolve_parse_failed",
            workflow_id=wid,
            tenant_id=tid or None,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return None


def assert_user_workflow_invokable(workflow_id: str, tenant_id: Optional[str]) -> Any:
    """
    Fail-closed resolve for ``user.*`` invoke.

    Prefers the tenant Redis catalog. Falls back to the in-process registry
    when the definition was registered on this worker without persist
    (``persist=False``) and metadata ``tenant_id`` matches the caller.
    """
    wid = (workflow_id or "").strip()
    if not is_user_workflow_id(wid):
        raise ValueError(f"{wid!r} is not a user workflow id")
    tid = str(tenant_id or "").strip()
    if not tid:
        raise ValueError("user workflow invoke requires tenant_id")
    resolved = resolve_user_workflow_for_tenant(wid, tid)
    if resolved is not None:
        return resolved
    from motet.core.workflow import WorkflowRegistry

    local = WorkflowRegistry.get(wid)
    if local is not None and user_workflow_visible_to_tenant(local, tid):
        return local
    raise ValueError(f"user workflow {wid!r} is not visible to tenant {tid!r}")


def _decode_key(raw: Any) -> str:
    if isinstance(raw, (bytes, bytearray)):
        return raw.decode("utf-8", errors="replace")
    return str(raw)


def _known_catalog_tenant_ids(client: Any = None) -> List[str]:
    from motet.core.distributed.redis_manager import get_sync_redis_client
    from motet.core.distributed.tenant_acl import catalog_tenant_ids

    redis = client or get_sync_redis_client(CATALOG_SERVICE)
    ids: set[str] = set()
    try:
        ids.update(catalog_tenant_ids(redis))
    except Exception as exc:
        logger.warning(
            "user_workflow_catalog_tenants_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )
    scan = getattr(redis, "scan", None)
    if scan is None:
        return sorted(ids)
    try:
        cursor = 0
        while True:
            cursor, batch = scan(cursor, match="*:user_wf:index", count=200)
            for raw in batch or []:
                key = _decode_key(raw)
                tid = key.split(":", 1)[0].strip()
                if tid:
                    ids.add(tid)
            if int(cursor) == 0:
                break
    except Exception as exc:
        logger.warning(
            "user_workflow_tenant_scan_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )
    return sorted(ids)


def _register_hydrated(workflow: Any) -> None:
    from motet.core.workflow import WorkflowRegistry

    WorkflowRegistry.register(workflow, scope=scope_for_user_workflow(workflow))


def load_user_workflows_into_registry() -> int:
    """
    Hydrate ``WorkflowRegistry`` from Redis on worker startup.

    Shared workers load every tenant. Visibility is enforced at list /
    discovery / invoke. Returns the number of workflows registered locally.
    """
    from motet.core.workflow import Workflow

    loaded = 0
    for tenant_id, workflow_id, raw in iter_catalog_user_workflows():
        try:
            workflow = Workflow.from_dict(raw)
            _register_hydrated(workflow)
            loaded += 1
        except Exception as exc:
            logger.warning(
                "user_workflow_hydrate_failed",
                workflow_id=workflow_id,
                tenant_id=tenant_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
    logger.info("user_workflows_hydrated", count=loaded)
    return loaded


def apply_user_workflow_sync(
    op: str,
    workflow_id: str,
    *,
    tenant_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Apply a register/unregister sync on this worker from Redis.

    Used by ``core.sync_user_workflow`` fan-out.
    """
    from motet.core.workflow import Workflow, WorkflowRegistry

    op_norm = (op or "").strip().lower()
    wid = (workflow_id or "").strip()
    tid = (tenant_id or "").strip()
    if op_norm == "unregister":
        removed = WorkflowRegistry.unregister(wid)
        if tid:
            _sync_user_workflow_discovery("unregister", wid, tid)
        return {"op": "unregister", "workflow_id": wid, "removed": bool(removed)}
    if op_norm != "register":
        raise ValueError(f"unsupported sync op: {op!r}")
    raw = fetch_user_workflow_dict(wid, tenant_id=tenant_id)
    if not raw:
        raise ValueError(f"user workflow {wid!r} not found in Redis catalog")
    if WorkflowRegistry.get(wid) is not None:
        WorkflowRegistry.unregister(wid)
    workflow = Workflow.from_dict(raw)
    _register_hydrated(workflow)
    resolved_tid = _workflow_tenant(workflow) or tid
    if resolved_tid:
        _sync_user_workflow_discovery("register", wid, resolved_tid)
    return {
        "op": "register",
        "workflow_id": wid,
        "step_count": len(workflow.steps or {}),
        "tenant_id": resolved_tid,
    }


def fan_out_user_workflow_sync(
    *,
    op: str,
    workflow_id: str,
    exclude_worker_id: Optional[str] = None,
    motet: Any = None,
    tenant_id: str = "default",
    principal_id: str = "",
) -> Dict[str, Any]:
    """
    Best-effort fan-out of sync_user_workflow to live AI workers.

    Prefer ``motet.apply`` when a MotetContext is available; otherwise invoke
    per-worker via ``global_invoker``. Redis remains the source of truth for
    workers that miss the fan-out (they hydrate on next startup).
    """
    import uuid

    try:
        from motet.core.bundles.deploy import _resolve_live_targeted_workers
        from motet.core.distributed.redis_manager import get_sync_redis_client
        from motet.core.commands.builtin.sync_user_workflow import (
            sync_user_workflow,
            SyncUserWorkflowData,
        )
    except Exception as exc:
        logger.warning(
            "user_workflow_fanout_import_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return {"acked": [], "failed": [], "skipped": True, "error": str(exc)}

    try:
        redis_client = get_sync_redis_client(CATALOG_SERVICE)
        workers = _resolve_live_targeted_workers(redis_client, targeting=None)
    except Exception as exc:
        logger.warning("user_workflow_fanout_list_workers_failed", error=str(exc))
        return {"acked": [], "failed": [], "error": str(exc)}

    if exclude_worker_id:
        workers = [w for w in workers if w != exclude_worker_id]
    if not workers:
        return {"acked": [], "failed": [], "note": "no live workers"}

    if motet is not None and hasattr(motet, "apply"):
        inputs = [
            {
                "op": op,
                "workflow_id": workflow_id,
                "target_worker_id": worker_id,
                "tenant_id": tenant_id,
            }
            for worker_id in workers
        ]
        try:
            results = motet.apply(sync_user_workflow, inputs=inputs)
            acked: List[str] = []
            failed: List[str] = []
            for i, result in enumerate(results or []):
                worker_id = workers[i] if i < len(workers) else f"worker-{i}"
                if isinstance(result, dict) and result.get("_error"):
                    failed.append(worker_id)
                else:
                    acked.append(worker_id)
            return {"acked": acked, "failed": failed}
        except Exception as exc:
            logger.warning(
                "user_workflow_fanout_apply_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )

    from motet.core.workers import global_invoker

    acked = []
    failed = []
    for worker_id in workers:
        try:
            cmd = sync_user_workflow(
                task_id=str(uuid.uuid4()),
                conversation_id="",
                tenant_id=tenant_id or "default",
                principal_id=principal_id or "",
                data=SyncUserWorkflowData(
                    op=op,
                    workflow_id=workflow_id,
                    target_worker_id=worker_id,
                    tenant_id=tenant_id,
                ),
            )
            ctx = getattr(cmd, "distributed_context", None)
            if ctx is not None:
                ctx.target_worker_id = worker_id
            result = global_invoker.execute_command(cmd)
            status = result.get("status") if isinstance(result, dict) else None
            if status in (None, "completed", "success", "ok"):
                acked.append(worker_id)
            else:
                failed.append(worker_id)
        except Exception as exc:
            failed.append(worker_id)
            logger.warning(
                "user_workflow_fanout_worker_error",
                worker_id=worker_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )

    logger.info(
        "user_workflow_fanout_complete",
        op=op,
        workflow_id=workflow_id,
        acked=len(acked),
        failed=len(failed),
    )
    return {"acked": acked, "failed": failed}


__all__ = [
    "USER_WF_FAMILY",
    "USER_WF_INDEX_LOGICAL",
    "USER_WF_REV_LOGICAL",
    "apply_user_workflow_sync",
    "assert_user_workflow_invokable",
    "caller_tenant_id",
    "catalog_user_workflow_discovery_doc_ids",
    "delete_user_workflow",
    "fan_out_user_workflow_sync",
    "fetch_user_workflow_dict",
    "is_user_workflow_id",
    "iter_catalog_user_workflows",
    "leftover_user_workflow_discovery_doc_id",
    "list_user_workflow_ids",
    "list_user_workflows_for_tenant",
    "list_visible_workflows",
    "load_user_workflows_into_registry",
    "persist_user_workflow",
    "resolve_user_workflow_for_tenant",
    "resolve_visible_workflow",
    "scope_for_user_workflow",
    "user_workflow_definition_key",
    "user_workflow_discovery_doc_id",
    "user_workflow_ids_key",
    "user_workflow_rev_key",
    "user_workflow_visible_to_tenant",
]
