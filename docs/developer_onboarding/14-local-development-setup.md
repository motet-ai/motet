# Local Development Setup

Setting up a productive local development environment is essential for efficient Motet development. This guide covers environment setup, configuration, development tools, and best practices.

## Environment Setup

### Prerequisites

Before you begin, ensure you have:

- **Python 3.11+** installed
- **Docker** and **Docker Compose** installed
- **Git** installed
- **Code Editor** (VS Code, PyCharm, etc.)

### Initial Setup

```bash
# Clone the repository
git clone https://github.com/motet-ai/motet.git
cd motet

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Install the SDK and runtime in editable mode (order matters: SDK first, then runtime).
# Python loads code from your repo so changes take effect without reinstall.
pip install -e motet-sdk
pip install -e .

# Install development dependencies (pytest, black, isort, flake8, mypy)
pip install -e ".[dev]"
```

**Why editable install?** For local development, install both the SDK and the runtime in editable mode. The environment then uses your source tree instead of copies in `site-packages`, so code changes (including CLI/compose behavior) take effect without reinstalling.

### motet-cli on your PATH

After `pip install -e motet-sdk` and `pip install -e .`, the `motet-cli` command is available **in the environment you installed into**:

- **With a virtualenv (recommended for development)**  
  Activate the venv (`source venv/bin/activate`); the install puts `motet-cli` in `venv/bin/`, which is on your PATH while the venv is active. No extra steps.

- **User install (available in every terminal without activating a venv)**  
  From the project root:
  ```bash
  pip install -e motet-sdk --user
  pip install -e . --user
  ```
  Scripts go to `~/.local/bin`. Ensure that directory is on your PATH (e.g. in `~/.zshrc` or `~/.bashrc`):
  ```bash
  export PATH="$HOME/.local/bin:$PATH"
  ```
  Then run `motet-cli` from any terminal.

- **System Python**  
  Installing with system/sudo pip can put `motet-cli` in `/usr/local/bin` (usually on PATH), but is not recommended for day-to-day development to avoid mixing with system packages.

## Configuration Management

### Environment Variables

Motet uses environment variables for configuration. Create a `.env` file in the **repo root before** `motet-cli local up` — Docker Compose reads that file.

You need **at least one** model provider key. The stack default is OpenAI `gpt-4o-mini`, so set `OPENAI_API_KEY`. Add other keys only for providers you will select. Provider ids and flagship model names are on [Supported models](./03a-supported-models.md).

```bash
# Redis Configuration
MOTET_REDIS_URL=redis://localhost:6379/0
MOTET_PURE_DISTRIBUTED_INVOKER_REDIS_URL=redis://localhost:6379/1

# Database Configuration
MOTET_PGVECTOR_DSN=postgresql://user:password@localhost:5432/imf
MOTET_PGVECTOR_TABLE=imf_embeddings

# Feature Flags
MOTET_ENABLE_VECTOR_MEMORY=true

# Model Configuration (stack default: OpenAI gpt-4o-mini)
MOTET_MODEL_PROVIDER=openai
MOTET_MODEL_NAME=gpt-4o-mini
OPENAI_API_KEY=your-openai-key-here
# Optional: Moonshot / Kimi K3 when selected in the UI
# MOTET_MOONSHOT_API_KEY=your-moonshot-key-here
# Optional: DeepSeek V4 (deepseek-v4-flash / deepseek-v4-pro)
# MOTET_DEEPSEEK_API_KEY=your-deepseek-key-here
# Optional: Meta Muse Spark (muse-spark-1.2)
# MOTET_META_API_KEY=your-meta-model-api-key-here
# Optional: Gemini
# MOTET_GEMINI_API_KEY=your-gemini-key-here

# MCP Configuration
MCP_INSTANCE_MANAGER_CONFIG=config/mcp_instance_manager.yaml

# Observability
MOTET_TRACE_ENABLED=true

# Development
MOTET_DEBUG_MODE=true
MOTET_LOG_LEVEL=DEBUG
```

### YAML Configuration

MCP servers configured in `config/mcp_instance_manager.yaml`:

```yaml
services:
  - service_id: "playwright"
    transport: "stdio"
    command: "npx"
    args:
      - "-y"
      - "@playwright/mcp"
    env:
      PLAYWRIGHT_HEADLESS: "true"

    # Instance sharing and lifecycle
    state_model: "stateful"
    credential_scope: "motet"
    visibility: "motet"
    lifecycle_duration: "permanent"
    instances: 1
```

## Running the local stack

`motet-cli local` is the supported way to run Motet on your machine. Starting Redis, Postgres, Celery, and the API as host processes is unsupported — the stack is more than those four services, and that path is not kept current.

```bash
# Start all services (writes tls/ when missing so redis-tls can start).
# Pulls Motet-bearing images when the tag is not local; --build rebuilds from this tree.
docker login ghcr.io   # eval / invite-only snapshot
motet-cli local up

# Check status
motet-cli local status

# View logs
motet-cli local logs --follow

# Open the local management UI (includes MCP Servers)
motet-cli local manage

# MCP manager health (host 9191 → container 9091)
curl http://localhost:9191/health
curl http://localhost:8000/api/v1/mcp/servers

# Stop services
motet-cli local down
```

## Edge worker for a remote Motet deployment

Use this when the **API and datacenter workers** run in a shared or hosted environment, but you need a **worker on your machine** so Motet can run commands and tools against local files, clipboard, or host bridges. Registration and lifecycle are handled by **`motet-cli device`** (Docker required on the machine that runs the edge worker).

**Prerequisites**

- Docker and Docker Compose on the machine that will run the edge worker
- Network access to the deployment API
- CLI auth for that API (`motet-cli auth login` or a stored token), and a default API base URL if you use one (`motet-cli setup set`)

**Typical flow**

1. **Register** the machine and persist a device profile (and tunnel config when the deployment uses that mode):

   ```bash
   motet-cli device register --device-name my-laptop \
     --read-path ~/Projects/foo \
     --write-path ~/Projects/foo/output
   ```

   Repeat **`--read-path`** / **`--write-path`** as needed. Paths must exist on the host; they become allowlisted roots for tools such as `core.file_read` and `core.file_write` inside the worker.

   By default the worker id is derived from the device id (`edge_<uuid8>`).
   Pass **`--worker-id`** to register under an explicit id (must start with
   `edge_` and be unclaimed) — used by products whose routing expects a
   specific worker id, e.g. a remote app-builder instance registering as
   `edge_app_builder_<app>`.

2. **Adjust paths later** without re-registering:

   ```bash
   motet-cli device configure --read-path ~/Other --write-path ~/Other/out
   ```

3. **Start** the edge runtime (compose stack: tunnel sidecar and worker, when applicable; profile `edge`):

   ```bash
   motet-cli device start
   ```

   Clipboard, shell-exec, and process-control **host bridges** are enabled by default for `device start`. Shell and process-control bridges only take effect when you set allowlists on the host, for example **`MOTET_SHELL_BRIDGE_CWD_ALLOWLIST`** and **`MOTET_PROCESS_CONTROL_CWD_ALLOWLIST`**. Disable bridges you do not want with **`--no-clipboard-bridge`**, **`--no-shell-exec-bridge`**, or **`--no-process-control-bridge`**.

   **`core.worker_exec` and MCP** use the **Docker** backend by default in `docker-compose.edge-worker.yml` (mounts the host `docker.sock`). Set **`MOTET_EXEC_BACKEND=subprocess`** before `device start` if you need in-process worker execution without Docker.

4. **Operate and debug**:

   ```bash
   motet-cli device status
   motet-cli device doctor
   motet-cli device logs --follow
   motet-cli device stop
   ```

**Other commands**

- **`motet-cli device list`** — devices registered for the current principal (API)
- **`motet-cli device revoke <device_id>`** — revoke the device and clean up local registration
- **`motet-cli device build`** — build the edge worker image (`motet-edge-worker`) when you develop against a repo checkout
- **`motet-cli device update`** — pull the recommended worker image and restart the stack

For a compact command grouped with the rest of the CLI, see [Motet CLI Reference](./37-motet-cli-reference.md). For how routing differs between datacenter workers and a device worker, see [Worker System & Routing](./08-worker-system-routing.md#datacenter-workers-and-device-workers).

## Development Tools

### Debugging

#### Debug Mode

Enable debug mode for comprehensive debugging:

```bash
export MOTET_DEBUG_MODE=true
```

**Features**:
- Task flow visualization
- Command metadata tracking
- Performance analysis
- Interactive debugging

#### Task Flow Visualization

Access at `http://localhost:8000/manage`:
- 3D force-directed graph
- 2D Mermaid diagrams
- Command metadata
- Performance metrics

#### Logging

Structured logging with correlation IDs:

```python
import structlog
logger = structlog.get_logger(__name__)

logger.info(
    "operation_started",
    operation="my_operation",
    param1=value1,
    correlation_id=correlation_id
)
```

### Tracing

Enable distributed tracing:

```bash
export MOTET_TRACE_ENABLED=true
```

**Features**:
- Distributed command tracing
- Cross-worker trace correlation
- Performance profiling
- Trace visualization

**View Traces**:
```bash
# List traces
motet-cli traces list

# Show trace
motet-cli traces show --trace-id <id>
```

### Metrics

Prometheus metrics available at `/metrics`:

```bash
# View metrics
curl http://localhost:8000/metrics

```

## Hot Reloading and Development Workflow

### Reloading API and worker code

Compose mounts the repo into the API and worker containers. After changing runtime Python, restart the stack so workers pick it up:

```bash
motet-cli local restart
```

### Bundle Hot Iteration

For rapid bundle development, prefer the CLI hot deploy workflows:

```bash
motet-cli bundle hot-deploy .
```

It syncs the bundle into each target container over Mutagen, then triggers a worker-side reload and index refresh. Watch mode is on by default, so edits redeploy as you save; `--no-watch` runs once. Linting is skipped by default for speed — pass `--lint` when you want the deploy checks before pushing.

### Development Workflow

1. **Make Changes**: Edit code
2. **Test Locally**: Run tests
3. **Check Logs**: Monitor logs for errors
4. **Verify**: Test changes manually
5. **Commit**: Commit when ready

## Testing Setup

### Unit Tests

```bash
# Run all tests
pytest tests/unit/

# Run specific test file
pytest tests/unit/test_agents_api.py

# Run with coverage
pytest --cov=motet tests/
```

### Integration Tests

```bash
# Run integration tests in Docker
docker-compose -f tests/docker-compose.test.yml run --rm test-runner
```

### E2E Tests

```bash
# Run end-to-end tests through the Docker test environment
docker-compose -f tests/docker-compose.test.yml run --rm test-runner
```

## Code Quality Tools

### Formatting

```bash
# Format code with Black
black motet/

# Sort imports with isort
isort motet/
```

### Linting

```bash
# Lint with flake8
flake8 motet/

# Type check with mypy
mypy motet/
```

### Pre-commit Hooks

Install pre-commit hooks:

```bash
pip install pre-commit
pre-commit install
```

## Common Development Tasks

### Adding a New Command

Put it in a bundle under `commands/`, decorate it with `@motet.command`, and deploy with `motet-cli bundle hot-deploy .`. The loader registers it; nothing in the runtime needs editing. [Building Your First Command](./15-building-your-first-command.md) walks through it.

If you are working on Motet itself and adding to `motet/core/commands/builtin/`, you must also add the module's import to `DistributedCommand._ensure_commands_registered()`. That list is the only thing that registers built-in command types, and omitting it fails at runtime with "Unknown command type" while your unit tests still pass.

### Adding a New Tool

Same split. In a bundle, `@motet.tool` in `tools/*.py` registers as `{bundle_id}.{name}` when loaded. In the runtime, tools live in `motet/core/tools/builtin/` and register with the tool registry. See [Tool Ecosystem](./21-tool-ecosystem.md).

### Adding a New Workflow

Workflows are YAML, not Python. Add a `.yaml` file under a bundle's `workflows/` directory and deploy; it is namespaced as `{bundle_id}.{workflow_id}`. See [Building Workflows](./17-building-workflows.md).

## Troubleshooting

### Services Not Starting

**Check**:
- Docker is running
- Ports are available
- Environment variables are set
- Dependencies are installed

### Import Errors

**Check**:
- Virtual environment is activated
- Dependencies are installed
- Python path is correct

### Connection Errors

**Check**:
- The local stack is up (`motet-cli local status`)
- Connection strings match the Compose-published ports
- Network connectivity

## Best Practices

### 1. Use Virtual Environment

```bash
# ✅ CORRECT: Always use virtual environment
python -m venv venv
source venv/bin/activate
```

### 2. Keep Environment Variables Updated

```bash
# ✅ CORRECT: Keep .env file updated
# Don't commit .env to git
```

### 3. Run Tests Before Committing

```bash
# ✅ CORRECT: Run tests before committing
pytest tests/unit/
```

### 4. Use Debug Mode During Development

```bash
# ✅ CORRECT: Enable debug mode
export MOTET_DEBUG_MODE=true
```

## Next Steps

Now that your environment is set up:

- **[Building Your First Command](./15-building-your-first-command.md)** - Hands-on tutorial
- **[Command Composition Patterns](./16-command-composition-patterns.md)** - Advanced patterns
- **[Building Workflows](./17-building-workflows.md)** - Workflow tutorial

## Navigation

- **[← Back to Documentation Home](./00-landing-page.md)** - Main documentation hub

---

**Last Updated**: 2026-08-26
