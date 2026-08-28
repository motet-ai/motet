"""
Motet - Debug API

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    Debug API for the Motet distributed framework.
    Provides REST API endpoints for debugging and developer tools, including trace management.

Dependencies:
    - fastapi: Web framework for REST API
    - structlog: Structured logging
    - motet.core.distributed: Redis managers and command data management
    - motet.core.observability: Trace store for trace management
    - motet.core.config: Configuration management

Usage:
    from motet.interfaces.api.v1.debug import router
    
    # Include in FastAPI app
    app.include_router(router)

Notes:
    - Provides debugging and developer tools
    - Integrates with distributed architecture
    - Requires MOTET_DEBUG_MODE=true AND an admin principal (issue #214).
      Dedicated-stack-only: leave debug off on hosted or design-partner stacks.
    - Trace endpoints provide both JSON and HTML views
    - Command/task list scans both ``cmd:meta:*`` and ``*:cmd:meta:*``. ``str.replace("cmd:meta:", "")`` is not used
      to parse command ids from prefixed keys.
    - GET /commands and memory endpoints use ``get_manage_app_scope`` so the
      manage-app tenant/motet selector can filter lists.
    - Memory SCAN/clear helpers live in ``interfaces.api.shared.memory_ops``;
      this module re-exports the names existing tests import.
"""


import asyncio
import inspect
import os
import re
import structlog
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple, cast
from pathlib import Path
from fastapi import APIRouter, HTTPException, Request, Header, Query, Depends
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from ....core.distributed.redis_manager import get_redis_manager
from ....core.distributed.redis_command_data_manager import get_redis_command_data_manager
from ....core.distributed.task_flow_loader import get_task_flow_sync
from ....core.distributed.tenant_keys import (
    tenant_key,
    cmd_key_scan_patterns,
    command_id_from_cmd_key,
    decode_redis_id,
)
from ....core.observability import trace_store as tracing
from ....core.config import Config
from ..shared.auth import get_current_principal, require_admin_principal
from ..shared.memory_ops import (
    clear_scoped_memory_stores,
    collect_memories_for_scope,
    memory_index_scan_patterns,
)
from ..shared.scope import ManageAppScope, get_manage_app_scope, matches_scope
from ....core.types import Principal

# Names existing unit tests import from this module.
_memory_index_scan_patterns = memory_index_scan_patterns
_get_all_memories_across_tenants = collect_memories_for_scope

logger = structlog.get_logger(__name__)


async def _maybe_await_redis(value: Any) -> Any:
    """Await Redis client results when stubs/runtime expose an awaitable; pass through otherwise."""
    if inspect.isawaitable(value):
        return await value  # type: ignore[unused-awaitable]
    return value


async def _scan_cmd_keys(
    kind: str,
    limit: Optional[int] = None,
    tenant_id: Optional[str] = None,
) -> List[str]:
    """Scan legacy ``cmd:{kind}:*`` and tenant-prefixed command keys."""
    tid = (tenant_id or "").strip()
    if tid:
        patterns = (f"{tid}:cmd:{kind}:*", f"cmd:{kind}:*")
    else:
        patterns = cmd_key_scan_patterns(kind)
    seen: set[str] = set()
    out: List[str] = []
    for pattern in patterns:
        found = await redis_manager.scan_keys(pattern, client_id="debug", limit=limit)
        for key in found:
            decoded = decode_redis_id(key)
            if decoded in seen:
                continue
            seen.add(decoded)
            out.append(decoded)
            if limit and len(out) >= limit:
                return out
    return out


async def _hgetall_cmd_meta(command_id: str) -> tuple[str, Optional[Dict[Any, Any]]]:
    """Load cmd:meta for *command_id* from the logical key or a tenant-prefixed match."""
    redis_client = redis_manager.get_client("debug")
    logical = f"cmd:meta:{command_id}"
    raw = await _maybe_await_redis(redis_client.hgetall(logical))
    if raw:
        return logical, raw
    matches = await redis_manager.scan_keys(f"*:cmd:meta:{command_id}", client_id="debug", limit=5)
    for key in matches:
        decoded = decode_redis_id(key)
        raw = await _maybe_await_redis(redis_client.hgetall(decoded))
        if raw:
            return decoded, raw
    return logical, None


def _cmd_blob_delete_keys(command_id: str, tenant_id: Optional[str], meta_key: Optional[str] = None) -> List[str]:
    """Logical + tenant-prefixed cmd:meta/data/result keys for one command."""
    keys: List[str] = []
    if meta_key:
        keys.append(meta_key)
    for kind in ("meta", "data", "result"):
        logical = f"cmd:{kind}:{command_id}"
        keys.append(logical)
        tid = (tenant_id or "").strip()
        if tid:
            keys.append(tenant_key(tid, logical))
    # Preserve order, drop duplicates
    seen: set[str] = set()
    unique: List[str] = []
    for key in keys:
        if key not in seen:
            seen.add(key)
            unique.append(key)
    return unique


# Debug mode configuration
DEBUG_MODE = os.getenv("MOTET_DEBUG_MODE", "false").lower() == "true"
DEBUG_TTL_MULTIPLIER = int(os.getenv("MOTET_DEBUG_TTL_MULTIPLIER", "6"))
DEBUG_COMMAND_DATA_TTL = int(os.getenv("MOTET_DEBUG_COMMAND_DATA_TTL", "3600"))  # 1 hour
DEBUG_COMMAND_RESULT_TTL = int(os.getenv("MOTET_DEBUG_COMMAND_RESULT_TTL", "1800"))  # 30 minutes


async def _require_debug_mode_and_auth(principal: Principal = Depends(get_current_principal)):
    """Require debug mode and an admin principal for debug endpoints.

    MOTET_DEBUG_MODE is a dedicated-stack kill switch. Even when it is on,
    debug routes stay admin-only so a hosted or design-partner stack cannot
    expose command blobs to an ordinary authenticated user (issue #214).
    """
    if not DEBUG_MODE:
        raise HTTPException(status_code=403, detail="Debug mode not enabled. Set MOTET_DEBUG_MODE=true to enable debug features.")
    require_admin_principal(
        principal,
        detail="Debug routes require an admin role",
    )
    return principal


router = APIRouter(
    prefix="/api/v1/debug",
    tags=["debug"],
    dependencies=[Depends(_require_debug_mode_and_auth)],
)

# Get Redis managers
redis_manager = get_redis_manager()
command_data_manager = get_redis_command_data_manager()

# Enable debug mode on command data manager
command_data_manager.set_debug_mode(DEBUG_MODE)


@router.get("/commands/{command_id}")
async def get_command_debug_data(command_id: str) -> Dict[str, Any]:
    """Get complete command data for debugging (debug mode only, requires authentication)."""
    
    try:
        # Get command metadata from Redis (cmd:meta stored as Redis hash with encrypted `_envelope`)
        metadata_key, raw = await _hgetall_cmd_meta(command_id)
        metadata = None
        try:
            redis_client = redis_manager.get_client("debug")
            if raw:
                from ....core.security.envelope_decode_helpers import decode_cmd_meta_envelope
                from ....core.security.redis_decode_helpers import normalize_redis_str_mapping

                normalized = normalize_redis_str_mapping(raw)

                envelope_json = normalized.get("_envelope") or ""
                if envelope_json:
                    tenant_id = (normalized.get("tenant_id") or "").strip()
                    motet_id = (normalized.get("motet_id") or "").strip()
                    if tenant_id and motet_id:
                        sensitive = decode_cmd_meta_envelope(
                            envelope_json=str(envelope_json),
                            command_id=command_id,
                            tenant_id=tenant_id,
                            motet_id=motet_id,
                        )
                        if isinstance(sensitive, dict):
                            normalized.update(sensitive)

                normalized.pop("_envelope", None)
                metadata = normalized
        except Exception:
            metadata = None
        
        # Get command data/result from Redis.
        # NOTE: With ADR-0056 AAD binding, encrypted command blobs require tenant_id for decryption.
        # We derive tenant_id from cmd:meta (plaintext index field) when available.
        data_key = f"cmd:data:{command_id}"
        tenant_id_for_decrypt = (metadata or {}).get("tenant_id") if isinstance(metadata, dict) else None
        try:
            command_data = command_data_manager.retrieve_command_data(data_key, tenant_id=tenant_id_for_decrypt)
        except Exception as e:
            logger.debug(
                "Failed to retrieve command data for debug view",
                command_id=command_id,
                tenant_id=tenant_id_for_decrypt,
                error=str(e),
            )
            command_data = None
        
        # Get command result if available
        result_key = f"cmd:result:{command_id}"
        motet_id_for_decrypt = (metadata or {}).get("motet_id") if isinstance(metadata, dict) else None
        try:
            command_result = command_data_manager.retrieve_command_result(
                result_key, 
                tenant_id=tenant_id_for_decrypt,
                motet_id=motet_id_for_decrypt
            )
        except Exception as e:
            logger.debug(
                "Failed to retrieve command result for debug view",
                command_id=command_id,
                tenant_id=tenant_id_for_decrypt,
                error=str(e),
            )
            command_result = None
        
        # Get TTL information (async client; do not use redis_manager.redis_client)
        ttl_remaining = None
        try:
            ttl_client = redis_manager.get_client("debug")
            ttl_remaining = await _maybe_await_redis(ttl_client.ttl(metadata_key))
        except Exception:
            pass  # TTL lookup optional; Redis may be unavailable

        # command_data["metadata"] is often None (payload field), not storage metadata.
        # Use `or {}` so an explicit null does not raise AttributeError and 404 the whole response.
        stored_at = None
        if isinstance(command_data, dict):
            payload_meta = command_data.get("metadata")
            if isinstance(payload_meta, dict):
                stored_at = payload_meta.get("stored_at")
        
        return {
            "command_id": command_id,
            "metadata": metadata,
            "command_data": command_data,
            "result": command_result,
            "debug_info": {
                "ttl_remaining": ttl_remaining,
                "stored_at": stored_at,
                "debug_mode": True
            }
        }
    except Exception as e:
        logger.error("Failed to get command debug data", command_id=command_id, error=str(e))
        raise HTTPException(status_code=404, detail=f"Command data not found or expired: {e}")


@router.get("/commands")
async def list_debug_commands(
    limit: int = Query(50, ge=1, le=500, description="Maximum commands to return after scope filters"),
    scope: ManageAppScope = Depends(get_manage_app_scope),
) -> Dict[str, Any]:
    """List recent commands for debugging (debug mode only, requires authentication)."""
    
    try:
        # Overscan when filtering so a tenant/motet is not starved by other keys.
        scan_limit = max(limit * 20, 500) if scope.is_set else limit
        keys = await _scan_cmd_keys("meta", limit=scan_limit, tenant_id=scope.tenant_id)
        
        commands = []
        redis_client = redis_manager.get_client("debug")
        for key in keys:
            try:
                raw = await _maybe_await_redis(redis_client.hgetall(key))
                if not raw:
                    continue
                
                # Normalize bytes -> str (only use plaintext fields for list view)
                metadata = {}
                for k, v in raw.items():
                    kk = k.decode("utf-8") if isinstance(k, (bytes, bytearray)) else str(k)
                    vv = v.decode("utf-8") if isinstance(v, (bytes, bytearray)) else v
                    metadata[kk] = vv

                if not matches_scope(
                    metadata.get("tenant_id"),
                    metadata.get("motet_id"),
                    scope.tenant_id,
                    scope.motet_id,
                ):
                    continue

                command_id = command_id_from_cmd_key(key)
                
                # Get TTL information
                ttl_remaining = None
                try:
                    ttl_remaining = await _maybe_await_redis(redis_client.ttl(key))
                except Exception:
                    pass  # TTL check is non-critical
                
                commands.append({
                    "command_id": command_id,
                    "command_type": metadata.get("command_type"),
                    "task_id": metadata.get("task_id"),
                    "conversation_id": metadata.get("conversation_id"),
                    "created_at": metadata.get("created_at"),
                    "status": metadata.get("status"),
                    "worker_id": metadata.get("worker_id"),
                    "principal_id": metadata.get("principal_id"),
                    "tenant_id": metadata.get("tenant_id"),
                    "motet_id": metadata.get("motet_id"),
                    "ttl_remaining": ttl_remaining
                })
            except Exception as e:
                logger.warning("Failed to process command metadata", key=key, error=str(e))
                continue
        
        commands = sorted(commands, key=lambda x: x.get("created_at", ""), reverse=True)[:limit]
        return {
            "total_commands": len(commands),
            "commands": commands,
        }
    except Exception as e:
        logger.error("Failed to list debug commands", error=str(e))
        raise HTTPException(status_code=500, detail=f"Failed to list commands: {e}")


@router.delete("/tasks/clear")
async def clear_all_tasks() -> Dict[str, Any]:
    """Clear all task metadata and command data from Redis (debug mode only, requires authentication)."""
    
    try:
        redis_client = redis_manager.get_client("debug")
        
        deleted_count = 0
        
        # Delete all command metadata, data, and results (legacy + tenant-prefixed)
        meta_keys = await _scan_cmd_keys("meta")
        if meta_keys:
            await _maybe_await_redis(redis_client.delete(*meta_keys))
            deleted_count += len(meta_keys)
            
        data_keys = await _scan_cmd_keys("data")
        if data_keys:
            await _maybe_await_redis(redis_client.delete(*data_keys))
            deleted_count += len(data_keys)
            
        result_keys = await _scan_cmd_keys("result")
        if result_keys:
            await _maybe_await_redis(redis_client.delete(*result_keys))
            deleted_count += len(result_keys)
            
        # Also clear binary data
        try:
            from ....core.distributed.redis_manager import get_binary_redis_client
            binary_redis = get_binary_redis_client("debug")
            
            for key in data_keys:
                try:
                    await _maybe_await_redis(binary_redis.delete(key))
                except Exception as e:
                    logger.warning("Failed to delete binary key", key=key, error=str(e))
                    
        except Exception as e:
            logger.warning("Failed to clear binary data", error=str(e))
        
        logger.info("Cleared all tasks and commands", deleted_count=deleted_count)
        
        return {
            "deleted_count": deleted_count,
            "message": f"Successfully cleared {deleted_count} task-related keys from Redis"
        }
        
    except Exception as e:
        logger.error("Failed to clear all tasks", error=str(e))
        raise HTTPException(status_code=500, detail=f"Failed to clear all tasks: {e}")


@router.delete("/tasks/{task_id}")
async def delete_task_and_commands(task_id: str) -> Dict[str, Any]:
    """Delete all commands and data associated with a task (debug mode only, requires authentication)."""
    
    try:
        # Get all commands for this task first (legacy + tenant-prefixed)
        keys = await _scan_cmd_keys("meta")
        
        deleted_commands = []
        redis_client = redis_manager.get_client("debug")
        
        # Find all commands belonging to this task
        for key in keys:
            try:
                raw = await _maybe_await_redis(redis_client.hgetall(key))
                if not raw:
                    continue
                
                metadata = {}
                for k, v in raw.items():
                    kk = k.decode("utf-8") if isinstance(k, (bytes, bytearray)) else str(k)
                    vv = v.decode("utf-8") if isinstance(v, (bytes, bytearray)) else v
                    metadata[kk] = vv
                
                if metadata.get("task_id") == task_id:
                    command_id = command_id_from_cmd_key(key)
                    deleted_commands.append({
                        "command_id": command_id,
                        "command_type": metadata.get("command_type"),
                        "status": metadata.get("status")
                    })
                    
                    keys_to_delete = _cmd_blob_delete_keys(
                        command_id,
                        metadata.get("tenant_id") if isinstance(metadata.get("tenant_id"), str) else None,
                        meta_key=key,
                    )
                    
                    for delete_key in keys_to_delete:
                        try:
                            await _maybe_await_redis(redis_client.delete(delete_key))
                        except Exception as e:
                            logger.warning("Failed to delete key", key=delete_key, error=str(e))
                    
                    # Also try to delete from binary Redis client for msgpack data
                    try:
                        from ....core.distributed.redis_manager import get_binary_redis_client
                        binary_redis = get_binary_redis_client("debug")
                        for delete_key in keys_to_delete:
                            if "cmd:data:" in delete_key:
                                await _maybe_await_redis(binary_redis.delete(delete_key))
                    except Exception as e:
                        logger.warning("Failed to delete binary data", command_id=command_id, error=str(e))
                        
            except Exception as e:
                logger.warning("Failed to process command for deletion", key=key, error=str(e))
                continue
        
        logger.info("Deleted task and associated commands", 
                   task_id=task_id, 
                   deleted_count=len(deleted_commands))
        
        return {
            "task_id": task_id,
            "deleted_commands": deleted_commands,
            "total_deleted": len(deleted_commands),
            "message": f"Successfully deleted task {task_id} and {len(deleted_commands)} associated commands"
        }
        
    except Exception as e:
        logger.error("Failed to delete task", task_id=task_id, error=str(e))
        raise HTTPException(status_code=500, detail=f"Failed to delete task: {e}")


@router.get("/task-flow/{task_id}")
async def get_task_command_flow(task_id: str) -> Dict[str, Any]:
    """Get complete command execution flow for a single task/orchestration turn (requires authentication)."""
    
    logger.info("Starting task flow request", task_id=task_id)
    
    try:
        # Load task flow (metadata + inputs + results) via shared sync loader
        logger.info("Loading task flow", task_id=task_id)
        task_commands = await asyncio.to_thread(get_task_flow_sync, task_id, None)
        logger.info("Task flow loaded", task_id=task_id, command_count=len(task_commands))

        # Don't raise 404 if no commands found - return partial data instead
        # This handles cases where tasks are still running or command metadata isn't stored
        if not task_commands:
            logger.info("No commands found for task, returning partial data", task_id=task_id)
            # Still try to get events even if no commands
            try:
                events = await _get_task_events(task_id)
                logger.info("Retrieved events for task with no commands", task_id=task_id, event_count=len(events))
            except Exception as e:
                logger.error("Failed to get events for task with no commands", task_id=task_id, error=str(e))
                events = []
            
            return {
                "task_id": task_id,
                "total_commands": 0,
                "execution_flow": {"nodes": [], "edges": []},
                "commands": [],
                "timeline": [],
                "summary": {"status": "no_commands_found", "message": "Task may still be running or command metadata not available"},
                "events": events
            }
        
        # Build execution flow graph
        logger.info("Building execution flow graph", task_id=task_id, command_count=len(task_commands))
        try:
            flow_graph = _build_command_flow_graph(task_commands)
            logger.info("Built execution flow graph", task_id=task_id, node_count=len(flow_graph.get("nodes", [])), edge_count=len(flow_graph.get("edges", [])))
        except Exception as e:
            logger.error("Failed to build execution flow graph", task_id=task_id, error=str(e))
            raise
        
        # Build execution timeline
        logger.info("Building execution timeline", task_id=task_id)
        try:
            timeline = _build_execution_timeline(task_commands)
            logger.info("Built execution timeline", task_id=task_id, timeline_length=len(timeline))
        except Exception as e:
            logger.error("Failed to build execution timeline", task_id=task_id, error=str(e))
            raise
        
        # Retrieve events for this task
        logger.info("Retrieving task events", task_id=task_id)
        try:
            events = await _get_task_events(task_id)
            logger.info("Retrieved task events", task_id=task_id, event_count=len(events))
        except Exception as e:
            logger.error("Failed to retrieve task events", task_id=task_id, error=str(e))
            raise
        
        result = {
            "task_id": task_id,
            "total_commands": len(task_commands),
            "execution_flow": flow_graph,
            "commands": sorted(task_commands, key=lambda x: x.get("created_at", "")),
            "timeline": timeline,
            "summary": _build_task_summary(task_commands),
            "events": events
        }
        
        logger.info("Successfully built task flow response", 
                   task_id=task_id, 
                   total_commands=len(task_commands),
                   event_count=len(events),
                   response_size_kb=len(str(result)) // 1024)
        
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to get task command flow", task_id=task_id, error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to get task flow: {e}")


async def _get_task_events(task_id: str) -> List[Dict[str, Any]]:
    """Retrieve all events for a specific task from Redis."""
    try:
        event_key = f"task:events:{task_id}"
        redis_client = redis_manager.get_client("debug")
        
        logger.debug("Getting task events", task_id=task_id, event_key=event_key)
        
        # Get all events for this task
        event_count = await _maybe_await_redis(redis_client.llen(event_key))
        logger.debug("Found event count", task_id=task_id, event_count=event_count)
        
        if event_count == 0:
            logger.debug("No events found for task", task_id=task_id)
            return []
        
        # Retrieve all events (0 to -1 means all items in the list)
        import json
        raw_events = await _maybe_await_redis(redis_client.lrange(event_key, 0, -1))
        logger.debug("Retrieved raw events", task_id=task_id, raw_event_count=len(raw_events))
        
        events = []
        parse_errors = 0
        for raw_event in raw_events:
            try:
                event = json.loads(raw_event)
                events.append(event)
            except Exception as e:
                parse_errors += 1
                logger.warning("Failed to parse event", error=str(e), task_id=task_id)
                continue
        
        logger.debug("Parsed events", task_id=task_id, parsed_count=len(events), parse_errors=parse_errors)
        
        # Sort by timestamp
        events.sort(key=lambda x: x.get("timestamp", ""))
        
        return events
    except Exception as e:
        logger.error("Failed to retrieve events for task", task_id=task_id, error=str(e), exc_info=True)
        return []


@router.get("/task-events/{task_id}")
async def get_task_events(task_id: str, limit: Optional[int] = None) -> Dict[str, Any]:
    """Get all events triggered during a specific task execution (debug mode only, requires authentication)."""
    
    try:
        events = await _get_task_events(task_id)
        
        # Apply limit if specified
        if limit and limit > 0:
            events = events[:limit]
        
        # Group events by type for summary
        event_types = {}
        for event in events:
            event_type = event.get("kind", "unknown")
            event_types[event_type] = event_types.get(event_type, 0) + 1
        
        return {
            "task_id": task_id,
            "total_events": len(events),
            "event_types": event_types,
            "events": events
        }
    except Exception as e:
        logger.error("Failed to get task events", task_id=task_id, error=str(e))
        raise HTTPException(status_code=500, detail=f"Failed to get task events: {e}")


def _build_command_flow_graph(commands: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Build a graph showing command dependencies and triggers."""
    graph = {
        "nodes": [],
        "edges": [],
        "execution_levels": []
    }
    
    # Create nodes for each command with null-safe access
    for cmd in commands:
        graph["nodes"].append({
            "id": cmd.get("command_id", "unknown"),
            "type": cmd.get("command_type", "unknown"),
            "status": cmd.get("status", "unknown"),
            "worker_id": cmd.get("worker_id", "unknown"),
            "duration_ms": cmd.get("duration_ms", 0),
            "created_at": cmd.get("created_at", "")
        })
        
        # Create edges for command triggers (use 'source'/'target' for frontend compatibility)
        if cmd.get("parent_command_id"):
            graph["edges"].append({
                "source": cmd["parent_command_id"],  # Parent command
                "target": cmd["command_id"],  # Child command
                "type": "triggers",  # Active voice: parent triggers child
                "label": f"triggers {cmd['command_type']}"
            })
    
    # Group commands by execution level (parallel execution)
    execution_levels = _group_by_execution_level(commands)
    graph["execution_levels"] = execution_levels
    
    return graph


def _build_execution_timeline(commands: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Build a chronological timeline of command execution."""
    timeline = []
    
    for cmd in commands:
        timeline.append({
            "timestamp": cmd["created_at"],
            "command_id": cmd["command_id"],
            "command_type": cmd["command_type"],
            "event": "started",
            "worker_id": cmd["worker_id"]
        })
        
        if cmd.get("completed_at"):
            timeline.append({
                "timestamp": cmd["completed_at"],
                "command_id": cmd["command_id"],
                "command_type": cmd["command_type"],
                "event": "completed",
                "status": cmd["status"],
                "duration_ms": cmd["duration_ms"]
            })
    
    return sorted(timeline, key=lambda x: x["timestamp"])


def _command_succeeded(cmd: Dict[str, Any]) -> bool:
    """Return True only if a command both completed and didn't semantically error.

    A command can reach lifecycle ``status == "completed"`` while its ADR-0029
    result payload reports ``results.status == "error"`` (e.g. a Redis timeout in
    a fire-and-forget sub-command). Counting only the lifecycle status overstates
    success, so we also honor the semantic result status when present.
    """
    if cmd.get("status") != "completed":
        return False
    results = cmd.get("results")
    if isinstance(results, dict):
        semantic = results.get("status")
        if isinstance(semantic, str) and semantic.lower() == "error":
            return False
    return True


def _build_task_summary(commands: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Build a summary of the task execution."""
    if not commands:
        return {}
    
    # Calculate performance metrics
    durations = [cmd.get("duration_ms", 0) for cmd in commands if cmd.get("duration_ms")]
    total_duration = sum(durations)
    avg_duration = sum(durations) / len(durations) if durations else 0
    
    # Find slowest and fastest commands (only those with valid durations)
    commands_with_duration = [cmd for cmd in commands if cmd.get("duration_ms") is not None]
    slowest_cmd = max(commands_with_duration, key=lambda x: x.get("duration_ms", 0)) if commands_with_duration else None
    fastest_cmd = min(commands_with_duration, key=lambda x: x.get("duration_ms", float('inf'))) if commands_with_duration else None
    
    # Count by status
    status_counts = {}
    for cmd in commands:
        status = cmd.get("status", "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
    
    # Count by worker
    worker_counts = {}
    for cmd in commands:
        worker_id = cmd.get("worker_id", "unknown")
        worker_counts[worker_id] = worker_counts.get(worker_id, 0) + 1
    
    # Count by command type
    type_counts = {}
    for cmd in commands:
        cmd_type = cmd.get("command_type", "unknown")
        type_counts[cmd_type] = type_counts.get(cmd_type, 0) + 1

    # Semantic success: completed lifecycle AND no ADR-0029 result error.
    succeeded = sum(1 for cmd in commands if _command_succeeded(cmd))
    semantic_error_count = sum(
        1
        for cmd in commands
        if cmd.get("status") == "completed"
        and isinstance(cmd.get("results"), dict)
        and str(cmd["results"].get("status", "")).lower() == "error"
    )

    return {
        "total_commands": len(commands),
        "total_duration_ms": total_duration,
        "average_duration_ms": avg_duration,
        "slowest_command": {
            "command_id": slowest_cmd["command_id"],
            "command_type": slowest_cmd["command_type"],
            "duration_ms": slowest_cmd.get("duration_ms", 0)
        } if slowest_cmd else None,
        "fastest_command": {
            "command_id": fastest_cmd["command_id"],
            "command_type": fastest_cmd["command_type"],
            "duration_ms": fastest_cmd.get("duration_ms", 0)
        } if fastest_cmd else None,
        "status_breakdown": status_counts,
        "worker_breakdown": worker_counts,
        "command_type_breakdown": type_counts,
        "semantic_error_count": semantic_error_count,
        "success_rate": succeeded / len(commands) if commands else 0
    }


def _group_by_execution_level(commands: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """Group commands by execution level (parallel execution)."""
    # Simple implementation - group by creation time windows
    # More sophisticated grouping could be implemented based on actual dependencies
    
    if not commands:
        return []
    
    # Sort commands by creation time
    sorted_commands = sorted(commands, key=lambda x: x.get("created_at", ""))
    
    levels = []
    current_level = []
    current_time_window = None
    
    for cmd in sorted_commands:
        created_at = cmd.get("created_at", "")
        if not created_at:
            continue
            
        try:
            cmd_time = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
            
            # If this is the first command or within 1 second of current window, add to current level
            if current_time_window is None or (cmd_time - current_time_window).total_seconds() < 1.0:
                current_level.append(cmd)
                if current_time_window is None:
                    current_time_window = cmd_time
            else:
                # Start new level
                if current_level:
                    levels.append(current_level)
                current_level = [cmd]
                current_time_window = cmd_time
        except Exception:
            # If timestamp parsing fails, add to current level
            current_level.append(cmd)
    
    # Add the last level
    if current_level:
        levels.append(current_level)
    
    return levels


@router.get("/command-flow/analysis/{task_id}")
async def analyze_command_flow(task_id: str) -> Dict[str, Any]:
    """Analyze command execution patterns and performance (requires authentication)."""
    
    try:
        # Get the task flow data
        flow_data = await get_task_command_flow(task_id)
        
        analysis = {
            "task_id": task_id,
            "performance_metrics": _analyze_performance(flow_data["commands"]),
            "execution_patterns": _analyze_execution_patterns(flow_data["execution_flow"]),
            "bottlenecks": _identify_bottlenecks(flow_data["commands"]),
            "worker_utilization": _analyze_worker_utilization(flow_data["commands"]),
            "command_dependencies": _analyze_dependencies(flow_data["execution_flow"])
        }
        
        return analysis
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to analyze command flow", task_id=task_id, error=str(e))
        raise HTTPException(status_code=500, detail=f"Failed to analyze command flow: {e}")


def _analyze_performance(commands: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Analyze performance metrics for the command flow."""
    durations = [cmd.get("duration_ms", 0) for cmd in commands if cmd.get("duration_ms")]
    
    if not durations:
        return {"error": "No duration data available"}
    
    return {
        "total_execution_time_ms": sum(durations),
        "average_command_duration_ms": sum(durations) / len(durations),
        "slowest_command": max(commands, key=lambda x: x.get("duration_ms", 0)),
        "fastest_command": min(commands, key=lambda x: x.get("duration_ms", float('inf'))),
        "parallel_execution_ratio": _calculate_parallel_ratio(commands)
    }


def _analyze_execution_patterns(flow_graph: Dict[str, Any]) -> Dict[str, Any]:
    """Analyze execution patterns and dependencies."""
    return {
        "total_commands": len(flow_graph["nodes"]),
        "sequential_chains": _find_sequential_chains(flow_graph),
        "parallel_branches": _find_parallel_branches(flow_graph),
        "dependency_depth": _calculate_dependency_depth(flow_graph),
        "execution_levels": len(flow_graph["execution_levels"])
    }


def _identify_bottlenecks(commands: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Identify performance bottlenecks in the command flow."""
    bottlenecks = []
    
    # Find commands that took significantly longer than average
    durations = [cmd.get("duration_ms", 0) for cmd in commands if cmd.get("duration_ms")]
    if durations:
        avg_duration = sum(durations) / len(durations)
        threshold = avg_duration * 2  # 2x average is considered a bottleneck
        
        for cmd in commands:
            duration = cmd.get("duration_ms", 0)
            if duration > threshold:
                bottlenecks.append({
                    "command_id": cmd["command_id"],
                    "command_type": cmd["command_type"],
                    "duration_ms": duration,
                    "severity": "high" if duration > avg_duration * 3 else "medium",
                    "reason": f"Duration {duration}ms is {duration/avg_duration:.1f}x average"
                })
    
    return sorted(bottlenecks, key=lambda x: x["duration_ms"], reverse=True)


def _analyze_worker_utilization(commands: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Analyze worker utilization patterns."""
    worker_stats = {}
    
    for cmd in commands:
        worker_id = cmd.get("worker_id", "unknown")
        duration = cmd.get("duration_ms", 0)
        
        if worker_id not in worker_stats:
            worker_stats[worker_id] = {
                "worker_id": worker_id,
                "total_commands": 0,
                "total_duration_ms": 0,
                "command_types": set()
            }
        
        worker_stats[worker_id]["total_commands"] += 1
        worker_stats[worker_id]["total_duration_ms"] += duration
        worker_stats[worker_id]["command_types"].add(cmd.get("command_type", "unknown"))
    
    # Convert sets to lists for JSON serialization
    for worker_id, stats in worker_stats.items():
        stats["command_types"] = list(stats["command_types"])
        stats["average_duration_ms"] = stats["total_duration_ms"] / stats["total_commands"] if stats["total_commands"] > 0 else 0
    
    return {
        "worker_count": len(worker_stats),
        "workers": list(worker_stats.values()),
        "utilization_balance": _calculate_utilization_balance(worker_stats)
    }


def _analyze_dependencies(flow_graph: Dict[str, Any]) -> Dict[str, Any]:
    """Analyze command dependencies and relationships."""
    edges = flow_graph.get("edges", [])
    nodes = flow_graph.get("nodes", [])
    
    # Count dependency types
    dependency_types = {}
    for edge in edges:
        dep_type = edge.get("type", "unknown")
        dependency_types[dep_type] = dependency_types.get(dep_type, 0) + 1
    
    # Find root commands (no incoming edges)
    root_commands = []
    for node in nodes:
        node_id = node["id"]
        has_incoming = any(edge["to"] == node_id for edge in edges)
        if not has_incoming:
            root_commands.append(node)
    
    # Find leaf commands (no outgoing edges)
    leaf_commands = []
    for node in nodes:
        node_id = node["id"]
        has_outgoing = any(edge["from"] == node_id for edge in edges)
        if not has_outgoing:
            leaf_commands.append(node)
    
    return {
        "total_dependencies": len(edges),
        "dependency_types": dependency_types,
        "root_commands": root_commands,
        "leaf_commands": leaf_commands,
        "max_dependency_depth": _calculate_dependency_depth(flow_graph)
    }


def _calculate_parallel_ratio(commands: List[Dict[str, Any]]) -> float:
    """Calculate the ratio of parallel vs sequential execution."""
    # Simple implementation - could be more sophisticated
    if len(commands) <= 1:
        return 1.0
    
    # Group commands by time windows to identify parallel execution
    time_windows = {}
    for cmd in commands:
        created_at = cmd.get("created_at", "")
        if created_at:
            try:
                # Round to nearest second for grouping
                cmd_time = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                time_key = cmd_time.replace(microsecond=0)
                if time_key not in time_windows:
                    time_windows[time_key] = []
                time_windows[time_key].append(cmd)
            except Exception:
                pass  # skip commands with unparseable timestamps
    
    # Calculate parallel ratio
    total_commands = len(commands)
    parallel_commands = sum(len(window_commands) for window_commands in time_windows.values() if len(window_commands) > 1)
    
    return parallel_commands / total_commands if total_commands > 0 else 0.0


def _find_sequential_chains(flow_graph: Dict[str, Any]) -> List[List[str]]:
    """Find sequential command chains in the flow."""
    # Simple implementation - find linear sequences
    chains = []
    edges = flow_graph.get("edges", [])
    nodes = flow_graph.get("nodes", [])
    
    # Build adjacency list
    outgoing = {}
    for edge in edges:
        from_id = edge["from"]
        to_id = edge["to"]
        if from_id not in outgoing:
            outgoing[from_id] = []
        outgoing[from_id].append(to_id)
    
    # Find chains starting from nodes with no incoming edges
    for node in nodes:
        node_id = node["id"]
        has_incoming = any(edge["to"] == node_id for edge in edges)
        
        if not has_incoming:
            # Start a chain from this node
            chain = [node_id]
            current = node_id
            
            while current in outgoing and len(outgoing[current]) == 1:
                current = outgoing[current][0]
                chain.append(current)
            
            if len(chain) > 1:
                chains.append(chain)
    
    return chains


def _find_parallel_branches(flow_graph: Dict[str, Any]) -> List[List[str]]:
    """Find parallel command branches in the flow."""
    branches = []
    edges = flow_graph.get("edges", [])
    nodes = flow_graph.get("nodes", [])
    
    # Build adjacency list
    outgoing = {}
    for edge in edges:
        from_id = edge["from"]
        to_id = edge["to"]
        if from_id not in outgoing:
            outgoing[from_id] = []
        outgoing[from_id].append(to_id)
    
    # Find nodes with multiple outgoing edges (branching points)
    for node in nodes:
        node_id = node["id"]
        if node_id in outgoing and len(outgoing[node_id]) > 1:
            branches.append(outgoing[node_id])
    
    return branches


def _calculate_dependency_depth(flow_graph: Dict[str, Any]) -> int:
    """Calculate the maximum dependency depth in the flow."""
    edges = flow_graph.get("edges", [])
    nodes = flow_graph.get("nodes", [])
    
    if not nodes:
        return 0
    
    # Build adjacency list
    outgoing = {}
    for edge in edges:
        from_id = edge["from"]
        to_id = edge["to"]
        if from_id not in outgoing:
            outgoing[from_id] = []
        outgoing[from_id].append(to_id)
    
    # Calculate depth for each node using DFS
    depths = {}
    
    def calculate_depth(node_id: str) -> int:
        if node_id in depths:
            return depths[node_id]
        
        if node_id not in outgoing or not outgoing[node_id]:
            depths[node_id] = 1
            return 1
        
        max_child_depth = max(calculate_depth(child_id) for child_id in outgoing[node_id])
        depths[node_id] = max_child_depth + 1
        return depths[node_id]
    
    # Calculate depth for all nodes
    for node in nodes:
        calculate_depth(node["id"])
    
    return max(depths.values()) if depths else 1


def _calculate_utilization_balance(worker_stats: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Calculate how balanced worker utilization is."""
    if not worker_stats:
        return {"balance_score": 1.0, "status": "perfect"}
    
    durations = [stats["total_duration_ms"] for stats in worker_stats.values()]
    if not durations:
        return {"balance_score": 1.0, "status": "perfect"}
    
    avg_duration = sum(durations) / len(durations)
    max_duration = max(durations)
    min_duration = min(durations)
    
    # Calculate balance score (0-1, where 1 is perfectly balanced)
    if max_duration == 0:
        balance_score = 1.0
    else:
        balance_score = min_duration / max_duration
    
    # Determine status
    if balance_score > 0.8:
        status = "excellent"
    elif balance_score > 0.6:
        status = "good"
    elif balance_score > 0.4:
        status = "fair"
    else:
        status = "poor"
    
    return {
        "balance_score": balance_score,
        "status": status,
        "max_worker_duration_ms": max_duration,
        "min_worker_duration_ms": min_duration,
        "average_worker_duration_ms": avg_duration
    }


# ==================== Memory Management Endpoints ====================
# SCAN / collect / clear implementations: ``interfaces.api.shared.memory_ops``.


@router.get("/routing/stats")
async def get_routing_stats(days: int = Query(default=7, ge=1, le=30)) -> Dict[str, Any]:
    """
    Get conversation-analysis routing decision counters (debug mode only, requires authentication).

    Aggregates the daily ``routing:analysis_decisions:{YYYY-MM-DD}`` hashes
    written by ``conversation_analysis`` (one increment per turn, field
    ``{mode}:{reason}``). Supports the routing eval loop: skip-rate and
    fallback-rate trends, and spotting reasons that dominate unexpectedly.
    """
    from ....core.security.redis_decode_helpers import normalize_redis_str_mapping

    try:
        redis_client = redis_manager.get_client("debug")
        today = datetime.now(timezone.utc).date()

        daily: Dict[str, Dict[str, int]] = {}
        totals_by_field: Dict[str, int] = {}
        totals_by_mode: Dict[str, int] = {}
        total = 0

        for offset in range(days):
            date_key = (today - timedelta(days=offset)).strftime("%Y-%m-%d")
            raw = await _maybe_await_redis(
                redis_client.hgetall(f"routing:analysis_decisions:{date_key}")
            )
            if not raw:
                continue
            counts = {
                field: int(value)
                for field, value in normalize_redis_str_mapping(raw).items()
            }
            daily[date_key] = counts
            for field, count in counts.items():
                if field == "total":
                    total += count
                    continue
                totals_by_field[field] = totals_by_field.get(field, 0) + count
                mode = field.split(":", 1)[0]
                totals_by_mode[mode] = totals_by_mode.get(mode, 0) + count

        return {
            "days": days,
            "total_decisions": total,
            "by_mode": totals_by_mode,
            "by_mode_reason": totals_by_field,
            "daily": daily,
        }
    except Exception as e:
        logger.error(
            "routing_stats_failed",
            operation="get_routing_stats",
            days=days,
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail=f"Failed to load routing stats: {e}")


@router.get("/memory/stats")
async def get_memory_stats(
    request: Request,
    scope: ManageAppScope = Depends(get_manage_app_scope),
) -> Dict[str, Any]:
    """Get memory statistics for collective memory tab."""
    try:
        # Get MotetStack from request state
        stack = request.app.state.stack
        
        if not stack.memory:
            return {
                "total_memories": 0,
                "recent_memories": 0,
                "memory_types": 0,
                "error": "Memory system not enabled"
            }
        
        all_memories = _get_all_memories_across_tenants(stack, scope.tenant_id, scope.motet_id)
        
        # Get recent memories (last 10 items from aggregated list)
        # Sort by created_at and take most recent
        recent_memories = sorted(
            all_memories,
            key=lambda m: getattr(m, 'created_at', None) or 0,
            reverse=True
        )[:10]
        
        # Count distinct memory types
        memory_types = set()
        for memory in all_memories:
            if hasattr(memory, 'type') and memory.type:
                memory_types.add(memory.type)
        
        # Collect scoping statistics (ADR-0027 Phase 2)
        scope_breakdown = {}
        motet_breakdown = {}
        tenant_breakdown = {}
        tagged_count = 0
        
        for memory in all_memories:
            # Scope distribution
            scope_type = getattr(memory, 'scope_type', None) or 'GLOBAL'
            scope_breakdown[scope_type] = scope_breakdown.get(scope_type, 0) + 1
            
            # Motet distribution
            motet_id = getattr(memory, 'motet_id', None) or 'default'
            motet_breakdown[motet_id] = motet_breakdown.get(motet_id, 0) + 1
            
            # Tenant distribution
            tenant_id = getattr(memory, 'tenant_id', None) or 'default'
            tenant_breakdown[tenant_id] = tenant_breakdown.get(tenant_id, 0) + 1
            
            # Tagged memories
            if hasattr(memory, 'tags') and memory.tags:
                tagged_count += 1
        
        return {
            "total_memories": len(all_memories),
            "recent_memories": len(recent_memories),
            "memory_types": len(memory_types),
            "type_breakdown": {t: sum(1 for m in all_memories if hasattr(m, 'type') and m.type == t) for t in memory_types},
            "scope_breakdown": scope_breakdown,
            "motet_breakdown": motet_breakdown,
            "tenant_breakdown": tenant_breakdown,
            "tagged_count": tagged_count
        }
    except Exception as e:
        logger.error("Failed to get memory stats", error=str(e), exc_info=True)
        return {
            "total_memories": 0,
            "recent_memories": 0,
            "memory_types": 0,
            "error": str(e)
        }


@router.delete("/memory/clear")
async def clear_all_memories(
    request: Request,
    scope: ManageAppScope = Depends(get_manage_app_scope),
) -> Dict[str, Any]:
    """Clear memories from the memory store. Honors manage-app tenant/motet scope."""
    try:
        stack = request.app.state.stack

        if not stack.memory:
            raise HTTPException(status_code=400, detail="Memory system not enabled")

        total_cleared = clear_scoped_memory_stores(
            stack,
            scope.tenant_id,
            scope.motet_id,
            clear_unscoped_default=not scope.is_set,
        )

        logger.info(
            "Cleared memories",
            cleared_count=total_cleared,
            tenant_id=scope.tenant_id,
            motet_id=scope.motet_id,
        )

        return {
            "cleared_count": total_cleared,
            "message": f"Successfully cleared {total_cleared} memories",
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to clear memories", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to clear memories: {e}")


@router.get("/memory/search")
async def search_memories(
    request: Request,
    q: str,
    limit: int = 50,
    scope: ManageAppScope = Depends(get_manage_app_scope),
) -> Dict[str, Any]:
    """Search memories by content or tags. Honors manage-app tenant/motet scope."""
    try:
        # Get MotetStack from request state
        stack = request.app.state.stack
        
        if not stack.memory:
            raise HTTPException(status_code=400, detail="Memory system not enabled")
        
        all_memories = _get_all_memories_across_tenants(stack, scope.tenant_id, scope.motet_id)
        
        # Filter by query (simple text search in content)
        query_lower = q.lower()
        matching_memories = []
        
        for memory in all_memories:
            # Search in content
            if hasattr(memory, 'content') and query_lower in memory.content.lower():
                matching_memories.append(memory)
                continue
            
            # Search in tags
            if hasattr(memory, 'tags') and memory.tags:
                if any(query_lower in tag.lower() for tag in memory.tags):
                    matching_memories.append(memory)
                    continue
        
        # Limit results
        matching_memories = matching_memories[:limit]
        
        # Serialize memories for JSON response
        serialized_memories = []
        for memory in matching_memories:
            serialized_memories.append(memory.model_dump(mode='json'))
        
        return {
            "query": q,
            "total_matches": len(matching_memories),
            "memories": serialized_memories
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to search memories", query=q, error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to search memories: {e}")


# Trace endpoints
# Templates for HTML trace views
_templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent.parent / "templates"))


@router.get(
    "/traces/{trace_id}.json",
    summary="Get trace as JSON",
    description="Get a specific trace by ID in JSON format",
    response_description="Trace events as JSON array"
)
async def get_trace_json(
    trace_id: str,
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    principal: Principal = Depends(get_current_principal)
) -> JSONResponse:
    """
    Get a specific trace by ID in JSON format.
    
    Returns all events for the specified trace ID as a JSON array.
    Each event includes timestamp, kind, and event data.
    
    Args:
        trace_id: The trace identifier
        x_api_key: API key for authentication (optional, JWT/service account can be used instead)
        principal: Authenticated principal (from JWT, service account, or headers)
        
    Returns:
        JSONResponse with trace events array
        
    Raises:
        HTTPException: 401 if authentication fails, 500 if trace loading fails
    """
    try:
        from ....core.security import RateLimiter
        from ....core.config import Config
        
        cfg = Config()
        rate_limiter = RateLimiter(
            backend=cfg.rate_limit_backend,
            redis_url=cfg.redis_url if cfg.rate_limit_backend == "redis" else None,
            limit_per_minute=cfg.rate_limit_per_minute,
        )
        rate_limiter.rate_limit(principal.id if principal else (x_api_key or "public"))
        
        events = tracing.load_trace(trace_id)
        return JSONResponse(events)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to load trace", trace_id=trace_id, error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to load trace: {str(e)}")


@router.get(
    "/traces/{trace_id}",
    summary="Get trace HTML view",
    description="Get a specific trace by ID as HTML page",
    response_class=HTMLResponse
)
async def get_trace_html(
    trace_id: str,
    request: Request,
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    principal: Principal = Depends(get_current_principal)
) -> HTMLResponse:
    """
    Get a specific trace by ID as HTML page.
    
    Returns an HTML page for viewing trace details interactively.
    
    Args:
        trace_id: The trace identifier
        request: FastAPI request object for template rendering
        x_api_key: API key for authentication (optional, JWT/service account can be used instead)
        principal: Authenticated principal (from JWT, service account, or headers)
        
    Returns:
        HTMLResponse with trace detail page
        
    Raises:
        HTTPException: 401 if authentication fails
    """
    return _templates.TemplateResponse(request, "trace_detail.html", {"trace_id": trace_id})


@router.get(
    "/traces.json",
    summary="List traces as JSON",
    description="List recent traces with optional filtering",
    response_description="List of trace metadata"
)
async def list_traces_json(
    request: Request,
    limit: int = Query(20, description="Maximum number of traces to return", ge=1, le=100),
    q: Optional[str] = Query(None, description="Search query to filter traces by trace_id"),
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    principal: Principal = Depends(get_current_principal)
) -> JSONResponse:
    """
    List recent traces with optional filtering.
    
    Returns a list of trace metadata including trace_id, creation time, and size.
    Supports filtering by trace_id search query and tenant isolation.
    
    Args:
        limit: Maximum number of traces to return (1-100)
        q: Optional search query to filter traces by trace_id
        request: FastAPI request object for tenant extraction
        x_api_key: API key for authentication (optional, JWT/service account can be used instead)
        principal: Authenticated principal (from JWT, service account, or headers)
        
    Returns:
        JSONResponse with list of trace metadata
        
    Raises:
        HTTPException: 401 if authentication fails, 500 if trace listing fails
    """
    try:
        from ....core.security import RateLimiter
        from ....core.config import Config
        
        cfg = Config()
        rate_limiter = RateLimiter(
            backend=cfg.rate_limit_backend,
            redis_url=cfg.redis_url if cfg.rate_limit_backend == "redis" else None,
            limit_per_minute=cfg.rate_limit_per_minute,
        )
        rate_limiter.rate_limit(principal.id if principal else (x_api_key or "public"))
        
        items = tracing.list_traces(limit=limit)
        
        # Filter by search query if provided
        if q:
            ql = q.lower()
            items = [it for it in items if ql in str(it.get("trace_id", "")).lower()]
        
        # Apply tenant filtering if enabled
        try:
            if getattr(cfg, "tenant_enforce_trace_filter", False) and request:
                tenant_id = getattr(request.state, "tenant_id", None) or (principal.tenant_id if principal else None)
                if tenant_id:
                    # Load and check tenant metadata in trace start events
                    filtered = []
                    for it in items:
                        tid = it.get("trace_id")
                        if not tid:
                            continue
                        evs = tracing.load_trace(str(tid))
                        allowed = False
                        for ev in evs:
                            if ev.get("kind") == "start":
                                meta = ev.get("metadata", {}) or {}
                                if meta.get("tenant_id") == tenant_id:
                                    allowed = True
                                    break
                        if allowed:
                            filtered.append(it)
                    items = filtered
        except Exception as e:
            logger.debug("Tenant filtering failed", error=str(e))
        
        return JSONResponse(items)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to list traces", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to list traces: {str(e)}")


@router.get(
    "/traces",
    summary="List traces HTML view",
    description="List traces as HTML page",
    response_class=HTMLResponse
)
async def list_traces_html(
    request: Request,
    principal: Principal = Depends(get_current_principal)
) -> HTMLResponse:
    """
    List traces as HTML page.
    
    Returns an HTML page for viewing and browsing traces interactively.
    
    Args:
        request: FastAPI request object for template rendering
        principal: Authenticated principal (from JWT, service account, or headers)
        
    Returns:
        HTMLResponse with traces list page
        
    Raises:
        HTTPException: 401 if authentication fails
    """
    return _templates.TemplateResponse(request, "traces.html")
