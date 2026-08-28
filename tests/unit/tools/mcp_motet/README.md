# Motet MCP Test Suite

Comprehensive test suite for the Motet Streams MCP communication architecture.

## Test Structure

### Unit Tests

#### Protocol Tests (`test_protocol.py`)
- **Stream Types & Context Types**: Validates enum values match the protocol specification
- **Message Formats**: Tests all message types (Request, Response, Log, Control, Event)
- **Stream Naming**: Tests stream name generation and parsing with various contexts
- **Context Selection**: Tests context selection algorithm for different service types
- **Message Validation**: Tests Pydantic validation and error handling
- **Edge Cases**: Tests unicode, long strings, special characters

#### Stream Bridge Tests (`test_motet_mcp_stream_bridge.py`)
- **Bridge Initialization**: Tests Redis connectivity and health stats
- **Message Publishing**: Tests Redis Streams message publishing with proper format
- **Consumer Groups**: Tests consumer group creation and management
- **Message Consumption**: Tests message consumption with proper parsing
- **Stream Information**: Tests stream metadata retrieval and statistics
- **Health Monitoring**: Tests health checks and error tracking
- **Error Handling**: Tests Redis failures and recovery scenarios

#### Proxy Tests (`test_motet_mcp_proxy.py`)
- **Proxy Initialization**: Tests proxy startup and stream configuration
- **MCP Server Management**: Tests process startup, monitoring, and restart
- **Request Handling**: Tests JSON-RPC request translation and forwarding
- **Response Correlation**: Tests response matching and correlation by ID
- **Log Processing**: Tests stderr log parsing and publishing
- **Event Publishing**: Tests lifecycle event publishing
- **Control Messages**: Tests control message handling (health, restart, stop)
- **Error Recovery**: Tests process failure detection and restart

#### Manager Tests (`test_motet_mcp_manager.py`, `manager/test_instance_isolation.py`)
- **Service Registration**: Tests service configuration and registration
- **Instance Management**: Tests context-aware instance creation and reuse
- **Tool Calling**: Tests end-to-end tool calls through the manager
- **Context Isolation**: Tests different contexts create separate instances
- **Resource Limits**: Tests max instance limits and enforcement
- **Health Monitoring**: Tests service and instance health reporting
- **Cleanup**: Tests instance cleanup and resource management
- **Error Handling**: Tests various error scenarios and recovery
- **Failed-service retry**: Health loop recreates a configured HTTP service that failed bootstrap with no live instance
- **HTTP Docker URL**: Attach-to-singleton copies the owner rewritten `base_url`; localhost is rewritten for Docker sidecars (`test_http_docker_base_url_rewrite.py`)

### Integration Tests (`test_motet_mcp_integration.py`)

#### End-to-End Workflows
- **Complete Tool Call Flow**: Manager → Instance → Proxy → MCP Server → Response
- **Multi-Instance Context Isolation**: Tests different contexts work independently
- **Proxy-Bridge Integration**: Tests message flow between proxy and stream bridge
- **Error Handling & Recovery**: Tests failure scenarios and recovery
- **Concurrent Operations**: Tests multiple simultaneous operations
- **Health Monitoring Integration**: Tests health monitoring across all components

#### Performance Tests
- **Message Throughput**: Tests 100+ messages per second per stream
- **Instance Creation Performance**: Tests instance creation under 5 seconds
- **Resource Usage**: Tests memory and CPU usage patterns

## Test Configuration

### Fixtures (`conftest.py`)
- **Mock Redis Client**: Provides realistic Redis behavior for testing
- **Mock MCP Process**: Simulates MCP server processes
- **Test Data Factory**: Creates standard test data objects
- **Environment Setup**: Configures test environment variables

### Markers
- `@pytest.mark.integration`: Integration tests requiring multiple components
- `@pytest.mark.performance`: Performance-focused tests
- `@pytest.mark.slow`: Tests that may take longer to run

## Running Tests

### Using the Test Runner
```bash
# Run all tests
python run_motet_mcp_tests.py

# Run unit tests only
python run_motet_mcp_tests.py --type unit

# Run integration tests only
python run_motet_mcp_tests.py --type integration

# Run with coverage
python run_motet_mcp_tests.py --coverage

# Run with verbose output and parallel execution
python run_motet_mcp_tests.py --verbose --parallel
```

### Using pytest directly
```bash
# Run all Motet MCP tests
pytest tests/unit/tools/mcp_motet/ tests/integration/test_motet_mcp_integration.py

# Run specific test file
pytest tests/unit/tools/mcp_motet/test_protocol.py -v

# Run tests with coverage
pytest tests/unit/tools/mcp_motet/ --cov=motet.core.tools.mcp_motet --cov-report=html

# Run integration tests only
pytest -m integration

# Run performance tests only
pytest -m performance
```

## Test Coverage Goals

- **Protocol Module**: 100% coverage (all message types and utilities)
- **Stream Bridge**: 95% coverage (core Redis operations)
- **Proxy**: 90% coverage (complex process management)
- **Manager**: 90% coverage (high-level orchestration)
- **Overall**: 90%+ coverage across all modules

## Success Criteria

### Functional Requirements
- ✅ All message formats validate correctly
- ✅ Stream naming follows conventions
- ✅ Context selection algorithm works as specified
- ✅ Redis Streams operations function properly
- ✅ Consumer groups handle load balancing
- ✅ MCP server processes start and communicate correctly
- ✅ Request/response correlation works across streams
- ✅ Health monitoring detects failures
- ✅ Error recovery and restart mechanisms work
- ✅ Resource limits are enforced
- ✅ Cleanup processes work correctly

### Performance Requirements
- ✅ Message throughput: 100+ messages/second per stream
- ✅ Instance creation: Under 5 seconds
- ✅ Memory usage: Reasonable for long-running processes
- ✅ Error recovery: Under 10 seconds for automatic restart

### Reliability Requirements
- ✅ Error handling: All failure modes handled gracefully
- ✅ State preservation: MCP server state preserved during proxy restarts
- ✅ Message durability: Messages persisted in Redis Streams
- ✅ Consumer group reliability: Messages not lost during failures

## Test Data

### Standard Test Scenarios
- **Playwright Service**: Stateful browser automation service
- **Weather Service**: Stateless API service
- **Multiple Contexts**: Conversation, task, tenant, and shared contexts
- **Error Conditions**: Process failures, Redis failures, timeout scenarios
- **Load Scenarios**: Multiple concurrent requests and instances

### Mock Configurations
- **Redis Client**: Realistic Redis Streams behavior without external dependency
- **MCP Processes**: Simulated MCP server processes with realistic stdio behavior
- **Network Conditions**: Simulated network delays and failures

## Debugging Tests

### Common Issues
1. **Async/Await**: Ensure all async operations are properly awaited
2. **Mock Patching**: Verify mocks are patched at the correct import path
3. **Event Loop**: Use proper async fixtures for event loop management
4. **Resource Cleanup**: Ensure tests clean up resources in finally blocks

### Debug Flags
```bash
# Run with maximum verbosity
pytest tests/unit/tools/mcp_motet/ -vvv --tb=long

# Run single test with debugging
pytest tests/unit/tools/mcp_motet/test_protocol.py::TestStreamNaming::test_generate_stream_name_shared_context -vvv --pdb

# Run with warnings enabled
pytest tests/unit/tools/mcp_motet/ --disable-warnings=false
```

This test suite ensures the Motet Streams MCP architecture meets all requirements specified in and provides confidence for production deployment.
