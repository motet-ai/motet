"""
Motet - Sync User Workflow Command

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-19

Description:
    Fan-out command that hydrates or removes a ``user.*`` workflow on a target
    worker from the Redis user-workflow catalog. Mirrors the
    ``core.reload_bundle`` / ``target_worker_id`` routing pattern used by bundle
    deploy.

Dependencies:
    - motet.core.commands.decorator / motet: @motet.command
    - motet.core.workflow.user_catalog: apply_user_workflow_sync

Usage:
    sync_user_workflow(data=SyncUserWorkflowData(op="register", workflow_id="user.acme.brief"))
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import Field

from motet import motet
from motet.core.commands.command_data_classes import BaseCommandData
from motet.core.commands.decorator import get_motet_context


class SyncUserWorkflowData(BaseCommandData):
    """Input for core.sync_user_workflow."""

    op: str = Field(..., description="register | unregister")
    workflow_id: str = Field(..., description="user.* workflow id to sync")
    target_worker_id: Optional[str] = Field(
        default=None,
        description="If set, route this sync to this worker only (fan-out).",
    )
    tenant_id: Optional[str] = Field(
        default=None,
        description="Owning tenant for the {tenant}:user_wf:{id} catalog key.",
    )


@motet.command(
    description="Apply a user-authored workflow catalog change on this worker (register, update, or remove a user.* workflow).",
    timeout_seconds=60)
def sync_user_workflow(data: SyncUserWorkflowData) -> Dict[str, Any]:
    """Apply a user-workflow catalog change on this worker."""
    from motet.core.workflow.user_catalog import apply_user_workflow_sync

    _ = get_motet_context()  # ensure command context is available
    return apply_user_workflow_sync(data.op, data.workflow_id, tenant_id=data.tenant_id)


__all__ = ["SyncUserWorkflowData", "sync_user_workflow"]
