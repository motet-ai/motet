"""
Motet - MCP Protocol Definitions

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Comprehensive MCP protocol definitions for the Motet distributed framework.
    Defines message formats, protocol structures, and communication patterns for
Motet Streams-based MCP communication. Includes stream types, context
    management, and comprehensive protocol validation.

    I/O stream keys (issue #235): logical body is
    ``mcp-{service}-{visibility}-{scope}-{type}``. Physical key is
    ``[{tenant_id}:]mcp:[{manager_id}:]mcp-…`` so the second segment is the
    ``mcp:`` family (like ``task:`` / ``mem:``), Valkey ``~{tid}:*`` matches
    tenant streams, and ``manager_id`` is the bus address — not
    Celery worker_id. GLOBAL / discovery omit ``{tid}:`` (``mcp:{manager}:…``).
    Tenant id ``mcp`` is reserved. ``motet:mcp:`` lifecycle signals and
    ``{manager_id}:mcp-control`` stay shared.

Dependencies:
    - enum: Protocol enumeration and type definitions
    - pydantic: Data validation and model definitions
    - typing: Type hints and annotations
    - uuid: Unique identifier generation
    - time: Timestamp and TTL management
    - motet.core.distributed.tenant_keys: maybe_tenant_key / usable tenant check

Usage:
    from motet.core.tools.mcp_motet.protocol import (
        MCPStreamMessage, MCPRequestMessage, MCPResponseMessage,
        StreamType, Visibility, LifecycleDuration,
        generate_instance_key, generate_stream_name
    )

    # Create stream message
    message = MCPStreamMessage(
        stream_type=StreamType.REQUESTS,
        service_id="weather",
        context_id="weather:tenant123:production:user456",
        data={"method": "tools/list"}
    )

    # Generate stream name
    instance_key = generate_instance_key(
        service_id="weather",
        visibility=Visibility.USER,
        lifecycle_duration=LifecycleDuration.PERMANENT,
        motet_id="production",
        tenant_id="tenant123",
        principal_id="user456"
    )
    stream_name = generate_stream_name(
        service_id="weather",
        visibility=Visibility.USER,
        instance_key=instance_key,
        stream_type=StreamType.REQUESTS,
        manager_id="mcp-local-default",
    )
    # "tenant123:mcp:mcp-local-default:mcp-weather-user-tenant123:production:user456-requests"

Notes:
    - Visibility/lifecycle-based naming for streams and instances
    - Context uses the four-dimensional visibility/lifecycle model
    - Issue #235 / §R2: physical key is {tid}:mcp:{manager}: + logical mcp- body
"""

import time
import uuid
from typing import Dict, Any, Optional, List, Union, Tuple
from enum import Enum
from pydantic import BaseModel, Field, ConfigDict


# --- ADR-0058: Four-dimensional isolation model ---


class StateModel(str, Enum):
    """Whether the MCP server maintains state between requests."""
    STATELESS = "stateless"
    STATEFUL = "stateful"


class CredentialScope(str, Enum):
    """Where credentials are sourced from."""
    MOTET = "motet"
    TENANT = "tenant"
    USER = "user"
    GLOBAL = "global"


class Visibility(str, Enum):
    """Who can see/access the same instance (isolation boundary)."""
    MOTET = "motet"
    GLOBAL = "global"
    TENANT = "tenant"
    USER = "user"


class LifecycleDuration(str, Enum):
    """How long the instance lives (cleanup triggers)."""
    PERMANENT = "permanent"
    SESSION = "session"
    CONVERSATION = "conversation"
    TASK = "task"
    IDLE_TIMEOUT = "idle_timeout"


class StreamType(str, Enum):
    """Stream types for MCP communication."""
    REQUESTS = "requests"
    RESPONSES = "responses" 
    NOTIFICATIONS = "notifications"
    LOGS = "logs"
    CONTROL = "control"
    EVENTS = "events"


# TTL (Time To Live) for Redis streams by lifecycle duration (in seconds)
# Streams are automatically cleaned up after this duration of inactivity
STREAM_TTL_SECONDS = {
    LifecycleDuration.PERMANENT: None,      # Permanent (no TTL) - shared resources
    LifecycleDuration.IDLE_TIMEOUT: 1800,   # 30 minutes - per-user instances with idle timeout
    LifecycleDuration.TASK: 3600,           # 1 hour - tasks are typically short-lived
    LifecycleDuration.CONVERSATION: 86400,  # 24 hours - multi-turn conversations
    LifecycleDuration.SESSION: 604800,      # 7 days - long-running session contexts
}


def validate_instance_spec(
    *,
    state_model: StateModel,
    credential_scope: CredentialScope,
    visibility: Visibility,
    lifecycle_duration: LifecycleDuration,
    instances: Optional[int] = None,
    shared_state_allowed: bool = False
) -> None:
    """
    Validate instance configuration according to ADR-0058.
    
    Raises:
        ValueError: If any rule is violated.
    """
    # Stateful must be single instance
    if state_model == StateModel.STATEFUL and instances and instances > 1:
        raise ValueError("Stateful services must use a single instance (instances=1 or omitted)")
    
    # Stateful with non-USER visibility requires explicit opt-in
    if state_model == StateModel.STATEFUL and visibility != Visibility.USER and not shared_state_allowed:
        raise ValueError("Stateful services with MOTET/TENANT/GLOBAL visibility require shared_state_allowed=True")
    
    # Credential scope USER requires USER visibility (user tokens cannot be shared)
    if credential_scope == CredentialScope.USER and visibility != Visibility.USER:
        raise ValueError("User credential scope requires USER visibility")
    
    # Credential scope GLOBAL should not be combined with USER visibility
    if credential_scope == CredentialScope.GLOBAL and visibility == Visibility.USER:
        raise ValueError("Global credential scope is invalid with USER visibility")
    
    # Lifecycle-specific ID requirements are handled in generate_instance_key
    # No return value; absence of exception implies valid spec.


def generate_instance_key(
    service_id: str,
    visibility: Visibility,
    lifecycle_duration: LifecycleDuration,
    *,
    motet_id: str,
    tenant_id: Optional[str] = None,
    principal_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    task_id: Optional[str] = None,
    session_id: Optional[str] = None
) -> str:
    """
    Generate consistent instance key based on visibility and lifecycle duration.
    
    Tenant-centric hierarchy:
    - GLOBAL: {service_id}:global
    - TENANT: {service_id}:{tenant_id}
    - MOTET: {service_id}:{tenant_id}:{motet_id}
    - USER: {service_id}:{tenant_id}:{motet_id}:{principal_id}
    
    Lifecycle suffixes (when applicable):
    - SESSION: :session:{session_id}
    - CONVERSATION: :conversation:{conversation_id}
    - TASK: :task:{task_id}
    - IDLE_TIMEOUT/PERMANENT: no suffix
    """
    if visibility == Visibility.GLOBAL:
        base_key = f"{service_id}:global"
    elif visibility == Visibility.TENANT:
        if not tenant_id:
            raise ValueError("Tenant visibility requires tenant_id")
        base_key = f"{service_id}:{tenant_id}"
    elif visibility == Visibility.MOTET:
        if not tenant_id:
            raise ValueError("Motet visibility requires tenant_id (motets belong to tenants)")
        base_key = f"{service_id}:{tenant_id}:{motet_id}"
    elif visibility == Visibility.USER:
        if not tenant_id or not principal_id:
            raise ValueError("User visibility requires tenant_id and principal_id")
        base_key = f"{service_id}:{tenant_id}:{motet_id}:{principal_id}"
    else:
        raise ValueError(f"Unknown visibility: {visibility}")
    
    if lifecycle_duration == LifecycleDuration.PERMANENT or lifecycle_duration == LifecycleDuration.IDLE_TIMEOUT:
        return base_key
    if lifecycle_duration == LifecycleDuration.SESSION:
        if not session_id:
            raise ValueError("SESSION lifecycle requires session_id")
        return f"{base_key}:session:{session_id}"
    if lifecycle_duration == LifecycleDuration.CONVERSATION:
        if not conversation_id:
            raise ValueError("CONVERSATION lifecycle requires conversation_id")
        return f"{base_key}:conversation:{conversation_id}"
    if lifecycle_duration == LifecycleDuration.TASK:
        if not task_id:
            raise ValueError("TASK lifecycle requires task_id")
        return f"{base_key}:task:{task_id}"
    raise ValueError(f"Unknown lifecycle duration: {lifecycle_duration}")


def resolve_visibility_and_lifecycle(
    *,
    tenant_id: Optional[str] = None,
    principal_id: Optional[str] = None,
    motet_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    task_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> Tuple[Visibility, LifecycleDuration]:
    """
    Resolve visibility and lifecycle from context IDs using heuristic rules.
    
    Visibility heuristic:
    - principal_id present → USER
    - tenant_id + motet_id present → MOTET
    - tenant_id present (no motet_id) → TENANT
    - Otherwise → GLOBAL
    
    Lifecycle heuristic:
    - session_id present → SESSION
    - conversation_id present → CONVERSATION
    - task_id present → TASK
    - Otherwise → PERMANENT
    
    Args:
        tenant_id: Optional tenant identifier
        principal_id: Optional principal (user) identifier
        motet_id: Optional motet identifier
        conversation_id: Optional conversation identifier
        task_id: Optional task identifier
        session_id: Optional session identifier
    
    Returns:
        Tuple of (visibility, lifecycle_duration)
    """
    # Determine visibility
    if principal_id:
        visibility = Visibility.USER
    elif tenant_id and motet_id:
        visibility = Visibility.MOTET
    elif tenant_id:
        visibility = Visibility.TENANT
    else:
        visibility = Visibility.GLOBAL
    
    # Determine lifecycle
    if session_id:
        lifecycle = LifecycleDuration.SESSION
    elif conversation_id:
        lifecycle = LifecycleDuration.CONVERSATION
    elif task_id:
        lifecycle = LifecycleDuration.TASK
    else:
        lifecycle = LifecycleDuration.PERMANENT
    
    return visibility, lifecycle


# Tenant-scoped family segment (issue #235). Physical keys are
# ``[{tenant_id}:]mcp:[{manager_id}:]{logical}``. Distinct from the logical
# body prefix ``mcp-`` and from ``{manager_id}:mcp-control``.
MCP_IO_FAMILY = "mcp"


def _logical_mcp_body(
    service_id: str,
    visibility: str,
    scope_path: str,
    stream_type: str,
) -> str:
    """Format ``mcp-{service}-{visibility}-{scope}-{type}``."""
    return f"mcp-{service_id}-{visibility}-{scope_path}-{stream_type}"


def logical_mcp_stream_name(
    service_id: str,
    visibility: Visibility,
    instance_key: str,
    stream_type: StreamType,
) -> str:
    """
    Instance-addressed MCP I/O stream name (no tenant, no manager).

    Format: mcp-{service_id}-{visibility}-{scope_path}-{stream_type}
    """
    parts = instance_key.split(":")
    if not parts or parts[0] != service_id:
        raise ValueError(f"instance_key must start with service_id: expected {service_id}, got {instance_key}")
    scope_parts = parts[1:]
    if not scope_parts:
        raise ValueError(f"instance_key missing scope components: {instance_key}")
    return _logical_mcp_body(
        service_id, visibility.value, ":".join(scope_parts), stream_type.value
    )


def _physical_mcp_io_key(
    *,
    tenant_id: Optional[str],
    manager_id: Optional[str],
    logical: str,
) -> str:
    """Assemble ``[{tenant_id}:]mcp:[{manager_id}:]{logical}``."""
    parts: List[str] = []
    if tenant_id:
        parts.append(tenant_id)
    parts.append(MCP_IO_FAMILY)
    if manager_id:
        parts.append(manager_id)
    parts.append(logical)
    return ":".join(parts)


def generate_stream_name(
    service_id: str,
    visibility: Visibility,
    instance_key: str,
    stream_type: StreamType,
    manager_id: Optional[str] = None,
) -> str:
    """
    Physical Redis stream key for MCP I/O (issue #235, ADR-0105 §R2).

    Logical body: mcp-{service_id}-{visibility}-{scope_path}-{stream_type}
    Physical: [{tenant_id}:]mcp:[{manager_id}:]{logical}

    ``mcp:`` is the family (same slot as ``task:`` / ``mem:``).
    ``manager_id`` is the sibling MCP manager bus address.
    GLOBAL / unusable tenant omit the tenant segment.
    """
    from motet.core.distributed.tenant_keys import is_usable_tenant_id

    logical = logical_mcp_stream_name(service_id, visibility, instance_key, stream_type)
    routing = (manager_id or "").strip() or None
    tid: Optional[str] = None
    if visibility != Visibility.GLOBAL:
        try:
            parsed = parse_instance_key(service_id, visibility, instance_key)
        except ValueError:
            parsed = {}
        candidate = (parsed.get("tenant_id") or "").strip()
        if is_usable_tenant_id(candidate):
            tid = candidate
    return _physical_mcp_io_key(tenant_id=tid, manager_id=routing, logical=logical)


def _parse_logical_mcp_stream_name(name_to_parse: str) -> Dict[str, str]:
    """Parse a logical ``mcp-…`` I/O stream body (no tenant / manager prefix)."""
    if not name_to_parse.startswith("mcp-"):
        raise ValueError(f"Invalid stream name format: {name_to_parse}")

    body = name_to_parse[len("mcp-"):]
    parts = body.split("-")
    if len(parts) < 2:
        raise ValueError(f"Invalid stream name format: {name_to_parse}")

    stream_type = parts[-1]
    try:
        stream_type_enum = StreamType(stream_type)
    except Exception as exc:
        raise ValueError(f"Invalid stream type in stream name: {stream_type}") from exc

    core_parts = parts[:-1]
    vis_values = {v.value for v in Visibility}
    visibility_idx = None
    for i, part in enumerate(core_parts):
        if part in vis_values:
            visibility_idx = i
            break

    if visibility_idx is None:
        raise ValueError(
            f"Invalid stream name format (no ADR-0058 visibility match): {name_to_parse}"
        )

    service_id = "-".join(core_parts[:visibility_idx])
    visibility_value = core_parts[visibility_idx]
    scope_path = (
        "-".join(core_parts[visibility_idx + 1:])
        if len(core_parts) > visibility_idx + 1
        else ""
    )
    try:
        visibility = Visibility(visibility_value)
    except Exception as exc:
        raise ValueError(f"Invalid visibility in stream name: {visibility_value}") from exc

    instance_key = f"{service_id}:{scope_path}" if scope_path else service_id
    return {
        "service_id": service_id,
        "visibility": visibility.value,
        "scope_path": scope_path,
        "instance_key": instance_key,
        "stream_type": stream_type_enum.value,
    }


def _logical_mcp_body_starts_at(segment: str) -> bool:
    """True when *segment* is ``mcp-{service}-{visibility}-…``, not a manager id."""
    if not segment.startswith("mcp-"):
        return False
    vis_values = {v.value for v in Visibility}
    return any(part in vis_values for part in segment[len("mcp-"):].split("-"))


def _tenant_and_manager_from_mcp_prefixes(
    prefixes: List[str],
) -> Tuple[Optional[str], Optional[str]]:
    """
    Read ``[{tenant_id}:]mcp:[{manager_id}:]`` in front of the logical body.

    Bare logical names have no prefixes. Family segment is required otherwise.
    """
    if not prefixes:
        return None, None
    if prefixes[0] == MCP_IO_FAMILY:
        if len(prefixes) > 2:
            raise ValueError(f"Invalid MCP I/O prefix: {':'.join(prefixes)}")
        manager_id = prefixes[1] if len(prefixes) == 2 else None
        return None, manager_id
    if len(prefixes) >= 2 and prefixes[1] == MCP_IO_FAMILY:
        if len(prefixes) > 3:
            raise ValueError(f"Invalid MCP I/O prefix: {':'.join(prefixes)}")
        tenant_id = prefixes[0]
        manager_id = prefixes[2] if len(prefixes) == 3 else None
        return tenant_id, manager_id
    raise ValueError(f"Invalid MCP I/O prefix (missing {MCP_IO_FAMILY}: family): {':'.join(prefixes)}")


def parse_stream_name(stream_name: str) -> Dict[str, str]:
    """
    Parse a physical or logical MCP I/O stream name.

    Physical: [{tenant_id}:]mcp:[{manager_id}:]mcp-{service}-{visibility}-{scope}-{type}

    The family token is ``mcp`` (colon). The logical body starts with ``mcp-``
    (hyphen). Manager ids such as ``mcp-local-default`` also start with
    ``mcp-`` but fail logical parse, so the body is the first later segment
    that parses as ADR-0058 I/O.
    """
    raw = (stream_name or "").strip()
    if not raw:
        raise ValueError("Invalid stream name format: empty")

    parts = raw.split(":")
    last_error: Optional[Exception] = None
    for i, part in enumerate(parts):
        if not _logical_mcp_body_starts_at(part):
            continue
        candidate = ":".join(parts[i:])
        try:
            result = _parse_logical_mcp_stream_name(candidate)
        except ValueError as exc:
            last_error = exc
            continue
        try:
            tenant_id, manager_id = _tenant_and_manager_from_mcp_prefixes(parts[:i])
        except ValueError as exc:
            last_error = exc
            continue
        if tenant_id:
            result["tenant_id"] = tenant_id
        if manager_id:
            result["manager_id"] = manager_id
        return result

    if last_error is not None:
        raise ValueError(f"Invalid stream name format: {stream_name}") from last_error
    raise ValueError(f"Invalid stream name format: {stream_name}")


def logical_mcp_bus_name(stream_name: str) -> str:
    """
    AAD-stable bus name: ``[manager_id:]mcp-…`` with no tenant or ``mcp:`` family.

    Binds ciphertext to the instance + which manager owns the stream, not
    the physical ``{tid}:mcp:`` ACL/family segments.
    """
    parsed = parse_stream_name(stream_name)
    logical = _logical_mcp_body(
        parsed["service_id"],
        parsed["visibility"],
        parsed["scope_path"],
        parsed["stream_type"],
    )
    manager_id = parsed.get("manager_id")
    if manager_id:
        return f"{manager_id}:{logical}"
    return logical


def manager_id_from_stream_name(stream_name: str) -> Optional[str]:
    """Return the MCP manager routing prefix, or None if the name has none."""
    if not stream_name:
        return None
    try:
        return parse_stream_name(stream_name).get("manager_id")
    except ValueError:
        return None


def mcp_io_stream_scan_patterns(
    manager_id: Optional[str] = None,
    *,
    service_id: Optional[str] = None,
    stream_type: Optional[str] = None,
) -> Tuple[str, ...]:
    """
    SCAN globs for this manager's I/O streams.

    Covers GLOBAL ``mcp:{manager}:mcp-…`` and tenant ``{tid}:mcp:{manager}:mcp-…``.
    Does not match ``{manager}:mcp-control`` or ``motet:mcp:`` signals.
    """
    body = f"mcp-{service_id}-" if service_id else "mcp-"
    suffix = f"-{stream_type}" if stream_type else ""
    mid = (manager_id or "").strip()
    if mid:
        return (
            f"{MCP_IO_FAMILY}:{mid}:{body}*{suffix}",
            f"*:{MCP_IO_FAMILY}:{mid}:{body}*{suffix}",
        )
    return (
        f"{MCP_IO_FAMILY}:{body}*{suffix}",
        f"*:{MCP_IO_FAMILY}:{body}*{suffix}",
    )


def parse_instance_key(
    service_id: str,
    visibility: Visibility,
    instance_key: str
) -> Dict[str, Optional[str]]:
    """
    Parse ADR-0058 instance key into components.
    
    Returns:
        Dict with tenant_id, motet_id, principal_id, conversation_id, task_id, session_id
    """
    parts = instance_key.split(":")
    if not parts or parts[0] != service_id:
        raise ValueError(f"instance_key must start with service_id: expected {service_id}, got {instance_key}")
    
    # Base offsets by visibility
    idx = 1
    tenant_id = motet_id = principal_id = None
    conversation_id = task_id = session_id = None
    
    if visibility == Visibility.GLOBAL:
        if len(parts) < 2 or parts[1] != "global":
            raise ValueError(f"Invalid GLOBAL instance_key: {instance_key}")
        idx = 2
    elif visibility == Visibility.TENANT:
        if len(parts) < 2:
            raise ValueError(f"Tenant visibility requires tenant_id in instance_key: {instance_key}")
        tenant_id = parts[1]
        idx = 2
    elif visibility == Visibility.MOTET:
        if len(parts) < 3:
            raise ValueError(f"Motet visibility requires tenant_id and motet_id in instance_key: {instance_key}")
        tenant_id = parts[1]
        motet_id = parts[2]
        idx = 3
    elif visibility == Visibility.USER:
        if len(parts) < 4:
            raise ValueError(f"User visibility requires tenant_id, motet_id, principal_id in instance_key: {instance_key}")
        tenant_id = parts[1]
        motet_id = parts[2]
        principal_id = parts[3]
        idx = 4
    else:
        raise ValueError(f"Unknown visibility: {visibility}")
    
    # Lifecycle suffix detection
    while idx < len(parts):
        token = parts[idx]
        if token == "session":
            session_id = parts[idx + 1] if idx + 1 < len(parts) else None
            break
        if token == "conversation":
            conversation_id = parts[idx + 1] if idx + 1 < len(parts) else None
            break
        if token == "task":
            task_id = parts[idx + 1] if idx + 1 < len(parts) else None
            break
        idx += 1
    
    return {
        "tenant_id": tenant_id,
        "motet_id": motet_id,
        "principal_id": principal_id,
        "conversation_id": conversation_id,
        "task_id": task_id,
        "session_id": session_id,
}


class MCPStreamMessage(BaseModel):
    """Base message format for all Motet Streams MCP communication.
    
    Uses instance_key for identification. Legacy context_type/context_id removed.
    """
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = Field(default_factory=time.time)
    stream_type: StreamType
    service_id: str
    instance_key: Optional[str] = None  # ADR-0058: Full instance key (replaces context_type/context_id)
    worker_id: Optional[str] = None


class MCPRequestMessage(MCPStreamMessage):
    """Request message format for MCP tool calls."""
    stream_type: StreamType = StreamType.REQUESTS
    timeout_ms: int = 30000
    jsonrpc_request: Dict[str, Any]
    
    # CommandContext fields for vault credential lookup
    principal_id: Optional[str] = None
    tenant_id: Optional[str] = None
    motet_id: Optional[str] = None
    
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": "req-uuid-1234",
                "timestamp": 1640995200,
                "stream_type": "requests",
                "service_id": "playwright",
                "instance_key": "playwright:default:production:user-123:conversation:abc123",
                "worker_id": "worker-abc",
                "timeout_ms": 30000,
                "principal_id": "user-123",
                "tenant_id": "default",
                "motet_id": "production",
                "jsonrpc_request": {
                    "jsonrpc": "2.0",
                    "id": "req-uuid-1234",
                    "method": "tools/call",
                    "params": {
                        "name": "screenshot",
                        "arguments": {"url": "https://example.com"}
                    }
                }
            }
        }
    )


class MCPResponseMessage(MCPStreamMessage):
    """Response message format for MCP tool results."""
    stream_type: StreamType = StreamType.RESPONSES
    request_id: str
    processing_time_ms: Optional[int] = None
    jsonrpc_response: Dict[str, Any]
    
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": "resp-uuid-5678",
                "timestamp": 1640995201,
                "stream_type": "responses",
                "service_id": "playwright",
                "instance_key": "playwright:default:production:user-123:conversation:abc123",
                "worker_id": "worker-abc",
                "request_id": "req-uuid-1234",
                "processing_time_ms": 1250,
                "jsonrpc_response": {
                    "jsonrpc": "2.0",
                    "id": "req-uuid-1234",
                    "result": {
                        "content": [{"type": "text", "text": "Screenshot saved successfully"}]
                    }
                }
            }
        }
    )


class MCPLogMessage(MCPStreamMessage):
    """Log message format for MCP server diagnostic output."""
    stream_type: StreamType = StreamType.LOGS
    request_id: Optional[str] = None
    level: str = "info"
    message: str
    raw_stderr: Optional[str] = None
    
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": "log-uuid-9012",
                "timestamp": 1640995201,
                "stream_type": "logs",
                "service_id": "playwright",
                "instance_key": "playwright:default:production:user-123:conversation:abc123",
                "request_id": "req-uuid-1234",
                "level": "info",
                "message": "Processing screenshot request for https://example.com",
                "raw_stderr": "Processing screenshot request..."
            }
        }
    )


class MCPNotificationMessage(MCPStreamMessage):
    """Notification message format for MCP server events."""
    stream_type: StreamType = StreamType.NOTIFICATIONS
    jsonrpc_notification: Dict[str, Any]


class MCPControlMessage(MCPStreamMessage):
    """Control message format for MCP server lifecycle management."""
    stream_type: StreamType = StreamType.CONTROL
    command: str  # start, stop, health, restart
    params: Optional[Dict[str, Any]] = None


class MCPEventMessage(MCPStreamMessage):
    """Event message format for MCP server lifecycle events."""
    stream_type: StreamType = StreamType.EVENTS
    event_type: str  # started, stopped, error, health_check
    event_data: Optional[Dict[str, Any]] = None


# Export message types
__all__ = [
    "StateModel",
    "CredentialScope",
    "Visibility",
    "LifecycleDuration",
    "StreamType",
    "MCPStreamMessage",
    "MCPRequestMessage",
    "MCPResponseMessage",
    "MCPLogMessage",
    "MCPNotificationMessage",
    "MCPControlMessage",
    "MCPEventMessage",
    "validate_instance_spec",
    "MCP_IO_FAMILY",
    "generate_instance_key",
    "logical_mcp_stream_name",
    "generate_stream_name",
    "parse_stream_name",
    "logical_mcp_bus_name",
    "manager_id_from_stream_name",
    "mcp_io_stream_scan_patterns",
    "parse_instance_key",
    "resolve_visibility_and_lifecycle",
    "STREAM_TTL_SECONDS",
]
