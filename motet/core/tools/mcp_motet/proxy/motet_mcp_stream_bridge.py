"""
Motet - Motet MCP Stream Bridge

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-20

Description:
    Technology-agnostic stream protocol handler for Redis Streams operations in the Motet
    distributed framework. Handles stream operations, consumer groups, and connection
    management for MCP proxy communication. Includes comprehensive stream lifecycle
    management and distributed coordination.

Dependencies:
    - asyncio: Asynchronous stream operations and consumer management
    - json: Stream message serialization and processing
    - structlog: Structured logging and observability
    - pydantic: Data validation and model definitions
    - Redis manager for distributed stream operations

Usage:
    from motet.core.tools.mcp_motet.proxy.motet_mcp_stream_bridge import MotetMCPStreamBridge

    # Create stream bridge
    bridge = MotetMCPStreamBridge(service_id="weather", context_id="user123")

    # Start bridge
    await bridge.start()

    # Send message
    await bridge.send_message(message)

    # Receive messages
    async for message in bridge.receive_messages():
        # Process message
        pass

Notes:
    - Provides technology-agnostic stream protocol handling
    - Includes Redis Streams operations and consumer group management
    - Supports comprehensive stream lifecycle management
    - Includes connection management and error handling
    - Supports distributed coordination and message routing
    - Integrates with MCP protocol and stream operations
    - Includes comprehensive observability and logging
"""

import asyncio
import json
import os
import time
import structlog
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional, cast
from pydantic import BaseModel

from motet.core.distributed.redis_manager import get_redis_client
from motet.core.security.encrypted_stream_codec import (
    encode_encrypted_message_data,
)
from motet.core.security.encryption_service import EncryptionError
from motet.core.tools.mcp_motet.stream_encryption import (
    resolve_mcp_stream_scope,
    encode_mcp_stream_fields,
    decode_mcp_stream_fields,
    should_purge_on_kek_mismatch,
)
from motet.core.tools.mcp_motet.protocol import (
    MCPStreamMessage, MCPRequestMessage, MCPResponseMessage, MCPLogMessage,
    StreamType,
    manager_id_from_stream_name,
    mcp_io_stream_scan_patterns,
)

logger = structlog.get_logger(__name__)


class ConsumerInfo(BaseModel):
    """Information about a stream consumer."""
    consumer_name: str
    group_name: str
    last_delivered_id: str
    pending_count: int
    idle_time_ms: int


class StreamStats(BaseModel):
    """Statistics for a Redis Stream."""
    stream_name: str
    length: int
    first_entry_id: str
    last_entry_id: str
    consumer_groups: List[str]
    consumers: List[ConsumerInfo]


class MotetMCPStreamBridge:
    """
    Technology-agnostic stream protocol handler for Motet Streams MCP communication.
    
    This class provides a clean abstraction over Redis Streams operations,
    making it easy to swap out the underlying technology if needed.
    
    Features:
    - Stream operations (publish/consume)
    - Consumer group management
    - Connection pooling and error handling
    - Message correlation and routing
    - Health monitoring and metrics
    """
    
    def __init__(self, client_id: str = "motet_mcp_bridge"):
        self.client_id = client_id
        self.redis_client = get_redis_client(client_id)
        self._consumer_groups: Dict[str, str] = {}  # stream_name -> group_name
        self._running = False
        self._kek_mismatch_purged: set[str] = set()
        self._health_stats = {
            "messages_published": 0,
            "messages_consumed": 0,
            "errors": 0,
            "last_error": None,
            "last_activity": time.time()
        }
    
    async def initialize(self) -> None:
        """
        Initialize the stream bridge and verify Redis connectivity.

        Retries on ``BusyLoadingError``: after a stack restart Valkey replays its
        AOF/RDB dataset and rejects commands with LOADING for a while. Treating
        that as fatal made the MCP instance manager come up with 0 instances
        (workers then never saw any service_ready signals and registered no MCP
        tools), so we wait it out instead of failing instance creation.
        """
        from redis.exceptions import BusyLoadingError

        deadline = time.monotonic() + float(
            os.getenv("MOTET_MCP_STREAM_BRIDGE_INIT_TIMEOUT_SECONDS", "120")
        )
        attempt = 0
        while True:
            attempt += 1
            try:
                await self.redis_client.ping()
                logger.info("Motet MCP Stream Bridge initialized successfully",
                           client_id=self.client_id, attempts=attempt)
                self._running = True
                return
            except BusyLoadingError as e:
                if time.monotonic() >= deadline:
                    logger.error("Redis still loading dataset after init deadline",
                                client_id=self.client_id, attempts=attempt, error=str(e))
                    self._health_stats["errors"] += 1
                    self._health_stats["last_error"] = str(e)
                    raise
                logger.warning("Redis busy loading dataset; retrying stream bridge init",
                              client_id=self.client_id, attempt=attempt)
                await asyncio.sleep(2.0)
            except Exception as e:
                logger.error("Failed to initialize Motet MCP Stream Bridge",
                            client_id=self.client_id, error=str(e))
                self._health_stats["errors"] += 1
                self._health_stats["last_error"] = str(e)
                raise
    
    async def publish_message(self, stream_name: str, message: MCPStreamMessage) -> str:
        """
        Publish a message to a Redis Stream and refresh its TTL.
        
        Args:
            stream_name: Name of the stream to publish to
            message: Message to publish
            
        Returns:
            Message ID assigned by Redis
            
        Raises:
            Exception: If publishing fails
        """
        try:
            # Redis Streams must not store sensitive message bodies in plaintext (ADR-0056).
            # Store `message_type` plaintext for routing/ops; store the JSON body encrypted in `_envelope`.
            tenant_id, motet_id = resolve_mcp_stream_scope(
                stream_name=stream_name,
                message=message,
                allow_env_fallback=True,
            )

            message_data = encode_mcp_stream_fields(
                stream_name=stream_name,
                message=message,
                tenant_id=tenant_id,
                motet_id=motet_id,
                message_type=message.stream_type.value,
            )
            
            # Publish to Redis Stream
            message_id = await self.redis_client.xadd(
                stream_name, cast(Any, message_data)
            )

            self._health_stats["messages_published"] += 1
            self._health_stats["last_activity"] = time.time()
            
            logger.debug("Published message to stream",
                        stream_name=stream_name,
                        message_id=message_id,
                        message_type=message.stream_type.value)
            
            return message_id
            
        except Exception as e:
            self._health_stats["errors"] += 1
            self._health_stats["last_error"] = str(e)
            logger.error("Failed to publish message to stream",
                        stream_name=stream_name,
                        message_type=message.stream_type.value,
                        error=str(e))
            raise
    
    async def create_consumer_group(self, stream_name: str, group_name: str, 
                                  start_id: str = "0") -> bool:
        """
        Create a consumer group for a stream.
        
        Args:
            stream_name: Name of the stream
            group_name: Name of the consumer group
            start_id: Starting message ID (default: "0" for beginning)
            
        Returns:
            True if group was created, False if it already exists
        """
        try:
            await self.redis_client.xgroup_create(
                stream_name, group_name, start_id, mkstream=True
            )
            
            self._consumer_groups[stream_name] = group_name
            
            logger.info("Created consumer group for stream",
                       stream_name=stream_name,
                       group_name=group_name,
                       start_id=start_id)
            
            return True
            
        except Exception as e:
            if "BUSYGROUP" in str(e):
                # Group already exists
                self._consumer_groups[stream_name] = group_name
                logger.debug("Consumer group already exists",
                           stream_name=stream_name,
                           group_name=group_name)
                return False
            else:
                self._health_stats["errors"] += 1
                self._health_stats["last_error"] = str(e)
                logger.error("Failed to create consumer group",
                           stream_name=stream_name,
                           group_name=group_name,
                           error=str(e))
                raise
    
    async def consume_messages(self, stream_name: str, group_name: str,
                             consumer_name: str, count: int = 1,
                             block_ms: Optional[int] = 1000) -> List[Dict[str, Any]]:
        """
        Consume messages from a stream using consumer group.
        
        Args:
            stream_name: Name of the stream
            group_name: Name of the consumer group
            consumer_name: Name of the consumer
            count: Maximum number of messages to consume
            block_ms: Milliseconds to block waiting for messages (None for no blocking)
            
        Returns:
            List of consumed messages with metadata
        """
        try:
            # Ensure consumer group exists
            await self.create_consumer_group(stream_name, group_name)
            
            streams_spec = cast(Any, {stream_name: ">"})

            if block_ms is not None:
                result = await self.redis_client.xreadgroup(
                    group_name, consumer_name, streams_spec, count=count, block=block_ms
                )
            else:
                result = await self.redis_client.xreadgroup(
                    group_name, consumer_name, streams_spec, count=count
                )
            
            messages = []
            if result:
                for stream, msgs in result:
                    for msg_id, fields in msgs:
                        tenant_id_field = ""
                        try:
                            from motet.core.security.redis_decode_helpers import normalize_redis_mapping

                            normalized_fields = normalize_redis_mapping(fields)
                            message_type = str(normalized_fields.get("message_type") or "unknown")
                            envelope_str = normalized_fields.get("_envelope")
                            
                            if not envelope_str:
                                logger.error("Encrypted stream message missing _envelope",
                                           stream_name=stream,
                                           message_id=msg_id)
                                continue

                            # Decode encrypted message data (no plaintext fallback)
                            request_id_field = str(normalized_fields.get("request_id") or "")
                            tenant_id_field = str(normalized_fields.get("tenant_id") or "")
                            motet_id_field = str(normalized_fields.get("motet_id") or "")
                            service_id_field = str(normalized_fields.get("service_id") or "")
                            if not request_id_field or not tenant_id_field or not motet_id_field or not service_id_field:
                                raise ValueError("Encrypted MCP stream message missing required AAD fields")

                            message_data = decode_mcp_stream_fields(
                                stream_name=str(stream),
                                envelope_json=str(envelope_str),
                                message_type=message_type,
                                request_id=request_id_field,
                                tenant_id=tenant_id_field,
                                motet_id=motet_id_field,
                                service_id=service_id_field,
                            )
                            
                            messages.append({
                                "stream_name": stream,
                                "message_id": msg_id,
                                "message_type": message_type,
                                "message_data": message_data,
                                "consumer_name": consumer_name,
                                "group_name": group_name
                            })
                            
                        except EncryptionError as e:
                            self._health_stats["errors"] += 1
                            self._health_stats["last_error"] = str(e)
                            logger.error(
                                "Failed to decode encrypted message data",
                                stream_name=stream,
                                message_id=msg_id,
                                error=str(e),
                            )

                            if should_purge_on_kek_mismatch(str(e)):
                                worker_id = manager_id_from_stream_name(str(stream))
                                purge_key = f"{worker_id}:{tenant_id_field}" if worker_id else f"unknown:{tenant_id_field}"
                                if purge_key not in self._kek_mismatch_purged:
                                    self._kek_mismatch_purged.add(purge_key)
                                    await self._purge_worker_mcp_streams(
                                        worker_id=worker_id,
                                        tenant_id=tenant_id_field,
                                        reason="kek_mismatch",
                                    )
                            elif "Key unwrapping failed" in str(e) or "KEK fingerprint mismatch" in str(e):
                                logger.error(
                                    "mcp_stream_decrypt_kek_mismatch_no_purge",
                                    stream_name=str(stream),
                                    message_id=msg_id,
                                    tenant_id=tenant_id_field,
                                    note="Set MOTET_MCP_PURGE_ON_KEK_MISMATCH=true to enable purge",
                                )
                            continue
                        except (json.JSONDecodeError, ValueError, KeyError) as e:
                            logger.error("Failed to decode encrypted message data",
                                       stream_name=stream,
                                       message_id=msg_id,
                                       error=str(e))
                            continue
            
            self._health_stats["messages_consumed"] += len(messages)
            self._health_stats["last_activity"] = time.time()
            
            if messages:
                logger.info("mcp_stream_bridge_consumed_messages",
                           stream_name=stream_name,
                           count=len(messages),
                           consumer_name=consumer_name,
                           group_name=group_name,
                           message_ids=[msg.get("message_id") for msg in messages])
            
            return messages
            
        except Exception as e:
            self._health_stats["errors"] += 1
            self._health_stats["last_error"] = str(e)
            logger.error("Failed to consume messages from stream",
                        stream_name=stream_name,
                        group_name=group_name,
                        consumer_name=consumer_name,
                        error=str(e))
            raise

    async def _purge_worker_mcp_streams(self, worker_id: Optional[str], tenant_id: str, reason: str) -> None:
        """Purge MCP streams for a worker after a KEK mismatch."""
        if not worker_id:
            logger.warning(
                "mcp_stream_purge_skipped_missing_worker_id",
                tenant_id=tenant_id,
                reason=reason,
            )
            return

        deleted = 0
        seen: set = set()
        try:
            for pattern in mcp_io_stream_scan_patterns(worker_id):
                cursor = 0
                while True:
                    cursor, keys = await self.redis_client.scan(cursor=cursor, match=pattern, count=500)
                    if keys:
                        new_keys = [key for key in keys if key not in seen]
                        if new_keys:
                            deleted += await self.redis_client.delete(*new_keys)
                            seen.update(new_keys)
                    if cursor == 0:
                        break

            logger.warning(
                "mcp_streams_purged_on_kek_mismatch",
                worker_id=worker_id,
                tenant_id=tenant_id,
                deleted_streams=deleted,
                reason=reason,
            )
        except Exception as exc:
            logger.error(
                "mcp_stream_purge_failed",
                worker_id=worker_id,
                tenant_id=tenant_id,
                reason=reason,
                error=str(exc),
                exc_info=True,
            )
    
    async def acknowledge_message(self, stream_name: str, group_name: str, 
                                message_id: str) -> bool:
        """
        Acknowledge processing of a message.
        
        Args:
            stream_name: Name of the stream
            group_name: Name of the consumer group
            message_id: ID of the message to acknowledge
            
        Returns:
            True if message was acknowledged
        """
        try:
            result = await self.redis_client.xack(stream_name, group_name, message_id)
            
            logger.debug("Acknowledged message",
                        stream_name=stream_name,
                        group_name=group_name,
                        message_id=message_id)
            
            return bool(result)
            
        except Exception as e:
            self._health_stats["errors"] += 1
            self._health_stats["last_error"] = str(e)
            logger.error("Failed to acknowledge message",
                        stream_name=stream_name,
                        group_name=group_name,
                        message_id=message_id,
                        error=str(e))
            raise
    
    async def get_stream_info(self, stream_name: str) -> Optional[StreamStats]:
        """
        Get information about a stream.
        
        Args:
            stream_name: Name of the stream
            
        Returns:
            StreamStats object or None if stream doesn't exist
        """
        try:
            # Get basic stream info
            info = await self.redis_client.xinfo_stream(stream_name)
            
            # Get consumer groups
            groups_info = await self.redis_client.xinfo_groups(stream_name)
            consumer_groups = [group["name"] for group in groups_info]
            
            # Get consumers for each group
            consumers = []
            for group in groups_info:
                group_name = group["name"]
                consumers_info = await self.redis_client.xinfo_consumers(stream_name, group_name)
                
                for consumer_info in consumers_info:
                    consumers.append(ConsumerInfo(
                        consumer_name=consumer_info["name"],
                        group_name=group_name,
                        last_delivered_id=consumer_info.get("last-delivered-id", "0-0"),
                        pending_count=consumer_info.get("pending", 0),
                        idle_time_ms=consumer_info.get("idle", 0)
                    ))
            
            return StreamStats(
                stream_name=stream_name,
                length=info["length"],
                first_entry_id=info.get("first-entry", ["0-0"])[0],
                last_entry_id=info.get("last-entry", ["0-0"])[0],
                consumer_groups=consumer_groups,
                consumers=consumers
            )
            
        except Exception as e:
            if "no such key" in str(e).lower():
                return None
            else:
                logger.error("Failed to get stream info",
                           stream_name=stream_name,
                           error=str(e))
                raise
    
    async def trim_stream(self, stream_name: str, max_length: int) -> int:
        """
        Trim a stream to maximum length.
        
        Args:
            stream_name: Name of the stream
            max_length: Maximum number of messages to keep
            
        Returns:
            Number of messages removed
        """
        try:
            result = await self.redis_client.xtrim(stream_name, maxlen=max_length, approximate=True)
            
            logger.debug("Trimmed stream",
                        stream_name=stream_name,
                        max_length=max_length,
                        removed_count=result)
            
            return result
            
        except Exception as e:
            self._health_stats["errors"] += 1
            self._health_stats["last_error"] = str(e)
            logger.error("Failed to trim stream",
                        stream_name=stream_name,
                        max_length=max_length,
                        error=str(e))
            raise
    
    async def health_check(self) -> Dict[str, Any]:
        """
        Perform health check and return status.
        
        Returns:
            Dictionary with health status and statistics
        """
        try:
            # Test Redis connectivity
            await self.redis_client.ping()
            
            health_status = {
                "status": "healthy" if self._running else "stopped",
                "client_id": self.client_id,
                "redis_connected": True,
                "statistics": self._health_stats.copy(),
                "consumer_groups": len(self._consumer_groups),
                "active_streams": list(self._consumer_groups.keys())
            }
            
            return health_status
            
        except Exception as e:
            self._health_stats["errors"] += 1
            self._health_stats["last_error"] = str(e)
            
            return {
                "status": "unhealthy",
                "client_id": self.client_id,
                "redis_connected": False,
                "error": str(e),
                "statistics": self._health_stats.copy()
            }
    
    async def shutdown(self) -> None:
        """Gracefully shutdown the stream bridge."""
        try:
            self._running = False
            
            # Close Redis client connections would be handled by the UnifiedRedisManager
            logger.info("Motet MCP Stream Bridge shutdown completed",
                       client_id=self.client_id)
            
        except Exception as e:
            logger.error("Error during stream bridge shutdown",
                        client_id=self.client_id,
                        error=str(e))


# Export the main class
__all__ = [
    "MotetMCPStreamBridge",
    "ConsumerInfo",
    "StreamStats"
]
