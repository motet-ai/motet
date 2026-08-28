"""
Motet - MCP Per-Service Status Registry

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    Redis hash of per-``service_id`` health records published by the sibling
    MCP instance manager. The ops dashboard and ``GET /api/v1/mcp/servers``
    read this hash; they must not scrape YAML from the API container.

Dependencies:
    - pydantic: status record
    - UnifiedRedisManager: hash storage (AGENTS.md)

Usage:
    publish_mcp_service_status(manager_id, status)
    rows = list_mcp_service_statuses(manager_id)

Notes:
    - Key: ``motet:manager_status:{manager_id}:mcp:services``
    - Hash field: ``service_id``; value: JSON object (no secrets)
    - TTL refreshed on each publish so a dead manager expires
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

import structlog
from pydantic import BaseModel, Field

from motet.core.distributed.redis_manager import (
    get_sync_redis_client,
    retrieve_structured_data_sync,
    store_structured_data_sync,
)
from motet.core.distributed.tenant_keys import (
    first_existing_key,
    product_key,
)

logger = structlog.get_logger(__name__)

_CLIENT_ID = "mcp_instance_manager"
_TTL_SECONDS = 120


def services_status_key(manager_id: str) -> str:
    """Redis hash key for one manager's per-service records."""
    return product_key(f"manager_status:{manager_id}:mcp:services")


class MCPServiceStatus(BaseModel):
    """Operator-visible health of one configured MCP service (no secrets)."""

    service_id: str = Field(..., description="Configured MCP service_id")
    manager_id: str = Field(..., description="Owning sibling manager_id")
    status: str = Field(
        ...,
        description=(
            "running | starting | failed | auth_required | not_started | disabled"
        ),
        json_schema_extra={"example": "running"},
    )
    healthy: bool = Field(..., description="True when at least one child is alive")
    transport: str = Field(default="stdio", description="stdio | http | streamable-http")
    visibility: Optional[str] = Field(default=None, description="Visibility")
    lifecycle_duration: Optional[str] = Field(
        default=None, description="Lifecycle duration"
    )
    state_model: Optional[str] = Field(default=None, description="State model")
    auth_type: str = Field(default="none", description="none | oauth2 | api_key | service_account")
    instance_count: int = Field(default=0, description="Live instance keys for this service")
    instance_ids: List[str] = Field(default_factory=list, description="Live instance keys")
    pids: List[int] = Field(default_factory=list, description="Child PIDs still alive")
    restart_count_window: int = Field(default=0, description="Restarts in the budget window")
    restart_budget_remaining: int = Field(default=0, description="Restarts still allowed")
    last_error: Optional[str] = Field(default=None, description="Short last error; no traceback")
    last_ready_at: Optional[float] = Field(default=None, description="Unix time of service_ready")
    last_removed_at: Optional[float] = Field(default=None, description="Unix time of service_removed")
    last_restarted_at: Optional[float] = Field(
        default=None, description="Unix time of last health restart"
    )
    tool_names: List[str] = Field(
        default_factory=list,
        description="Tool names observed at discovery (mcp.{service_id}.* local names)",
    )
    updated_at: float = Field(..., description="Unix time this record was published")
    disabled: bool = Field(default=False, description="True when operator/bundle disabled the service")


def publish_mcp_service_status(status: MCPServiceStatus) -> None:
    """Upsert one service record and refresh the hash TTL."""
    key = services_status_key(status.manager_id)
    payload = status.model_dump()
    try:
        store_structured_data_sync(
            _CLIENT_ID,
            key,
            {status.service_id: payload},
            format_type="hash",
        )
        redis_client = get_sync_redis_client(_CLIENT_ID)
        redis_client.expire(key, _TTL_SECONDS)
    except Exception as e:
        logger.warning(
            "mcp_service_status_publish_failed",
            manager_id=status.manager_id,
            service_id=status.service_id,
            error=str(e),
            exc_info=True,
        )


def delete_mcp_service_status(manager_id: str, service_id: str) -> None:
    """Remove one service field after unregister (hash may still list others)."""
    try:
        redis_client = get_sync_redis_client(_CLIENT_ID)
        redis_client.hdel(product_key(f"manager_status:{manager_id}:mcp:services"), service_id)
    except Exception as e:
        logger.warning(
            "mcp_service_status_delete_failed",
            manager_id=manager_id,
            service_id=service_id,
            error=str(e),
        )


def list_mcp_service_statuses(manager_id: Optional[str] = None) -> List[MCPServiceStatus]:
    """
    Load per-service records.

    Args:
        manager_id: When set, only that manager. When None, SCAN all
            ``motet:manager_status:*:mcp:services`` hashes.
    """
    rows: List[MCPServiceStatus] = []
    try:
        redis_client = get_sync_redis_client(_CLIENT_ID)
        keys: List[str]
        if manager_id:
            found = first_existing_key(
                redis_client,
                product_key(f"manager_status:{manager_id}:mcp:services"),
            )
            keys = [found] if found else [services_status_key(manager_id)]
        else:
            keys = []
            seen: set[str] = set()
            for pattern in (
                "motet:manager_status:*:mcp:services",
            ):
                cursor = 0
                while True:
                    cursor, found = redis_client.scan(cursor, match=pattern, count=100)
                    for raw in found:
                        decoded = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
                        if decoded not in seen:
                            seen.add(decoded)
                            keys.append(decoded)
                    if cursor == 0:
                        break

        for key in keys:
            data = retrieve_structured_data_sync(_CLIENT_ID, key, format_type="hash") or {}
            for field, value in data.items():
                record: Any = value
                if isinstance(record, str):
                    try:
                        record = json.loads(record)
                    except json.JSONDecodeError:
                        continue
                if not isinstance(record, dict):
                    continue
                record.setdefault("service_id", field)
                try:
                    rows.append(MCPServiceStatus.model_validate(record))
                except Exception:
                    logger.debug(
                        "mcp_service_status_skip_invalid",
                        key=key,
                        field=field,
                    )
    except Exception as e:
        logger.warning("mcp_service_status_list_failed", manager_id=manager_id, error=str(e))
    return rows
