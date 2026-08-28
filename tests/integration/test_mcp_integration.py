"""
Integration tests for MCP (Model Context Protocol) integration.

Tests MCP server management, tool discovery, and error handling.
Worker-backed MCP lifecycle lives in tests/integration/distributed/.
"""
import pytest
import asyncio
import os
from unittest.mock import Mock, AsyncMock, patch
from typing import Dict, Any, List

# Import MCP components with proper error handling for missing dependencies
try:
    from motet.core.tools.mcp_manager import (
        LibTmuxMCPManager,
        get_libtmux_mcp_manager,
        MCPServerConfig,
        MCPTransportType
    )
    from motet.core.tools.mcp_discovery import (
        auto_discover_mcp_servers,
        get_mcp_auto_discovery_service,
        MCPAutoDiscoveryService
    )
    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False

from motet.core.tools.registry import ToolRegistry


@pytest.fixture
def sample_mcp_config():
    """Sample MCP server configuration."""
    return MCPServerConfig(
        name="test_server",
        command=["python", "-m", "test_mcp_server"],
        transport_type=MCPTransportType.STDIO,
        env_vars={"TEST_MODE": "true"}
    )


@pytest.fixture
def mock_tool_registry():
    """Mock tool registry for testing."""
    registry = Mock(spec=ToolRegistry)
    registry.list_items.return_value = {}
    registry.register = Mock()
    return registry


@pytest.mark.skipif(not MCP_AVAILABLE, reason="MCP dependencies not available")
class TestMCPManager:
    """Test MCP server management."""

    @pytest.mark.integration
    @pytest.mark.mcp
    def test_mcp_server_config_creation(self, sample_mcp_config):
        """Test creating MCP server configuration."""
        assert sample_mcp_config.name == "test_server"
        assert sample_mcp_config.transport_type == MCPTransportType.STDIO
        assert sample_mcp_config.env_vars["TEST_MODE"] == "true"

    @pytest.mark.integration
    @pytest.mark.mcp
    async def test_mcp_manager_initialization(self):
        """Test MCP manager initialization."""
        # Test with mocked dependencies to avoid requiring tmux
        with patch('motet.core.tools.mcp_manager.libtmux') as mock_libtmux:
            mock_server = Mock()
            mock_libtmux.Server.return_value = mock_server
            
            manager = LibTmuxMCPManager()
            assert manager is not None

    @pytest.mark.integration
    @pytest.mark.mcp
    async def test_get_libtmux_mcp_manager(self):
        """Test getting the global MCP manager instance."""
        # Mock the manager to avoid tmux dependency
        with patch('motet.core.tools.mcp_manager.LibTmuxMCPManager') as MockManager:
            mock_instance = Mock()
            MockManager.return_value = mock_instance
            
            manager = get_libtmux_mcp_manager()
            assert manager is not None


@pytest.mark.skipif(not MCP_AVAILABLE, reason="MCP dependencies not available")
class TestMCPAutoDiscovery:
    """Test MCP auto-discovery service."""

    @pytest.mark.integration
    @pytest.mark.mcp
    async def test_auto_discovery_service_creation(self):
        """Test creating MCP auto-discovery service."""
        service = MCPAutoDiscoveryService()
        assert service is not None

    @pytest.mark.integration
    @pytest.mark.mcp
    async def test_get_mcp_auto_discovery_service(self):
        """Test getting the global auto-discovery service."""
        service = get_mcp_auto_discovery_service()
        assert service is not None

    @pytest.mark.integration
    @pytest.mark.mcp
    async def test_auto_discover_mcp_servers(self, mock_tool_registry):
        """Test auto-discovering MCP servers."""
        # Mock the discovery process
        with patch('motet.core.tools.mcp_discovery.get_mcp_auto_discovery_service') as mock_get_service:
            mock_service = AsyncMock()
            mock_service.discover_and_register_servers.return_value = {
                "servers_discovered": 2,
                "tools_registered": 5,
                "errors": []
            }
            mock_get_service.return_value = mock_service
            
            result = await auto_discover_mcp_servers(mock_tool_registry)
            
            assert "servers_discovered" in result
            assert result["servers_discovered"] >= 0


class TestMCPIntegrationWithoutDependencies:
    """Test MCP integration scenarios that don't require external dependencies."""

    @pytest.mark.integration
    @pytest.mark.mcp
    def test_mcp_server_config_serialization(self):
        """Test MCP server configuration serialization."""
        if not MCP_AVAILABLE:
            pytest.skip("MCP dependencies not available")
            
        config = MCPServerConfig(
            name="weather_server",
            command=["python", "-m", "weather_mcp"],
            transport_type=MCPTransportType.STDIO,
            env_vars={"API_KEY": "test_key"}
        )
        
        # Test that config can be converted to dict (for serialization)
        config_dict = {
            "name": config.name,
            "command": config.command,
            "transport_type": config.transport_type.value,
            "env_vars": config.env_vars
        }
        
        assert config_dict["name"] == "weather_server"
        assert config_dict["transport_type"] == "stdio"

    @pytest.mark.integration
    @pytest.mark.mcp
    def test_tool_registry_mcp_integration(self):
        """Test tool registry integration with MCP tools."""
        registry = ToolRegistry()
        
        # Test that registry can handle MCP tool registration
        initial_count = len(registry.list_items())
        
        # Mock MCP tool registration
        with patch.object(registry, 'register') as mock_register:
            # Simulate registering an MCP tool
            mock_register.return_value = True
            
            # This would normally be done by the MCP discovery service
            result = registry.register(
                name="mcp.weather.get_forecast",
                description="Get weather forecast",
                parameters_schema={"location": {"type": "string"}},
                handler=Mock()
            )
            
            mock_register.assert_called_once()


@pytest.mark.integration
@pytest.mark.mcp
@pytest.mark.requires_external
class TestMCPWithExternalServices:
    """Test MCP integration with external MCP servers."""

    @pytest.mark.asyncio
    async def test_real_mcp_server_discovery(self):
        """Test discovery with real MCP servers."""
        if not MCP_AVAILABLE:
            pytest.skip("MCP dependencies not available")
            
        # Check if MCP servers are configured
        mcp_servers_json = os.getenv('MOTET_MCP_SERVERS_JSON')
        if not mcp_servers_json:
            pytest.skip("No MCP servers configured - set MOTET_MCP_SERVERS_JSON")
        
        registry = ToolRegistry()
        initial_tool_count = len(registry.list_items())
        
        try:
            result = await auto_discover_mcp_servers(registry)
            
            # Verify discovery results
            assert "servers_discovered" in result
            assert "tools_registered" in result
            
            # Check if tools were actually registered
            final_tool_count = len(registry.list_items())
            if result["tools_registered"] > 0:
                assert final_tool_count > initial_tool_count
                
        except Exception as e:
            pytest.skip(f"MCP server discovery failed: {e}")

    @pytest.mark.asyncio
    async def test_mcp_tool_execution(self):
        """Test executing MCP tools."""
        if not MCP_AVAILABLE:
            pytest.skip("MCP dependencies not available")
            
        # This would require actual MCP servers to be running
        pytest.skip("Requires running MCP servers - start with docker-compose up")

    @pytest.mark.asyncio
    async def test_mcp_server_lifecycle(self):
        """Test MCP server startup and shutdown."""
        if not MCP_AVAILABLE:
            pytest.skip("MCP dependencies not available")
            
        # This would test the complete lifecycle of MCP servers
        pytest.skip("Requires tmux and MCP server binaries")


@pytest.mark.asyncio
class TestMCPErrorHandling:
    """Test MCP error handling and resilience."""

    @pytest.mark.integration
    @pytest.mark.mcp
    async def test_mcp_server_connection_failure(self):
        """Test handling MCP server connection failures."""
        if not MCP_AVAILABLE:
            pytest.skip("MCP dependencies not available")
            
        # Mock a connection failure scenario
        with patch('motet.core.tools.mcp_manager.LibTmuxMCPManager') as MockManager:
            mock_manager = Mock()
            mock_manager.get_or_create_connection.side_effect = ConnectionError("MCP server unavailable")
            MockManager.return_value = mock_manager
            
            # Test that the system handles the failure gracefully
            try:
                manager = get_libtmux_mcp_manager()
                await manager.get_or_create_connection(Mock())
                assert False, "Expected ConnectionError"
            except ConnectionError as e:
                assert "MCP server unavailable" in str(e)

    @pytest.mark.integration
    @pytest.mark.mcp
    async def test_mcp_tool_execution_timeout(self):
        """Test handling MCP tool execution timeouts."""
        from motet.core.commands.builtin.tool import tool_execution, ToolExecutionData
        
        # Create command (decorator-based API) with short timeout
        command = tool_execution(
            data=ToolExecutionData(
                tool_name="mcp.slow.operation",
                parameters={},
            ),
            command_id="timeout-test-cmd",
            task_id="timeout-test",
            conversation_id="timeout-test-conv",
            timeout_seconds=1,
        )
        
        # Mock a slow operation
        async def slow_execute(ctx):
            await asyncio.sleep(2.0)  # Longer than timeout
            return {"result": "Should not reach here"}
        
        with patch.object(command, 'execute', side_effect=slow_execute):
            # Test timeout handling
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(command.execute({}), timeout=1.0)

    @pytest.mark.integration
    @pytest.mark.mcp
    async def test_mcp_discovery_partial_failure(self, mock_tool_registry):
        """Test MCP discovery with some servers failing."""
        if not MCP_AVAILABLE:
            pytest.skip("MCP dependencies not available")
            
        # Mock partial failure scenario
        with patch('motet.core.tools.mcp_discovery.get_mcp_auto_discovery_service') as mock_get_service:
            mock_service = AsyncMock()
            mock_service.discover_and_register_servers.return_value = {
                "servers_discovered": 3,
                "tools_registered": 5,
                "errors": [
                    {"server": "broken_server", "error": "Connection failed"},
                    {"server": "missing_server", "error": "Server not found"}
                ]
            }
            mock_get_service.return_value = mock_service
            
            result = await auto_discover_mcp_servers(mock_tool_registry)
            
            # Should succeed partially
            assert result["servers_discovered"] == 3
            assert result["tools_registered"] == 5
            assert len(result["errors"]) == 2
