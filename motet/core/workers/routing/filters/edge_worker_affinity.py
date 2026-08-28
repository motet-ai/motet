"""
Motet - Edge Worker Affinity Filter

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Prevents cross-principal/cross-tenant command dispatch to edge workers.
    Edge workers (worker_id starts with ``edge_``) are personal or
    tenant-scoped devices running on user hardware. This filter ensures
    that a command's identity matches the worker's registered scope before
    the router considers the worker as a candidate.

    Cloud workers (non-``edge_`` prefix) are always allowed through.

Dependencies:
    - motet.core.workers.routing.filters.base: WorkerFilter ABC

Usage:
    from motet.core.workers.routing.filters.edge_worker_affinity import (
        EdgeWorkerAffinityFilter,
    )
    worker_filter = EdgeWorkerAffinityFilter()
    filtered = worker_filter.filter_workers(workers, context)

Notes:
    - ``command_scope`` on the worker can be "principal" (default) or "tenant".
    - "principal" scope: command principal_id must match worker owner_principal_id.
    - "tenant" scope: command tenant_id must match worker owner_tenant_id.
    - Commands with no principal_id/tenant_id (system/lifecycle) pass through.
"""

from typing import Any, Dict, List

import structlog

from .base import WorkerFilter

logger = structlog.get_logger(__name__)


class EdgeWorkerAffinityFilter(WorkerFilter):
    """Exclude edge workers that do not match the command identity."""

    def filter_workers(
        self,
        workers: List[Dict[str, Any]],
        context: Any,
    ) -> List[Dict[str, Any]]:
        cmd_principal: str = getattr(context, "principal_id", None) or ""
        cmd_tenant: str = getattr(context, "tenant_id", None) or ""

        if not cmd_principal and not cmd_tenant:
            return workers

        result: List[Dict[str, Any]] = []
        for worker in workers:
            worker_id: str = worker.get("worker_id", "")
            if not worker_id.startswith("edge_"):
                result.append(worker)
                continue

            scope = worker.get("command_scope") or "principal"
            if scope == "principal":
                owner = worker.get("owner_principal_id") or ""
                if not owner or not cmd_principal or cmd_principal == owner:
                    result.append(worker)
                else:
                    logger.debug(
                        "edge_worker_affinity_filter_blocked",
                        worker_id=worker_id,
                        scope=scope,
                        cmd_principal=cmd_principal,
                        owner_principal=owner,
                    )
            else:
                owner_tenant = worker.get("owner_tenant_id") or ""
                if not owner_tenant or not cmd_tenant or cmd_tenant == owner_tenant:
                    result.append(worker)
                else:
                    logger.debug(
                        "edge_worker_affinity_filter_blocked",
                        worker_id=worker_id,
                        scope=scope,
                        cmd_tenant=cmd_tenant,
                        owner_tenant=owner_tenant,
                    )

        return result
