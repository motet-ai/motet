"""
Motet - MCP Manager Control Commands

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Redis Streams control plane for the sibling MCP instance manager.
    Bundle reload and the ops API enqueue commands; the manager process
    consumes them. This replaces in-process ``get_instance_manager()``
    which is always None in the sibling topology.

Dependencies:
    - json: command payload
    - UnifiedRedisManager: stream XADD / XREADGROUP

Usage:
    enqueue_mcp_control_command("mcp-local-default", {
        "op": "register",
        "service_id": "bundle.weather",
        "config": {"command": "mcp-weather", "transport": "stdio"},
    })

Notes:
    - Stream: ``{manager_id}:mcp-control``
    - Ops: register | unregister | restart | disable | enable
    - Consumer group: ``mcp-manager`` (one sibling manager per stream)
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import structlog

from motet.core.distributed.redis_manager import get_sync_redis_client

logger = structlog.get_logger(__name__)

_CLIENT_ID = "mcp_instance_manager"
MCP_CONTROL_OPS = frozenset({"register", "unregister", "restart", "disable", "enable"})
_GROUP = "mcp-manager"
_CONSUMER = "mcp-instance-manager"


def mcp_control_stream_key(manager_id: str) -> str:
    """Redis stream name for one manager's control commands."""
    return f"{manager_id}:mcp-control"


def enqueue_mcp_control_command(
    manager_id: str,
    command: Dict[str, Any],
) -> str:
    """
    Append a control command for the sibling manager.

    Args:
        manager_id: MOTET_MCP_MANAGER_ID of the target manager
        command: Must include ``op`` in MCP_CONTROL_OPS and ``service_id``

    Returns:
        Redis stream entry id

    Raises:
        ValueError: missing manager_id, op, or service_id
    """
    if not manager_id or not str(manager_id).strip():
        raise ValueError("manager_id is required to enqueue MCP control commands")
    op = str(command.get("op") or "").strip()
    if op not in MCP_CONTROL_OPS:
        raise ValueError(f"Unsupported MCP control op: {op!r}")
    service_id = str(command.get("service_id") or "").strip()
    if not service_id:
        raise ValueError("service_id is required")

    payload = dict(command)
    payload["op"] = op
    payload["service_id"] = service_id
    payload.setdefault("enqueued_at", time.time())
    payload.setdefault("command_id", str(uuid.uuid4()))

    redis_client = get_sync_redis_client(_CLIENT_ID)
    stream = mcp_control_stream_key(manager_id)
    entry_id = redis_client.xadd(
        stream,
        {"payload": json.dumps(payload)},
        maxlen=1000,
        approximate=True,
    )
    logger.info(
        "mcp_control_command_enqueued",
        manager_id=manager_id,
        op=op,
        service_id=service_id,
        command_id=payload["command_id"],
        stream=stream,
        entry_id=str(entry_id),
    )
    return str(entry_id)


def ensure_mcp_control_group(manager_id: str) -> None:
    """Create the consumer group if it does not exist (MKSTREAM)."""
    redis_client = get_sync_redis_client(_CLIENT_ID)
    stream = mcp_control_stream_key(manager_id)
    try:
        redis_client.xgroup_create(stream, _GROUP, id="0", mkstream=True)
    except Exception as e:
        # BUSYGROUP is expected after the first call
        if "BUSYGROUP" not in str(e).upper():
            logger.warning(
                "mcp_control_group_create_failed",
                manager_id=manager_id,
                error=str(e),
            )


def read_mcp_control_commands(
    manager_id: str,
    *,
    count: int = 8,
    block_ms: int = 2000,
) -> List[Tuple[str, Dict[str, Any]]]:
    """
    Blocking XREADGROUP for this manager.

    Returns:
        List of (entry_id, payload_dict). Empty on timeout or error.
    """
    ensure_mcp_control_group(manager_id)
    redis_client = get_sync_redis_client(_CLIENT_ID)
    stream = mcp_control_stream_key(manager_id)
    try:
        results = redis_client.xreadgroup(
            groupname=_GROUP,
            consumername=_CONSUMER,
            streams={stream: ">"},
            count=count,
            block=block_ms,
        )
    except Exception as e:
        logger.warning(
            "mcp_control_read_failed",
            manager_id=manager_id,
            error=str(e),
        )
        return []

    parsed: List[Tuple[str, Dict[str, Any]]] = []
    if not results:
        return parsed
    for _stream_name, entries in results:
        for entry_id, fields in entries:
            eid = entry_id.decode("utf-8") if isinstance(entry_id, bytes) else str(entry_id)
            raw = fields.get(b"payload") or fields.get("payload") or b"{}"
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("mcp_control_payload_invalid", entry_id=eid)
                ack_mcp_control_command(manager_id, eid)
                continue
            if isinstance(payload, dict):
                parsed.append((eid, payload))
    return parsed


def ack_mcp_control_command(manager_id: str, entry_id: str) -> None:
    """XACK a processed control command."""
    try:
        redis_client = get_sync_redis_client(_CLIENT_ID)
        redis_client.xack(mcp_control_stream_key(manager_id), _GROUP, entry_id)
    except Exception as e:
        logger.warning(
            "mcp_control_ack_failed",
            manager_id=manager_id,
            entry_id=entry_id,
            error=str(e),
        )


def resolve_mcp_manager_id() -> Optional[str]:
    """Worker/API helper: MOTET_MCP_MANAGER_ID then Config.mcp_manager_id."""
    import os

    raw = os.getenv("MOTET_MCP_MANAGER_ID", "").strip()
    if raw:
        return raw
    try:
        from motet.core.config import Config

        cfg_id = (Config().mcp_manager_id or "").strip()
        if cfg_id:
            return cfg_id
    except Exception:
        pass
    return None
