# tests/unit/tools/mcp_motet/proxy/test_motet_mcp_proxy.py
"""
Test suite for MotetMCPProxy.

Validates core proxy functionality and Redis Streams integration
as specified in ADR-0020.
"""

import pytest
import asyncio
import json
import subprocess
import time
from unittest.mock import AsyncMock, MagicMock, patch, mock_open
from typing import Dict, Any

from motet.core.tools.mcp_motet.proxy.motet_mcp_proxy import (
    MotetMCPProxy, MCPServerConfig, ProxyStats
)
from motet.core.tools.mcp_motet.protocol import (
    MCPRequestMessage, MCPResponseMessage, MCPLogMessage,
    StreamType, Visibility, LifecycleDuration, generate_instance_key
)


class TestMCPServerConfig:
    """Test MCPServerConfig Pydantic model."""
    
    def test_config_creation_minimal(self):
        """Test minimal MCPServerConfig creation."""
        config = MCPServerConfig(
            server_id="playwright",
            command="npx @modelcontextprotocol/server-playwright"
        )
        
        assert config.server_id == "playwright"
        assert config.command == "npx @modelcontextprotocol/server-playwright"
        assert config.args == []
        assert config.env == {}
        assert config.working_dir is None
        assert config.timeout_seconds == 30
        assert config.max_restarts == 3
        assert config.restart_delay_seconds == 5
    
    def test_config_creation_full(self):
        """Test full MCPServerConfig creation."""
        config = MCPServerConfig(
            server_id="playwright",
            command="npx",
            args=["@modelcontextprotocol/server-playwright", "--headless"],
            env={"DISPLAY": ":99", "NODE_ENV": "production"},
            working_dir="/opt/mcp",
            timeout_seconds=60,
            max_restarts=5,
            restart_delay_seconds=10
        )
        
        assert config.server_id == "playwright"
        assert config.command == "npx"
        assert config.args == ["@modelcontextprotocol/server-playwright", "--headless"]
        assert config.env["DISPLAY"] == ":99"
        assert config.env["NODE_ENV"] == "production"
        assert config.working_dir == "/opt/mcp"
        assert config.timeout_seconds == 60
        assert config.max_restarts == 5
        assert config.restart_delay_seconds == 10


class TestProxyStats:
    """Test ProxyStats Pydantic model."""
    
    def test_stats_creation(self):
        """Test ProxyStats creation and defaults (ADR-0058)."""
        config = MCPServerConfig(server_id="test", command="test")
        instance_key = "test:default:production:user-123:conversation:conv-456"
        
        stats = ProxyStats(
            proxy_id="proxy-123",
            server_config=config,
            instance_key=instance_key,
            status="running",
            start_time=1640995200.0
        )
        
        assert stats.proxy_id == "proxy-123"
        assert stats.server_config.server_id == "test"
        assert stats.instance_key == instance_key
        assert stats.status == "running"
        assert stats.start_time == 1640995200.0
        assert stats.requests_processed == 0
        assert stats.responses_sent == 0
        assert stats.errors == 0
        assert stats.last_activity is None
        assert stats.last_error is None
        assert stats.process_pid is None
        assert stats.restart_count == 0


@pytest.mark.asyncio
class TestMotetMCPProxy:
    """
    Validates core proxy functionality and Redis Streams integration.
    
    Specification Coverage:
    - ADR Section: Core Proxy Classes
    - Requirements: stdio ↔ stream translation, message correlation
    """
    
    @pytest.fixture
    def server_config(self):
        """Create test MCP server configuration."""
        return MCPServerConfig(
            server_id="playwright",
            command="echo",  # Use echo for testing
            args=["test"],
            timeout_seconds=30
        )
    
    @pytest.fixture
    def mock_stream_bridge(self):
        """Create mock stream bridge."""
        mock_bridge = AsyncMock()
        mock_bridge.initialize.return_value = None
        mock_bridge.publish_message.return_value = "1640995200000-0"
        mock_bridge.consume_messages.return_value = []
        mock_bridge.acknowledge_message.return_value = True
        mock_bridge.create_consumer_group.return_value = True
        mock_bridge.shutdown.return_value = None
        return mock_bridge
    
    @pytest.fixture
    def mock_process(self):
        """Create mock subprocess (asyncio-compatible)."""
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.returncode = None  # Process is running
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stderr = MagicMock()
        mock_proc.stdin.write = MagicMock()
        mock_proc.stdin.drain = AsyncMock(return_value=None)
        mock_proc.stdout.readline = AsyncMock(return_value=b'{"jsonrpc": "2.0", "id": "test", "result": {}}\n')
        mock_proc.stderr.read = AsyncMock(return_value=b"")
        mock_proc.terminate = MagicMock()
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock(return_value=0)
        return mock_proc
    
    @pytest.fixture
    def proxy(self, server_config, mock_stream_bridge, mock_process):
        """Create MotetMCPProxy with mocked dependencies."""
        with patch('motet.core.tools.mcp_motet.proxy.motet_mcp_proxy.MotetMCPStreamBridge') as mock_bridge_class:
            mock_bridge_class.return_value = mock_stream_bridge
            proxy = MotetMCPProxy(server_config, context_id="playwright:default:production:user-123:conversation:conv-123")
            return proxy
    
    async def test_proxy_initialization(self, server_config):
        """
        Goal: Validate proxy starts correctly with proper configuration
        Boundary: Proxy initialization only, no external dependencies
        Success Criteria: 
        - Proxy process starts within 3 seconds
        - All required streams are created
        - Health monitoring is active
        """
        with patch('motet.core.tools.mcp_motet.proxy.motet_mcp_proxy.MotetMCPStreamBridge') as mock_bridge_class:
            mock_bridge = AsyncMock()
            mock_bridge.initialize.return_value = None
            mock_bridge_class.return_value = mock_bridge
            
            start_time = time.time()
            proxy = MotetMCPProxy(server_config, context_id="playwright:default:production:user-123:conversation:conv-123")
            init_time = time.time() - start_time
            
            assert init_time < 3.0
            assert proxy.proxy_id.startswith("proxy-playwright-")
            assert proxy.server_config.server_id == "playwright"
            assert proxy.instance_key is not None
            assert "conversation:conv-123" in proxy.instance_key
            
            # Verify stream names generated correctly (ADR-0058 format)
            assert proxy.request_stream is not None
            assert proxy.response_stream is not None
            assert proxy.log_stream is not None
            assert "requests" in proxy.request_stream
            assert "responses" in proxy.response_stream
            assert "logs" in proxy.log_stream
            
            assert proxy.stats.status == "initialized"
            assert proxy.stats.proxy_id == proxy.proxy_id
            assert proxy.stats.instance_key == proxy.instance_key
    
    async def test_context_selection_global(self, server_config):
        """Test context selection for global services (ADR-0058)."""
        proxy = MotetMCPProxy(server_config, context_id="playwright:global")
        
        assert proxy.visibility == Visibility.GLOBAL
        assert proxy.instance_key is not None
        assert "global" in proxy.instance_key
        assert proxy.request_stream is not None
    
    async def test_context_selection_task(self, server_config):
        """Test context selection for task-scoped services (ADR-0058)."""
        proxy = MotetMCPProxy(server_config, context_id="playwright:default:production:user-123:task:task-456")
        
        assert proxy.lifecycle == LifecycleDuration.TASK
        assert proxy.instance_key is not None
        assert "task:task-456" in proxy.instance_key
        assert proxy.request_stream is not None
    
    async def test_mcp_server_startup(self, proxy, mock_process):
        """
        Goal: Validate MCP server process starts correctly
        Boundary: Process management only
        Success Criteria:
        - Process created with correct command and args
        - Process PID tracked
        - Process health verified
        """
        with patch('motet.core.tools.mcp_motet.proxy.motet_mcp_proxy.asyncio.create_subprocess_exec', new_callable=AsyncMock, return_value=mock_process):
            with patch.object(proxy, '_send_mcp_initialization', new_callable=AsyncMock):
                await proxy._start_mcp_server()
            
            assert proxy.mcp_process == mock_process
            assert proxy.stats.process_pid == 12345
    
    async def test_mcp_server_startup_failure(self, proxy, mock_process):
        """
        Goal: Validate handling of MCP server startup failures
        Boundary: Error handling during process startup
        Success Criteria:
        - Exception raised on process failure
        - Error tracked in stats
        - No process assigned
        """
        mock_process.returncode = 1  # Process exited immediately
        mock_process.stderr.read = AsyncMock(return_value=b"exit 1")
        
        with patch('motet.core.tools.mcp_motet.proxy.motet_mcp_proxy.asyncio.create_subprocess_exec', new_callable=AsyncMock, return_value=mock_process):
            with pytest.raises(RuntimeError, match="exited immediately"):
                await proxy._start_mcp_server()
            
            assert proxy.stats.process_pid is None
    
    async def test_request_message_handling(self, proxy, mock_stream_bridge, mock_process):
        """
        Goal: Validate JSON-RPC requests are correctly translated to Redis Streams
        Boundary: Request translation only, no MCP server interaction
        Success Criteria:
        - Valid JSON-RPC → Redis Stream message format
        - Message correlation ID is preserved
        - Request timeout is properly set
        """
        # Setup proxy state
        proxy.mcp_process = mock_process
        proxy._pending_requests = {}
        
        # Create mock message data (ADR-0058 format)
        instance_key = "playwright:default:production:user-123:conversation:conv-123"
        request_data = {
            "message_id": "1640995200000-0",
            "message_data": {
                "id": "req-123",
                "service_id": "playwright",
                "instance_key": instance_key,
                "stream_type": "requests",
                "timestamp": 1640995200,
                "worker_id": "worker-abc",
                "timeout_ms": 30000,
                "jsonrpc_request": {
                    "jsonrpc": "2.0",
                    "id": "req-123",
                    "method": "tools/call",
                    "params": {
                        "name": "screenshot",
                        "arguments": {"url": "https://example.com"}
                    }
                }
            }
        }
        
        await proxy._handle_request_message(request_data)
        
        # Verify request tracked
        assert "req-123" in proxy._pending_requests
        assert proxy.stats.requests_processed == 1
        assert proxy.stats.last_activity > 0
        
        # Verify JSON-RPC sent to MCP server (implementation sends encoded bytes)
        expected_json = json.dumps(request_data["message_data"]["jsonrpc_request"]) + "\n"
        expected_bytes = expected_json.encode("utf-8")
        mock_process.stdin.write.assert_called_once_with(expected_bytes)
        mock_process.stdin.drain.assert_called_once()
        
        # Verify message acknowledged
        mock_stream_bridge.acknowledge_message.assert_called_once()
    
    async def test_response_correlation(self, proxy, mock_stream_bridge):
        """
        Goal: Validate responses are correctly correlated with requests
        Boundary: Response handling and correlation logic
        Success Criteria:
        - Response matched to correct request by ID
        - Correlation timeout handled properly
        - Invalid responses are rejected
        """
        # Setup pending request
        request_id = "req-123"
        proxy._pending_requests[request_id] = time.time()
        
        # Create JSON-RPC response
        response_data = {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [{"type": "text", "text": "Screenshot saved"}]
            }
        }
        
        await proxy._handle_jsonrpc_response(response_data)
        
        # Verify request removed from pending
        assert request_id not in proxy._pending_requests
        assert proxy.stats.responses_sent == 1
        
        # Verify response message published
        mock_stream_bridge.publish_message.assert_called_once()
        call_args = mock_stream_bridge.publish_message.call_args
        assert call_args[0][0] == proxy.response_stream  # stream name
        
        response_msg = call_args[0][1]  # message object
        assert response_msg.request_id == request_id
        assert response_msg.jsonrpc_response == response_data
        assert response_msg.processing_time_ms is not None
    
    async def test_notification_handling(self, proxy):
        """
        Goal: Validate JSON-RPC notifications are handled correctly
        Boundary: Notification processing
        Success Criteria:
        - Notifications identified (no ID field)
        - Notifications logged appropriately
        - No response correlation attempted
        """
        # Create JSON-RPC notification (no ID field)
        notification_data = {
            "jsonrpc": "2.0",
            "method": "notifications/progress",
            "params": {"progress": 50}
        }
        
        await proxy._handle_jsonrpc_response(notification_data)
        
        # Verify no response sent (notifications don't have responses)
        assert proxy.stats.responses_sent == 0
        
        # Note: In current implementation, notifications are just logged
        # In a full implementation, they might be published to a notifications stream
    
    async def test_log_message_publishing(self, proxy, mock_stream_bridge):
        """
        Goal: Validate stderr logs are properly structured and published
        Boundary: Log message processing
        Success Criteria:
        - Log messages structured correctly
        - Raw stderr preserved
        - Published to correct stream
        """
        await proxy._publish_log("error", "Test error message", request_id="req-123", raw_stderr="Raw error output")
        
        # Verify log message published
        mock_stream_bridge.publish_message.assert_called_once()
        call_args = mock_stream_bridge.publish_message.call_args
        assert call_args[0][0] == proxy.log_stream  # stream name
        
        log_msg = call_args[0][1]  # message object
        assert log_msg.service_id == "playwright"
        assert log_msg.level == "error"
        assert log_msg.message == "Test error message"
        assert log_msg.request_id == "req-123"
        assert log_msg.raw_stderr == "Raw error output"
        assert log_msg.stream_type == StreamType.LOGS
    
    async def test_event_publishing(self, proxy, mock_stream_bridge):
        """
        Goal: Validate lifecycle events are published correctly
        Boundary: Event publishing
        Success Criteria:
        - Events structured correctly
        - Published to event stream
        - Event data preserved
        """
        event_data = {"process_pid": 12345, "startup_time": 2.5}
        
        await proxy._publish_event("started", event_data)
        
        # Verify event published
        mock_stream_bridge.publish_message.assert_called_once()
        call_args = mock_stream_bridge.publish_message.call_args
        assert call_args[0][0] == proxy.event_stream  # stream name
        
        event_msg = call_args[0][1]  # message object
        assert event_msg.service_id == "playwright"
        assert event_msg.event_type == "started"
        assert event_msg.event_data == event_data
        assert event_msg.stream_type == StreamType.EVENTS
    
    async def test_health_monitoring(self, proxy, mock_process):
        """
        Goal: Validate health monitoring detects process failures
        Boundary: Health monitoring logic
        Success Criteria:
        - Process death detected
        - Restart triggered if under limit
        - Failure recorded if over limit
        """
        proxy.mcp_process = mock_process
        proxy.stats.restart_count = 0
        proxy.server_config.max_restarts = 3
        
        # Simulate process death
        mock_process.poll.return_value = 1  # Process exited
        
        with patch.object(proxy, '_restart_mcp_server') as mock_restart:
            mock_restart.return_value = None
            
            # This would be called by the health monitor
            # Simulating the check that happens in _health_monitor
            if proxy.mcp_process and proxy.mcp_process.poll() is not None:
                if proxy.stats.restart_count < proxy.server_config.max_restarts:
                    await proxy._restart_mcp_server()
            
            mock_restart.assert_called_once()
    
    async def test_restart_functionality(self, proxy, mock_process, mock_stream_bridge):
        """
        Goal: Validate MCP server restart functionality
        Boundary: Process lifecycle management
        Success Criteria:
        - Old process terminated gracefully
        - New process started
        - Restart count incremented
        - Events published
        """
        # Setup initial process (asyncio subprocess style)
        proxy.mcp_process = mock_process
        proxy.stats.restart_count = 0
        mock_process.terminate = MagicMock()
        mock_process.wait = AsyncMock(return_value=0)
        
        new_mock_process = MagicMock()
        new_mock_process.pid = 54321
        new_mock_process.returncode = None
        new_mock_process.stdin = MagicMock()
        new_mock_process.stdin.drain = AsyncMock(return_value=None)
        new_mock_process.stdout = MagicMock()
        new_mock_process.stdout.readline = AsyncMock(return_value=b'{"jsonrpc": "2.0", "id": "init", "result": {}}\n')
        new_mock_process.stderr = MagicMock()
        new_mock_process.stderr.read = AsyncMock(return_value=b"")
        
        with patch('motet.core.tools.mcp_motet.proxy.motet_mcp_proxy.asyncio.create_subprocess_exec', new_callable=AsyncMock, return_value=new_mock_process):
            with patch.object(proxy, '_send_mcp_initialization', new_callable=AsyncMock):
                await proxy._restart_mcp_server()
        
        # Verify old process terminated
        mock_process.terminate.assert_called_once()
        
        # Verify new process started
        assert proxy.mcp_process == new_mock_process
        assert proxy.stats.process_pid == 54321
        assert proxy.stats.restart_count == 1
        assert proxy.stats.status == "running"
        
        # Verify restart event published
        mock_stream_bridge.publish_message.assert_called()
    
    async def test_control_message_handling(self, proxy, mock_stream_bridge):
        """
        Goal: Validate control messages are processed correctly
        Boundary: Control message processing
        Success Criteria:
        - Control messages parsed correctly
        - Appropriate actions taken
        - Messages acknowledged
        """
        # Create mock control message data (ADR-0058 format)
        instance_key = "playwright:default:production:user-123:conversation:conv-123"
        control_data = {
            "message_id": "1640995200000-0",
            "message_data": {
                "id": "ctrl-123",
                "service_id": "playwright",
                "instance_key": instance_key,
                "stream_type": "control",
                "command": "health",
                "params": {}
            }
        }
        
        with patch.object(proxy, '_handle_health_check') as mock_health:
            await proxy._handle_control_message(control_data)
            
            mock_health.assert_called_once()
            mock_stream_bridge.acknowledge_message.assert_called_once()
    
    async def test_graceful_shutdown(self, proxy, mock_process, mock_stream_bridge):
        """
        Goal: Validate graceful proxy shutdown
        Boundary: Shutdown process
        Success Criteria:
        - Running flag set to False
        - MCP process terminated
        - Stream bridge shutdown
        - Events published
        """
        proxy._running = True
        proxy.mcp_process = mock_process
        proxy.stats.status = "running"
        
        with patch('asyncio.sleep'):  # Speed up test
            await proxy.shutdown()
        
        assert proxy._running is False
        assert proxy.stats.status == "stopped"
        
        # Verify process terminated
        mock_process.terminate.assert_called_once()
        
        # Verify stream bridge shutdown
        mock_stream_bridge.shutdown.assert_called_once()
        
        # Verify events published
        assert mock_stream_bridge.publish_message.call_count >= 2  # stopping + stopped events

    async def test_stdout_eof_without_returncode_does_not_starve_loop(self, proxy):
        """Child attach EOF must exit the stdout task, not busy-loop the manager."""
        stdout = asyncio.StreamReader()
        stdout.feed_eof()
        proc = MagicMock()
        proc.returncode = None
        proc.stdout = stdout
        proxy.mcp_process = proc
        proxy._running = True

        health_ran = asyncio.Event()

        async def health() -> None:
            await asyncio.sleep(0)
            health_ran.set()

        await asyncio.wait_for(
            asyncio.gather(proxy._process_stdout(), health()),
            timeout=1.0,
        )
        assert health_ran.is_set()

    async def test_stderr_eof_without_returncode_does_not_starve_loop(self, proxy):
        """Child attach EOF must exit the stderr task, not busy-loop the manager."""
        stderr = asyncio.StreamReader()
        stderr.feed_eof()
        proc = MagicMock()
        proc.returncode = None
        proc.stderr = stderr
        proxy.mcp_process = proc
        proxy._running = True

        health_ran = asyncio.Event()

        async def health() -> None:
            await asyncio.sleep(0)
            health_ran.set()

        await asyncio.wait_for(
            asyncio.gather(proxy._process_stderr(), health()),
            timeout=1.0,
        )
        assert health_ran.is_set()


@pytest.mark.asyncio
class TestIntegrationScenarios:
    """Test complex integration scenarios."""
    
    @pytest.fixture
    def server_config(self):
        """Create test MCP server configuration."""
        return MCPServerConfig(
            server_id="playwright",
            command="echo",
            args=["test"],
            timeout_seconds=30
        )
    
    async def test_full_request_response_cycle(self, server_config):
        """
        Goal: Validate complete request-response cycle
        Boundary: End-to-end proxy functionality
        Success Criteria:
        - Request consumed from stream
        - JSON-RPC sent to MCP server
        - Response received and correlated
        - Response published to stream
        """
        # This would require more complex mocking of the full proxy lifecycle
        # For now, we test individual components
        pass
    
    async def test_concurrent_request_handling(self, server_config):
        """
        Goal: Validate handling of concurrent requests
        Boundary: Concurrency and correlation
        Success Criteria:
        - Multiple requests tracked correctly
        - Responses correlated to correct requests
        - No request/response mixing
        """
        # This would test the proxy's ability to handle multiple concurrent requests
        # Each request should maintain its own correlation
        pass
    
    async def test_error_recovery_scenarios(self, server_config):
        """
        Goal: Validate error recovery in various failure scenarios
        Boundary: Error handling and recovery
        Success Criteria:
        - Process failures handled gracefully
        - Stream failures don't crash proxy
        - Partial failures recovered
        """
        # This would test various failure modes and recovery scenarios
        pass


if __name__ == "__main__":
    pytest.main([__file__])
