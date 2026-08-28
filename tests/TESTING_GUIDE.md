# Motet - Testing Guide

## 🚨 CRITICAL: Always Use Docker for Integration Tests

**NEVER run integration tests directly on your local machine.** This causes:
- Hanging pytest processes
- Resource conflicts with local Redis/Postgres
- Inconsistent test environments
- Missing dependencies (Keycloak for Auth tests)

## Quick Start

### Test lanes (what “all tests” means)

- **Lane A — Unit (local):** `pytest tests/unit/`
- **Lane B — Docker integration (CI default):** from `tests/`, `docker compose -f docker-compose.test.yml run --rm test-runner` — runs pytest with **`-m "not distributed"`** (skips distributed/full-stack E2E tests).
- **Lane C — Distributed / full E2E:** start workers with `--profile workers`. Native chat suites use in-process ASGI + `WorkerReadinessService` (see `tests/README.md`). A few suites still need `MOTET_DISTRIBUTED_STACK_HTTP_URL`; see *Running Tests Alongside Distributed Environment* below.

Use **`cd tests`** so Compose project and networks match CI (`motet_test`). Avoid `docker compose run --no-deps` unless you intentionally reattach the `motet_test` network; otherwise Redis DNS (`redis:6379`) fails inside the runner.

### Run All Tests (Integration + Unit)
```bash
# From repository root (Compose file sets project name motet_test)

# FIRST TIME ONLY: Build the Docker image (2-3 minutes)
docker compose -f tests/docker-compose.test.yml build test-runner

# Run tests (FAST - reuses existing image, ~12 minutes)
docker compose -f tests/docker-compose.test.yml run --rm test-runner

# Alternative: Run with auto-cleanup
docker compose -f tests/docker-compose.test.yml up test-runner
```

### Run Specific Test Suite
```bash
# API tests only (FAST - no rebuild)
docker compose -f tests/docker-compose.test.yml run --rm test-runner \
    python -m pytest tests/integration/api/ -v

# Unit tests only (can run locally)
pytest tests/unit/ -v

# Specific test file (FAST)
docker compose -f tests/docker-compose.test.yml run --rm test-runner \
    python -m pytest tests/integration/api/test_commands_api.py -v

# Single test (FAST)
docker compose -f tests/docker-compose.test.yml run --rm test-runner \
    python -m pytest tests/integration/api/test_commands_api.py::test_deploy_command_success -v
```

### When to Rebuild Docker Image

**Only rebuild when**:
- ✅ First time running tests
- ✅ `requirements.txt` changed
- ✅ Any service Dockerfile under `docker/images/` changed
- ✅ Dependencies are broken/outdated

```bash
# Rebuild when needed
docker compose -f tests/docker-compose.test.yml build test-runner

# Force clean rebuild (if issues persist)
docker compose -f tests/docker-compose.test.yml build --no-cache test-runner
```

**Do NOT rebuild** for:
- ❌ Code changes in `motet/` (code is mounted as volume)
- ❌ Test file changes (tests are mounted as volume)
- ❌ Configuration changes (env vars in docker-compose.test.yml)
- ❌ Every test run (wastes 2-3 minutes)

### Clean Up After Tests
```bash
# Stop and remove containers, networks, volumes
docker compose -f tests/docker-compose.test.yml down -v

# Remove test logs
rm -rf logs/ traces/
```

## Running Tests Alongside Distributed Environment

**Good news**: Test and distributed environments can run simultaneously!

The test environment uses different ports:
- **Test Redis**: localhost:6479 (external) → redis:6379 (internal)
- **Test Postgres**: localhost:5532 (external) → postgres:5432 (internal)
- **Test Keycloak**: localhost:8180 (external) → keycloak:8080 (internal)

The distributed environment uses standard ports:
- **Distributed Redis**: localhost:6379
- **Distributed Postgres**: localhost:5432
- **Distributed Keycloak**: localhost:8080

### Example: Running Both Simultaneously

```bash
# Terminal 1: Start distributed environment
docker compose --project-name imf_dev -f docker-compose.distributed.yml up

# Terminal 2: Run tests (in parallel)
docker compose -f tests/docker-compose.test.yml up --build test-runner

# Both environments run independently without conflicts!
```

### Worker-Backed Command Execution (`--profile workers`)

A test that lets the HTTP layer dispatch a real distributed command depends on
three settings that must agree between `test-runner` and the worker services.
All three are configured in `docker-compose.test.yml`; the notes matter when
adding a worker service or debugging a command that never arrives.

1. **Per-worker queue.** The router dispatches every command to
   `worker.<worker_id>` (worker ids are `cloud_`-prefixed, so `worker-1` listens
   on `worker.cloud_test-worker-1`). A worker that omits this queue leaves
   commands sitting in Redis until the caller times out.
2. **Invoker Redis DB.** `MOTET_PURE_DISTRIBUTED_INVOKER_REDIS_URL` must match
   the runner's, as in the distributed dev stack where every service shares one
   invoker DB.
3. **Vault master key.** `MOTET_VAULT_MASTER_KEY` must match, because ADR-0056
   derives tenant keys from it. Streamed tokens are written by the worker as
   encrypted stream frames and decrypted by the HTTP process; mismatched keys
   surface as decrypt failures rather than as a missing-key error.

Keep the celery invocation on **one line**. In a YAML folded scalar (`command: >`)
the more-indented continuation lines retain their newlines, which ends the
command after `worker` and silently discards `--queues`, `--hostname`, and every
other flag.

### Accessing Services

When both environments are running:

```bash
# Access distributed Keycloak (production-like)
open http://localhost:8080

# Access test Keycloak (test environment)
open http://localhost:8180

# Connect to distributed Redis
redis-cli -p 6379

# Connect to test Redis
redis-cli -p 6479

# Connect to distributed Postgres
psql -h localhost -p 5432 -U motet -d motet_distributed

# Connect to test Postgres
psql -h localhost -p 5532 -U motet -d imf_test
```

## Test Environment

### Services Included

**Note**: Test services use different ports than `docker-compose.distributed.yml` to avoid conflicts:
- Test environment can run alongside distributed environment
- Test ports: Redis=6479, Postgres=5532, Keycloak=8180
- Distributed ports: Redis=6379, Postgres=5432, Keycloak=8080

1. **Redis** - Distributed state and caching
   - External Port: **6479** (mapped to avoid conflict with distributed Redis on 6379)
   - Internal Port: 6379
   - Database: 1 (tests), 2 (distributed invoker)

2. **PostgreSQL with pgvector** - Memory storage
   - External Port: **5532** (mapped to avoid conflict with distributed Postgres on 5432)
   - Internal Port: 5432
   - Database: `imf_test`
   - User: `imf`
   - Password: `imf_test_password`

3. **Keycloak** - OAuth/JWT authentication
   - External Port: **8180** (mapped to avoid conflict with distributed Keycloak on 8080)
   - Internal Port: 8080
   - Admin: `admin` / `admin`
   - Realm: `motet` (auto-imported)
   - Client: `motet-ai-stack`
   - Web UI: http://localhost:8180 (when tests are running)

4. **Test Runner** - Python environment with all dependencies
   - Runs pytest with proper configuration
   - All environment variables pre-configured
   - Volume-mounted source code for live updates

### Environment Variables

All required environment variables are pre-configured in `docker-compose.test.yml`:

- **Test Mode**: `MOTET_TEST_MODE=true`
- **Database**: `MOTET_POSTGRES_URL=postgresql://motet:imf_test_password@postgres:5432/imf_test`
- **Redis**: `MOTET_REDIS_URL=redis://redis:6379/1`
- **OAuth/JWT**: Full Keycloak configuration
- **API Key**: `MOTET_API_KEY=test-key`
- **Model Provider**: `MOTET_MODEL_PROVIDER=mock`
- **Vault Master Key**: `MOTET_VAULT_MASTER_KEY` — the same value on the runner
  and the worker services so encrypted payloads written by one are readable by
  the other

## Test Organization

### Directory Structure

```
tests/
├── unit/                    # Unit tests (can run locally)
│   ├── core/
│   └── interfaces/
├── integration/             # Integration tests (MUST use Docker)
│   ├── api/                # API endpoint tests
│   │   ├── conftest.py     # Shared fixtures and configuration
│   │   ├── test_commands_api.py
│   │   ├── test_auth_api.py
│   │   ├── test_oauth_api.py
│   │   └── ...
│   └── distributed/        # Distributed system tests
└── e2e/                    # End-to-end tests (MUST use Docker)
```

### Test Categories

1. **Unit Tests** - Fast, no external dependencies
   - Can run locally: `pytest tests/unit/`
   - No Docker required
   - Mock all external services

2. **Integration Tests** - Test component interactions
   - **MUST use Docker**: `docker compose -f tests/docker-compose.test.yml`
   - Require real Redis, Postgres, Keycloak
   - Test API endpoints, distributed commands, etc.

3. **E2E Tests** - Full system workflows
   - **MUST use Docker with distributed profile**
   - Require all services + Celery workers
   - Test complete user workflows

## Common Test Patterns

### API Test Pattern (from conftest.py)

```python
import pytest
from motet.interfaces.http import create_app
from tests.integration.api.conftest import with_env, get_test_env_vars

@pytest.mark.integration
def test_my_api_endpoint(test_headers):
    """Test my API endpoint."""
    with with_env(get_test_env_vars()):
        app = create_app()
        
        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=10) as client:
                response = await client.get(
                    "/api/v1/my-endpoint",
                    headers=test_headers
                )
                
                assert response.status_code == 200
                data = response.json()
                assert "result" in data
        
        asyncio.run(_run())
```

### Shared Fixtures (from conftest.py)

- `test_service_account_token` - Authentication token
- `test_headers` - Standard HTTP headers with auth
- `with_env()` - Context manager for environment variables
- `get_test_env_vars()` - Standard test environment

### OAuth Test Skipping

OAuth tests automatically skip when Keycloak is not configured:

```python
# OAuth provider not configured - skip
if response.status_code == 404:
    pytest.skip("OAuth provider not configured (expected in test environment)")
```

## Troubleshooting

### Tests Hanging?

**Cause**: Running tests locally instead of in Docker

**Solution**: Kill pytest processes and use Docker
```bash
# Kill hanging tests
pkill -9 -f pytest

# Run in Docker instead
docker compose -f tests/docker-compose.test.yml up --build test-runner
```

### Connection Refused Errors?

**Cause**: Services not ready or wrong hostname

**Solution**: Ensure using Docker hostnames (inside containers)
- Use `redis` not `localhost` for Redis (internal: port 6379)
- Use `postgres` not `localhost` for Postgres (internal: port 5432)
- Use `keycloak` not `localhost` for Keycloak (internal: port 8080)

**Note**: External ports (6479, 5532, 8180) are only for accessing services from your host machine, not from within test containers.

### Tests Pass Locally but Fail in Docker?

**Cause**: Environment differences

**Solution**: Check environment variables in `docker-compose.test.yml`

### Keycloak Not Starting?

**Cause**: Missing realm configuration

**Solution**: Ensure `docker/keycloak/realm-motet.json` exists

```bash
# Check Keycloak logs
docker compose -f tests/docker-compose.test.yml logs keycloak

# Restart Keycloak
docker compose -f tests/docker-compose.test.yml restart keycloak
```

## Test Results

### Expected Pass Rates

After fixes applied:
- **OAuth/Auth Tests**: 13 tests skip gracefully when Keycloak not configured
- **Streaming Tests**: 3 tests skip gracefully with timeouts (no events in test env)
- **Overall Pass Rate**: ~75-80% (100-105 passing, 20-25 failing, 13+ skipped)

### Known Issues

1. **Vault API Tests** - Redis connection issues (investigating)
2. **Schedules API Tests** - Command type registry issues (investigating)
3. **Legacy Tests** - Old test files need updating

## Best Practices

1. ✅ **Always use Docker** for integration/API/E2E tests
2. ✅ **Use shared fixtures** from `conftest.py`
3. ✅ **Add timeouts** to streaming/async tests
4. ✅ **Skip gracefully** when dependencies not configured
5. ✅ **Clean up after tests** with `docker-compose down -v`
6. ✅ **Run full suite** before committing
7. ✅ **Check for hanging processes** if tests seem stuck

## CI/CD Integration

The same `docker-compose.test.yml` configuration is used in CI/CD:

```yaml
# .github/workflows/test.yml (example)
- name: Run Integration Tests
  run: docker compose -f tests/docker-compose.test.yml up --build --abort-on-container-exit test-runner
```

This ensures tests run identically in local development and CI/CD.

## Quick Reference

| Command | Purpose |
|---------|---------|
| `docker compose -f tests/docker-compose.test.yml up --build test-runner` | Run all tests |
| `docker compose -f tests/docker-compose.test.yml run --rm test-runner python -m pytest tests/integration/api/ -v` | Run API tests |
| `docker compose -f tests/docker-compose.test.yml down -v` | Clean up |
| `docker compose -f tests/docker-compose.test.yml logs keycloak` | Check Keycloak logs |
| `docker compose -f tests/docker-compose.test.yml ps` | Check service status |
| `pkill -9 -f pytest` | Kill hanging local tests |
| `pytest tests/unit/` | Run unit tests locally |

---

**Remember**: When in doubt, use Docker! 🐳

