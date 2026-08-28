# Test Migration Plan

## 📊 Current Status

**Remaining Tests to Migrate**: 50+ test files in `tests/` root  
**Already Migrated**: 4 unit tests in `tests/unit/core/`  
**Distributed Tests**: 15 tests in `tests/distributed/` (need updating)

## 🎯 Test Categorization

### **Unit Tests** (No external dependencies)
Move to `tests/unit/`:

#### Core Components (`tests/unit/core/`)
- `test_breaker_transitions.py` → `test_circuit_breaker_advanced.py`
- `test_memory_consolidation.py` → `test_memory.py`  
- `test_memory_scopes.py` → `test_memory_scopes.py`
- `test_tool_parser_properties.py` → `test_tools.py`
- `test_entities_pii.py` → `test_security.py`
- `test_reflection_unit.py` → `test_metacognition.py`

#### Interfaces (`tests/unit/interfaces/`)
- `test_cli_ops.py` → `test_cli.py`
- `test_dialogue_context_and_telemetry.py` → `test_dialogue.py`

#### Models (`tests/unit/models/`)
- `test_reasoning_selection.py` → `test_reasoning.py`

#### Orchestration (`tests/unit/orchestration/`)
- `test_orchestrator_priority.py` → `test_orchestrator.py`

### **Integration Tests** (Require Redis/internal services)
Move to `tests/integration/`:

#### API Integration
- `test_api.py` → `test_http_api.py`
- `test_api_auth_and_tools.py` → `test_api_auth.py`
- `test_tools_endpoint.py` → `test_tool_endpoints.py`
- `test_tools_plugins_endpoint.py` → `test_plugin_endpoints.py`
- `test_retrieval_endpoints.py` → `test_memory_endpoints.py`
- `test_traces_api.py` → `test_tracing_endpoints.py`

#### Memory Integration
- `test_assistant_memory.py` → `test_memory_integration.py`
- `test_rag.py` → `test_rag_integration.py`

#### Reasoning Integration  
- `test_reasoning_and_ws.py` → `test_reasoning_integration.py`
- `test_enhanced_orchestration.py` → `test_orchestration_integration.py`
- `test_unified_orchestration.py` → `test_orchestration_unified.py`

#### Event System Integration
- `test_events_delivery.py` → `test_event_delivery.py`
- `test_events_stats.py` → `test_event_metrics.py`
- `test_ws_stream.py` → `test_websocket_streaming.py`

#### Security Integration
- `test_jwt_*.py` → `test_jwt_integration.py` (combine all JWT tests)
- `test_observation_context_policy.py` → `test_security_policies.py`

#### Background Processing
- `test_summarization_background.py` → `test_background_processing.py`
- `test_reflection_background.py` → `test_background_reflection.py`
- `test_metacognition_metrics_and_feedback.py` → `test_metacognition_integration.py`

### **End-to-End Tests** (Full system scenarios)
Move to `tests/e2e/`:

#### User Scenarios
- `test_mvp.py` → `test_user_scenarios.py`
- `test_file_read_and_rate_limit.py` → `test_file_operations.py`
- `test_tracing.py` → `test_observability.py`
- `test_replay_trace.py` → `test_trace_replay.py`

#### CLI Scenarios
- `test_reflection_cli.py` → `test_cli_scenarios.py`
- `test_reflection_api.py` → `test_api_scenarios.py`

### **Performance Tests** (Load and benchmarks)
Move to `tests/performance/`:

- `test_scheduler_load.py` → `test_load_balancing.py`

### **External Service Tests** (Require Docker)
Move to `tests/integration/` with `@pytest.mark.requires_external`:

- `test_pgvector_integration.py` → `test_database_integration.py`

### **Sprint/Legacy Tests** (Archive or integrate)
Move to `tests/legacy/` or integrate:

- `test_sprint01_*.py` → Archive or integrate into relevant categories
- `test_tool_policies*.py` → Combine into `test_tool_policies.py`

## 🐳 Docker Requirements

### **Services Needed for Full Test Suite**

#### **Always Required** (Integration+ tests)
```yaml
services:
  redis:
    image: redis:7-alpine
    ports: ["6379:6379"]
    
  postgres:
    image: pgvector/pgvector:pg16
    environment:
      POSTGRES_DB: imf_test
      POSTGRES_USER: motet
      POSTGRES_PASSWORD: imf_test_password
    ports: ["5432:5432"]
```

#### **For Distributed Tests** (E2E tests)
```yaml
  # Add worker services
  reasoning-worker:
    build: .
    command: celery -A motet.core.eventing.tasks worker --queues=reasoning
    
  tool-worker:
    build: .
    command: celery -A motet.core.eventing.tasks worker --queues=tools
    
  model-worker:
    build: .
    command: celery -A motet.core.eventing.tasks worker --queues=models
```

#### **For MCP Tests** (External integration)
```yaml
  # MCP servers for testing
  playwright-mcp:
    image: mcp-playwright-server
    
  weather-mcp:
    image: mcp-weather-server
```

### **Test Environment Variables**
```bash
# Test database
MOTET_PGVECTOR_DSN=postgresql://motet:imf_test_password@localhost:5432/imf_test

# Redis for tests
MOTET_REDIS_URL=redis://localhost:6379/1
MOTET_PURE_DISTRIBUTED_INVOKER_REDIS_URL=redis://localhost:6379/2

# Test mode
MOTET_TEST_MODE=true
MOTET_MODEL_PROVIDER=mock
MOTET_ENABLE_VECTOR_MEMORY=true

# External services flag
EXTERNAL_SERVICES_AVAILABLE=true
```

## 🔄 Migration Strategy

### **Phase 1: Unit Tests** (No Docker needed)
1. Create subdirectories in `tests/unit/`
2. Move and categorize unit tests
3. Update imports and add proper markers
4. Verify all unit tests pass

### **Phase 2: Integration Tests** (Redis + Postgres)
1. Move integration tests to `tests/integration/`
2. Add `@pytest.mark.requires_redis` markers
3. Create Docker Compose for basic services
4. Update test configuration

### **Phase 3: Distributed Tests** (Full Docker stack)
1. Fix and migrate `tests/distributed/` tests
2. Add worker services to Docker Compose
3. Create distributed test fixtures
4. Verify distributed functionality

### **Phase 4: E2E Tests** (Complete system)
1. Move end-to-end scenarios to `tests/e2e/`
2. Add external service requirements
3. Create comprehensive test scenarios
4. Performance validation

## 📋 Test Execution Strategy

### **Local Development** (No Docker)
```bash
# Unit tests only
pytest tests/unit/ -m "not requires_redis"

# Fast feedback loop
pytest tests/unit/ --maxfail=5 -x
```

### **CI/CD Pipeline** (Docker available)
```bash
# Stage 1: Unit tests
pytest tests/unit/

# Stage 2: Integration tests  
docker-compose up -d redis postgres
pytest tests/integration/ -m "not requires_external"

# Stage 3: Full system tests
docker-compose up -d
pytest tests/e2e/ tests/performance/
```

### **Full Local Testing** (Docker required)
```bash
# Start services
docker-compose -f tests/docker-compose.test.yml up -d

# Run all tests
pytest tests/ --cov=motet

# Cleanup
docker-compose -f tests/docker-compose.test.yml down
```

## 🎯 Success Criteria

- ✅ **Unit Tests**: Run without Docker, < 30 seconds total
- ✅ **Integration Tests**: Run with Redis+Postgres, < 5 minutes total  
- ✅ **E2E Tests**: Run with full stack, < 15 minutes total
- ✅ **Performance Tests**: Validate benchmarks, < 10 minutes total
- ✅ **Coverage**: >90% for unit, >80% for integration
- ✅ **Reliability**: <1% flaky test rate across all categories
