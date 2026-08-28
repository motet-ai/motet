# Manual Test Scripts

This directory contains manual test scripts for debugging and exploratory testing during development.

## 📋 Overview

These scripts are colocated under `tests/` for discoverability but are **excluded from automated collection**. A `conftest.py` here sets `collect_ignore_glob = ["*.py"]`, so a normal `pytest` / `pytest tests/` run never imports or executes them — important because several load multi-GB model assets, hit live services (Redis, the local-inference container, the API, a browser), or run top-level code on import. Run them directly with `python` instead.

They are standalone scripts for:
- Testing new API features
- Debugging specific functionality
- Exploring system behavior
- Quick validation during development

## 🚀 Usage

All scripts can be run directly from the project root (never via `pytest` — they
are intentionally excluded from collection):

```bash
# From project root:
python tests/manual/test_context_based_api.py
```

Some local-model harnesses also run inside the `local-inference` container (the
repo is mounted at `/app`):

```bash
docker exec -e PYTHONPATH=/app motet_dev-local-inference-1 \
 python /app/tests/manual/_adr0117_structured.py gemma-4-e4b
```

## 📝 Scripts

### **API Testing Scripts** (Require API Running)

These scripts test the HTTP API endpoints. **Start Docker first:**
```bash
docker-compose -f docker-compose.distributed.yml up -d
```

#### `test_context_based_api.py`
**Purpose:** Tests the context-based API structure (`/api/v1/commands/run`)

**What it tests:**
- CommandContext + DistributedCommandContext structured requests
- Worker targeting via context
- Future-proof API design

**Updated:** 2025-11-20 - Fixed endpoint from `/api/v1/execution/run` to `/api/v1/commands/run`

**Run:**
```bash
python tests/manual/test_context_based_api.py
```

#### `test_worker_targeting.py`
**Purpose:** Tests worker targeting via API

**What it tests:**
- Target specific worker by ID
- Required capabilities filtering
- Worker routing strategies

**Updated:** 2025-11-20 - Fixed endpoint from `/api/v1/execution/run` to `/api/v1/commands/run`

**Run:**
```bash
python tests/manual/test_worker_targeting.py
```

#### `test_worker_targeting_each.py`
**Purpose:** Tests targeting each worker individually

**What it tests:**
- Targeting each worker in Docker setup
- Verifying worker-specific execution
- Worker ID validation

**Updated:** 2025-11-20 - Fixed endpoint from `/api/v1/execution/run` to `/api/v1/commands/run`

**Run:**
```bash
python tests/manual/test_worker_targeting_each.py
```

### **Local Inference Scripts** (No API Required)

These scripts test local model inference directly through the codebase.

#### `test_local_inference.py`
**Purpose:** Tests local inference setup and connectivity

**What it tests:**
- Redis connectivity
- LocalInferenceClient initialization
- Available local models in registry
- Basic inference requests

**Run:**
```bash
python tests/manual/test_local_inference.py
```

#### `test_local_inference_direct.py`
**Purpose:** Tests direct local inference without API

**What it tests:**
- Direct model inference calls
- Model loading and initialization
- Response format validation

**Run:**
```bash
python tests/manual/test_local_inference_direct.py
```

#### `test_local_inference_schedule.py`
**Purpose:** Tests scheduled local inference

**What it tests:**
- Scheduled inference jobs
- Cron-based scheduling
- Background inference execution

**Run:**
```bash
python tests/manual/test_local_inference_schedule.py
```

#### `test_local_model_inference.py`
**Purpose:** Tests local model inference capabilities

**What it tests:**
- Model inference with various parameters
- Response streaming
- Model capabilities

**Run:**
```bash
python tests/manual/test_local_model_inference.py
```

#### `test_local_model_via_api.py`
**Purpose:** Tests local models through the API endpoint

**What it tests:**
- API endpoint for local models
- Model selection via API
- API response format

**Requires:** API running

**Run:**
```bash
python tests/manual/test_local_model_via_api.py
```

## 🔧 Prerequisites

### For API Scripts
1. **Docker running:**
 ```bash
 docker-compose -f docker-compose.distributed.yml up -d
 ```

2. **Verify API is up:**
 ```bash
 curl http://localhost:8000/health
 ```

### For Local Inference Scripts
1. **Redis running** (usually started by Docker)
2. **Local models configured** (if testing model inference)

## 📚 Related Documentation

- **Automated Tests:** `tests/` directory
- **API Documentation:** `motet/interfaces/api/v1/README.md`

## 🔄 Maintenance

### When to Update These Scripts
- ✅ When API endpoints change (like `/api/v1/execution/run` → `/api/v1/commands/run`)
- ✅ When request/response formats change
- ✅ When testing new features during development

### When to Remove These Scripts
- ❌ Don't remove if they test features that may need future debugging
- ❌ Don't remove if they provide useful examples for development
- ✅ Remove if the tested feature is removed from the codebase
- ✅ Remove if replaced by automated tests in `tests/`

## 📝 Notes

- **Not for CI/CD:** These scripts are for manual use only
- **May require updates:** As the API evolves, these may need fixes
- **Example code:** These serve as examples for API usage
- **Debugging tools:** Keep them as debugging aids for development

## 🆕 Recent Updates

### 2026-06-04
- ✅ Moved manual scripts from `scripts/manual_tests/` to `tests/manual/` to colocate with the test suite.
- ✅ Added `conftest.py` (`collect_ignore_glob = ["*.py"]`) so pytest never auto-collects/imports these asset- and service-dependent harnesses.
- ✅ Local-model harnesses added for `_adr0117_smoke.py` (jinja-primary load path) and `_adr0117_structured.py` (grammar-constrained structured output).

### 2025-11-20
- ✅ Fixed API endpoint references from `/api/v1/execution/run` to `/api/v1/commands/run`
- ✅ Moved all scripts from project root to `tests/manual/`
- ✅ Added this README documentation

### 2025-10-23
- Created local inference test scripts
- Added various local model testing capabilities

