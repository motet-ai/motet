# Motet - Testing

## Test lanes

| Lane | What runs | Typical command |
|------|-----------|-----------------|
| **A — Unit (local)** | Fast tests, no Docker stack | `pytest tests/unit/ -v` |
| **B — Docker integration (CI default)** | Unit + integration in Compose with Redis, Postgres, Keycloak. The `test-runner` command uses `-m "not distributed"`. | `cd tests && docker compose -f docker-compose.test.yml run --rm test-runner` |
| **C — Full stack / distributed E2E** | Tests marked `@pytest.mark.distributed` dispatch to real Celery workers (`--profile workers`). Native chat uses in-process ASGI + `WorkerReadinessService`; a few suites still need `MOTET_DISTRIBUTED_STACK_HTTP_URL`. | See [TESTING_GUIDE.md](./TESTING_GUIDE.md); `--profile workers` + optional embedding env for artifact/RAG. |
| **D — Live local models** | Gated GGUF smoke suite for all configured local models: chat/stop behavior plus capability-scoped structured output, tool use, and thinking controls. | `MOTET_RUN_LOCAL_MODEL_TESTS=1 MOTET_LOCAL_MODEL_DIR="$(pwd)/models" pytest tests/local_models/ -s` |

Lane B is **not** the same as full E2E: it matches CI and skips distributed tests by default.

**Compose:** Run integration tests from the `tests/` directory (project name `motet_test`) so services resolve (`redis`, `postgres`, `keycloak`) on the `motet_test` network. Using a different project name or `docker compose run --no-deps` without attaching that network breaks Redis/DNS.

Services do **not** set `container_name:` (#126) — names are compose-project-prefixed (`motet_test-redis-1`, …) so concurrent compose projects (e.g. parallel app-builder test gates) do not collide. Prefer `docker compose ps -q <service>` or service DNS on the compose network over fixed container names.

## Quick Start

### Run All Tests (Integration + Unit)
```bash
# From the tests/ directory (same as CI)

# FIRST TIME ONLY: Build the image
docker compose -f docker-compose.test.yml build test-runner

# Then run tests (FAST - no rebuild needed)
docker compose -f docker-compose.test.yml run --rm test-runner
```

### Run Specific Tests
```bash
# API tests only (FAST - uses existing image)
docker compose -f docker-compose.test.yml run --rm test-runner \
 python -m pytest tests/integration/api/ -v

# Unit tests only (can run locally without Docker).
# motet-host CLI tests need the sibling package (also pulled in by.[dev]):
# pip install -e hosting/motet-host
# The Docker test-runner skips them via importorskip.
pytest tests/unit/ -v

# Specific test file
docker compose -f docker-compose.test.yml run --rm test-runner \
 python -m pytest tests/integration/api/test_commands_api.py -v

# Specific test
docker compose -f docker-compose.test.yml run --rm test-runner \
 python -m pytest tests/integration/api/test_commands_api.py::test_deploy_command_success -v
```

### Run Artifact RAG E2E With Embeddings
```bash
# Starts the optional embedding server, then runs the full
# upload -> derivation -> indexing -> chat/citation artifact RAG path.
make test-artifact-rag-e2e
```

Equivalent explicit Docker commands:

```bash
docker compose -f docker-compose.test.yml --profile workers up -d embedding-server

docker compose -f docker-compose.test.yml run --rm \
 -e MOTET_EMBEDDING_TOPOLOGY=sibling \
 -e MOTET_EMBEDDING_ENDPOINT=http://embedding-server:8091 \
 test-runner python -m pytest tests/integration/test_artifact_rag_e2e.py -q
```

The artifact RAG E2E tests intentionally skip in default runs when
`MOTET_EMBEDDING_ENDPOINT` is not set. Use this target for the full
embedding-backed validation lane.

### Run the OpenAI-Compatible Facade Suites

Three suites cover the facade at increasing depth. The first two run in lane B:

```bash
# HTTP contract: flag gating, auth, per-credential policy, error envelopes,
# session mapping. No workers, no inference.
docker compose -f docker-compose.test.yml run --rm test-runner \
 python -m pytest tests/integration/api/test_openai_compat_api.py -v

# Real command bodies: model_inference / model_stream execute in-process
# against the deterministic mock adapter, with real Redis stream frames.
# Includes hosted_tools allowlist round-trip (forced mock tool call + real
# core.tools_list via tool_execution).
docker compose -f docker-compose.test.yml run --rm test-runner \
 python -m pytest tests/integration/api/test_openai_compat_worker_e2e.py -v

# Agent mode: MotetStack / agent_turn path, turn-aggregated usage, conversation
# chaining via previous_response_id (in-process nested invoker).
docker compose -f docker-compose.test.yml run --rm test-runner \
 python -m pytest tests/integration/api/test_openai_compat_agent_e2e.py -v
```

The third is lane C: it dispatches to a real Celery worker, which is the only
way to cover tokens crossing a process boundary as encrypted stream frames.

```bash
docker compose -f docker-compose.test.yml --profile workers up -d worker-1

docker compose -f docker-compose.test.yml run --rm \
 -e MOTET_DISTRIBUTED_STACK_HTTP_URL=http://localhost:8000 \
 test-runner python -m pytest \
 tests/integration/api/test_openai_compat_distributed.py -v
```

The distributed suite skips itself when no worker has registered as ready, so
forgetting the profile reports a skip rather than a timeout.

### Native Chat Worker Suite (Lane C)

Native `/api/v1/chat` (SSE, WebSocket, memory, events, health, `core.math_eval`)
uses the same `--profile workers` gate. Fixtures live in
`tests/integration/conftest.py` (`isolated_async_redis`, `ready_celery_workers`,
`native_chat_client`). Compose `mock-small` replies `You said: <prompt>` — assert
the echo, not live-model essays. Live-provider CoT/tool checks are
`tests/integration/reasoning/test_reasoning_live.py` (not `@distributed`; skip on mock).

```bash
docker compose -f docker-compose.test.yml --profile workers up -d worker-1

docker compose -f docker-compose.test.yml run --rm test-runner \
 python -m pytest \
 tests/integration/streaming/test_sse_streaming.py \
 tests/integration/streaming/test_websocket_streaming.py \
 tests/integration/events/test_event_delivery.py \
 tests/integration/reasoning/test_reasoning_integration.py \
 tests/integration/test_health_monitoring.py \
 tests/integration/test_distributed_system_full.py \
 tests/integration/memory/test_memory_integration.py \
 -v --override-ini="addopts="
```

### When to Rebuild

**Only rebuild when**:
- Requirements changed (`requirements.txt`)
- Service Dockerfiles (under `docker/images/`) changed
- First time running tests
- Dependencies are broken

```bash
cd tests

# Rebuild the image
docker compose -f docker-compose.test.yml build test-runner

# Or force rebuild everything
docker compose -f docker-compose.test.yml build --no-cache
```

### Clean Up
```bash
cd tests
docker compose -f docker-compose.test.yml down -v
```

## 🚨 IMPORTANT: Always Use Docker for Integration Tests

**NEVER run integration tests directly** with `pytest tests/integration/` - this causes:
- Hanging pytest processes
- Resource conflicts
- Missing dependencies (Keycloak)
- Inconsistent test results

See **[TESTING_GUIDE.md](./TESTING_GUIDE.md)** for full documentation.

## Test Organization

```
tests/
├── README.md # This file (quick reference)
├── TESTING_GUIDE.md # Complete testing documentation
├── conftest.py # Shared pytest configuration
├── unit/ # Unit tests (can run locally)
│ ├── core/
│ └── interfaces/
├── integration/ # Integration tests (MUST use Docker)
│ ├── api/ # API endpoint tests
│ │ ├── conftest.py # API-specific fixtures
│ │ ├── test_commands_api.py
│ │ ├── test_auth_api.py
│ │ └──...
│ └── distributed/ # Distributed system tests
├── local_models/ # Gated live GGUF tests (MOTET_RUN_LOCAL_MODEL_TESTS=1)
└── e2e/ # End-to-end tests (MUST use Docker)
```

## Test Services (Docker)

The test environment uses different ports than the distributed environment:

| Service | Test Port | Distributed Port |
|---------|-----------|------------------|
| Redis | 6479 | 6379 |
| PostgreSQL | 5532 | 5432 |
| Keycloak | 8180 | 8080 |

This allows both environments to run simultaneously without conflicts.

## Common Commands

All `docker compose` examples assume the **`tests/`** working directory (`cd tests`).

```bash
cd tests

# First time: Build the image (takes 2-3 minutes)
docker compose -f docker-compose.test.yml build test-runner

# Run all tests (FAST - reuses existing image)
docker compose -f docker-compose.test.yml run --rm test-runner

# Run with verbose output
docker compose -f docker-compose.test.yml run --rm test-runner \
 python -m pytest tests/ -vv

# Run with coverage
docker compose -f docker-compose.test.yml run --rm test-runner \
 python -m pytest tests/ --cov=motet --cov-report=html

# Run specific test pattern
docker compose -f docker-compose.test.yml run --rm test-runner \
 python -m pytest tests/integration/api/ -k "test_auth" -v

# Run and keep services running (for debugging)
docker compose -f docker-compose.test.yml up test-runner

# Clean up everything
docker compose -f docker-compose.test.yml down -v
rm -rf../logs/../traces/../htmlcov/

# Rebuild only when requirements change
docker compose -f docker-compose.test.yml build test-runner
```

## Documentation

- **[TESTING_GUIDE.md](./TESTING_GUIDE.md)** - Complete testing guide with troubleshooting
- **[../docs/operations/port-allocation.md](../docs/operations/port-allocation.md)** - Port allocation reference
- **[../AGENTS.md](../AGENTS.md)** - Agent instructions including testing requirements

## Test Development Workflow

1. **Write test** in appropriate directory (`unit/`, `integration/api/`, etc.)
2. **Run in Docker**: `cd tests && docker compose -f docker-compose.test.yml run --rm test-runner`
3. **Review results** in Docker logs
4. **Iterate** by updating test and re-running
5. **Clean up**: `cd tests && docker compose -f docker-compose.test.yml down -v`

## Troubleshooting

### Tests Hanging?
```bash
# Kill local pytest processes
pkill -9 -f pytest

# Always use Docker instead
cd tests && docker compose -f docker-compose.test.yml run --rm test-runner
```

### Port Conflicts?
The test environment uses ports 6479, 5532, 8180 to avoid conflicts with the distributed environment (6379, 5432, 8080).

Both environments can run simultaneously.

### OAuth/Auth Tests Failing?
These tests skip automatically when Keycloak is not configured. In Docker, Keycloak starts automatically (~40 seconds to initialize).

## More Information

See **[TESTING_GUIDE.md](./TESTING_GUIDE.md)** for:
- Service configuration details
- Environment variables
- Test patterns and fixtures
- Comprehensive troubleshooting
- Best practices
- CI/CD integration
