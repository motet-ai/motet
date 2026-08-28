# tests/unit/tools/mcp_motet/conftest.py
"""
Pytest configuration and fixtures for Motet MCP tests.

Provides common fixtures and test configuration for all Motet MCP tests.
"""

import pytest
import asyncio
import os
from unittest.mock import MagicMock, AsyncMock, patch


@pytest.fixture(scope="session")
def event_loop():
    """Create an instance of the default event loop for the test session."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def mock_redis_client():
    """Create a mock Redis client for testing."""
    mock_client = AsyncMock()
    mock_client.ping.return_value = True
    mock_client.xadd.return_value = "1640995200000-0"
    mock_client.xgroup_create.return_value = True
    mock_client.xreadgroup.return_value = []
    mock_client.xack.return_value = 1
    mock_client.xinfo_stream.return_value = {
        "length": 0,
        "first-entry": ["0-0", {}],
        "last-entry": ["0-0", {}]
    }
    mock_client.xinfo_groups.return_value = []
    mock_client.xinfo_consumers.return_value = []
    mock_client.xtrim.return_value = 0
    return mock_client


@pytest.fixture
def mock_subprocess_popen():
    """Create a mock subprocess.Popen for MCP server processes."""
    mock_process = MagicMock()
    mock_process.pid = 12345
    mock_process.poll.return_value = None  # Process is running
    mock_process.returncode = None
    
    # Mock stdin/stdout/stderr
    mock_process.stdin = MagicMock()
    mock_process.stdout = MagicMock()
    mock_process.stderr = MagicMock()
    
    # Mock realistic behavior
    mock_process.stdin.write = MagicMock()
    mock_process.stdin.flush = MagicMock()
    mock_process.stdout.readline = MagicMock(return_value="")
    mock_process.stderr.readline = MagicMock(return_value="")
    mock_process.terminate = MagicMock()
    mock_process.kill = MagicMock()
    
    return mock_process


@pytest.fixture(autouse=True)
def mock_redis_manager():
    """Automatically mock Redis manager for all tests."""
    with patch('motet.core.distributed.redis_manager.get_redis_client') as mock_get_client:
        mock_client = AsyncMock()
        mock_client.ping.return_value = True
        mock_get_client.return_value = mock_client
        yield mock_client


@pytest.fixture
def test_env_vars():
    """Set up test environment variables."""
    original_env = os.environ.copy()
    
    # Set test environment variables
    test_env = {
        'MOTET_REDIS_URL': 'redis://localhost:6379/15',  # Use test database
        'TESTING': '1'
    }
    
    os.environ.update(test_env)
    
    yield test_env
    
    # Restore original environment
    os.environ.clear()
    os.environ.update(original_env)


# Pytest configuration
def pytest_configure(config):
    """Configure pytest for Motet MCP tests."""
    # Add custom markers
    config.addinivalue_line(
        "markers", "integration: mark test as integration test"
    )
    config.addinivalue_line(
        "markers", "performance: mark test as performance test"
    )
    config.addinivalue_line(
        "markers", "slow: mark test as slow running"
    )


def pytest_collection_modifyitems(config, items):
    """Modify test collection to add markers based on test location."""
    for item in items:
        # Add integration marker for integration tests
        if "integration" in str(item.fspath):
            item.add_marker(pytest.mark.integration)
        
        # Add slow marker for tests that might be slow
        if any(keyword in item.name.lower() for keyword in ["performance", "concurrent", "stress"]):
            item.add_marker(pytest.mark.slow)


# Test data factories
class TestDataFactory:
    """Factory for creating test data objects."""
    
    @staticmethod
    def create_mcp_request_data():
        """Create standard MCP request test data."""
        return {
            "jsonrpc": "2.0",
            "id": "test-request-123",
            "method": "tools/call",
            "params": {
                "name": "screenshot",
                "arguments": {
                    "url": "https://example.com",
                    "selector": "body"
                }
            }
        }
    
    @staticmethod
    def create_mcp_response_data():
        """Create standard MCP response test data."""
        return {
            "jsonrpc": "2.0",
            "id": "test-request-123",
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": "Screenshot saved successfully"
                    }
                ]
            }
        }
    
    @staticmethod
    def create_mcp_error_data():
        """Create standard MCP error test data."""
        return {
            "jsonrpc": "2.0",
            "id": "test-request-123",
            "error": {
                "code": -32602,
                "message": "Invalid params",
                "data": {
                    "details": "URL parameter is required"
                }
            }
        }


@pytest.fixture
def test_data_factory():
    """Provide test data factory."""
    return TestDataFactory
