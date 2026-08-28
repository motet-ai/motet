"""
Motet - MotetMCPStreamBridge Tests

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-07-10

Description:
    Unit tests for the MCP Motet Redis Streams bridge.

    Validates publish/consume flows, consumer group behavior, and error recovery
    while enforcing ADR-0056 encryption-at-rest for stream payloads (encrypted `_envelope`)
    and minimal plaintext routing fields.

Dependencies:
    - pytest: test runner
    - unittest.mock: patching dependencies
    - motet.core.tools.mcp_motet.proxy.motet_mcp_stream_bridge: implementation under test

Usage:
    pytest tests/unit/tools/mcp_motet/proxy/test_motet_mcp_stream_bridge.py

Notes:
    - These tests avoid pytest-asyncio and use `asyncio.run()` for async helpers.
"""

import pytest
import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch
from typing import Dict, Any, List

from motet.core.tools.mcp_motet.proxy.motet_mcp_stream_bridge import (
    MotetMCPStreamBridge, ConsumerInfo, StreamStats
)
from motet.core.tools.mcp_motet.protocol import (
    MCPRequestMessage, MCPResponseMessage, MCPLogMessage,
    StreamType, Visibility, LifecycleDuration, generate_instance_key, generate_stream_name
)

@pytest.fixture
def mock_redis_client():
    """Create mock Redis client for testing."""
    mock_client = AsyncMock()
    mock_client.ping.return_value = True
    mock_client.xadd.return_value = "1640995200000-0"
    mock_client.xgroup_create.return_value = True
    mock_client.xreadgroup.return_value = []
    mock_client.xack.return_value = 1
    mock_client.xinfo_stream.return_value = {
        "length": 10,
        "first-entry": ["1640995100000-0", {}],
        "last-entry": ["1640995200000-0", {}],
    }
    mock_client.xinfo_groups.return_value = [{"name": "test-group"}]
    mock_client.xinfo_consumers.return_value = [
        {
            "name": "consumer-1",
            "last-delivered-id": "1640995200000-0",
            "pending": 0,
            "idle": 1000,
        }
    ]
    mock_client.xtrim.return_value = 5
    return mock_client


@pytest.fixture
def stream_bridge(mock_redis_client):
    """Create MotetMCPStreamBridge with mocked Redis client."""
    with patch("motet.core.tools.mcp_motet.proxy.motet_mcp_stream_bridge.get_redis_client") as mock_get_client:
        mock_get_client.return_value = mock_redis_client
        bridge = MotetMCPStreamBridge("test_bridge")
        asyncio.run(bridge.initialize())
        return bridge


class TestMotetMCPStreamBridge:
    """
    Validates Redis Streams protocol implementation.
    
    Specification Coverage:
    - ADR Section: Technology-agnostic stream protocol handler
    - Requirements: Stream operations, consumer groups, connection pooling
    """
    
    def test_bridge_initialization(self, mock_redis_client):
        """
        Goal: Validate bridge initializes correctly with proper configuration
        Boundary: Bridge initialization only, no external dependencies
        Success Criteria: 
        - Bridge initializes within 3 seconds
        - Redis connectivity verified
        - Health stats initialized
        """
        with patch('motet.core.tools.mcp_motet.proxy.motet_mcp_stream_bridge.get_redis_client') as mock_get_client:
            mock_get_client.return_value = mock_redis_client
            
            start_time = time.time()
            bridge = MotetMCPStreamBridge("test_bridge")
            asyncio.run(bridge.initialize())
            init_time = time.time() - start_time
            
            assert init_time < 3.0
            assert bridge._running is True
            assert bridge.client_id == "test_bridge"
            assert bridge._health_stats["messages_published"] == 0
            assert bridge._health_stats["messages_consumed"] == 0
            assert bridge._health_stats["errors"] == 0
            
            mock_redis_client.ping.assert_called_once()

    def test_bridge_initialization_retries_on_busy_loading(self, mock_redis_client):
        """
        Goal: Validate initialize() retries while Valkey replays its AOF/RDB dataset
        Boundary: Initialization only; sleep patched so the test is instant
        Success Criteria:
        - BusyLoadingError is retried (not fatal) until ping succeeds
        - Bridge ends up running
        """
        from redis.exceptions import BusyLoadingError

        mock_redis_client.ping.side_effect = [
            BusyLoadingError("Valkey is loading the dataset in memory"),
            BusyLoadingError("Valkey is loading the dataset in memory"),
            True,
        ]
        with patch('motet.core.tools.mcp_motet.proxy.motet_mcp_stream_bridge.get_redis_client') as mock_get_client, \
             patch('motet.core.tools.mcp_motet.proxy.motet_mcp_stream_bridge.asyncio.sleep', new=AsyncMock()) as mock_sleep:
            mock_get_client.return_value = mock_redis_client

            bridge = MotetMCPStreamBridge("test_bridge")
            asyncio.run(bridge.initialize())

            assert bridge._running is True
            assert mock_redis_client.ping.call_count == 3
            assert mock_sleep.await_count == 2
            assert bridge._health_stats["errors"] == 0

    def test_bridge_initialization_busy_loading_deadline_exceeded(self, mock_redis_client, monkeypatch):
        """
        Goal: Validate initialize() eventually gives up if Valkey never finishes loading
        Boundary: Initialization only; deadline forced to zero via env var
        Success Criteria:
        - BusyLoadingError is raised once the deadline is exceeded
        - Error is recorded in health stats and bridge is not running
        """
        from redis.exceptions import BusyLoadingError

        monkeypatch.setenv("MOTET_MCP_STREAM_BRIDGE_INIT_TIMEOUT_SECONDS", "0")
        mock_redis_client.ping.side_effect = BusyLoadingError("Valkey is loading the dataset in memory")
        with patch('motet.core.tools.mcp_motet.proxy.motet_mcp_stream_bridge.get_redis_client') as mock_get_client:
            mock_get_client.return_value = mock_redis_client

            bridge = MotetMCPStreamBridge("test_bridge")
            with pytest.raises(BusyLoadingError):
                asyncio.run(bridge.initialize())

            assert bridge._running is False
            assert bridge._health_stats["errors"] == 1

    def test_publish_message_success(self, stream_bridge, mock_redis_client):
        """
        Goal: Validate messages are correctly published to Redis Streams
        Boundary: Message publishing only, no consumption
        Success Criteria:
        - Message published with correct format
        - Message ID returned
        - Health stats updated
        """
        # Create test message (ADR-0058 format)
        instance_key = "playwright:default:production:user-123:conversation:conv-123"
        request_msg = MCPRequestMessage(
            service_id="playwright",
            instance_key=instance_key,
            tenant_id="default",
            motet_id="production",
            jsonrpc_request={
                "jsonrpc": "2.0",
                "id": "req-123",
                "method": "tools/call",
                "params": {"name": "screenshot", "arguments": {"url": "https://example.com"}}
            }
        )
        
        stream_name = generate_stream_name(
            service_id="playwright",
            visibility=Visibility.USER,
            instance_key=instance_key,
            stream_type=StreamType.REQUESTS,
        )
        
        # Publish message
        with patch(
            "motet.core.tools.mcp_motet.proxy.motet_mcp_stream_bridge.encode_mcp_stream_fields",
            return_value={
                "message_type": "requests",
                "request_id": "req-123",
                "service_id": "playwright",
                "tenant_id": "default",
                "motet_id": "production",
                "_envelope": "{\"encrypted\":true}",
            },
        ):
            message_id = asyncio.run(stream_bridge.publish_message(stream_name, request_msg))
        
        # Verify results
        assert message_id == "1640995200000-0"
        assert stream_bridge._health_stats["messages_published"] == 1
        assert stream_bridge._health_stats["last_activity"] > 0
        
        # Verify Redis call
        mock_redis_client.xadd.assert_called_once()
        call_args = mock_redis_client.xadd.call_args
        assert call_args[0][0] == stream_name  # stream name
        assert "message_type" in call_args[0][1]
        assert call_args[0][1]["message_type"] == "requests"
        assert call_args[0][1].get("service_id") == "playwright"
        assert call_args[0][1].get("request_id") == "req-123"
        assert "_envelope" in call_args[0][1]
        assert "message_data" not in call_args[0][1]  # no plaintext body
    
    def test_publish_message_failure(self, stream_bridge, mock_redis_client):
        """
        Goal: Validate error handling when message publishing fails
        Boundary: Error handling only
        Success Criteria:
        - Exception raised on Redis failure
        - Error stats updated
        - Health stats reflect error
        """
        # Configure mock to raise exception
        mock_redis_client.xadd.side_effect = Exception("Redis connection failed")
        
        instance_key = "test:global"
        request_msg = MCPRequestMessage(
            service_id="test",
            instance_key=instance_key,
            jsonrpc_request={"jsonrpc": "2.0", "id": "test", "method": "test"}
        )

        stream_name = generate_stream_name(
            service_id="test",
            visibility=Visibility.GLOBAL,
            instance_key=instance_key,
            stream_type=StreamType.REQUESTS,
        )

        stream_name = generate_stream_name(
            service_id="test",
            visibility=Visibility.GLOBAL,
            instance_key=instance_key,
            stream_type=StreamType.REQUESTS,
        )
        
        # Attempt to publish message
        with pytest.raises(Exception, match="Redis connection failed"):
            with patch(
                "motet.core.tools.mcp_motet.proxy.motet_mcp_stream_bridge.encode_mcp_stream_fields",
                return_value={
                    "message_type": "requests",
                    "request_id": "test",
                    "service_id": "test",
                    "tenant_id": "default",
                    "motet_id": "default",
                    "_envelope": "{\"encrypted\":true}",
                },
            ):
                asyncio.run(stream_bridge.publish_message(stream_name, request_msg))
        
        # Verify error stats updated
        assert stream_bridge._health_stats["errors"] == 1
        assert stream_bridge._health_stats["last_error"] == "Redis connection failed"
    
    def test_create_consumer_group_new(self, stream_bridge, mock_redis_client):
        """
        Goal: Validate consumer group creation for new groups
        Boundary: Consumer group management
        Success Criteria:
        - New consumer group created successfully
        - Group tracked in bridge state
        - Redis XGROUP CREATE called correctly
        """
        result = asyncio.run(stream_bridge.create_consumer_group("test-stream", "test-group", "0"))
        
        assert result is True
        assert stream_bridge._consumer_groups["test-stream"] == "test-group"
        
        mock_redis_client.xgroup_create.assert_called_once_with(
            "test-stream", "test-group", "0", mkstream=True
        )
    
    def test_create_consumer_group_existing(self, stream_bridge, mock_redis_client):
        """
        Goal: Validate handling of existing consumer groups
        Boundary: Consumer group management
        Success Criteria:
        - Existing group detected correctly
        - No error raised
        - Group still tracked in bridge state
        """
        # Configure mock to simulate existing group
        mock_redis_client.xgroup_create.side_effect = Exception("BUSYGROUP Consumer Group name already exists")
        
        result = asyncio.run(stream_bridge.create_consumer_group("test-stream", "test-group", "0"))
        
        assert result is False
        assert stream_bridge._consumer_groups["test-stream"] == "test-group"
    
    def test_consume_messages_success(self, stream_bridge, mock_redis_client):
        """
        Goal: Validate message consumption from Redis Streams
        Boundary: Message consumption only
        Success Criteria:
        - Messages consumed correctly
        - Consumer group auto-created if needed
        - Message data parsed properly
        """
        # Configure mock response (encrypted-only fields; decrypt is patched)
        mock_redis_client.xreadgroup.return_value = [
            ("test-stream", [
                ("1640995200000-0", {
                    "message_type": "requests",
                    "request_id": "req-123",
                        "service_id": "playwright",
                    "tenant_id": "default",
                    "motet_id": "production",
                    "_envelope": "{\"encrypted\":true}"
                })
            ])
        ]
        
        with patch(
            "motet.core.tools.mcp_motet.proxy.motet_mcp_stream_bridge.decode_mcp_stream_fields",
            return_value={
                "id": "req-123",
                "service_id": "playwright",
                "instance_key": "playwright:default:production:user-123:conversation:conv-123",
                "stream_type": "requests",
                "timestamp": 1640995200,
                "jsonrpc_request": {"jsonrpc": "2.0", "id": "req-123", "method": "tools/call"},
            },
        ):
            messages = asyncio.run(
                stream_bridge.consume_messages("test-stream", "test-group", "consumer-1", count=1, block_ms=1000)
        )
        
        assert len(messages) == 1
        message = messages[0]
        assert message["stream_name"] == "test-stream"
        assert message["message_id"] == "1640995200000-0"
        assert message["message_type"] == "requests"
        assert message["consumer_name"] == "consumer-1"
        assert message["group_name"] == "test-group"
        
        # Verify message data parsed correctly (from decrypted envelope)
        message_data = message["message_data"]
        assert message_data["id"] == "req-123"
        assert message_data["service_id"] == "playwright"
        
        # Verify health stats updated
        assert stream_bridge._health_stats["messages_consumed"] == 1
    
    def test_consume_messages_no_messages(self, stream_bridge, mock_redis_client):
        """
        Goal: Validate behavior when no messages available
        Boundary: Message consumption with empty stream
        Success Criteria:
        - Empty list returned
        - No errors raised
        - Consumer group still created
        """
        mock_redis_client.xreadgroup.return_value = []
        
        messages = asyncio.run(stream_bridge.consume_messages("test-stream", "test-group", "consumer-1", count=1, block_ms=1000))
        
        assert messages == []
        assert stream_bridge._health_stats["messages_consumed"] == 0
    
    def test_consume_messages_invalid_json(self, stream_bridge, mock_redis_client):
        """
        Goal: Validate handling of invalid JSON in messages
        Boundary: Error handling during message parsing
        Success Criteria:
        - Invalid messages skipped
        - Valid messages still processed
        - No exceptions raised
        """
        # Configure mock with invalid envelope / decrypt errors (encrypted-only path)
        mock_redis_client.xreadgroup.return_value = [
            ("test-stream", [
                ("1640995200000-0", {
                    "message_type": "requests",
                    "request_id": "bad-1",
                    "service_id": "test",
                    "tenant_id": "default",
                    "motet_id": "default",
                    "_envelope": "not-json"
                }),
                ("1640995200000-1", {
                    "message_type": "requests",
                    "request_id": "valid",
                    "service_id": "test",
                    "tenant_id": "default",
                    "motet_id": "default",
                    "_envelope": "{\"encrypted\":true}"
                })
            ])
        ]
        
        def _decode_side_effect(*args: Any, **kwargs: Any) -> Dict[str, Any]:
            # First call fails, second succeeds.
            if kwargs.get("request_id") == "bad-1":
                raise ValueError("bad envelope")
            return {"id": "valid", "service_id": "test"}

        with patch(
            "motet.core.tools.mcp_motet.proxy.motet_mcp_stream_bridge.decode_mcp_stream_fields",
            side_effect=_decode_side_effect,
        ):
            messages = asyncio.run(stream_bridge.consume_messages("test-stream", "test-group", "consumer-1", count=2))
        
        # Only valid message should be returned
        assert len(messages) == 1
        assert messages[0]["message_data"]["id"] == "valid"
    
    def test_acknowledge_message(self, stream_bridge, mock_redis_client):
        """
        Goal: Validate message acknowledgment functionality
        Boundary: Message acknowledgment only
        Success Criteria:
        - Message acknowledged correctly
        - Redis XACK called with correct parameters
        - Return value indicates success
        """
        result = asyncio.run(stream_bridge.acknowledge_message("test-stream", "test-group", "1640995200000-0"))
        
        assert result is True
        mock_redis_client.xack.assert_called_once_with(
            "test-stream", "test-group", "1640995200000-0"
        )
    
    def test_get_stream_info_success(self, stream_bridge, mock_redis_client):
        """
        Goal: Validate stream information retrieval
        Boundary: Stream metadata queries
        Success Criteria:
        - Stream stats returned correctly
        - Consumer information included
        - All fields populated
        """
        stream_stats = asyncio.run(stream_bridge.get_stream_info("test-stream"))
        
        assert stream_stats is not None
        assert stream_stats.stream_name == "test-stream"
        assert stream_stats.length == 10
        assert stream_stats.first_entry_id == "1640995100000-0"
        assert stream_stats.last_entry_id == "1640995200000-0"
        assert "test-group" in stream_stats.consumer_groups
        assert len(stream_stats.consumers) == 1
        
        consumer = stream_stats.consumers[0]
        assert consumer.consumer_name == "consumer-1"
        assert consumer.group_name == "test-group"
        assert consumer.last_delivered_id == "1640995200000-0"
        assert consumer.pending_count == 0
        assert consumer.idle_time_ms == 1000
    
    def test_get_stream_info_nonexistent(self, stream_bridge, mock_redis_client):
        """
        Goal: Validate handling of nonexistent streams
        Boundary: Error handling for missing streams
        Success Criteria:
        - None returned for nonexistent stream
        - No exceptions raised
        """
        mock_redis_client.xinfo_stream.side_effect = Exception("no such key")
        
        stream_stats = asyncio.run(stream_bridge.get_stream_info("nonexistent-stream"))
        
        assert stream_stats is None
    
    def test_trim_stream(self, stream_bridge, mock_redis_client):
        """
        Goal: Validate stream trimming functionality
        Boundary: Stream maintenance operations
        Success Criteria:
        - Stream trimmed to specified length
        - Number of removed messages returned
        - Redis XTRIM called correctly
        """
        result = asyncio.run(stream_bridge.trim_stream("test-stream", 100))
        
        assert result == 5  # Mock returns 5
        mock_redis_client.xtrim.assert_called_once_with(
            "test-stream", maxlen=100, approximate=True
        )
    
    def test_health_check_healthy(self, stream_bridge, mock_redis_client):
        """
        Goal: Validate health check for healthy bridge
        Boundary: Health monitoring
        Success Criteria:
        - Status reported as healthy
        - Redis connectivity confirmed
        - Statistics included
        """
        health_status = asyncio.run(stream_bridge.health_check())
        
        assert health_status["status"] == "healthy"
        assert health_status["client_id"] == "test_bridge"
        assert health_status["redis_connected"] is True
        assert "statistics" in health_status
        assert "consumer_groups" in health_status
        assert "active_streams" in health_status
    
    def test_health_check_unhealthy(self, stream_bridge, mock_redis_client):
        """
        Goal: Validate health check for unhealthy bridge
        Boundary: Health monitoring with Redis failure
        Success Criteria:
        - Status reported as unhealthy
        - Error information included
        - Statistics still available
        """
        mock_redis_client.ping.side_effect = Exception("Redis connection failed")
        
        health_status = asyncio.run(stream_bridge.health_check())
        
        assert health_status["status"] == "unhealthy"
        assert health_status["redis_connected"] is False
        assert "error" in health_status
        assert health_status["error"] == "Redis connection failed"
        assert "statistics" in health_status
    
    def test_shutdown(self, stream_bridge):
        """
        Goal: Validate graceful shutdown
        Boundary: Bridge lifecycle management
        Success Criteria:
        - Bridge stops running
        - Shutdown completes without errors
        """
        assert stream_bridge._running is True
        
        asyncio.run(stream_bridge.shutdown())
        
        assert stream_bridge._running is False


class TestConsumerInfo:
    """Test ConsumerInfo Pydantic model."""
    
    def test_consumer_info_creation(self):
        """Test ConsumerInfo model creation and validation."""
        consumer = ConsumerInfo(
            consumer_name="consumer-1",
            group_name="test-group",
            last_delivered_id="1640995200000-0",
            pending_count=5,
            idle_time_ms=1000
        )
        
        assert consumer.consumer_name == "consumer-1"
        assert consumer.group_name == "test-group"
        assert consumer.last_delivered_id == "1640995200000-0"
        assert consumer.pending_count == 5
        assert consumer.idle_time_ms == 1000
    
    def test_consumer_info_serialization(self):
        """Test ConsumerInfo JSON serialization."""
        consumer = ConsumerInfo(
            consumer_name="consumer-1",
            group_name="test-group",
            last_delivered_id="1640995200000-0",
            pending_count=5,
            idle_time_ms=1000
        )
        
        data = consumer.model_dump()
        assert data["consumer_name"] == "consumer-1"
        assert data["pending_count"] == 5
        
        json_str = consumer.model_dump_json()
        assert "consumer-1" in json_str
        assert "test-group" in json_str


class TestStreamStats:
    """Test StreamStats Pydantic model."""
    
    def test_stream_stats_creation(self):
        """Test StreamStats model creation with consumers."""
        consumers = [
            ConsumerInfo(
                consumer_name="consumer-1",
                group_name="group-1",
                last_delivered_id="1640995200000-0",
                pending_count=0,
                idle_time_ms=1000
            )
        ]
        
        stats = StreamStats(
            stream_name="test-stream",
            length=100,
            first_entry_id="1640995100000-0",
            last_entry_id="1640995200000-0",
            consumer_groups=["group-1"],
            consumers=consumers
        )
        
        assert stats.stream_name == "test-stream"
        assert stats.length == 100
        assert stats.first_entry_id == "1640995100000-0"
        assert stats.last_entry_id == "1640995200000-0"
        assert "group-1" in stats.consumer_groups
        assert len(stats.consumers) == 1
        assert stats.consumers[0].consumer_name == "consumer-1"
    
    def test_stream_stats_empty_consumers(self):
        """Test StreamStats with no consumers."""
        stats = StreamStats(
            stream_name="empty-stream",
            length=0,
            first_entry_id="0-0",
            last_entry_id="0-0",
            consumer_groups=[],
            consumers=[]
        )
        
        assert stats.stream_name == "empty-stream"
        assert stats.length == 0
        assert len(stats.consumer_groups) == 0
        assert len(stats.consumers) == 0


class TestIntegrationScenarios:
    """Test integration scenarios and complex workflows."""
    
    def test_full_message_lifecycle(self, stream_bridge, mock_redis_client):
        """
        Goal: Validate complete message lifecycle (publish → consume → acknowledge)
        Boundary: End-to-end message flow
        Success Criteria:
        - Message published successfully
        - Message consumed by consumer
        - Message acknowledged properly
        """
        # Setup mock for consumption (ADR-0058 format)
        instance_key = "playwright:default:production:user-123:conversation:conv-123"
        request_msg = MCPRequestMessage(
            service_id="playwright",
            instance_key=instance_key,
            tenant_id="default",
            motet_id="production",
            jsonrpc_request={"jsonrpc": "2.0", "id": "req-123", "method": "tools/call"}
        )
        
        stream_name = generate_stream_name(
            service_id="playwright",
            visibility=Visibility.USER,
            instance_key=instance_key,
            stream_type=StreamType.REQUESTS,
        )
        
        mock_redis_client.xreadgroup.return_value = [
            (stream_name, [
                ("1640995200000-0", {
                    "message_type": "requests",
                    "request_id": "req-123",
                    "service_id": "playwright",
                    "tenant_id": "default",
                    "motet_id": "production",
                    "_envelope": "{\"encrypted\":true}",
                })
            ])
        ]
        
        # 1. Publish message
        with patch(
            "motet.core.tools.mcp_motet.proxy.motet_mcp_stream_bridge.encode_mcp_stream_fields",
            return_value={
                "message_type": "requests",
                "request_id": "req-123",
                "service_id": "playwright",
                "tenant_id": "default",
                "motet_id": "production",
                "_envelope": "{\"encrypted\":true}",
            },
        ):
            message_id = asyncio.run(stream_bridge.publish_message(stream_name, request_msg))
        assert message_id == "1640995200000-0"
        
        # 2. Consume message
        with patch(
            "motet.core.tools.mcp_motet.proxy.motet_mcp_stream_bridge.decode_mcp_stream_fields",
            return_value=request_msg.model_dump(mode="json"),
        ):
            messages = asyncio.run(stream_bridge.consume_messages(stream_name, "test-group", "consumer-1", count=1))
        assert len(messages) == 1
        consumed_message = messages[0]
        
        # 3. Acknowledge message
        ack_result = asyncio.run(stream_bridge.acknowledge_message(stream_name, "test-group", consumed_message["message_id"]))
        assert ack_result is True
        
        # Verify all operations completed
        assert stream_bridge._health_stats["messages_published"] == 1
        assert stream_bridge._health_stats["messages_consumed"] == 1
    
    def test_multiple_consumer_groups(self, stream_bridge, mock_redis_client):
        """
        Goal: Validate multiple consumer groups on same stream
        Boundary: Consumer group isolation
        Success Criteria:
        - Multiple groups created successfully
        - Each group tracked separately
        - No interference between groups
        """
        # Create multiple consumer groups
        result1 = asyncio.run(stream_bridge.create_consumer_group("test-stream", "group-1", "0"))
        result2 = asyncio.run(stream_bridge.create_consumer_group("test-stream", "group-2", "0"))
        
        assert result1 is True
        assert result2 is True
        
        # Note: In real implementation, we'd need to track multiple groups per stream
        # Current implementation only tracks one group per stream
        assert stream_bridge._consumer_groups["test-stream"] == "group-2"  # Last one wins
        
        # Verify Redis calls
        assert mock_redis_client.xgroup_create.call_count == 2
    
    def test_error_recovery_scenarios(self, stream_bridge, mock_redis_client):
        """
        Goal: Validate error recovery and resilience
        Boundary: Error handling and recovery
        Success Criteria:
        - Temporary failures handled gracefully
        - Operations can succeed after failure
        - Error stats tracked correctly
        """
        # First attempt fails
        mock_redis_client.xadd.side_effect = Exception("Temporary failure")
        
        instance_key = "test:global"
        request_msg = MCPRequestMessage(
            service_id="test",
            instance_key=instance_key,
            jsonrpc_request={"jsonrpc": "2.0", "id": "test", "method": "test"}
        )

        stream_name = generate_stream_name(
            service_id="test",
            visibility=Visibility.GLOBAL,
            instance_key=instance_key,
            stream_type=StreamType.REQUESTS,
        )
        
        with pytest.raises(Exception, match="Temporary failure"):
            with patch(
                "motet.core.tools.mcp_motet.proxy.motet_mcp_stream_bridge.encode_mcp_stream_fields",
                return_value={
                    "message_type": "requests",
                    "request_id": "test",
                    "service_id": "test",
                    "tenant_id": "default",
                    "motet_id": "default",
                    "_envelope": "{\"encrypted\":true}",
                },
            ):
                asyncio.run(stream_bridge.publish_message(stream_name, request_msg))
        
        assert stream_bridge._health_stats["errors"] == 1
        
        # Second attempt succeeds
        mock_redis_client.xadd.side_effect = None
        mock_redis_client.xadd.return_value = "1640995200000-1"
        
        with patch(
            "motet.core.tools.mcp_motet.proxy.motet_mcp_stream_bridge.encode_mcp_stream_fields",
            return_value={
                "message_type": "requests",
                "request_id": "test",
                "service_id": "test",
                "tenant_id": "default",
                "motet_id": "default",
                "_envelope": "{\"encrypted\":true}",
            },
        ):
            message_id = asyncio.run(stream_bridge.publish_message(stream_name, request_msg))
        assert message_id == "1640995200000-1"
        assert stream_bridge._health_stats["messages_published"] == 1


if __name__ == "__main__":
    pytest.main([__file__])
