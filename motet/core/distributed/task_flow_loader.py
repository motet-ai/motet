"""
Motet - Task Flow Loader (sync)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-16

Description:
    Synchronous loader for task execution flow: command metadata, inputs, and results.
    Used by the debug API (via asyncio.to_thread) and by the motet_admin.get_task_flow tool.
    Provides the same data as GET /api/v1/debug/task-flow/{task_id} for agent consumption.

Dependencies:
    - motet.core.distributed.redis_manager: sync Redis client
    - motet.core.distributed.redis_command_data_manager: command data/result retrieval
    - motet.core.security.envelope_decode_helpers: decode_cmd_meta_envelope for metadata

Usage:
    from motet.core.distributed.task_flow_loader import get_task_flow_sync, extract_command_inputs_for_display

    commands = get_task_flow_sync(task_id="...", tenant_id="my-tenant")
    # commands: list of dicts with command_id, command_type, status, inputs, results, ...
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from motet.core.commands.distributed_types import (
    AGENTIC_LOOP_ITERATION_META_KEY,
    parse_agentic_loop_iteration,
)

from .redis_manager import get_sync_redis_client
from .redis_command_data_manager import get_redis_command_data_manager
from .tenant_keys import command_id_from_cmd_key, iter_cmd_keys_sync


def _decode(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    return value


def _decode_hash(raw: Dict[Any, Any]) -> Dict[str, Any]:
    return {str(_decode(k)): _decode(v) for k, v in (raw or {}).items()}


def extract_command_inputs_for_display(command_type: str, raw_inputs: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extract and format command inputs for display using generic auto-discovery.

    Args:
        command_type: The type of command (e.g., 'core.model_inference', 'core.tool_execution')
        raw_inputs: Raw command input data from Redis

    Returns:
        Formatted input data suitable for display
    """
    if not isinstance(raw_inputs, dict):
        return {}

    MAX_LIST_ITEMS = 100
    MAX_DICT_ITEMS = 100
    MAX_STRING_LENGTH = 10000
    MAX_TOTAL_SIZE = 100000

    def smart_truncate(value: Any, max_items: int = MAX_LIST_ITEMS) -> Any:
        if isinstance(value, list):
            if len(value) > max_items:
                return value[:max_items] + [f"... and {len(value) - max_items} more items"]
            return value
        elif isinstance(value, dict):
            if len(value) > MAX_DICT_ITEMS:
                truncated = dict(list(value.items())[:MAX_DICT_ITEMS])
                truncated["..."] = f"and {len(value) - MAX_DICT_ITEMS} more fields"
                return truncated
            return value
        elif isinstance(value, str) and len(value) > MAX_STRING_LENGTH:
            return value[:MAX_STRING_LENGTH] + "..."
        return value

    def calculate_size(obj: Any) -> int:
        if isinstance(obj, (str, int, float, bool)):
            return len(str(obj))
        elif isinstance(obj, list):
            return sum(calculate_size(item) for item in obj)
        elif isinstance(obj, dict):
            return sum(calculate_size(v) for v in obj.values())
        return 100

    field_priority = {
        "tool_name": 1, "model": 1, "reasoning_strategy": 1, "plan_type": 1,
        "workflow_id": 1, "memory_type": 1, "analysis_type": 1,
        "parameters": 2, "messages": 2, "content": 2, "query": 2,
        "temperature": 2, "max_tokens": 2, "max_iterations": 2,
        "conversation_history": 3, "tools": 3, "tags": 3, "metadata": 3,
        "context": 3, "execution_context": 3, "input_data": 3,
    }

    sorted_fields = sorted(
        raw_inputs.items(),
        key=lambda x: (field_priority.get(x[0], 4), x[0]),
    )

    extracted: Dict[str, Any] = {}
    total_size = 0
    omitted_fields: List[str] = []

    for key, value in sorted_fields:
        if value is None or (isinstance(value, (list, dict)) and len(value) == 0):
            continue
        if key.startswith("_") or key in ("debug_info", "internal_metadata"):
            continue

        formatted_value = smart_truncate(value)
        field_size = calculate_size(formatted_value)
        if total_size + field_size > MAX_TOTAL_SIZE:
            # Skip only the oversized field so smaller fields after it still
            # appear; record its name so the UI can offer a full-data view.
            omitted_fields.append(key)
            continue
        extracted[key] = formatted_value
        total_size += field_size

    if omitted_fields:
        extracted["..."] = (
            f"{len(omitted_fields)} field(s) omitted for display (too large): "
            f"{', '.join(omitted_fields)}. Use the command debug view for full data."
        )

    if not extracted:
        extracted = {
            "summary": f"Command has {len(raw_inputs)} fields",
            "field_names": list(raw_inputs.keys())[:10],
        }

    return extracted


def get_task_flow_sync(
    task_id: str,
    tenant_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Load full task flow for a task: command metadata plus inputs and results per command.

    Callable from sync context (e.g. Celery worker / admin tool). For async callers
    use asyncio.to_thread(get_task_flow_sync, task_id, tenant_id).

    Args:
        task_id: Task ID to load.
        tenant_id: If set, only include commands for this tenant. If None, include all.

    Returns:
        List of command dicts, each with: command_id, command_type, created_at,
        executed_at, completed_at, status, worker_id, parent_command_id, triggered_by,
        duration_ms, conversation_id, agentic_loop_iteration (when stamped),
        inputs (formatted), results (truncated if large).
    """
    redis_client = get_sync_redis_client("task_flow_loader")
    cmd_data_manager = get_redis_command_data_manager()

    from ..security.envelope_decode_helpers import decode_cmd_meta_envelope

    task_commands: List[Dict[str, Any]] = []
    hard_limit = 2000

    for key in iter_cmd_keys_sync(redis_client, kind="meta"):
        command_id = command_id_from_cmd_key(key)
        if not command_id:
            continue

        raw = redis_client.hgetall(key)
        if not raw:
            continue

        metadata = _decode_hash(raw)
        if metadata.get("task_id") != task_id:
            continue
        if tenant_id and metadata.get("tenant_id") and metadata.get("tenant_id") != tenant_id:
            continue

        envelope_json = metadata.get("_envelope") or ""
        if envelope_json:
            tenant_id_meta = (metadata.get("tenant_id") or "").strip()
            motet_id_meta = (metadata.get("motet_id") or "").strip()
            try:
                sensitive = decode_cmd_meta_envelope(
                    envelope_json=str(envelope_json),
                    command_id=command_id,
                    tenant_id=tenant_id_meta,
                    motet_id=motet_id_meta,
                )
                if isinstance(sensitive, dict):
                    metadata.update(sensitive)
            except Exception:
                pass  # optional envelope decode; continue without sensitive fields
        metadata.pop("_envelope", None)

        command_inputs: Optional[Dict[str, Any]] = None
        command_results: Optional[Any] = None

        try:
            data_key = f"cmd:data:{command_id}"
            raw_command_data = cmd_data_manager.retrieve_command_data(
                data_key, tenant_id=metadata.get("tenant_id")
            )
            if raw_command_data:
                command_type = metadata.get("command_type", "unknown")
                command_inputs = extract_command_inputs_for_display(command_type, raw_command_data)
        except (ValueError, Exception):
            pass

        try:
            result_key = f"cmd:result:{command_id}"
            command_results = cmd_data_manager.retrieve_command_result(
                result_key, tenant_id=metadata.get("tenant_id")
            )
            if isinstance(command_results, dict) and "content" in command_results:
                content = command_results["content"]
                if len(str(content)) > 500:
                    command_results = dict(command_results)
                    command_results["content"] = str(content)[:500] + "... [truncated]"
        except (ValueError, Exception):
            pass

        duration_ms = metadata.get("duration_ms")
        if duration_ms is not None:
            try:
                duration_ms = int(duration_ms) if isinstance(duration_ms, str) else duration_ms
            except (ValueError, TypeError):
                duration_ms = None

        command_row: Dict[str, Any] = {
            "command_id": command_id,
            "command_type": metadata.get("command_type"),
            "created_at": metadata.get("created_at"),
            "executed_at": metadata.get("executed_at"),
            "completed_at": metadata.get("completed_at"),
            "status": metadata.get("status"),
            "worker_id": metadata.get("worker_id"),
            "parent_command_id": metadata.get("parent_command_id"),
            "triggered_by": metadata.get("triggered_by"),
            "duration_ms": duration_ms,
            "conversation_id": metadata.get("conversation_id"),
            "inputs": command_inputs,
            "results": command_results,
        }
        iteration = parse_agentic_loop_iteration(
            metadata.get(AGENTIC_LOOP_ITERATION_META_KEY)
        )
        if iteration is not None:
            command_row[AGENTIC_LOOP_ITERATION_META_KEY] = iteration
        task_commands.append(command_row)

        if len(task_commands) >= hard_limit:
            break

    task_commands.sort(key=lambda x: str(x.get("created_at", "")))
    return task_commands
