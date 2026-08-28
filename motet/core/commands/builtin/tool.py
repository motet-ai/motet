"""
Motet - Tool Execution Commands

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Comprehensive tool command system for the Motet distributed framework.
    Provides unified distributed commands for tool operations:
    - tool_execution(): Execute any tool (built-in, MCP, memory, custom)
    - tool_list(): List all available tools from worker registry
    - tool_discovery: Embedding-only tool candidate discovery

    Includes parameter injection, MCP credential retrieval, and error handling.

    Uses the decorator-based command pattern with tool_execution, tool_list,
    and tool_discovery decorated functions.

    Oversized tool arguments are offloaded to ArtifactKind.TOOL_ARGUMENTS
    so transcript replay can restore unmodified JSON for provider tool-call round-trips.

Dependencies:
    - uuid: Unique identifier generation
    - json: Data serialization and tool parameters
    - typing: Type hints and annotations
    - Distributed command system (decorator pattern)
    - ToolDiscoveryService (embedding search via FunctionDiscoveryVectorStore)
    - MotetContext for resource access

Usage:
    # NEW: Decorator pattern (recommended - ADR-0030)
    from motet.core.commands.builtin.tool import (
        tool_execution, tool_list, tool_discovery,
        ToolExecutionData, ToolListData, ToolDiscoveryData
    )
    
    # Execute tool via motet.do() (within another decorated command) - automatic unwrapping
    data = ToolExecutionData(tool_name="web_search", parameters={"query": "AI"})
    result = motet.do(tool_execution, data=data)
    
    # List tools via motet.do() - automatic unwrapping
    list_data = ToolListData()
    tools = motet.do(tool_list, data=list_data)
    
    # Discover tools via motet.do() - automatic unwrapping
    discovery_data = ToolDiscoveryData(content="search weather", max_tools=3)
    candidates = motet.do(tool_discovery, data=discovery_data)
    
    # Execute via global invoker (outside decorated commands)
    from motet.core.workers import global_invoker
    command = tool_execution(task_id="task123", conversation_id="conv456", data=data)
    result = await asyncio.to_thread(global_invoker.execute_command, command)
    
    # Create command and run via invoker
    command = tool_execution(data=ToolExecutionData(tool_name="web_search", parameters={"query": "AI"}), task_id="...", conversation_id="...")
    result = await asyncio.to_thread(global_invoker.execute_command, command)

Notes:
    - Supports execution of built-in tools (web_search, http_get, file_read, etc.)
    - Includes MCP tool execution (mcp.server_id.tool_name format)
    - Supports memory tools (memory_tag, memory_recall, etc.)
    - Provides automatic parameter injection
    - Includes MCP credential retrieval from vault
    - Includes tool result formatting and observation
    - Supports error handling and retries
    - Integrates with distributed worker routing and capability management
    - Uses pool-agnostic concurrency primitives for Celery compatibility
"""


import json
import time
from typing import Any, Dict, List, Optional, Type
from uuid import uuid4
from datetime import datetime, timezone

from motet import motet
from motet.core.commands.capabilities import WorkerCapability
from motet.core.commands.decorator import get_motet_context, MotetContext
from motet.core.commands.command_data_classes import CreateArtifactData, ToolExecutionData, ToolListData, ToolDiscoveryData
from motet.core.tools.distributed_discovery import ToolCandidate, ToolDiscoveryService
from motet.core.tools.tool_transcripts import ToolInvocation, ToolInvocationStatus
from motet.core.types import Message
from motet.core.workers.observers import EventPriority
import structlog
from pydantic import BaseModel

logger = structlog.get_logger(__name__)

def _truncate_utf8(text: str, max_bytes: int) -> str:
    if max_bytes <= 0:
        return ""
    if not text:
        return ""
    raw = text.encode("utf-8", errors="ignore")
    if len(raw) <= max_bytes:
        return text
    suffix = "\n…[truncated]"
    suffix_bytes = suffix.encode("utf-8", errors="ignore")
    budget = max(0, max_bytes - len(suffix_bytes))
    head = raw[:budget]
    trimmed = head.decode("utf-8", errors="ignore")
    return trimmed + suffix


from motet.core.tools.result_formatting import format_tool_result_preview


# ADR-0061: Removed _should_store_tool_observation and _persist_tool_observation
# Tool observations are replaced by ToolInvocation + ToolArtifact storage (hard cutover)


_SENSITIVE_TOOL_NAME_PATTERNS = (
    "oauth",
    "auth",
    "download",
    "token",
    "credential",
    "secret",
)


def _tool_artifact_denied(*, cfg: Any, tool_name: str) -> bool:
    """True when denylist or sensitive-name heuristics block artifact storage."""
    deny_raw = getattr(cfg, "tool_artifact_denylist", None)
    if deny_raw:
        deny = {t.strip() for t in str(deny_raw).split(",") if t.strip()}
        if tool_name in deny:
            return True
    tool_lower = tool_name.lower()
    return any(pattern in tool_lower for pattern in _SENSITIVE_TOOL_NAME_PATTERNS)


def _should_store_tool_artifact(
    *,
    stack: Any,
    tool_name: str,
    tool_info: Any,
) -> bool:
    """
    Determine if we should store raw tool payload as ToolArtifact (ADR-0061).
    
    Policy:
    - Global flag: Config.store_tool_artifacts (off by default)
    - Per-tool allowlist/denylist: Config.tool_artifact_allowlist / denylist
    - Default deny for sensitive classes (OAuth, auth endpoints, downloads, binary)
    """
    cfg = getattr(stack, "config", None)
    if not bool(getattr(cfg, "store_tool_artifacts", False)):
        return False

    deny_raw = getattr(cfg, "tool_artifact_denylist", None)
    if deny_raw:
        deny = {t.strip() for t in str(deny_raw).split(",") if t.strip()}
        if tool_name in deny:
            return False

    allow_raw = getattr(cfg, "tool_artifact_allowlist", None)
    allowlisted = False
    if allow_raw:
        allow = {t.strip() for t in str(allow_raw).split(",") if t.strip()}
        if tool_name not in allow:
            return False
        allowlisted = True

    # Explicit allowlist should override default sensitive-pattern deny.
    if not allowlisted and _tool_artifact_denied(cfg=cfg, tool_name=tool_name):
        return False

    return True


def _should_store_oversized_tool_result(
    *,
    stack: Any,
    tool_name: str,
    result_size_bytes: int,
) -> bool:
    """Store oversized non-sensitive results so history can stay clipped.

    Complements the allowlist: large ``file_read`` / ``edge_exec`` payloads are
    offloaded even when not explicitly allowlisted (ADR-0061 cost control).
    Caller passes the serialized result size so the payload is serialized once.
    """
    cfg = getattr(stack, "config", None)
    if not bool(getattr(cfg, "store_tool_artifacts", False)):
        return False
    if _tool_artifact_denied(cfg=cfg, tool_name=tool_name):
        return False
    min_bytes = int(getattr(cfg, "tool_result_artifact_min_bytes", 8192) or 8192)
    if min_bytes <= 0:
        return False
    return result_size_bytes >= min_bytes


def _persist_tool_invocation(
    *,
    stack: Any,
    motet: MotetContext,
    invocation: Any,  # ToolInvocation - imported locally to avoid circular imports
) -> None:
    """
    Persist ToolInvocation metadata to MemoryManager (ADR-0061).
    
    Stores as MemoryItem(type="tool_invocation") with structured metadata
    for schema-correct transcript reconstruction.
    """
    try:
        memory_manager = getattr(motet, "memory", None) or getattr(stack, "memory_manager", None)
        if not memory_manager:
            return
        
        from motet.core.types import MemoryScopeType
        
        # Create human-readable content summary for quick inspection
        status_str = invocation.status.value if hasattr(invocation.status, 'value') else str(invocation.status)
        content = f"Executed tool '{invocation.tool_name}': {status_str}"
        if invocation.error_summary:
            content += f" ({invocation.error_summary[:100]})"
        
        # Store as MemoryItem with ToolInvocation metadata
        # Using model_dump() from Pydantic model
        #
        # IMPORTANT (ADR-0061):
        # ToolInvocation records are updated over time (STARTED -> SUCCESS/ERROR/AUTH_REQUIRED).
        # Memory storage is append-only unless we supply a stable item_id. If we don't, retries or
        # best-effort persist failures can lead to multiple records per tool_call_id which then
        # creates duplicate tool transcript messages on replay. We upsert by a stable key.
        conversation_id = invocation.conversation_id or getattr(motet, "conversation_id", None) or "unknown"
        tool_invocation_item_id = f"tool_invocation:{conversation_id}:{invocation.tool_call_id}"

        store_result = memory_manager.store_memory(
            content=content,
            type="tool_invocation",
            tags=[invocation.tool_name],
            metadata=invocation.model_dump(mode='json', exclude_none=True),
            item_id=tool_invocation_item_id,
            working=False,  # Not working memory - persist across turns
            scope=MemoryScopeType.CONVERSATION,
            motet_context=motet,
        )
        
        logger.debug(
            "tool_invocation_stored",
            tool_name=invocation.tool_name,
            tool_call_id=invocation.tool_call_id,
            status=status_str,
            conversation_id=motet.conversation_id,
            task_id=motet.task_id,
            stored_id=store_result.get("id") if isinstance(store_result, dict) else None,
        )
    except Exception as e:
        logger.warning("tool_invocation_persist_failed", tool_name=invocation.tool_name, error=str(e))


def _store_tool_artifact(
    *,
    stack: Any,
    motet: MotetContext,
    raw_result: Dict[str, Any],
    tool_name: str,
    tool_call_id: str,
    content_type: str = "application/json",
    serialized_result: Optional[str] = None,
    ttl_seconds: Optional[int] = None,
    trigger_derivations: bool = True,
) -> Optional[str]:
    """
    Store raw tool payload as ToolArtifact (policy-gated - ADR-0061).

    ``serialized_result`` lets callers reuse the JSON string computed for the
    oversized-size check instead of serializing twice. Oversized (non-allowlisted)
    offloads pass a TTL and disable derivations; allowlisted artifacts keep the
    original persistent + derived behavior.

    Returns:
        artifact_id if stored, None if not stored (policy denied)
    """
    try:
        from motet.core.commands.builtin.artifacts import create_artifact

        serialized = (
            serialized_result
            if serialized_result is not None
            else json.dumps(raw_result, default=str)
        )
        payload = serialized.encode("utf-8")
        metadata = {
            "tool_name": tool_name,
            "tool_call_id": tool_call_id,
            "status": raw_result.get("status"),
            "conversation_id": motet.conversation_id,
        }
        artifact_result = motet.do(
            create_artifact,
            data=CreateArtifactData(
                payload=payload,
                content_type=content_type,
                kind="tool_artifact",
                filename=f"{tool_name.replace('.', '_')}.json",
                conversation_id=motet.conversation_id,
                metadata=metadata,
                ttl_seconds=ttl_seconds,
                trigger_derivations=trigger_derivations,
                include_text_derivation_for_json=trigger_derivations,
            ),
        )
        artifact_id = str(artifact_result.get("artifact_id") or "") if isinstance(artifact_result, dict) else ""
        
        logger.debug(
            "tool_artifact_stored",
            artifact_id=artifact_id,
            content_type=content_type,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            tenant_id=motet.tenant_id,
            principal_id=motet.principal_id,
        )
        
        return artifact_id or None
    except Exception as e:
        logger.warning("tool_artifact_store_failed", error=str(e))
        return None


def _store_tool_arguments_artifact(
    *,
    motet: MotetContext,
    full_arguments_json: str,
    tool_name: str,
    tool_call_id: str,
    arguments_hash: str,
) -> Optional[str]:
    """
    Store full unmodified tool-call arguments for provider replay (ADR-0061).

    Not gated by result-artifact allowlists. Derivations are disabled.
    """
    try:
        from motet.core.commands.builtin.artifacts import create_artifact

        payload = full_arguments_json.encode("utf-8")
        metadata = {
            "role": "tool_arguments",
            "tool_name": tool_name,
            "tool_call_id": tool_call_id,
            "arguments_hash": arguments_hash,
            "conversation_id": motet.conversation_id,
            "bytes": len(payload),
        }
        artifact_result = motet.do(
            create_artifact,
            data=CreateArtifactData(
                payload=payload,
                content_type="application/json",
                kind="tool_arguments",
                filename=f"{tool_name.replace('.', '_')}.arguments.json",
                conversation_id=motet.conversation_id,
                metadata=metadata,
                ttl_seconds=None,
                trigger_derivations=False,
                include_text_derivation_for_json=False,
            ),
        )
        artifact_id = str(artifact_result.get("artifact_id") or "") if isinstance(artifact_result, dict) else ""
        logger.info(
            "tool_arguments_artifact_stored",
            artifact_id=artifact_id or None,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            arguments_hash=arguments_hash,
            bytes=len(payload),
            tenant_id=motet.tenant_id,
            conversation_id=motet.conversation_id,
        )
        return artifact_id or None
    except Exception as e:
        logger.error(
            "tool_arguments_artifact_store_failed",
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
        return None


# Config classes have been replaced by CommandData classes in command_data_classes.py


# ============================================================================
# DECORATED COMMANDS (New Pattern - ADR-0030)
# ============================================================================

def _infer_tool_capabilities(data: BaseModel) -> List[WorkerCapability]:
    """
    Determine required capabilities using registry-declared routing hints (item 4).

    This function intentionally avoids name-based inference. All tools (built-in and MCP)
    must be registered in `ToolRegistry` with `required_capabilities` set.
    """
    if not isinstance(data, ToolExecutionData):
        raise ValueError("capability inference requires ToolExecutionData")

    # Import here to avoid import cycles at module load time.
    from motet.core.tools.registry import registry as tool_registry

    name = data.tool_name
    tool = tool_registry.get(name)
    if not tool:
        raise ValueError(f"Tool '{data.tool_name}' is not registered; cannot determine required capabilities.")

    caps_raw = list(getattr(tool, "required_capabilities", []) or [])
    if not caps_raw:
        raise ValueError(f"Tool '{data.tool_name}' has no required_capabilities declared in registry.")

    caps: List[WorkerCapability] = []
    for cap_name in caps_raw:
        # We store enum member names (e.g., TOOL_EXECUTION) in the registry.
        try:
            caps.append(WorkerCapability[str(cap_name)])
        except Exception:
            raise ValueError(
                f"Tool '{data.tool_name}' has unknown capability '{cap_name}' (expected WorkerCapability member name)."
            )

    return caps


def _extract_result_media(result_value: Any) -> Optional[List[Dict[str, Any]]]:
    """Extract artifact-backed media parts from a tool result (ADR-0113).

    Tools that produce media (e.g. ``core.image_generation``) return a ``media`` list of
    serialized MediaPart dicts, each carrying an ``artifact_id``. Returns the subset that
    carries an ``artifact_id`` (so it can be persisted on the ToolInvocation and resurfaced
    on transcript replay), or ``None`` when the result has no artifact-backed media.
    """
    if not isinstance(result_value, dict):
        return None
    parts = result_value.get("media")
    if not isinstance(parts, list):
        return None
    media = [
        part
        for part in parts
        if isinstance(part, dict) and part.get("artifact_id")
    ]
    return media or None


@motet.command(
    description="Execute a registered tool by name with parameters (built-in, bundle, or MCP), returning the tool observation.",
    timeout_seconds=300,
    capability_inference=_infer_tool_capabilities  # Dynamically infer capabilities during __init__
)
def tool_execution(data: ToolExecutionData) -> Dict[str, Any]:
    """
    Execute a tool through the distributed system (ADR-0061: Tool Invocation Transcripts).
    
    Supports:
    - Built-in tools (web_search, http_get, file_read, etc.)
    - MCP tools (mcp.server_id.tool_name format)
    - Memory tools (memory_tag, memory_recall, etc.)
    - Custom tools registered in tool registry
    
    Automatically handles:
    - Dynamic capability inference based on tool name (affects worker routing)
    - Parameter injection (ADR-0046)
    - MCP credential retrieval from vault
    - Tool result formatting
    - Error handling and retries
    - ToolInvocation persistence (before and after execution) - ADR-0061
    - ToolArtifact storage (policy-gated) - ADR-0061
    
    Capability Inference:
    - Capabilities are inferred during command initialization (__init__)
    - This affects worker selection and routing
    - web_search, http_* → HTTP_OPERATIONS + TOOL_EXECUTION
    - memory_* → MEMORY_OPERATIONS + TOOL_EXECUTION
    - mcp.* → TOOL_EXECUTION only
    
    Args:
        data: ToolExecutionData with tool_name, parameters, tool_call_id, and optional batching metadata
        motet: MotetContext for resource access
    
    Returns:
        Dict with tool execution result
    """
    
    motet = get_motet_context()
    started_at = time.time()

    invocation_id = None  # Track invocation for update after execution
    tool_call_id = data.tool_call_id or f"call_{uuid4().hex}"  # Used for started/completed/error events
    invocation = None  # Track invocation object for exception handler
    invocation_obj = None  # Track invocation object for exception handler (alias for clarity)
    stack = None  # Track stack for exception handler
    
    # Initialize variables used in success path to avoid UnboundLocalError
    exec_result = None
    completed_at = None
    execution_duration_ms = None
    final_status = None
    error_summary = None
    artifact_id = None
    # Flag set to True once ToolInvocation and stream events are finalized, so
    # the outer except handler knows not to double-process on re-raise.
    _invocation_finalized = False

    try:
        # Get stack from motet
        stack = motet.stack
        if not stack:
            raise ValueError("Stack not available in motet context")
        
        # Prepare arguments JSON (ADR-0061): keep memory capped; offload full JSON
        # to ArtifactKind.TOOL_ARGUMENTS so provider replay stays unmodified/valid.
        from motet.core.tools.arguments_offload import plan_arguments_storage

        cfg = getattr(stack, "config", None)
        max_args_bytes = int(getattr(cfg, "tool_invocation_arguments_max_bytes", 8192) or 8192) if cfg else 8192

        full_arguments_json = json.dumps(data.parameters)
        arguments_json, arguments_hash, needs_artifact, arguments_truncated = plan_arguments_storage(
            full_arguments_json,
            max_args_bytes,
        )
        arguments_artifact_id: Optional[str] = None
        if needs_artifact:
            arguments_artifact_id = _store_tool_arguments_artifact(
                motet=motet,
                full_arguments_json=full_arguments_json,
                tool_name=data.tool_name,
                tool_call_id=tool_call_id,
                arguments_hash=arguments_hash,
            )
            if not arguments_artifact_id:
                raise RuntimeError(
                    "Failed to store oversized tool arguments artifact; "
                    "refusing to persist truncated/invalid arguments for provider replay"
                )
        
        # Determine provider type
        provider = "mcp" if data.tool_name.startswith("mcp.") else "builtin"
        
        # Create ToolInvocation record (status=started) - ADR-0061
        # Models imported at top level
        
        invocation = ToolInvocation(
            tool_name=data.tool_name,
            tool_call_id=tool_call_id,
            provider=provider,
            arguments_json=arguments_json,
            arguments_hash=arguments_hash,
            arguments_truncated=arguments_truncated,
            arguments_artifact_id=arguments_artifact_id,
            tool_call_group_id=data.tool_call_group_id,
            tool_call_index=data.tool_call_index,
            status=ToolInvocationStatus.STARTED,
            started_at=datetime.now(timezone.utc),
            task_id=motet.task_id,
            command_id=getattr(motet, "command_id", None),
            parent_command_id=getattr(motet, "parent_command_id", None),
            conversation_id=motet.conversation_id,
            tenant_id=motet.tenant_id,
            principal_id=motet.principal_id,
            motet_id=motet.motet_id,
        )
        
        # Persist ToolInvocation before execution (status=started) - ADR-0061
        try:
            _persist_tool_invocation(stack=stack, motet=motet, invocation=invocation)
            invocation_id = invocation.tool_call_id  # Track for update
        except Exception as e:
            logger.warning(
                "tool_invocation_persist_started_failed",
                tool_name=data.tool_name,
                tool_call_id=tool_call_id,
                status="started",
                error=str(e),
            )
        
        # Store invocation object for exception handler
        invocation_obj = invocation
        
        # Stream tool_execution_started event for real-time UI updates
        # Use explicit stream_key from data if provided (from agentic_loop)
        # Use data=json.dumps(...) pattern for consistency with other events
        try:
            stream_key = data.stream_key if data.stream_key else None
            event_data = {
                "tool_name": data.tool_name,
                "tool_call_id": tool_call_id,
            }
            motet.stream_event(
                "tool_execution_started",
                stream_key=stream_key,
                data=json.dumps(event_data),
            )
            tool_use_kind = "mcp" if data.tool_name.startswith("mcp.") else "motet"
            motet.stream_event(
                "tool_use",
                kind=tool_use_kind,
                tool_name=data.tool_name,
                tool_call_id=tool_call_id,
                status="started",
                stream_key=stream_key,
            )
        except Exception as stream_err:
            logger.debug("tool_execution_stream_started_failed", error=str(stream_err))
        
        # Perform parameter injection (last-mile, single injection point - ADR-0046)
        from motet.core.tools.parameter_injection import ParameterInjectionService
        parameter_injector = ParameterInjectionService(registry=stack.tool_registry)
        
        logger.info("tool_execution_injecting_parameters",
                   tool_name=data.tool_name,
                   initial_parameters=data.parameters,
                   principal_id=motet.principal_id,
                   tenant_id=motet.tenant_id)
        
        # Inject parameters using motet context values
        injected_parameters = parameter_injector.inject_parameters(
            tool_name=data.tool_name,
            llm_parameters=data.parameters,
            principal_id=motet.principal_id,
            tenant_id=motet.tenant_id,
            task_id=motet.task_id,
            conversation_id=motet.conversation_id,
            # Future: access_token, api_key from OAuth/Vault
        )
        
        # Update parameters with injected values
        data.parameters = injected_parameters
        logger.info("tool_execution_parameters_injected",
                   tool_name=data.tool_name,
                   final_parameters=data.parameters)
        
        # Execute tool
        exec_error = None
        try:
            # Check if this is an MCP tool that should use Motet MCP
            if data.tool_name.startswith('mcp.'):
                exec_result = _execute_mcp_tool_via_motet(data, motet)
            else:
                exec_result = _execute_regular_tool(data, motet, stack)
        except Exception as e:
            exec_error = e
            exec_result = {
                "error": str(e),
                "error_type": type(e).__name__,
            }
        
        # Determine final status and extract raw result
        completed_at = datetime.now(timezone.utc)
        execution_duration_ms = int((time.time() - started_at) * 1000)
        
        if exec_error:
            final_status = ToolInvocationStatus.ERROR
            error_summary = str(exec_error)[:500]  # Cap error summary
            artifact_id = None
        elif isinstance(exec_result, dict) and exec_result.get("auth_required"):
            final_status = ToolInvocationStatus.AUTH_REQUIRED
            error_summary = None
            artifact_id = None
        else:
            final_status = ToolInvocationStatus.SUCCESS
            error_summary = None
            
            # Store ToolArtifact if policy allows (ADR-0061)
            artifact_id = None
            try:
                tool_info = None
                try:
                    tool_registry = getattr(stack, "tool_registry", None)
                    tool_info = tool_registry.get(data.tool_name) if tool_registry is not None else None
                except Exception:
                    tool_info = None
                
                # Extract raw result for artifact storage
                raw_result: Optional[Dict[str, Any]] = None
                if isinstance(exec_result, dict):
                    nested_result = exec_result.get("result")
                    if isinstance(nested_result, dict):
                        raw_result = nested_result
                    else:
                        raw_result = exec_result
                
                # Store artifact if allowlisted or oversized (history stays clipped).
                # Serialize once and reuse for both the size check and the payload.
                serialized_result: Optional[str] = None
                if raw_result:
                    try:
                        serialized_result = json.dumps(raw_result, default=str)
                    except (TypeError, ValueError):
                        serialized_result = None
                allowlisted_artifact = bool(raw_result) and _should_store_tool_artifact(
                    stack=stack, tool_name=data.tool_name, tool_info=tool_info
                )
                oversized_artifact = (
                    not allowlisted_artifact
                    and serialized_result is not None
                    and _should_store_oversized_tool_result(
                        stack=stack,
                        tool_name=data.tool_name,
                        result_size_bytes=len(serialized_result.encode("utf-8")),
                    )
                )
                if raw_result and (allowlisted_artifact or oversized_artifact):
                    # Determine content type
                    content_type = "application/json"
                    if isinstance(raw_result, dict) and "content_type" in raw_result:
                        content_type = raw_result["content_type"]

                    # Oversized offloads exist for within-cycle artifact_read after
                    # observation clipping — expire them and skip derivations so
                    # frequent large results don't grow Redis unbounded.
                    oversized_ttl: Optional[int] = None
                    if oversized_artifact:
                        cfg = getattr(stack, "config", None)
                        oversized_ttl = int(
                            getattr(cfg, "tool_result_artifact_ttl_seconds", 604800)
                            or 604800
                        )

                    artifact_id = _store_tool_artifact(
                        stack=stack,
                        motet=motet,
                        raw_result=raw_result,
                        tool_name=data.tool_name,
                        tool_call_id=tool_call_id,
                        content_type=content_type,
                        serialized_result=serialized_result,
                        ttl_seconds=oversized_ttl,
                        trigger_derivations=not oversized_artifact,
                    )
            except Exception as e:
                logger.warning("tool_artifact_store_wrapper_failed", tool_name=data.tool_name, error=str(e))
        
        # Update ToolInvocation after execution (status, artifact_id, execution_duration_ms) - ADR-0061
        if invocation_obj:
            try:
                invocation_obj.status = final_status
                invocation_obj.completed_at = completed_at
                invocation_obj.execution_duration_ms = execution_duration_ms
                invocation_obj.error_summary = error_summary
                invocation_obj.artifact_id = artifact_id
                
                # Add preview observation for UI/debugging (capped, properly formatted)
                if isinstance(exec_result, dict):
                    # Extract the actual result value (could be nested)
                    result_value = exec_result.get("result", exec_result)
                    preview = format_tool_result_preview(data.tool_name, result_value, max_chars=200)
                    invocation_obj.preview_observation = preview
                    # ADR-0113: capture artifact-backed media (e.g. generated images) the
                    # tool produced so the transcript can resurface it on history replay.
                    invocation_obj.result_media = _extract_result_media(result_value)
                
                _persist_tool_invocation(stack=stack, motet=motet, invocation=invocation_obj)
            except Exception as e:
                logger.warning(
                    "tool_invocation_persist_completed_failed",
                    tool_name=data.tool_name,
                    tool_call_id=tool_call_id,
                    status=str(final_status.value if hasattr(final_status, "value") else final_status),
                    error=str(e),
                )
        
        # Stream tool_execution_completed event for real-time UI updates
        # Use explicit stream_key from data if provided (from agentic_loop)
        # Use data=json.dumps(...) pattern for consistency with other events
        try:
            # Generate preview for streaming (safe, capped)
            preview = ""
            if isinstance(exec_result, dict):
                result_value = exec_result.get("result", exec_result)
                preview = format_tool_result_preview(data.tool_name, result_value, max_chars=200)
            
            stream_key = data.stream_key if data.stream_key else None
            event_data = {
                "tool_name": data.tool_name,
                "tool_call_id": tool_call_id,
                "status": "success" if final_status == ToolInvocationStatus.SUCCESS else "error",
                "duration_ms": execution_duration_ms,
                "preview": preview,
            }
            motet.stream_event(
                "tool_execution_completed",
                stream_key=stream_key,
                data=json.dumps(event_data),
            )
            tool_use_kind = "mcp" if data.tool_name.startswith("mcp.") else "motet"
            motet.stream_event(
                "tool_use",
                kind=tool_use_kind,
                tool_name=data.tool_name,
                tool_call_id=tool_call_id,
                status="success" if final_status == ToolInvocationStatus.SUCCESS else "error",
                stream_key=stream_key,
            )
        except Exception as stream_err:
            logger.debug("tool_execution_stream_completed_failed", error=str(stream_err))

        # Mark finalized so the outer except handler skips double-processing.
        _invocation_finalized = True

        # Re-raise tool execution errors so the decorator emits a proper ADR-0029
        # status:"error" response.  motet.do()/motet.join() then surface it as a
        # CommandExecutionError via the existing "_error" path, making error handling
        # uniform for every caller without any caller-side boilerplate.
        if exec_error:
            raise exec_error

        # Surface artifact_id so agentic_loop can point clipped observations at
        # the full TOOL_ARTIFACT payload (cost control / ADR-0061).
        if artifact_id:
            if isinstance(exec_result, dict):
                exec_result = dict(exec_result)
                exec_result["artifact_id"] = artifact_id
            else:
                exec_result = {"result": exec_result, "artifact_id": artifact_id}

        return exec_result

    except Exception as e:
        # Update ToolInvocation to error status if we have an invocation object.
        # Skip if _invocation_finalized is True - that means exec_error was re-raised
        # after cleanup was already done above and we must not double-process.
        if not _invocation_finalized and invocation_obj and stack:
            try:
                # datetime and timezone already imported at top level
                invocation_obj.status = ToolInvocationStatus.ERROR
                invocation_obj.completed_at = datetime.now(timezone.utc)
                invocation_obj.execution_duration_ms = int((time.time() - started_at) * 1000)
                invocation_obj.error_summary = str(e)[:500]
                
                _persist_tool_invocation(stack=stack, motet=motet, invocation=invocation_obj)
            except Exception:
                pass  # Best-effort
        
        logger.error("tool_execution_failed",
                    error=str(e),
                    error_type=type(e).__name__,
                    tool_name=data.tool_name,
                    parameters=data.parameters,
                    task_id=motet.task_id,
                    exc_info=True)

        # Only emit stream events if cleanup wasn't already done above; otherwise
        # we'd send tool_execution_failed after tool_execution_completed which
        # confuses the UI.
        if not _invocation_finalized:
            try:
                stream_key = data.stream_key if data.stream_key else None
                event_data = {
                    "tool_name": data.tool_name,
                    "tool_call_id": tool_call_id,
                    "status": "error",
                    "error": str(e)[:500],
                    "duration_ms": int((time.time() - started_at) * 1000),
                }
                motet.stream_event(
                    "tool_execution_failed",
                    stream_key=stream_key,
                    data=json.dumps(event_data),
                )
                tool_use_kind = "mcp" if data.tool_name.startswith("mcp.") else "motet"
                motet.stream_event(
                    "tool_use",
                    kind=tool_use_kind,
                    tool_name=data.tool_name,
                    tool_call_id=tool_call_id,
                    status="error",
                    stream_key=stream_key,
                )
            except Exception:
                pass  # Best-effort, don't fail on streaming errors

        # Raise to let decorator handle error response wrapping
        raise


def _get_mcp_tool_timeout(tool_name: str, service_id: str, motet: MotetContext) -> float:
    """
    Determine appropriate timeout for an MCP tool execution.
    
    Priority:
    1. Tool's default_timeout_seconds from registry (if registered)
    2. Service-specific timeout (Google Workspace: 90s, others: 30s)
    3. Default: 30s
    
    Args:
        tool_name: Full MCP tool name (e.g., "mcp.google_workspace.search_gmail_messages")
        service_id: MCP service ID (e.g., "google_workspace")
        motet: MotetContext for accessing tool registry
    
    Returns:
        Timeout in seconds
    """
    # Try to get timeout from tool registry first
    try:
        if motet.tools:
            tool_info = motet.tools.get(tool_name)
            if tool_info and hasattr(tool_info, 'default_timeout_seconds') and tool_info.default_timeout_seconds:
                logger.debug("tool_execution_using_registry_timeout",
                           tool_name=tool_name,
                           timeout=tool_info.default_timeout_seconds)
                return float(tool_info.default_timeout_seconds)
    except Exception as e:
        logger.debug("tool_execution_timeout_lookup_failed",
                    tool_name=tool_name,
                    error=str(e))
    
    # Service-specific timeouts
    _SERVICE_TIMEOUTS: Dict[str, float] = {
        # Google Workspace API calls can be slow (Drive, Gmail, Sheets)
        "google_workspace": 90.0,
        # Browser automation: cold-start Chromium + page load easily exceeds 30s
        "playwright": 120.0,
        "puppeteer": 120.0,
        "browser": 120.0,
        "selenium": 120.0,
    }
    if service_id in _SERVICE_TIMEOUTS:
        svc_timeout = _SERVICE_TIMEOUTS[service_id]
        logger.debug("tool_execution_using_service_timeout",
                   tool_name=tool_name,
                   service_id=service_id,
                   timeout=svc_timeout)
        return svc_timeout

    # Default timeout for other MCP services
    logger.debug("tool_execution_using_default_timeout",
               tool_name=tool_name,
               timeout=60.0)
    return 60.0


def _execute_mcp_tool_via_motet(data: ToolExecutionData, motet: MotetContext) -> Dict[str, Any]:
    """
    Execute MCP tool using MotetMCPClient (Option 3.5 - Simplified, Synchronous).
    
    Includes credential checking (ADR-0057) - if OAuth credentials are missing,
    returns auth_required status instead of executing the tool.
    """
    logger.info("tool_execution_using_motet_mcp_client",
               tool_name=data.tool_name)
    
    # Parse MCP tool name: mcp.service_id.tool_name
    parts = data.tool_name.split('.', 2)
    if len(parts) != 3 or parts[0] != 'mcp':
        raise ValueError(f"Invalid MCP tool name format: {data.tool_name}")
    
    service_id = parts[1]
    tool_name = parts[2]
    
    logger.debug("tool_execution_mcp_parsed",
                service_id=service_id,
                tool_name=tool_name)
    
    # Check service credentials before execution (ADR-0057)
    logger.debug("tool_execution_checking_auth",
                tool_name=data.tool_name,
                service_id=service_id,
                principal_id=motet.principal_id or "None",
                tenant_id=motet.tenant_id or "None",
                motet_id=motet.motet_id or "None",
                task_id=motet.task_id,
                conversation_id=motet.conversation_id)
    auth_result = _check_mcp_service_auth(service_id, motet)
    if auth_result:
        logger.warning("tool_execution_auth_required",
                        tool_name=data.tool_name,
                        service_id=service_id,
                        principal_id=motet.principal_id or "None",
                        tenant_id=motet.tenant_id or "None",
                        motet_id=motet.motet_id or "None",
                        reason="Token lookup failed - check if principal_id/tenant_id are set correctly")
        return auth_result
    
    params = dict(data.parameters or {})

    # Use service-appropriate timeout (Google Workspace: 90s, others: 30s)
    timeout_seconds = _get_mcp_tool_timeout(data.tool_name, service_id, motet)
    
    # Use MotetMCPClient.call_tool() - synchronous, replaces 300+ lines of manual Redis code
    from motet.core.tools.mcp_motet.client.motet_mcp_client import get_motet_mcp_client
    
    motet_mcp_client = get_motet_mcp_client()
    
    try:
        # Call tool through MotetMCPClient with context (synchronous for Celery)
        result = motet_mcp_client.call_tool(
            service_id=service_id,
            tool_name=tool_name,
            params=params,
            conversation_id=motet.conversation_id,
            task_id=motet.task_id,
            tenant_id=motet.tenant_id,
            principal_id=motet.principal_id,  # Required for USER visibility services like google_workspace
            motet_id=motet.motet_id,  # Required for MOTET visibility services
            target_worker_id=getattr(motet, 'target_worker_id', None),  # Respect worker affinity for stateful services
            timeout_seconds=int(timeout_seconds)
        )
        
        logger.info("tool_execution_motet_mcp_success",
                   tool_name=data.tool_name)
        
        # Check if MCP server returned an auth error in its response (ADR-0057)
        auth_error_result = _check_mcp_result_for_auth_error(result, service_id, motet)
        if auth_error_result:
            return auth_error_result
        
        # Return result (decorator will wrap in ADR-0029 format)
        return {
            'tool_name': data.tool_name,
            'result': result,
            'executed': True,
            'execution_method': 'motet_mcp_client'
        }
        
    except Exception as e:
        logger.error("tool_execution_motet_mcp_failed",
                    error=str(e),
                    tool_name=data.tool_name,
                    exc_info=True)
        # Re-raise to let decorator handle error response
        raise


def _check_mcp_service_auth(service_id: str, motet: MotetContext) -> Optional[Dict[str, Any]]:
    """
    Check if MCP service requires authorization (ADR-0057).
    
    Queries the MCP Instance Manager for the service's auth configuration
    and checks if credentials exist in vault. If OAuth credentials are missing,
    returns an auth_required response for the UI to handle.
    
    Args:
        service_id: MCP service identifier
        motet: MotetContext for context info
        
    Returns:
        None if credentials are present or not required
        Dict with auth_required status if user needs to authorize
    """
    try:
        # Import auth config types
        from motet.core.tools.mcp_motet.proxy.mcp_instance_manager import (
            AuthType, ServiceAuthConfig, CredentialCheckResult
        )
        
        # Get service auth config from vault MCP integration
        from motet.core.security.vault_mcp_integration import get_service_auth_config
        auth_config = get_service_auth_config(service_id)
        
        if not auth_config or auth_config.get("type") == "none":
            # No auth required for this service
            return None
        
        auth_type = auth_config.get("type", "none")
        
        # Only OAuth2 services should prompt user
        if auth_type != "oauth2":
            # api_key and service_account are admin-configured, not user-prompted
            return None
        
        # Check if credentials exist in vault using OAuth manager's per-user lookup
        # This tries keys in order: per-tenant-per-user, per-user, per-tenant, global
        from motet.core.security.oauth_manager import get_oauth_manager
        from motet.core.utils.async_helpers import run_async_safe
        
        oauth_manager = get_oauth_manager()
        
        # Extract principal_id/tenant_id/motet_id from motet context for detailed logging
        principal_id = motet.principal_id
        tenant_id = motet.tenant_id
        motet_id = motet.motet_id
        
        # Detailed logging to diagnose context isolation issues
        logger.info("_check_mcp_service_auth: MotetContext values",
                   service_id=service_id,
                   motet_principal_id=principal_id or "None/empty",
                   motet_tenant_id=tenant_id or "None/empty",
                   motet_motet_id=motet_id or "None/empty",
                   motet_principal_id_type=type(principal_id).__name__ if principal_id else "None",
                   motet_tenant_id_type=type(tenant_id).__name__ if tenant_id else "None",
                   motet_motet_id_type=type(motet_id).__name__ if motet_id else "None",
                   motet_has_command=bool(motet._command) if hasattr(motet, "_command") else False,
                   motet_command_has_distributed_context=bool(motet._command.distributed_context) if (hasattr(motet, "_command") and motet._command) else False,
                   distributed_context_principal_id=motet._command.distributed_context.principal_id if (hasattr(motet, "_command") and motet._command and motet._command.distributed_context) else "N/A",
                   distributed_context_tenant_id=motet._command.distributed_context.tenant_id if (hasattr(motet, "_command") and motet._command and motet._command.distributed_context) else "N/A",
                   distributed_context_motet_id=motet._command.distributed_context.motet_id if (hasattr(motet, "_command") and motet._command and motet._command.distributed_context) else "N/A")
        
        # Use the OAuth manager's get_tokens which has per-user fallback logic
        # Use run_async_safe for pool-aware async execution (ADR-0033: works on gevent/eventlet/fork/threads)
        try:
            logger.info("_check_mcp_service_auth: Calling oauth_manager.get_tokens",
                       service_id=service_id,
                       principal_id=principal_id if principal_id else None,
                       tenant_id=tenant_id if tenant_id else None,
                       motet_id=motet_id if motet_id else None,
                       principal_id_is_none=principal_id is None,
                       principal_id_is_empty=principal_id == "",
                       tenant_id_is_none=tenant_id is None,
                       tenant_id_is_empty=tenant_id == "",
                       motet_id_is_none=motet_id is None,
                       motet_id_is_empty=motet_id == "")
            credential_data = run_async_safe(
                oauth_manager.get_tokens(
                    server_id=service_id,
                    principal_id=principal_id if principal_id else None,
                    tenant_id=tenant_id if tenant_id else None,
                    motet_id=motet_id if motet_id else None
                )
            )
            logger.info("OAuth token check result",
                       service_id=service_id,
                       found=credential_data is not None,
                       has_access_token=bool(credential_data.get("access_token")) if credential_data else False)
        except Exception as e:
            logger.warning("Failed to check OAuth tokens",
                         service_id=service_id,
                         principal_id=motet.principal_id,
                         tenant_id=motet.tenant_id,
                         motet_id=motet.motet_id,
                         error=str(e),
                         exc_info=True)
            credential_data = None
        
        if credential_data:
            # Credentials exist, proceed with execution
            token_field = auth_config.get("token_field", "access_token")
            if credential_data.get(token_field):
                logger.debug("OAuth credentials found for service",
                           service_id=service_id,
                           principal_id=motet.principal_id,
                           tenant_id=motet.tenant_id)
                return None
        
        # Credentials missing - return auth_required response
        logger.info("mcp_service_auth_required",
                   service_id=service_id,
                   principal_id=motet.principal_id,
                   tenant_id=motet.tenant_id)
        
        # Get OAuth URLs from config (must be explicitly configured in YAML)
        provider = auth_config.get("provider", service_id)
        auth_url = auth_config.get("auth_url")
        if not auth_url:
            logger.warning("OAuth auth_url not configured in YAML",
                         service_id=service_id,
                         provider=provider)
            # Return auth_required anyway, but log the missing config
        
        display_name = auth_config.get("display_name") or _get_service_display_name(service_id)
        
        # Return properly formatted auth_required response
        # Note: Do NOT include 'status' key at top level - decorator checks for that
        # and would return it as-is without proper ADR-0029 wrapping
        return {
            'tool_name': f"mcp.{service_id}.*",
            'auth_required': True,  # Use auth_required flag instead of status
            'service_id': service_id,
            'provider': provider,
            'display_name': display_name,
            'message': f"{display_name} requires authorization to continue.",
            'authorization_endpoint': f"/api/v1/oauth/{service_id}/initiate",
            'auth_url': auth_url,
            'required_scopes': auth_config.get("scopes", []),
            'executed': False,
            'execution_method': 'auth_required'
        }
        
    except Exception as e:
        # Log error but don't block execution - let it fail naturally
        logger.warning("mcp_service_auth_check_failed",
                      service_id=service_id,
                      error=str(e))
        return None


def _get_service_display_name(service_id: str) -> str:
    """
    Get user-friendly display name for a service from YAML config (ADR-0057 Phase 4).
    
    Falls back to formatted service_id if not found in config.
    """
    # Try to get from auth config first
    from motet.core.security.vault_mcp_integration import get_service_auth_config
    
    auth_config = get_service_auth_config(service_id)
    if auth_config and auth_config.get("display_name"):
        return auth_config["display_name"]
    
    # Fallback to formatted service_id
    return service_id.replace("_", " ").replace("-", " ").title()


def _check_mcp_result_for_auth_error(
    result: Any,
    service_id: str,
    motet: MotetContext
) -> Optional[Dict[str, Any]]:
    """
    Check if MCP server returned an authentication error in its response (ADR-0057).
    
    MCP servers may return auth errors in their response content even if the
    tool call technically succeeded. This function detects these errors and
    converts them to auth_required responses.
    
    Common auth error patterns:
    - Google Workspace: "Authentication error occurred for {session_id}"
    - GitHub: "Bad credentials" or "401 Unauthorized"
    - Slack: "invalid_auth" or "not_authed"
    
    Args:
        result: MCP tool result (may have content array with error messages)
        service_id: MCP service identifier
        motet: MotetContext for context info
        
    Returns:
        None if no auth error detected
        Dict with auth_required status if auth error found
    """
    if not result:
        return None
    
    # Check for isError flag in result
    is_error = False
    error_text = ""
    
    if isinstance(result, dict):
        is_error = result.get("isError", False)
        
        # Extract error text from content array
        content = result.get("content", [])
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text = item.get("text", "")
                    if text:
                        error_text = text
                        break
        elif isinstance(content, str):
            error_text = content
    
    if not is_error or not error_text:
        return None
    
    # Check for auth-related error patterns
    auth_error_patterns = [
        # Google Workspace patterns
        "authentication error",
        "please try running `start_google_auth`",
        "reauthenticate",
        "token has been expired or revoked",
        "invalid_grant",
        "unauthorized",
        # GitHub patterns
        "bad credentials",
        "401 unauthorized",
        "requires authentication",
        # Slack patterns
        "invalid_auth",
        "not_authed",
        "token_revoked",
        # Generic patterns
        "access denied",
        "permission denied",
        "auth failed",
        "authentication failed",
        "authorization failed",
    ]
    
    error_lower = error_text.lower()
    is_auth_error = any(pattern in error_lower for pattern in auth_error_patterns)
    
    if not is_auth_error:
        return None
    
    logger.info("mcp_result_auth_error_detected",
               service_id=service_id,
               error_text=error_text[:200])
    
    # Get auth config for the service
    from motet.core.security.vault_mcp_integration import get_service_auth_config
    
    auth_config = get_service_auth_config(service_id) or {}
    
    # Only return auth_required for OAuth2 services
    auth_type = auth_config.get("type", "none")
    if auth_type != "oauth2":
        # Not an OAuth service - can't prompt user to authorize
        return None
    
    # Build auth_required response (auth_url must be explicitly configured in YAML)
    provider = auth_config.get("provider", service_id)
    auth_url = auth_config.get("auth_url")
    if not auth_url:
        logger.warning("OAuth auth_url not configured in YAML",
                     service_id=service_id,
                     provider=provider)
        # Return auth_required anyway, but log the missing config
    
    display_name = auth_config.get("display_name") or _get_service_display_name(service_id)
    
    # Return properly formatted auth_required response
    # Note: Do NOT include 'status' key at top level - decorator checks for that
    # and would return it as-is without proper ADR-0029 wrapping
    return {
        'tool_name': f"mcp.{service_id}.*",
        'auth_required': True,  # Use auth_required flag instead of status
        'service_id': service_id,
        'provider': provider,
        'display_name': display_name,
        'message': f"{display_name} authentication has expired or been revoked. Please re-authorize to continue.",
        'authorization_endpoint': f"/api/v1/oauth/{service_id}/initiate",
        'auth_url': auth_url,
        'required_scopes': auth_config.get("scopes", []),
        'original_error': error_text[:500],  # Include original error for debugging
        'executed': False,
        'execution_method': 'auth_required'
    }


def _execute_regular_tool(data: ToolExecutionData, motet: MotetContext, stack: Any) -> Dict[str, Any]:
    """Execute tool using the traditional tool registry approach."""
    try:
        tool_registry = getattr(stack, 'tool_registry', None)
        if not tool_registry:
            raise ValueError("Tool registry not available in stack")
        
        logger.debug("tool_execution_registry_info",
                    registry_tool_count=len(tool_registry.list_items()),
                    tool_name=data.tool_name)
        
        # Get the tool from registry
        tool_info = tool_registry.get(data.tool_name)
        if not tool_info:
            available_tools = list(tool_registry.list_items().keys())
            raise ValueError(f"Tool '{data.tool_name}' not found in registry. Available: {available_tools[:10]}...")
        
        # MotetContext is automatically available via WorkerLocal (ADR-0033)
        # Tools can access it using get_motet_context() from decorator module
        # No need to manually set/clear - WorkerLocal handles isolation
        
        # Set runtime stack for built-in tools to access context
        # Built-in tools access principal_id/tenant_id/motet_id/conversation_id via get_runtime_stack() (thread-safe via WorkerLocal)
        from motet.core.tools.registry import set_runtime_stack
        setattr(stack, "_conversation_id", motet.conversation_id or "")
        setattr(stack, "_task_id", motet.task_id or "")
        from motet.core.commands.command_data_classes import SCHEDULE_CONTEXT_KEYS
        metadata = getattr(motet, "metadata", {}) or {}
        for key in SCHEDULE_CONTEXT_KEYS:
            val = metadata.get(key)
            if key == "principal_roles":
                setattr(stack, f"_{key}", val or [])
            elif key == "enable_thinking":
                setattr(stack, f"_{key}", val)
            else:
                setattr(stack, f"_{key}", val or "")
        set_runtime_stack(stack)
        
        # Execute the tool directly through the registry
        logger.info("tool_execution_executing_regular_tool",
                   tool_name=data.tool_name,
                   parameters=data.parameters,
                   motet_id=motet.motet_id,
                   tenant_id=motet.tenant_id,
                   conversation_id=motet.conversation_id)
        
        try:
            result = tool_registry._execute_tool_only(
                name=data.tool_name,
                params=data.parameters,
                timeout=30,
                persist_observation=False,
            )
        finally:
            set_runtime_stack(None)
        
        logger.info("tool_execution_regular_tool_success",
                   tool_name=data.tool_name)
        
        # Return result (decorator will wrap in ADR-0029 format)
        return {
            'tool_name': data.tool_name,
            'result': result,
            'executed': True
        }
        
    except Exception as e:
        logger.error("tool_execution_regular_tool_failed",
                    error=str(e),
                    tool_name=data.tool_name,
                    exc_info=True)
        raise


@motet.command(
    description="List tools available on this worker from the tool registry, with names, categories, and descriptions.",
    timeout_seconds=30,
    required_capabilities=[WorkerCapability.TOOL_EXECUTION]
)
def tool_list(data: ToolListData) -> Dict[str, Any]:
    """
    List all available tools from the worker's tool registry.
    
    This ensures tool listing happens in the worker process that has access
    to the shared memory tool registry with all registered tools (including MCP tools).
    
    Args:
        data: ToolListData (empty, no parameters needed)
        motet: MotetContext for resource access
    
    Returns:
        Dict with list of all available tools and their metadata
    """

    motet = get_motet_context()

    try:
        stack = motet.stack
        if not stack:
            raise ValueError("Stack not available in motet context")
        
        # Get the worker's tool registry (should have all tools including MCP)
        tool_registry = getattr(stack, 'tool_registry', None)
        if not tool_registry:
            raise ValueError("Tool registry not available in stack")
        
        logger.info("tool_list_registry_info",
                   registry_tool_count=len(tool_registry.list_items()))
        
        # Get all tools from the registry
        all_tools = tool_registry.list_items()
        
        # Convert tools to serializable format
        serialized_tools = []
        for tool_name, tool_info in all_tools.items():
            # Handle schema serialization - convert Pydantic models to dicts
            schema = None
            if hasattr(tool_info, 'tool_schema') and tool_info.tool_schema is not None:
                try:
                    # If it's a Pydantic model, convert to dict
                    if hasattr(tool_info.tool_schema, 'model_dump'):
                        # Pydantic model class - create an instance and convert to dict
                        try:
                            # Try to create an instance with empty parameters
                            instance = tool_info.tool_schema()
                            schema = instance.model_dump(mode='json')
                        except Exception:
                            # If that fails, try to get the schema from the model
                            if hasattr(tool_info.tool_schema, 'model_json_schema'):
                                schema = tool_info.tool_schema.model_json_schema()
                            else:
                                schema = None
                    elif hasattr(tool_info.tool_schema, 'dict'):
                        schema = tool_info.tool_schema.dict()
                    elif isinstance(tool_info.tool_schema, dict):
                        schema = tool_info.tool_schema
                    else:
                        # Try to convert to dict using json.dumps/json.loads
                        schema = json.loads(json.dumps(tool_info.tool_schema, default=str))
                except Exception as e:
                    logger.warning("tool_list_schema_serialization_failed",
                                  tool_name=tool_name,
                                  error=str(e))
                    schema = None
            
            tool_data = {
                'name': tool_name,
                'description': tool_info.description if hasattr(tool_info, 'description') else '',
                'schema': schema,
                'category': tool_info.category if hasattr(tool_info, 'category') else 'unknown',
                'keywords': tool_info.keywords if hasattr(tool_info, 'keywords') else [],
                'data_types': tool_info.data_types if hasattr(tool_info, 'data_types') else [],
                'priority': tool_info.priority if hasattr(tool_info, 'priority') else 10,
                'cost_class': tool_info.cost_class if hasattr(tool_info, 'cost_class') else 'medium'
            }
            serialized_tools.append(tool_data)
        
        logger.info("tool_list_success",
                   total_tools=len(serialized_tools))
        
        return {
            'tools': serialized_tools,
            'total_tools': len(serialized_tools)
        }
        
    except Exception as e:
        logger.error("tool_list_failed",
                    error=str(e),
                    error_type=type(e).__name__,
                    exc_info=True)
        raise


@motet.command(
    description="Semantically discover relevant tools for a natural-language query using hybrid embedding and keyword search.",
    timeout_seconds=30,
    required_capabilities=[WorkerCapability.TOOL_EXECUTION]
)
def tool_discovery(data: ToolDiscoveryData) -> Dict[str, Any]:
    """
    Discover relevant tools for a query via embedding search (ADR-0051 / ADR-0074).

    Runs on a worker that holds the tool registry and FunctionDiscoveryVectorStore.
    Returns ranked ToolCandidate payloads only — no native function-calling path.

    Args:
        data: ToolDiscoveryData with content, context_type, max_tools, etc.

    Returns:
        Dict with serializable candidates, registry_tool_count, and discovery_context.
        Registered as core.tool_discovery / tool_discovery.
    """

    motet = get_motet_context()

    try:
        stack = motet.stack
        if not stack:
            raise ValueError("Stack not available in motet context")

        # Worker registry (built-in + MCP tools) for optional candidate enrichment
        tool_registry = getattr(stack, 'tool_registry', None)
        if not tool_registry:
            raise ValueError("Tool registry not available in stack")

        logger.info("tool_discovery_starting",
                   registry_tool_count=len(tool_registry.list_items()),
                   content=data.content,
                   max_tools=data.max_tools)

        # Normalize history for the vector store (BaseCommandData may already yield Messages)
        conversation_messages: List[Message] = []
        if data.conversation_history:
            for msg_data in data.conversation_history:
                if isinstance(msg_data, Message):
                    conversation_messages.append(msg_data)
                elif isinstance(msg_data, dict):
                    conversation_messages.append(Message(
                        role=msg_data.get('role', 'user'),
                        content=msg_data.get('content', '')
                    ))

        function_discovery_store = getattr(motet, 'function_discovery_store', None)
        discovery_service = ToolDiscoveryService(
            tool_registry=tool_registry,
            function_discovery_store=function_discovery_store,
        )

        candidates: List[ToolCandidate] = discovery_service.discover_tools(
            content=data.content,
            context_type=data.context_type,
            max_tools=data.max_tools,
            conversation_history=conversation_messages or None,
        )

        logger.info("tool_discovery_completed",
                   candidates_found=len(candidates),
                   content=data.content,
                   discovery_method="embedding")

        # Convert candidates to serializable format (omit full RegisteredTool objects)
        serialized_candidates: List[Dict[str, Any]] = []
        for candidate in candidates:
            candidate_data: Dict[str, Any] = {
                'name': candidate.name,
                'parameters': candidate.parameters,
                'confidence': candidate.confidence,
                'reasoning': candidate.reasoning,
                'discovery_method': candidate.discovery_method,
                'tool_info': None,
            }

            if candidate.tool_info is not None:
                candidate_data['tool_info'] = {
                    'name': getattr(candidate.tool_info, 'name', candidate.name),
                    'description': getattr(candidate.tool_info, 'description', ''),
                    'category': getattr(candidate.tool_info, 'category', None),
                }

            serialized_candidates.append(candidate_data)

        context_value = (
            data.context_type.value
            if hasattr(data.context_type, 'value')
            else str(data.context_type)
        )

        return {
            'candidates': serialized_candidates,
            'registry_tool_count': len(tool_registry.list_items()),
            'discovery_context': context_value,
            'discovery_method': 'embedding',
        }

    except Exception as e:
        logger.error("tool_discovery_failed",
                    error=str(e),
                    error_type=type(e).__name__,
                    content=data.content,
                    exc_info=True)
        raise



__all__ = [
    # Decorated commands (ADR-0030)
    "tool_execution",
    "tool_list",
    "tool_discovery",
    # Data classes
    "ToolExecutionData",
    "ToolListData",
    "ToolDiscoveryData",
]
