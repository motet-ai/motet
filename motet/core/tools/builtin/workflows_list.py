"""
Motet - Workflows List Tool

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-20

Description:
    Built-in tool that lists workflows visible in the current worker context.
    Includes local WorkflowRegistry entries plus Redis bundle catalog fallback
    so deployed bundle workflows are discoverable even before local module load.

Dependencies:
    - pydantic: Parameter validation for tool inputs
    - motet.core.workflow: Local workflow registry access
    - motet.core.bundles.deploy: Bundle catalog lookup helper
    - motet.core.distributed.redis_manager: Redis client for catalog reads
    - motet.core.tools.protocol: Standard tool response helpers

Usage:
    from motet.core.tools.builtin.workflows_list import run
    result = run({"bundle_id": "calculator", "name_contains": "calc"})

Notes:
    - Returns workflow IDs callable as `workflow_<workflow_id>` from LLM tools.
    - Applies optional bundle/name filtering and pagination.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from ...registry import namespace_from_qualified_name
from ..protocol import ok, err
from ..registry import ToolRegistry


class WorkflowsListParams(BaseModel):
    """Parameters for listing available workflows."""

    bundle_id: Optional[str] = Field(
        default=None,
        description="Optional bundle namespace filter (e.g. 'calculator'). Use 'core' for built-in workflows.",
    )
    name_contains: Optional[str] = Field(
        default=None,
        description="Case-insensitive substring match over workflow_id and description.",
    )
    include_steps: bool = Field(
        default=False,
        description="If True, include serialized steps and execution_order in each workflow result.",
    )
    limit: Optional[int] = Field(default=100, ge=1, le=500, description="Maximum number of workflows to return.")
    offset: int = Field(default=0, ge=0, description="Number of workflows to skip.")


def run(params: Dict[str, Any]) -> Dict[str, Any]:
    """List visible workflows from registry and bundle catalog (synchronous)."""
    try:
        parsed = WorkflowsListParams(**(params or {}))
    except Exception as exc:
        return err(f"validation error: {exc}")

    try:
        from ...workflow.user_catalog import (
            caller_tenant_id,
            is_user_workflow_id,
            list_visible_workflows,
        )

        tenant_id = caller_tenant_id()
        workflows: List[Dict[str, Any]] = []
        known_ids = set()

        for wf in list_visible_workflows(tenant_id):
            item: Dict[str, Any] = {
                "workflow_id": wf.workflow_id,
                "name": wf.name or wf.workflow_id,
                "description": wf.description or "",
                "step_count": len(wf.steps) if wf.steps else 0,
                "bundle_id": namespace_from_qualified_name(wf.workflow_id),
                "source": (
                    "user_catalog"
                    if is_user_workflow_id(wf.workflow_id)
                    else "registry"
                ),
            }
            if parsed.include_steps:
                item["steps"] = {
                    sid: {
                        "step_id": step.step_id,
                        "name": step.name,
                        "command_type": step.command_type,
                        "command_data": step.command_data if hasattr(step, "command_data") else {},
                        "dependencies": step.dependencies or [],
                        "execution_context": step.execution_context or {},
                    }
                    for sid, step in (wf.steps or {}).items()
                }
                item["execution_order"] = wf.execution_order or []
            workflows.append(item)
            known_ids.add(wf.workflow_id)

        # Best-effort merge from bundle catalogs.
        try:
            from ...distributed.redis_manager import get_sync_redis_client
            from motet.core.bundles.deploy import _list_all_catalogs

            catalogs = _list_all_catalogs(get_sync_redis_client())
            for bundle_id, catalog in sorted(catalogs.items()):
                for workflow_id in catalog.get("workflows", []):
                    if workflow_id in known_ids:
                        continue
                    workflows.append(
                        {
                            "workflow_id": workflow_id,
                            "name": workflow_id,
                            "description": f"Bundle workflow from '{bundle_id}' (catalog)",
                            "step_count": 0,
                            "bundle_id": bundle_id,
                            "source": "catalog",
                        }
                    )
                    known_ids.add(workflow_id)
        except Exception:
            pass  # best-effort: catalog merge optional if Redis unavailable

        bundle_filter = (parsed.bundle_id or "").strip()
        if bundle_filter:
            if bundle_filter == "core":
                workflows = [w for w in workflows if w.get("bundle_id") == "core"]
            else:
                workflows = [w for w in workflows if str(w.get("workflow_id", "")).startswith(f"{bundle_filter}.")]

        name_filter = (parsed.name_contains or "").strip().lower()
        if name_filter:
            workflows = [
                w
                for w in workflows
                if name_filter in str(w.get("workflow_id", "")).lower()
                or name_filter in str(w.get("description", "")).lower()
            ]

        workflows.sort(key=lambda x: str(x.get("workflow_id", "")))
        total = len(workflows)
        start = parsed.offset
        end = start + parsed.limit if parsed.limit else None
        paged = workflows[start:end] if end is not None else workflows[start:]

        return ok(
            {
                "total": total,
                "workflows": paged,
                "limit": parsed.limit,
                "offset": parsed.offset,
            }
        )
    except Exception as exc:
        return err(f"failed to list workflows: {exc}")


def register(registry: ToolRegistry) -> None:
    """Register the built-in workflows list tool."""
    registry.register(
        name="core.workflows_list",
        description=(
            "List registered workflows (core + bundles) available for function calling. "
            "Use this to discover workflow IDs callable as workflow_<workflow_id>."
        ),
        func=run,
        tool_schema=WorkflowsListParams,
        triggers=["workflows:", "workflows_list:", "list_workflows:"],
        category="system",
        default_timeout_seconds=3.0,
        suggested_max_calls=1,
        cost_class="low",
    )


__all__ = ["register", "run", "WorkflowsListParams"]
