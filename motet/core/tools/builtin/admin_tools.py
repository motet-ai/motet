"""
Motet - Admin Persona Built-In Tools

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Read-only admin tools used by the Motet Admin Persona.
    These tools provide cross-cutting operational visibility across workers,
    tasks, schedules, deployments, costs, vault metadata, conversations, and MCP status.
    All tools are registered with the `motet_admin.` prefix so `core.motet_admin`
    can be scoped to them via ToolFilter(prefix="motet_admin.").
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from typing import Any, Dict, List, Optional, cast

from pydantic import BaseModel, Field

from ..protocol import err, ok
from ..registry import ToolRegistry, get_runtime_stack


def _runtime_context() -> Dict[str, Any]:
    stack = get_runtime_stack()
    principal = getattr(stack, "_principal", None) if stack else None
    from ...workers.invoker_context import resolve_current_identity
    from ...workers.invoker_context import IdentityContext
    from ...security.system_principals import (
        SYSTEM_PRINCIPAL_MCP_MANAGER,
        SYSTEM_TENANT_ID,
        SYSTEM_MOTET_ID,
    )
    identity = resolve_current_identity(
        system_defaults=IdentityContext(
            principal_id=SYSTEM_PRINCIPAL_MCP_MANAGER,
            tenant_id=SYSTEM_TENANT_ID,
            motet_id=SYSTEM_MOTET_ID,
        ),
    )
    roles = list(getattr(principal, "roles", []) or [])
    role_hint = getattr(stack, "_role", None) if stack else None
    if isinstance(role_hint, str) and role_hint:
        roles.append(role_hint)
    return {
        "motet_id": identity.motet_id,
        "tenant_id": identity.tenant_id,
        "principal_id": identity.principal_id,
        "roles": sorted(set([r for r in roles if isinstance(r, str) and r])),
    }


def _decode(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    return value


def _decode_hash(raw: Dict[Any, Any]) -> Dict[str, Any]:
    return {str(_decode(k)): _decode(v) for k, v in (raw or {}).items()}


def _safe_json_loads(value: Any) -> Optional[Any]:
    try:
        if isinstance(value, (bytes, bytearray)):
            value = value.decode("utf-8", errors="replace")
        if isinstance(value, str):
            return json.loads(value)
    except Exception:
        return None
    return None


def _list_command_rows(
    *,
    limit: int,
    status: Optional[str] = None,
    worker_id: Optional[str] = None,
    command_type: Optional[str] = None,
    task_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    from ...distributed.redis_manager import get_sync_redis_client

    from ...distributed.tenant_keys import command_id_from_cmd_key, iter_cmd_keys_sync

    redis_client = get_sync_redis_client("admin_tools")
    out: List[Dict[str, Any]] = []
    match_status = status.lower().strip() if isinstance(status, str) and status else None
    hard_limit = max(limit * 4, 250)

    for key in iter_cmd_keys_sync(redis_client, kind="meta"):
        cmd_id = command_id_from_cmd_key(key)
        raw = cast(Any, redis_client.hgetall(key))
        if not raw:
            continue
        row = _decode_hash(cast(Dict[Any, Any], raw if isinstance(raw, dict) else {}))
        row["command_id"] = cmd_id

        if tenant_id and row.get("tenant_id") and row.get("tenant_id") != tenant_id:
            continue
        if match_status and str(row.get("status", "")).lower() != match_status:
            continue
        if worker_id and row.get("worker_id") != worker_id:
            continue
        if command_type and row.get("command_type") != command_type:
            continue
        if task_id and row.get("task_id") != task_id:
            continue

        out.append(row)
        if len(out) >= hard_limit:
            break

    out.sort(key=lambda r: str(r.get("created_at", "")), reverse=True)
    return out[:limit]


class AdminWorkerSummaryParams(BaseModel):
    limit: int = Field(default=50, ge=1, le=500)


def run_get_worker_summary(params: Dict[str, Any]) -> Dict[str, Any]:
    try:
        parsed = AdminWorkerSummaryParams(**(params or {}))
        from ...distributed.worker_lifecycle import get_lifecycle_service
        from ...distributed.worker_readiness import get_readiness_service

        readiness = get_readiness_service()
        lifecycle = get_lifecycle_service()
        all_workers = readiness.get_all_workers()

        workers: List[Dict[str, Any]] = []
        unhealthy_ids: List[str] = []
        for wid, info in list(all_workers.items())[: parsed.limit]:
            metrics = lifecycle.get_worker_health_metrics(wid)
            healthy = metrics.is_healthy() if metrics else True
            if not healthy:
                unhealthy_ids.append(wid)
            workers.append(
                {
                    "worker_id": wid,
                    "state": getattr(getattr(info, "state", None), "value", str(getattr(info, "state", "unknown"))),
                    "capabilities": list(getattr(info, "capabilities", []) or []),
                    "active_commands": int(getattr(info, "active_commands", 0) or 0),
                    "max_concurrency": int(getattr(info, "max_concurrency", 0) or 0),
                    "tool_count": int(getattr(info, "tool_count", 0) or 0),
                    "mcp_tool_count": int(getattr(info, "mcp_tool_count", 0) or 0),
                    "healthy": healthy,
                    "last_heartbeat": getattr(info, "last_heartbeat", 0),
                }
            )

        return ok({"total_workers": len(all_workers), "unhealthy_workers": unhealthy_ids, "workers": workers})
    except Exception as exc:
        return err(f"failed to get worker summary: {exc}")


class AdminWorkerDetailParams(BaseModel):
    worker_id: str
    task_limit: int = Field(default=20, ge=1, le=200)


def run_get_worker_detail(params: Dict[str, Any]) -> Dict[str, Any]:
    try:
        parsed = AdminWorkerDetailParams(**(params or {}))
        from ...distributed.worker_lifecycle import get_lifecycle_service
        from ...distributed.worker_readiness import get_readiness_service

        readiness = get_readiness_service()
        lifecycle = get_lifecycle_service()
        info = readiness.get_worker_info(parsed.worker_id)
        if not info:
            return err(f"worker not found: {parsed.worker_id}")

        metrics = lifecycle.get_worker_health_metrics(parsed.worker_id)
        rows = _list_command_rows(limit=parsed.task_limit, worker_id=parsed.worker_id)
        return ok(
            {
                "worker_id": parsed.worker_id,
                "state": getattr(getattr(info, "state", None), "value", str(getattr(info, "state", "unknown"))),
                "capabilities": list(getattr(info, "capabilities", []) or []),
                "active_commands": int(getattr(info, "active_commands", 0) or 0),
                "max_concurrency": int(getattr(info, "max_concurrency", 0) or 0),
                "tool_count": int(getattr(info, "tool_count", 0) or 0),
                "mcp_tool_count": int(getattr(info, "mcp_tool_count", 0) or 0),
                "tools": list(getattr(info, "tools", []) or []),
                "health": {
                    "healthy": metrics.is_healthy() if metrics else True,
                    "cpu_usage_percent": getattr(metrics, "cpu_usage_percent", 0.0) if metrics else 0.0,
                    "memory_usage_mb": getattr(metrics, "memory_usage_mb", 0.0) if metrics else 0.0,
                    "error_count_last_hour": getattr(metrics, "error_count_last_hour", 0) if metrics else 0,
                    "success_rate": getattr(metrics, "success_rate", 1.0) if metrics else 1.0,
                },
                "recent_task_rows": rows,
            }
        )
    except Exception as exc:
        return err(f"failed to get worker detail: {exc}")


class AdminTaskHistoryParams(BaseModel):
    limit: int = Field(default=50, ge=1, le=300)
    status: Optional[str] = None
    worker_id: Optional[str] = None
    command_type: Optional[str] = None


def _to_task_rows(command_rows: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for row in command_rows:
        task = str(row.get("task_id") or row.get("command_id") or "")
        if task:
            grouped.setdefault(task, []).append(row)

    out: List[Dict[str, Any]] = []
    for task_id, rows in grouped.items():
        rows = sorted(rows, key=lambda r: str(r.get("created_at", "")))
        statuses = [str(r.get("status", "")).lower() for r in rows]
        if any(s in {"running", "pending"} for s in statuses):
            overall = "running"
        elif any(s in {"failed", "error"} for s in statuses):
            overall = "failed"
        elif statuses and all(s in {"completed", "success"} for s in statuses):
            overall = "completed"
        else:
            overall = statuses[-1] if statuses else "unknown"
        out.append(
            {
                "task_id": task_id,
                "status": overall,
                "command_count": len(rows),
                "workers": sorted(set([str(r.get("worker_id", "")) for r in rows if r.get("worker_id")])),
                "created_at": rows[0].get("created_at"),
                "latest_at": rows[-1].get("created_at"),
                "principal_id": rows[0].get("principal_id"),
                "tenant_id": rows[0].get("tenant_id"),
                "motet_id": rows[0].get("motet_id"),
                "error_summary": next((r.get("error") for r in reversed(rows) if r.get("error")), None),
            }
        )
    out.sort(key=lambda it: str(it.get("latest_at", "")), reverse=True)
    return out[:limit]


def run_get_task_history(params: Dict[str, Any]) -> Dict[str, Any]:
    try:
        parsed = AdminTaskHistoryParams(**(params or {}))
        ctx = _runtime_context()
        rows = _list_command_rows(
            limit=max(parsed.limit * 4, 200),
            status=parsed.status,
            worker_id=parsed.worker_id,
            command_type=parsed.command_type,
            tenant_id=ctx["tenant_id"],
        )
        tasks = _to_task_rows(rows, parsed.limit)
        return ok({"total_tasks": len(tasks), "tasks": tasks})
    except Exception as exc:
        return err(f"failed to get task history: {exc}")


class AdminSearchTasksParams(AdminTaskHistoryParams):
    task_id_contains: Optional[str] = None


def run_search_tasks(params: Dict[str, Any]) -> Dict[str, Any]:
    try:
        parsed = AdminSearchTasksParams(**(params or {}))
        base = run_get_task_history(parsed.model_dump())
        if base.get("status") != "success":
            return base
        tasks = list((base.get("result") or {}).get("tasks", []))
        q = (parsed.task_id_contains or "").strip().lower()
        if q:
            tasks = [t for t in tasks if q in str(t.get("task_id", "")).lower()]
        return ok({"total_tasks": len(tasks), "tasks": tasks[: parsed.limit]})
    except Exception as exc:
        return err(f"failed to search tasks: {exc}")


class AdminTaskFlowParams(BaseModel):
    """Parameters for get_task_flow: full task flow with command inputs and results."""

    task_id: str
    include_events: bool = Field(default=True, description="Include task execution events")
    event_limit: int = Field(default=200, ge=1, le=1000, description="Max events to return")


def run_get_task_flow(params: Dict[str, Any]) -> Dict[str, Any]:
    """Get full task flow (same data as debug task-flow API): commands with inputs and results."""
    try:
        from ...distributed.task_flow_loader import get_task_flow_sync

        parsed = AdminTaskFlowParams(**(params or {}))
        ctx = _runtime_context()
        tenant_id = ctx.get("tenant_id") or "default"

        commands = get_task_flow_sync(parsed.task_id, tenant_id=tenant_id)

        events: List[Dict[str, Any]] = []
        if parsed.include_events:
            from ...distributed.redis_manager import get_sync_redis_client

            redis_client = get_sync_redis_client("admin_tools")
            raw_events = cast(Any, redis_client.lrange(
                f"task:events:{parsed.task_id}", 0, max(parsed.event_limit - 1, 0)
            ))
            if not isinstance(raw_events, list):
                raw_events = []
            for item in raw_events:
                data = _safe_json_loads(item)
                if isinstance(data, dict):
                    events.append(data)

        return ok({
            "task_id": parsed.task_id,
            "total_commands": len(commands),
            "commands": commands,
            "events": events,
        })
    except Exception as exc:
        return err(f"failed to get task flow: {exc}")


class AdminTaskDetailParams(BaseModel):
    task_id: str
    include_events: bool = True
    event_limit: int = Field(default=200, ge=1, le=1000)


def run_get_task_detail(params: Dict[str, Any]) -> Dict[str, Any]:
    try:
        parsed = AdminTaskDetailParams(**(params or {}))
        ctx = _runtime_context()
        from ...distributed.redis_manager import get_sync_redis_client

        redis_client = get_sync_redis_client("admin_tools")
        rows = _list_command_rows(limit=1000, task_id=parsed.task_id, tenant_id=ctx["tenant_id"])
        rows = sorted(rows, key=lambda r: str(r.get("created_at", "")))

        events: List[Dict[str, Any]] = []
        if parsed.include_events:
            raw_events = cast(Any, redis_client.lrange(f"task:events:{parsed.task_id}", 0, max(parsed.event_limit - 1, 0)))
            if not isinstance(raw_events, list):
                raw_events = []
            for item in raw_events:
                data = _safe_json_loads(item)
                if isinstance(data, dict):
                    events.append(data)

        summary = {
            "task_id": parsed.task_id,
            "command_count": len(rows),
            "status_breakdown": dict(Counter([str(r.get("status", "unknown")) for r in rows])),
            "command_type_breakdown": dict(Counter([str(r.get("command_type", "unknown")) for r in rows])),
            "worker_breakdown": dict(Counter([str(r.get("worker_id", "unknown")) for r in rows])),
        }
        return ok({"summary": summary, "commands": rows, "events": events})
    except Exception as exc:
        return err(f"failed to get task detail: {exc}")


class AdminScheduleSummaryParams(BaseModel):
    status: Optional[str] = None
    limit: int = Field(default=100, ge=1, le=500)


def run_get_schedule_summary(params: Dict[str, Any]) -> Dict[str, Any]:
    try:
        parsed = AdminScheduleSummaryParams(**(params or {}))
        ctx = _runtime_context()
        from ...orchestration.scheduling.manager import ScheduledCommandManager
        from ...orchestration.scheduling.models import ScheduleFilter, ScheduleStatus

        filt = ScheduleFilter(tenant_id=ctx["tenant_id"], motet_id=ctx["motet_id"], limit=parsed.limit)
        if parsed.status:
            filt.status = ScheduleStatus(parsed.status.lower())
        manager = ScheduledCommandManager()
        schedules = manager.list_schedules(filt)
        rows = []
        failures = 0
        for s in schedules:
            if getattr(s, "last_error", None):
                failures += 1
            rows.append(
                {
                    "schedule_id": s.schedule_id,
                    "name": s.name,
                    "command_type": s.command_type,
                    "status": getattr(s.status, "value", str(s.status)),
                    "schedule_type": getattr(s.schedule_type, "value", str(s.schedule_type)),
                    "next_execution_at": s.next_execution_at.isoformat() if s.next_execution_at else None,
                    "last_execution_at": s.last_execution_at.isoformat() if s.last_execution_at else None,
                    "execution_count": s.execution_count,
                    "last_error": s.last_error,
                }
            )
        return ok({"total_schedules": len(rows), "recent_failures": failures, "schedules": rows})
    except Exception as exc:
        return err(f"failed to get schedule summary: {exc}")


class AdminDeploySummaryParams(BaseModel):
    limit: int = Field(default=100, ge=1, le=500)


def run_get_deploy_summary(params: Dict[str, Any]) -> Dict[str, Any]:
    try:
        parsed = AdminDeploySummaryParams(**(params or {}))
        from ...distributed.redis_manager import get_sync_redis_client
        from motet.core.bundles.deploy import _get_bundle_history, _list_all_catalogs

        redis_client = get_sync_redis_client("admin_tools")
        catalogs = _list_all_catalogs(redis_client)
        bundles = []
        for bundle_id, cat in sorted(catalogs.items())[: parsed.limit]:
            history = _get_bundle_history(redis_client, bundle_id)
            latest = history[-1] if history else {}
            bundles.append(
                {
                    "bundle_id": bundle_id,
                    "bundle_version": cat.get("bundle_version"),
                    "commands": len(cat.get("commands", []) or []),
                    "tools": len(cat.get("tools", []) or []),
                    "workflows": len(cat.get("workflows", []) or []),
                    "agents": len(cat.get("agents", []) or []),
                    "mcp_servers": len(cat.get("mcp_servers", []) or []),
                    "last_deploy_status": latest.get("status"),
                    "last_deployed_at": latest.get("deployed_at") or latest.get("timestamp"),
                }
            )
        return ok({"total_bundles": len(bundles), "bundles": bundles})
    except Exception as exc:
        return err(f"failed to get deploy summary: {exc}")


class AdminDeployHistoryParams(BaseModel):
    bundle_id: str
    limit: int = Field(default=50, ge=1, le=500)


def run_get_deploy_history(params: Dict[str, Any]) -> Dict[str, Any]:
    try:
        parsed = AdminDeployHistoryParams(**(params or {}))
        from ...distributed.redis_manager import get_sync_redis_client
        from motet.core.bundles.deploy import _get_bundle_history, _get_worker_state

        redis_client = get_sync_redis_client("admin_tools")
        history = _get_bundle_history(redis_client, parsed.bundle_id)
        worker_state = _get_worker_state(redis_client, parsed.bundle_id)
        return ok({"bundle_id": parsed.bundle_id, "history": list(reversed(history))[: parsed.limit], "worker_state": worker_state})
    except Exception as exc:
        return err(f"failed to get deploy history: {exc}")


class AdminCostSummaryParams(BaseModel):
    date: Optional[str] = None
    include_by_principal: bool = True


def run_get_cost_summary(params: Dict[str, Any]) -> Dict[str, Any]:
    try:
        parsed = AdminCostSummaryParams(**(params or {}))
        ctx = _runtime_context()
        from ...cost import get_cost_tracking_service

        service = get_cost_tracking_service()
        summary = service.get_daily_summary(ctx["tenant_id"], date_key=parsed.date)
        payload: Dict[str, Any] = {"daily_summary": summary}
        if parsed.include_by_principal:
            payload["by_principal"] = service.get_daily_summary_by_principal(ctx["tenant_id"], date_key=parsed.date)
        return ok(payload)
    except Exception as exc:
        return err(f"failed to get cost summary: {exc}")


class AdminConversationCostParams(BaseModel):
    conversation_id: str = Field(..., description="Conversation ID to read cost totals for")
    tenant_id: Optional[str] = Field(
        default=None,
        description="Tenant whose cost totals to read (default: caller tenant)",
    )
    include_children: bool = Field(
        default=False,
        description=(
            "Also sum totals for child conversation_ids "
            "({parent}__suffix), e.g. isolate_conversation workflow chunks"
        ),
    )


def run_get_conversation_cost(params: Dict[str, Any]) -> Dict[str, Any]:
    try:
        parsed = AdminConversationCostParams(**(params or {}))
        ctx = _runtime_context()
        from ...cost import get_cost_tracking_service

        tenant_id = (parsed.tenant_id or "").strip() or ctx["tenant_id"]
        service = get_cost_tracking_service()
        summary = service.get_conversation_cost_summary(
            tenant_id=tenant_id,
            conversation_id=parsed.conversation_id,
            include_children=bool(parsed.include_children),
        )
        return ok(summary)
    except Exception as exc:
        return err(f"failed to get conversation cost: {exc}")


class AdminVaultSummaryParams(BaseModel):
    limit: int = Field(default=100, ge=1, le=1000)
    credential_type: Optional[str] = None
    include_all: bool = False


def run_get_vault_summary(params: Dict[str, Any]) -> Dict[str, Any]:
    try:
        parsed = AdminVaultSummaryParams(**(params or {}))
        ctx = _runtime_context()
        from ...security.vault_service import CredentialType, DistributedVaultService

        vault = DistributedVaultService()
        cred_type = None
        if parsed.credential_type:
            try:
                cred_type = CredentialType(parsed.credential_type)
            except Exception:
                return err(f"invalid credential_type: {parsed.credential_type}")

        is_admin = any(r in {"admin", "motet-admin", "operator"} for r in ctx["roles"])
        include_all = bool(parsed.include_all and is_admin)
        rows = vault.list_credentials(
            principal_id=ctx["principal_id"],
            tenant_id=ctx["tenant_id"],
            motet_id=ctx["motet_id"],
            credential_type=cred_type,
            include_all=include_all,
        )
        out = []
        for row in rows[: parsed.limit]:
            item = row.model_dump(mode="json")
            item.pop("encrypted_data", None)
            out.append(item)
        return ok({"total_credentials": len(out), "include_all": include_all, "credentials": out})
    except Exception as exc:
        return err(f"failed to get vault summary: {exc}")


class AdminConversationSummaryParams(BaseModel):
    limit: int = Field(default=50, ge=1, le=500)
    include_children: bool = Field(
        default=False,
        description=(
            "Attach child_conversation_ids (workflow isolate_conversation "
            "chunks indexed under each conversation) for cycle observability"
        ),
    )


def run_get_conversation_summary(params: Dict[str, Any]) -> Dict[str, Any]:
    try:
        parsed = AdminConversationSummaryParams(**(params or {}))
        ctx = _runtime_context()
        from ...conversations.registry import list_conversations_sync

        convs = list_conversations_sync(
            motet_id=ctx["motet_id"],
            tenant_id=ctx["tenant_id"],
            principal_id=ctx["principal_id"],
            limit=parsed.limit,
        )
        if parsed.include_children:
            from ...conversations.lineage import list_child_conversations_sync

            convs = [dict(c) for c in convs]
            for conv in convs:
                cid = str(conv.get("id") or "").strip()
                if not cid:
                    continue
                children = list_child_conversations_sync(
                    tenant_id=ctx["tenant_id"], conversation_id=cid
                )
                if children:
                    conv["child_conversation_ids"] = children
        return ok({"total_conversations": len(convs), "conversations": convs})
    except Exception as exc:
        return err(f"failed to get conversation summary: {exc}")


class AdminBundleCatalogParams(BaseModel):
    bundle_id: str


def run_get_bundle_catalog(params: Dict[str, Any]) -> Dict[str, Any]:
    try:
        parsed = AdminBundleCatalogParams(**(params or {}))
        from ...distributed.redis_manager import get_sync_redis_client
        from motet.core.bundles.deploy import _get_catalog

        redis_client = get_sync_redis_client("admin_tools")
        catalog = _get_catalog(redis_client, parsed.bundle_id)
        if not catalog:
            return err(f"bundle catalog not found: {parsed.bundle_id}")
        return ok(catalog)
    except Exception as exc:
        return err(f"failed to get bundle catalog: {exc}")


class AdminMcpStatusParams(BaseModel):
    include_workers: bool = True


def run_get_mcp_status(params: Dict[str, Any]) -> Dict[str, Any]:
    try:
        parsed = AdminMcpStatusParams(**(params or {}))
        from ...distributed.worker_readiness import get_readiness_service

        readiness = get_readiness_service()
        all_workers = readiness.get_all_workers()
        workers = []
        total_mcp_tools = 0
        for wid, info in all_workers.items():
            tools = list(getattr(info, "tools", []) or [])
            mcp_tools = [t for t in tools if isinstance(t, str) and t.startswith("mcp.")]
            total_mcp_tools += len(mcp_tools)
            workers.append(
                {
                    "worker_id": wid,
                    "mcp_tool_count": len(mcp_tools),
                    "mcp_tools_preview": mcp_tools[:20],
                    "warmup_completed": bool(getattr(info, "warmup_completed", False)),
                    "last_heartbeat": getattr(info, "last_heartbeat", 0),
                }
            )
        payload: Dict[str, Any] = {"workers_total": len(all_workers), "total_mcp_tools": total_mcp_tools}
        if parsed.include_workers:
            payload["workers"] = workers
        return ok(payload)
    except Exception as exc:
        return err(f"failed to get mcp status: {exc}")


def register(registry: ToolRegistry) -> None:
    registry.register(name="motet_admin.get_worker_summary", description="Get worker list, health, capabilities, and queue/load snapshot for operations triage.", func=run_get_worker_summary, tool_schema=AdminWorkerSummaryParams, category="motet_admin", default_timeout_seconds=5.0, suggested_max_calls=3, cost_class="low")
    registry.register(name="motet_admin.get_worker_detail", description="Get detailed state and recent task rows for a specific worker.", func=run_get_worker_detail, tool_schema=AdminWorkerDetailParams, category="motet_admin", default_timeout_seconds=8.0, suggested_max_calls=3, cost_class="low")
    registry.register(name="motet_admin.get_task_history", description="Get compact recent task history for troubleshooting and timeline inspection.", func=run_get_task_history, tool_schema=AdminTaskHistoryParams, category="motet_admin", default_timeout_seconds=6.0, suggested_max_calls=4, cost_class="low")
    registry.register(name="motet_admin.search_tasks", description="Search task history by status, worker, command type, and task id substring.", func=run_search_tasks, tool_schema=AdminSearchTasksParams, category="motet_admin", default_timeout_seconds=6.0, suggested_max_calls=4, cost_class="low")
    registry.register(name="motet_admin.get_task_detail", description="Get full detail for one task including command timeline and execution events.", func=run_get_task_detail, tool_schema=AdminTaskDetailParams, category="motet_admin", default_timeout_seconds=10.0, suggested_max_calls=3, cost_class="low")
    registry.register(name="motet_admin.get_task_flow", description="Get full task flow for one task: command metadata, inputs, and results (same data as debug task-flow). Use for deep troubleshooting.", func=run_get_task_flow, tool_schema=AdminTaskFlowParams, category="motet_admin", default_timeout_seconds=15.0, suggested_max_calls=3, cost_class="medium")
    registry.register(name="motet_admin.get_schedule_summary", description="Get schedule status summary with next run, last run, and failure signal.", func=run_get_schedule_summary, tool_schema=AdminScheduleSummaryParams, category="motet_admin", default_timeout_seconds=6.0, suggested_max_calls=3, cost_class="low")
    registry.register(name="motet_admin.get_deploy_summary", description="Get deployed bundle summary including catalog composition and latest deploy status.", func=run_get_deploy_summary, tool_schema=AdminDeploySummaryParams, category="motet_admin", default_timeout_seconds=6.0, suggested_max_calls=3, cost_class="low")
    registry.register(name="motet_admin.get_deploy_history", description="Get deployment history and worker state for a bundle.", func=run_get_deploy_history, tool_schema=AdminDeployHistoryParams, category="motet_admin", default_timeout_seconds=8.0, suggested_max_calls=3, cost_class="low")
    registry.register(name="motet_admin.get_cost_summary", description="Get daily cost and usage summary with optional principal breakdown.", func=run_get_cost_summary, tool_schema=AdminCostSummaryParams, category="motet_admin", default_timeout_seconds=5.0, suggested_max_calls=3, cost_class="low")
    registry.register(
        name="motet_admin.get_conversation_cost",
        description=(
            "Read exact running cost totals for one conversation_id "
            "(optional include_children for {parent}__* isolate IDs)."
        ),
        func=run_get_conversation_cost,
        tool_schema=AdminConversationCostParams,
        category="motet_admin",
        default_timeout_seconds=8.0,
        suggested_max_calls=5,
        cost_class="low",
    )
    registry.register(name="motet_admin.get_vault_summary", description="List vault credential metadata visible to caller (no secret values).", func=run_get_vault_summary, tool_schema=AdminVaultSummaryParams, category="motet_admin", default_timeout_seconds=8.0, suggested_max_calls=3, cost_class="low")
    registry.register(name="motet_admin.get_conversation_summary", description="List recent conversations for current principal in tenant/motet scope.", func=run_get_conversation_summary, tool_schema=AdminConversationSummaryParams, category="motet_admin", default_timeout_seconds=5.0, suggested_max_calls=3, cost_class="low")
    registry.register(name="motet_admin.get_bundle_catalog", description="Get bundle catalog with commands/tools/workflows/agents/mcp/model ids.", func=run_get_bundle_catalog, tool_schema=AdminBundleCatalogParams, category="motet_admin", default_timeout_seconds=5.0, suggested_max_calls=3, cost_class="low")
    registry.register(name="motet_admin.get_mcp_status", description="Get MCP availability snapshot across workers including per-worker tool counts.", func=run_get_mcp_status, tool_schema=AdminMcpStatusParams, category="motet_admin", default_timeout_seconds=6.0, suggested_max_calls=3, cost_class="low")


__all__ = ["register"]
