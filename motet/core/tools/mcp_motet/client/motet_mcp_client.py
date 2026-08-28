"""
Motet - Motet MCP Client

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-20

Description:
    Simplified distributed MCP client for Celery workers using Motet Streams in the Motet
    distributed framework. Provides synchronous tool calling interface compatible with
    existing LibTmuxMCPManager API. Includes comprehensive tool execution and
    schema discovery with distributed coordination.
    This client is sync-only by design (no async public API in this class).

Dependencies:
    - time: Timestamp and timeout management
    - uuid: Unique identifier generation
    - structlog: Structured logging and observability
    - typing: Type hints and annotations
    - MCP protocol and stream operations

Usage:
    from motet.core.tools.mcp_motet.client.motet_mcp_client import MotetMCPClient

    # Create client
    client = MotetMCPClient(service_id="weather", context_id="user123")

    # List tools
    tools = client.list_tools()

    # Execute tool
    result = client.call_tool("get_weather", {"location": "NYC"})

Notes:
    - Provides simplified distributed MCP client for Celery workers
    - Includes synchronous tool calling interface
    - Supports Motet Streams for distributed coordination
    - Includes comprehensive tool execution and schema discovery
    - Supports timeout handling and error management
    - Integrates with MCP protocol and stream operations
    - Includes comprehensive observability and logging
"""

import os
import time
import uuid
import structlog
from typing import Any, Dict, Optional, cast
import yaml
from pathlib import Path
from ....workers.concurrency_primitives import worker_sleep

from motet.core.security.encrypted_stream_codec import (
    encode_encrypted_message_data,
)
from motet.core.security.encryption_service import EncryptionError
from motet.core.tools.mcp_motet.stream_encryption import (
    decode_mcp_stream_fields,
    encode_mcp_stream_fields,
    should_purge_on_kek_mismatch,
)

from motet.core.tools.mcp_motet.protocol import (
    MCPRequestMessage,
    MCPResponseMessage,
    StreamType,
    Visibility,
    LifecycleDuration,
    generate_instance_key,
    generate_stream_name,
    manager_id_from_stream_name,
    mcp_io_stream_scan_patterns,
    parse_stream_name,
    resolve_visibility_and_lifecycle,
)

logger = structlog.get_logger(__name__)


def _resolve_mcp_routing_prefix(target_worker_id: Optional[str] = None) -> Optional[str]:
    """Resolve the MCP bus routing prefix (ADR-0105 §R2).

    Under ADR-0105 the MCP request/response stream prefix is the
    **manager_id** (the sibling MCPInstanceManager process serving this
    worker), NOT the worker_id. ``target_worker_id`` here historically
    meant "send this MCP request to a specific worker's stream"; in the
    sibling-manager world that semantic no longer applies — MCP routing
    is manager-keyed and worker affinity is enforced separately by the
    Celery command bus.

    Order of precedence:

    1. ``MOTET_MCP_MANAGER_ID`` env var (canonical: matches the sibling
       manager service env in docker compose / k8s template).
    2. ``Config.mcp_manager_id`` (typed-config equivalent of #1).
    3. ``target_worker_id`` (legacy back-compat: only consulted when no
       manager_id is configured, e.g. single-process tests or pre-0105
       paths where the worker still owned its own MCP manager).
    4. Auto-detected current worker_id (single-process tests / pre-0105
       paths).
    """
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

    if target_worker_id:
        return target_worker_id

    try:
        from motet.core.workers.worker_utils import get_worker_id

        return get_worker_id()
    except (ImportError, AttributeError):
        return None


def _purge_worker_mcp_streams(redis_client: Any, worker_id: Optional[str], reason: str) -> int:
    """Purge MCP streams for a worker after a KEK mismatch."""
    if not worker_id:
        return 0
    deleted = 0
    seen: set[str] = set()
    for pattern in mcp_io_stream_scan_patterns(worker_id):
        cursor = 0
        while True:
            cursor, keys = redis_client.scan(cursor=cursor, match=pattern, count=500)
            if keys:
                new_keys = [key for key in keys if key not in seen]
                if new_keys:
                    deleted += redis_client.delete(*new_keys)
                    seen.update(new_keys)
            if cursor == 0:
                break
    logger.warning(
        "mcp_streams_purged_on_kek_mismatch",
        worker_id=worker_id,
        deleted_streams=deleted,
        reason=reason,
    )
    return deleted


class MotetMCPClient:
    """
    Synchronous Motet MCP client for Celery workers.
    
    This is a thin client that publishes tool call requests to Motet Streams
    and waits for responses. All instance management (proxy creation, lifecycle,
    health monitoring) is handled by MCPInstanceManager running in the parent
    Celery worker process.
    
    Key features:
    - Synchronous API suitable for Celery worker tasks
    - Worker-affinity routing for stateful services
    - Context-aware stream naming (shared/conversation/task/tenant)
    - Compatible with existing LibTmuxMCPManager API

    Note:
    - This class is intentionally synchronous. Async callers should invoke these
      methods via an executor (as done by StdioMCPTransport).
    """
    
    def __init__(self, manager_id: Optional[str] = None):
        self.manager_id = manager_id or f"mcp-manager-{str(uuid.uuid4())[:8]}"
        self._request_timeout_seconds = 30
        self._pending_requests: Dict[str, Dict[str, Any]] = {}
        # Cache of service scoping config loaded from mcp_instance_manager.yaml (ADR-0058).
        # This ensures stream naming matches configured visibility/lifecycle rather than
        # heuristic inference from presence of conversation_id.
        self._service_scope_cache: Dict[str, Dict[str, Any]] = {}
        self._service_scope_cache_loaded_at: float = 0.0
        
        # Statistics
        self.stats = {
            "requests_processed": 0,
            "errors": 0,
            "start_time": time.time()
        }

    def _load_service_scope_from_yaml(self, service_id: str) -> Optional[Dict[str, Any]]:
        """
        Load service scope config (visibility + lifecycle_duration) from YAML (ADR-0058).

        If config cannot be loaded, returns None and callers may fall back to heuristics.
        """
        # Reload at most every 60s to allow config reloads without restart.
        now = time.time()
        if self._service_scope_cache and (now - self._service_scope_cache_loaded_at) < 60.0:
            return self._service_scope_cache.get(service_id)

        config_path = os.getenv("MCP_INSTANCE_MANAGER_CONFIG", "config/mcp_instance_manager.yaml")
        config_file = Path(config_path)
        config_exists = config_file.exists()
        try:
            with open(config_path, "r") as f:
                config_data = yaml.safe_load(f) or {}
        except Exception as e:
            # Fail fast when config exists but is unreadable; fallback would create
            # heuristic scope mismatches that ADR-0058 explicitly aims to prevent.
            if config_exists:
                raise RuntimeError(
                    f"MCP config exists but could not be loaded: {config_path} ({e})"
                ) from e
            logger.debug(
                "mcp_client_config_not_found_or_unreadable",
                config_path=config_path,
                error=str(e),
            )
            return None

        services = config_data.get("services") or []
        scope_cache: Dict[str, Dict[str, Any]] = {}
        try:
            for svc in services:
                sid = (svc or {}).get("service_id")
                if not sid:
                    continue
                visibility_raw = (svc or {}).get("visibility", "global")
                lifecycle_raw = (svc or {}).get("lifecycle_duration", "permanent")
                try:
                    scope_cache[sid] = {
                        "visibility": Visibility(visibility_raw),
                        "lifecycle": LifecycleDuration(lifecycle_raw),
                    }
                except Exception:
                    # If YAML has invalid values, skip caching for that service.
                    continue
        finally:
            self._service_scope_cache = scope_cache
            self._service_scope_cache_loaded_at = now

        scope = self._service_scope_cache.get(service_id)
        if scope is None and config_exists:
            # Fail fast when the config exists but is missing the service. Otherwise
            # we will fall back to heuristic visibility/lifecycle and reintroduce
            # stream/proxy mismatches under multi-conversation usage.
            raise KeyError(
                f"Service {service_id} not found in MCP config: {config_path}"
            )
        return scope
    
    def _refresh_stream_ttl(self, redis_client, stream_name: str) -> None:
        """
        Refresh TTL for a stream based on lifecycle (ADR-0058).
        Keeps active streams alive while allowing inactive ones to expire.
        """
        try:
            parsed = parse_stream_name(stream_name)
            if parsed:
                lifecycle_value = parsed.get("lifecycle") or parsed.get("lifecycle_duration")
                lifecycle = None
                if lifecycle_value:
                    try:
                        lifecycle = LifecycleDuration(lifecycle_value)
                    except Exception:
                        lifecycle = None

                ttl_by_lifecycle = {
                    LifecycleDuration.IDLE_TIMEOUT: 1800,
                    LifecycleDuration.SESSION: 3600,
                    LifecycleDuration.CONVERSATION: 86400,
                    LifecycleDuration.TASK: 3600,
                    LifecycleDuration.PERMANENT: None,
                }
                ttl_seconds: Optional[int] = None
                if lifecycle is not None:
                    ttl_seconds = ttl_by_lifecycle.get(lifecycle)

                if ttl_seconds is not None:
                    redis_client.expire(stream_name, ttl_seconds)
                    logger.debug(
                        "Refreshed stream TTL",
                        stream_name=stream_name,
                        lifecycle=lifecycle.value if lifecycle else None,
                        ttl_seconds=ttl_seconds,
                    )
        except Exception as e:
            # Don't fail the request if TTL refresh fails
            logger.warning(
                "Failed to refresh stream TTL", stream_name=stream_name, error=str(e)
            )
    
    def call_tool(self, service_id: str, tool_name: str, params: Dict[str, Any],
                  conversation_id: Optional[str] = None,
                  task_id: Optional[str] = None,
                  tenant_id: Optional[str] = None,
                  principal_id: Optional[str] = None,
                  motet_id: Optional[str] = None,
                  target_worker_id: Optional[str] = None,
                  timeout_seconds: Optional[int] = None,
                  command_context: Optional[Any] = None) -> Dict[str, Any]:
        """
        Call a tool on an MCP service (synchronous for Celery workers).
        
        This method provides compatibility with the existing LibTmuxMCPManager API
        while using Motet Streams for communication. Uses synchronous Redis operations
        suitable for Celery worker context.
        
        Args:
            service_id: ID of the service
            tool_name: Name of the tool to call
            params: Tool parameters
            conversation_id: Optional conversation context
            task_id: Optional task context
            tenant_id: Optional tenant context
            target_worker_id: Optional worker to target (defaults to current worker)
            timeout_seconds: Optional timeout override
            
        Returns:
            Tool result dictionary
            
        Raises:
            ValueError: If service is not registered
            RuntimeError: If tool call fails
            TimeoutError: If request times out
        """
        from motet.core.distributed.redis_manager import get_sync_redis_client
        import json
        
        try:
            # ADR-0105 §R2: stream prefix is the manager_id (when configured),
            # not the worker_id. ``worker_id`` here is retained as a telemetry
            # / log-label local that records which worker actually issued this
            # call.
            routing_prefix = _resolve_mcp_routing_prefix(target_worker_id)
            try:
                from motet.core.workers.worker_utils import get_worker_id

                worker_id = get_worker_id()
            except (ImportError, AttributeError):
                worker_id = target_worker_id

            # Extract CommandContext fields for vault credential lookup and context routing
            context_tenant_id = None
            context_motet_id = None
            
            if command_context:
                try:
                    principal_id = principal_id or getattr(command_context, 'principal_id', None)
                    context_tenant_id = getattr(command_context, 'tenant_id', None)
                    context_motet_id = getattr(command_context, 'motet_id', None)
                except Exception as e:
                    logger.warning("Failed to extract CommandContext fields", error=str(e))
            
            logger.info("MotetMCPClient.call_tool (sync)",
                       service_id=service_id,
                       tool_name=tool_name,
                       conversation_id=conversation_id,
                       principal_id=principal_id,
                       motet_id=motet_id,
                       worker_id=worker_id)
            
            # Determine scope IDs (normalize empty strings to fall back correctly)
            scope_tenant = tenant_id or context_tenant_id or os.getenv("MOTET_TENANT_ID", "default")
            effective_motet_id = motet_id or context_motet_id or os.getenv("MOTET_MOTET_ID", "default")
            if not scope_tenant:
                scope_tenant = os.getenv("MOTET_TENANT_ID", "default")
            if not effective_motet_id:
                effective_motet_id = os.getenv("MOTET_MOTET_ID", "default")

            # Determine visibility/lifecycle from YAML config (ADR-0058). This prevents
            # the presence of conversation_id from implicitly switching lifecycle to CONVERSATION.
            configured_scope = self._load_service_scope_from_yaml(service_id)
            if not configured_scope:
                # ADR-0058: Scope must be config-authoritative. Heuristic fallback can
                # silently produce stream/proxy mismatches (e.g., conversation_id present
                # ⇒ CONVERSATION lifecycle) and reintroduce hard-to-debug timeouts.
                raise RuntimeError(
                    f"Missing configured visibility/lifecycle for MCP service {service_id}. "
                    f"Ensure it exists in MCP_INSTANCE_MANAGER_CONFIG and is readable."
                )
            else:
                effective_visibility = configured_scope["visibility"]
                effective_lifecycle = configured_scope["lifecycle"]
            
            # Generate instance key
            if effective_lifecycle == LifecycleDuration.SESSION:
                raise ValueError(
                    f"Service {service_id} is configured with SESSION lifecycle but MotetMCPClient.call_tool() "
                    f"does not receive a session_id. Configure a different lifecycle_duration or extend the client API."
                )
            instance_key = generate_instance_key(
                service_id=service_id,
                visibility=effective_visibility,
                lifecycle_duration=effective_lifecycle,
                motet_id=effective_motet_id,
                tenant_id=scope_tenant,
                principal_id=principal_id,
                conversation_id=conversation_id,
                task_id=task_id,
            )
            context_id = instance_key
            visibility = effective_visibility
            lifecycle = effective_lifecycle
            
            request_stream = generate_stream_name(
                service_id=service_id,
                visibility=visibility,
                instance_key=instance_key,
                stream_type=StreamType.REQUESTS,
                manager_id=routing_prefix,
            )
            response_stream = generate_stream_name(
                service_id=service_id,
                visibility=visibility,
                instance_key=instance_key,
                stream_type=StreamType.RESPONSES,
                manager_id=routing_prefix,
            )

            # Create request message
            request_id = str(uuid.uuid4())
            timeout_ms = (timeout_seconds or self._request_timeout_seconds) * 1000

            request_msg = MCPRequestMessage(
                id=request_id,
                service_id=service_id,
                instance_key=context_id,
                worker_id=self.manager_id,
                timeout_ms=timeout_ms,
                principal_id=principal_id,
                tenant_id=scope_tenant,  # Use scope_tenant which has proper fallback (tenant_id -> context_tenant_id -> env)
                motet_id=effective_motet_id,  # Use effective_motet_id for consistency
                jsonrpc_request={
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "tools/call",
                    "params": {
                        "name": tool_name,
                        "arguments": params
                    }
                }
            )
            
            # Track the request
            self._pending_requests[request_id] = {
                "start_time": time.time(),
                "timeout_seconds": timeout_seconds or self._request_timeout_seconds,
                "service_id": service_id,
                "tool_name": tool_name,
                "response_stream": response_stream
            }
            
            # Get sync Redis client
            redis_client = get_sync_redis_client(f"motet_mcp_manager_{self.manager_id}")
            
            # Publish request using sync Redis
            if not scope_tenant:
                raise ValueError("tenant_id is required for MCP stream encryption")
            if not effective_motet_id:
                raise ValueError("motet_id is required for MCP stream encryption")

            request_data = encode_mcp_stream_fields(
                stream_name=request_stream,
                message=request_msg,
                tenant_id=str(scope_tenant),
                motet_id=str(effective_motet_id),
                message_type=request_msg.stream_type.value,
            )
            
            self._prepare_response_group(request_id, response_stream, redis_client)
            message_id = redis_client.xadd(request_stream, cast(Any, request_data))
            
            # Refresh TTL to keep active streams alive
            self._refresh_stream_ttl(redis_client, request_stream)
            
            logger.info("Published request to Motet Stream",
                       request_id=request_id,
                       stream=request_stream,
                       message_id=message_id)
            
            # Wait for response (synchronous)
            response = self._wait_for_response_sync(request_id, response_stream, timeout_ms, redis_client)
            
            self.stats["requests_processed"] += 1
            
            return response
            
        except Exception as e:
            self.stats["errors"] += 1
            logger.error("Tool call failed",
                        manager_id=self.manager_id,
                        service_id=service_id,
                        tool_name=tool_name,
                        error=str(e),
                        exc_info=True)
            raise

    def _send_mcp_request(
        self,
        service_id: str,
        method: str,
        params: Dict[str, Any],
        conversation_id: Optional[str] = None,
        task_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        principal_id: Optional[str] = None,
        motet_id: Optional[str] = None,
        target_worker_id: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
        command_context: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """
        Send a generic MCP JSON-RPC request and return the result (ADR-0076 Scope 3).
        Used by list_resources, read_resource, list_prompts, get_prompt.
        """
        from motet.core.distributed.redis_manager import get_sync_redis_client
        context_tenant_id = None
        context_motet_id = None
        if command_context:
            try:
                principal_id = principal_id or getattr(command_context, "principal_id", None)
                context_tenant_id = getattr(command_context, "tenant_id", None)
                context_motet_id = getattr(command_context, "motet_id", None)
            except Exception as e:
                logger.warning("Failed to extract CommandContext fields", error=str(e))
        scope_tenant = tenant_id or context_tenant_id or os.getenv("MOTET_TENANT_ID", "default")
        effective_motet_id = motet_id or context_motet_id or os.getenv("MOTET_MOTET_ID", "default")
        if not scope_tenant:
            scope_tenant = os.getenv("MOTET_TENANT_ID", "default")
        if not effective_motet_id:
            effective_motet_id = os.getenv("MOTET_MOTET_ID", "default")
        configured_scope = self._load_service_scope_from_yaml(service_id)
        if not configured_scope:
            raise RuntimeError(
                f"Missing configured visibility/lifecycle for MCP service {service_id}. "
                "Ensure it exists in MCP_INSTANCE_MANAGER_CONFIG and is readable."
            )
        effective_visibility = configured_scope["visibility"]
        effective_lifecycle = configured_scope["lifecycle"]
        if effective_lifecycle == LifecycleDuration.SESSION:
            raise ValueError(
                f"Service {service_id} is configured with SESSION lifecycle but "
                "send_request does not receive session_id."
            )
        # ADR-0105 §R2: stream prefix is the manager_id when configured.
        routing_prefix = _resolve_mcp_routing_prefix(target_worker_id)
        try:
            from motet.core.workers.worker_utils import get_worker_id

            worker_id = get_worker_id()
        except (ImportError, AttributeError):
            worker_id = target_worker_id
        instance_key = generate_instance_key(
            service_id=service_id,
            visibility=effective_visibility,
            lifecycle_duration=effective_lifecycle,
            motet_id=effective_motet_id,
            tenant_id=scope_tenant,
            principal_id=principal_id,
            conversation_id=conversation_id,
            task_id=task_id,
        )
        request_stream = generate_stream_name(
            service_id=service_id,
            visibility=effective_visibility,
            instance_key=instance_key,
            stream_type=StreamType.REQUESTS,
            manager_id=routing_prefix,
        )
        response_stream = generate_stream_name(
            service_id=service_id,
            visibility=effective_visibility,
            instance_key=instance_key,
            stream_type=StreamType.RESPONSES,
            manager_id=routing_prefix,
        )
        request_id = str(uuid.uuid4())
        timeout_ms = (timeout_seconds or self._request_timeout_seconds) * 1000
        request_msg = MCPRequestMessage(
            id=request_id,
            service_id=service_id,
            instance_key=instance_key,
            worker_id=self.manager_id,
            timeout_ms=timeout_ms,
            principal_id=principal_id,
            tenant_id=scope_tenant,
            motet_id=effective_motet_id,
            jsonrpc_request={
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            },
        )
        self._pending_requests[request_id] = {
            "start_time": time.time(),
            "timeout_seconds": timeout_seconds or self._request_timeout_seconds,
            "service_id": service_id,
            "method": method,
            "response_stream": response_stream,
        }
        redis_client = get_sync_redis_client(f"motet_mcp_manager_{self.manager_id}")
        if not scope_tenant or not effective_motet_id:
            raise ValueError("tenant_id and motet_id are required for MCP stream encryption")
        request_data = encode_mcp_stream_fields(
            stream_name=request_stream,
            message=request_msg,
            tenant_id=str(scope_tenant),
            motet_id=str(effective_motet_id),
            message_type=request_msg.stream_type.value,
        )
        self._prepare_response_group(request_id, response_stream, redis_client)
        message_id = redis_client.xadd(request_stream, cast(Any, request_data))
        self._refresh_stream_ttl(redis_client, request_stream)
        logger.info(
            "Published MCP request to Motet Stream",
            request_id=request_id,
            method=method,
            stream=request_stream,
        )
        response = self._wait_for_response_sync(request_id, response_stream, timeout_ms, redis_client)
        self._pending_requests.pop(request_id, None)
        self.stats["requests_processed"] += 1
        return response

    def _prepare_response_group(self, request_id: str, response_stream: str, redis_client) -> None:
        """Create the per-request response consumer group before publishing.

        This closes the fast-reply race where a manager can publish a response
        between request ``XADD`` and the worker's later ``XGROUP CREATE``.
        """
        group_name = f"manager-{self.manager_id}-{request_id}"
        try:
            redis_client.xgroup_create(response_stream, group_name, id="$", mkstream=True)
            logger.debug(
                "mcp_client_created_consumer_group_pre_publish",
                response_stream=response_stream,
                group_name=group_name,
                request_id=request_id,
            )
        except Exception as e:
            if "BUSYGROUP" not in str(e):
                logger.debug(
                    "mcp_client_consumer_group_note",
                    response_stream=response_stream,
                    group_name=group_name,
                    request_id=request_id,
                    error=str(e),
                )

    def _wait_for_response_sync(self, request_id: str, response_stream: str, 
                                timeout_ms: int, redis_client) -> Dict[str, Any]:
        """Wait for a response to a specific request (synchronous for Celery workers)."""
        import json
        
        start_time = time.time()
        timeout_seconds = timeout_ms / 1000
        
        # Create an isolated consumer group per request to prevent cross-request
        # response stealing when multiple consumers block on the same stream.
        group_name = f"manager-{self.manager_id}-{request_id}"
        consumer_name = f"consumer-{self.manager_id}-{request_id}"
        
        # Debug-only: this can be high-volume under concurrent tool usage.
        logger.debug(
            "mcp_client_waiting_for_response",
            manager_id=self.manager_id,
            request_id=request_id,
            response_stream=response_stream,
            timeout_seconds=timeout_seconds,
        )
        
        try:
            # Try to create consumer group (ignore if exists)
            try:
                # Use "$" to read new responses after this request starts waiting.
                redis_client.xgroup_create(response_stream, group_name, id="$", mkstream=True)
                logger.debug("mcp_client_created_consumer_group",
                            response_stream=response_stream,
                            group_name=group_name)
            except Exception as e:
                if "BUSYGROUP" not in str(e):
                    logger.debug("mcp_client_consumer_group_note",
                                response_stream=response_stream,
                                group_name=group_name,
                                error=str(e))
            
            iteration = 0
            while time.time() - start_time < timeout_seconds:
                iteration += 1
                elapsed = time.time() - start_time
                if iteration % 5 == 0:  # Log every 5 seconds
                    logger.debug("mcp_client_still_waiting",
                                manager_id=self.manager_id,
                                request_id=request_id,
                                response_stream=response_stream,
                                elapsed_seconds=round(elapsed, 1),
                                timeout_seconds=timeout_seconds)
                # Check for response messages (blocking read with 1s timeout)
                try:
                    messages = redis_client.xreadgroup(
                        group_name, consumer_name,
                        {response_stream: ">"},
                        count=10,
                        block=1000
                    )
                except Exception as e:
                    logger.debug(f"Error reading from stream: {e}")
                    worker_sleep(0.025)
                    continue
                
                if not messages:
                    continue
                
                for stream_name, stream_messages in messages:
                    for message_id, message_data in stream_messages:
                        try:
                            from motet.core.security.redis_decode_helpers import normalize_redis_mapping

                            fields = normalize_redis_mapping(message_data)
                            message_type = str(fields.get("message_type") or "unknown")
                            envelope_str = fields.get("_envelope")
                            if not envelope_str:
                                raise ValueError("Encrypted MCP stream message missing _envelope")
                            
                            request_id_field = str(fields.get("request_id") or "")
                            tenant_id_field = str(fields.get("tenant_id") or "")
                            motet_id_field = str(fields.get("motet_id") or "")
                            service_id_field = str(fields.get("service_id") or "")
                            if not request_id_field or not tenant_id_field or not motet_id_field or not service_id_field:
                                raise ValueError("Encrypted MCP stream message missing required AAD fields")
                            
                            message_data_dict = decode_mcp_stream_fields(
                                stream_name=str(stream_name),
                                envelope_json=str(envelope_str),
                                message_type=message_type,
                                request_id=request_id_field,
                                tenant_id=tenant_id_field,
                                motet_id=motet_id_field,
                                service_id=service_id_field,
                            )
                            
                            response_msg = MCPResponseMessage(**message_data_dict)
                            
                            if response_msg.request_id == request_id:
                                # Found our response
                                elapsed = time.time() - start_time
                                logger.info("mcp_client_received_response",
                                          manager_id=self.manager_id,
                                          request_id=request_id,
                                          response_stream=response_stream,
                                          elapsed_seconds=round(elapsed, 2),
                                          has_result="result" in response_msg.jsonrpc_response,
                                          has_error="error" in response_msg.jsonrpc_response)
                                
                                redis_client.xack(response_stream, group_name, message_id)
                                
                                # Return the JSON-RPC response
                                jsonrpc_response = response_msg.jsonrpc_response
                                
                                if "result" in jsonrpc_response:
                                    return jsonrpc_response["result"]
                                elif "error" in jsonrpc_response:
                                    raise RuntimeError(f"MCP tool error: {jsonrpc_response['error']}")
                                else:
                                    raise RuntimeError(f"Invalid MCP response: {jsonrpc_response}")
                            
                            # Different request for this stream. Acknowledge for this
                            # per-request group only and keep waiting.
                            redis_client.xack(response_stream, group_name, message_id)
                            
                        except EncryptionError as e:
                            logger.error(
                                "Error processing response message",
                                request_id=request_id,
                                error=str(e),
                            )
                            if should_purge_on_kek_mismatch(str(e)):
                                worker_id = manager_id_from_stream_name(str(stream_name))
                                _purge_worker_mcp_streams(
                                    redis_client=redis_client,
                                    worker_id=worker_id,
                                    reason="kek_mismatch",
                                )
                            elif "Key unwrapping failed" in str(e) or "KEK fingerprint mismatch" in str(e):
                                logger.error(
                                    "mcp_stream_decrypt_kek_mismatch_no_purge",
                                    request_id=request_id,
                                    stream_name=str(stream_name),
                                    note="Set MOTET_MCP_PURGE_ON_KEK_MISMATCH=true to enable purge",
                                )
                            continue
                        except Exception as e:
                            logger.error("Error processing response message",
                                       request_id=request_id,
                                       error=str(e))
                            continue
            
            # Timeout reached
            elapsed = time.time() - start_time
            logger.warning("mcp_client_response_timeout",
                         manager_id=self.manager_id,
                         request_id=request_id,
                         response_stream=response_stream,
                         timeout_seconds=timeout_seconds,
                         elapsed_seconds=round(elapsed, 2),
                         note="No response received from MCP proxy - check if proxy is running and consuming from request stream")
            self._pending_requests.pop(request_id, None)
            raise TimeoutError(f"Request {request_id} timed out after {timeout_seconds} seconds")
            
        except Exception as e:
            self._pending_requests.pop(request_id, None)
            raise
        finally:
            # Best-effort cleanup: per-request consumer group should not persist.
            try:
                redis_client.xgroup_destroy(response_stream, group_name)
            except Exception:
                pass  # best-effort cleanup; consumer group may not exist
    def list_tools(self, service_id: str,
                    conversation_id: Optional[str] = None,
                    task_id: Optional[str] = None,
                    session_id: Optional[str] = None,
                    tenant_id: Optional[str] = None,
                    principal_id: Optional[str] = None,
                    visibility: Optional[Visibility] = None,
                    lifecycle: Optional[LifecycleDuration] = None,
                    target_worker_id: Optional[str] = None,
                    timeout_seconds: Optional[int] = None) -> Dict[str, Any]:
        """
        List available tools from a service (synchronous for Celery workers).
        
        Args:
            service_id: ID of the service
            conversation_id: Optional conversation context
            task_id: Optional task context
            session_id: Optional session context
            tenant_id: Optional tenant context
            target_worker_id: Optional worker to target (defaults to current worker)
            timeout_seconds: Optional timeout override
            
        Returns:
            Dictionary with 'tools' key containing list of tool definitions
        """
        from motet.core.distributed.redis_manager import get_sync_redis_client
        import json
        
        try:
            # ADR-0105 §R2: stream prefix is the manager_id when configured.
            routing_prefix = _resolve_mcp_routing_prefix(target_worker_id)
            try:
                from motet.core.workers.worker_utils import get_worker_id

                worker_id = get_worker_id()
            except (ImportError, AttributeError):
                worker_id = target_worker_id

            logger.info(
                "MotetMCPClient.list_tools (sync)",
                service_id=service_id,
                conversation_id=conversation_id,
                worker_id=worker_id,
                routing_prefix=routing_prefix,
            )
            
            # Resolve encryption scope explicitly (avoid locals() hacks).
            # These are used for encrypted stream publishing for tools/list (ADR-0056).
            scope_tenant_id: str = (tenant_id or os.getenv("MOTET_TENANT_ID", "default")) or "default"
            scope_motet_id: str = (os.getenv("MOTET_MOTET_ID", "default")) or "default"
            
            # Explicit identity path: if visibility is provided, honor the caller's scope directly.
            if visibility is not None:
                motet_id = scope_motet_id
                effective_visibility = visibility
                effective_lifecycle = lifecycle or LifecycleDuration.PERMANENT
                tenant = tenant_id
                principal = principal_id

                if effective_visibility == Visibility.MOTET:
                    tenant = tenant or os.getenv("MOTET_TENANT_ID", "default")
                if effective_visibility == Visibility.USER:
                    tenant = tenant or os.getenv("MOTET_TENANT_ID", "default")
                    if not principal:
                        raise ValueError("User visibility requires principal_id")

                instance_key = generate_instance_key(
                    service_id=service_id,
                    visibility=effective_visibility,
                    lifecycle_duration=effective_lifecycle,
                    motet_id=motet_id,
                    tenant_id=tenant,
                    principal_id=principal,
                    conversation_id=conversation_id,
                    task_id=task_id,
                    session_id=session_id,
                )
                visibility = effective_visibility
                lifecycle = effective_lifecycle
                # For MOTET/USER visibilities, tenant may be defaulted above; keep scope values aligned.
                scope_tenant_id = (tenant or scope_tenant_id) or "default"
            else:
                # Fallback: use heuristic-based scope resolution
                effective_motet_id = scope_motet_id
                effective_tenant = scope_tenant_id
                
                # Use shared helper to resolve visibility and lifecycle
                effective_visibility, effective_lifecycle = resolve_visibility_and_lifecycle(
                    tenant_id=effective_tenant,
                    principal_id=principal_id,
                    motet_id=effective_motet_id,
                    conversation_id=conversation_id,
                    task_id=task_id,
                )
                
                # Generate instance key
                instance_key = generate_instance_key(
                    service_id=service_id,
                    visibility=effective_visibility,
                    lifecycle_duration=effective_lifecycle,
                    motet_id=effective_motet_id,
                    tenant_id=effective_tenant,
                    principal_id=principal_id,
                    conversation_id=conversation_id,
                    task_id=task_id,
                )
                visibility = effective_visibility
                lifecycle = effective_lifecycle
            
            request_stream = generate_stream_name(
                service_id=service_id,
                visibility=visibility,
                instance_key=instance_key,
                stream_type=StreamType.REQUESTS,
                manager_id=routing_prefix,
            )
            response_stream = generate_stream_name(
                service_id=service_id,
                visibility=visibility,
                instance_key=instance_key,
                stream_type=StreamType.RESPONSES,
                manager_id=routing_prefix,
            )
            
            # Create tools/list request message
            request_id = str(uuid.uuid4())
            timeout_ms = (timeout_seconds or 30) * 1000
            
            request_msg = MCPRequestMessage(
                id=request_id,
                service_id=service_id,
                instance_key=instance_key,
                worker_id=self.manager_id,
                timeout_ms=timeout_ms,
                tenant_id=scope_tenant_id,
                principal_id=principal_id,
                motet_id=scope_motet_id,
                jsonrpc_request={
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "tools/list",
                    "params": {}
                }
            )
            
            # Track the request
            self._pending_requests[request_id] = {
                "start_time": time.time(),
                "timeout_seconds": timeout_seconds or 30,
                "service_id": service_id,
                "method": "tools/list",
                "response_stream": response_stream
            }
            
            # Get sync Redis client
            redis_client = get_sync_redis_client(f"motet_mcp_manager_{self.manager_id}")
            
            # Publish request using sync Redis
            # IMPORTANT (ADR-0056): MCP streams must not store message bodies in plaintext.
            # Store message_type in plaintext for routing/ops; store JSON body encrypted in `_envelope`.
            if not scope_tenant_id:
                raise ValueError("tenant_id is required for MCP stream encryption")
            if not scope_motet_id:
                raise ValueError("motet_id is required for MCP stream encryption")

            request_data = encode_mcp_stream_fields(
                stream_name=request_stream,
                message=request_msg,
                tenant_id=str(scope_tenant_id),
                motet_id=str(scope_motet_id),
                message_type=request_msg.stream_type.value,
            )
            
            self._prepare_response_group(request_id, response_stream, redis_client)
            message_id = redis_client.xadd(request_stream, cast(Any, request_data))
            
            # Refresh TTL to keep active streams alive
            self._refresh_stream_ttl(redis_client, request_stream)
            
            logger.info("Published tools/list request to Motet Stream",
                       request_id=request_id,
                       stream=request_stream,
                       message_id=message_id)
            
            # Wait for response (synchronous)
            response = self._wait_for_response_sync(request_id, response_stream, timeout_ms, redis_client)
            
            return response
            
        except Exception as e:
            self.stats["errors"] += 1
            logger.error("List tools failed",
                        manager_id=self.manager_id,
                        service_id=service_id,
                        error=str(e),
                        error_type=type(e).__name__,
                        exc_info=True)
            # Return empty tools but log the error for debugging
            # This allows discovery to continue even if one service fails
            return {"tools": [], "error": str(e), "error_type": type(e).__name__}

    def list_resources(
        self,
        service_id: str,
        conversation_id: Optional[str] = None,
        task_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        principal_id: Optional[str] = None,
        motet_id: Optional[str] = None,
        target_worker_id: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
        command_context: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """List resources via MCP resources/list (ADR-0076 Scope 3). Returns dict with 'resources' key."""
        return self._send_mcp_request(
            service_id, "resources/list", {},
            conversation_id=conversation_id,
            task_id=task_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
            motet_id=motet_id,
            target_worker_id=target_worker_id,
            timeout_seconds=timeout_seconds,
            command_context=command_context,
        )

    def read_resource(
        self,
        service_id: str,
        uri: str,
        conversation_id: Optional[str] = None,
        task_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        principal_id: Optional[str] = None,
        motet_id: Optional[str] = None,
        target_worker_id: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
        command_context: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Read resource via MCP resources/read (ADR-0076 Scope 3). Returns dict with 'contents' key."""
        return self._send_mcp_request(
            service_id, "resources/read", {"uri": uri},
            conversation_id=conversation_id,
            task_id=task_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
            motet_id=motet_id,
            target_worker_id=target_worker_id,
            timeout_seconds=timeout_seconds,
            command_context=command_context,
        )

    def list_prompts(
        self,
        service_id: str,
        conversation_id: Optional[str] = None,
        task_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        principal_id: Optional[str] = None,
        motet_id: Optional[str] = None,
        target_worker_id: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
        command_context: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """List prompts via MCP prompts/list (ADR-0076 Scope 3). Returns dict with 'prompts' key."""
        return self._send_mcp_request(
            service_id, "prompts/list", {},
            conversation_id=conversation_id,
            task_id=task_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
            motet_id=motet_id,
            target_worker_id=target_worker_id,
            timeout_seconds=timeout_seconds,
            command_context=command_context,
        )

    def get_prompt(
        self,
        service_id: str,
        name: str,
        arguments: Optional[Dict[str, Any]] = None,
        conversation_id: Optional[str] = None,
        task_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        principal_id: Optional[str] = None,
        motet_id: Optional[str] = None,
        target_worker_id: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
        command_context: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Get prompt via MCP prompts/get (ADR-0076 Scope 3). Returns dict with 'messages' and optional 'description'."""
        params: Dict[str, Any] = {"name": name}
        if arguments is not None:
            params["arguments"] = arguments
        return self._send_mcp_request(
            service_id, "prompts/get", params,
            conversation_id=conversation_id,
            task_id=task_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
            motet_id=motet_id,
            target_worker_id=target_worker_id,
            timeout_seconds=timeout_seconds,
            command_context=command_context,
        )

    def get_stats(self) -> Dict[str, Any]:
        """Get manager statistics."""
        return {
            **self.stats,
            "uptime_seconds": time.time() - self.stats["start_time"],
            "manager_id": self.manager_id
        }


_global_motet_mcp_client: Optional[MotetMCPClient] = None


def get_motet_mcp_client() -> MotetMCPClient:
    """Get the global Motet MCP client instance synchronously for worker contexts."""
    global _global_motet_mcp_client
    
    if _global_motet_mcp_client is None:
        _global_motet_mcp_client = MotetMCPClient()
        logger.info("Motet MCP Client initialized synchronously for worker context")
    
    return _global_motet_mcp_client


# Export the main classes
__all__ = [
    "MotetMCPClient",
    "get_motet_mcp_client"
]
