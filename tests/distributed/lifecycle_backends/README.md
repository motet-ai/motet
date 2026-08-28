# Lifecycle Backend Tests

This folder holds tests and docs for the **worker lifecycle pluggable backends**: Docker (same host) and HTTP (multi-host/PaaS).

## What’s here

- **Unit tests** for the backend interface and implementations (e.g. `DockerLifecycleBackend` with mocked Docker).
- **README** (this file) – how to run tests and where manual Phase 2 testing is described.

## Running tests

From repo root (prefer Docker for integration tests; see project AGENTS.md):

```bash
# Unit tests only (no Docker required; subprocess is mocked)
pytest tests/distributed/lifecycle_backends/ -v

# With coverage
pytest tests/distributed/lifecycle_backends/ -v --cov=motet.core.distributed.worker_lifecycle_backends
```

## Backend configuration

- **Docker backend (default):** `MOTET_LIFECYCLE_BACKEND` unset or `docker`. Lifecycle and agent workers must share the same Docker daemon.
- **HTTP backend:** `MOTET_LIFECYCLE_BACKEND=http`, `MOTET_LIFECYCLE_HTTP_BASE_URL` set. Used when workers run on another host or PaaS (e.g. Railway).

Deployable assets (e.g. Railway webhook) are in **`hosting/lifecycle-backends/`**. Phase 2 hosted testing covers two stacks, a mock webhook, and Railway/Fly.io.
