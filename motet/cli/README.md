# Motet CLI

Modular CLI structure aligned with API organization (api/v1/).

## 📁 Structure

```
motet/cli/
├── main.py # Main entry point with all groups
├── _logging.py # Shared logging configuration
│
├── commands.py # Command management (api/v1/commands.py)
├── chat.py # Chat operations (api/v1/chat.py)
├── models.py # Model listing (api/v1/models.py)
├── tools.py # Tool execution (api/v1/tools.py)
├── skills.py # Agent Skills listing (api/v1/skills.py)
├── memories.py # Memory operations (api/v1/memories.py)
├── traces.py # Trace operations (CLI-only)
├── database.py # Database operations (CLI-only)
├── local.py # Local Docker stack operations (CLI-only)
│
├── test.py # Command testing (motet-cli command test)
├── testing.py # Test utilities
└── __main__.py # Module entry point (python -m motet.cli)
```

## 🚀 Usage

### Command Management
```bash
motet-cli command list # List commands (includes description + schema yes/no)
motet-cli command info <name> # Details including description and data_schema JSON
motet-cli command run <name> # Execute a command
motet-cli command test <name> # Test command locally (--file path optional)
```
Deploy commands via bundles: `motet-cli bundle init`, `motet-cli bundle lint`, then `motet-cli deploy dir-deploy.` or `motet-cli deploy git-deploy...`.

### Local Stack
```bash
motet-cli local up # Start distributed stack (writes tls/ if missing)
motet-cli local down # Stop stack (orphan cleanup on)
motet-cli local restart # Restart stack (down/up)
motet-cli local status # Compose ps + workers readiness summary
motet-cli local manage # Open local manage UI in browser
motet-cli local logs --follow # Tail all stack logs
motet-cli local doctor # Validate docker/compose/readiness
```

### AWS hosting (separate CLI)
AWS deploy env switching is **`motet-host`**, not `motet-cli`:

```bash
pip install -e hosting/motet-host
motet-host setup
motet-host status
```

See `hosting/motet-host/README.md`.

### Workers
```bash
motet-cli workers readiness # Worker readiness summary
motet-cli workers health # Worker health status
motet-cli workers managers # Instance manager status
motet-cli workers skill-workspaces # Active skill workspace bindings
```

### Device / edge worker runtime
```bash
motet-cli device register --device-name my-mac # Register device and save local profile
motet-cli device build # Build edge worker image (motet-edge-worker)
motet-cli device list # List registered devices
motet-cli device revoke <device_id> # Revoke device access
motet-cli device start # Start edge worker stack (compose profile: edge)
motet-cli device stop # Stop edge worker runtime (deregisters readiness via device token)
motet-cli device status # Compose + API device status
motet-cli device logs --follow # Tail edge worker runtime logs
motet-cli device doctor # Validate docker/profile/API connectivity
motet-cli device update # Pull latest edge worker image and restart
```

### Chat
```bash
motet-cli chat --message "Hello" # Chat with AI agent
motet-cli chat --message "Hi" --stream # Stream response
motet-cli chat --message "Hello" --provider openai --model-name gpt-4o
motet-cli chat --message "Summarize this" --artifact-id <id> --artifact-rag-scope conversation
```

### Models
```bash
motet-cli models # List all models
motet-cli models --provider openai # Filter by provider
```

### Tools
```bash
motet-cli tools call --name <tool> --params '{}' # Execute a tool
```

### Skills
```bash
motet-cli skills list # List installed Agent Skills
motet-cli skills list --bundle-id demo # Filter skills by bundle
```

### Workflows
```bash
motet-cli workflows list # List workflows (GET /api/v1/workflows)
motet-cli workflows validate --yaml-file wf.yaml
motet-cli workflows register --yaml-file wf.yaml [--replace]
motet-cli workflows unregister user.<owner>.<name>
motet-cli workflows execute --workflow-id <id> --workflow-name <name> --steps <steps.json>
```

### Memories
```bash
motet-cli memories inspect --limit 10 # Inspect memories
motet-cli memories consolidate # Trigger consolidation
motet-cli memories retrieve --q "..." # Retrieve memories
motet-cli memories store --content ".." # Store via POST /api/v1/memories/store
motet-cli memories store-dir./docs # Batch-import text files via API
motet-cli memories retrieval-eval... # Evaluate retrieval
```

### Traces (CLI-only)
```bash
motet-cli traces list --limit 10 # List recent traces
motet-cli traces show --trace-id <id> # Show specific trace
motet-cli traces replay --trace-id <id> # Replay trace
motet-cli traces watch --duration 10 # Watch live events
```

### Database (CLI-only)
```bash
motet-cli database migrate-pgvector # Create pgvector tables
```

## 🔐 Authentication

The CLI supports multiple authentication methods (see `docs/operations/authentication.md`):

1. **JWT Token** (Production): `export MOTET_JWT_TOKEN="eyJhbGc..."`
2. **Service Account Token** (Automation): `export MOTET_SERVICE_ACCOUNT_TOKEN="sa_..."`
3. **Stored Credentials**: Automatically saved to `~/.motet/credentials.json` with `--store` flag
4. **Header-Based Auth** (Dev Mode): `export MOTET_PRINCIPAL_ID="cli-user"`

### Service Account Management

```bash
# Create service account
motet-cli service-account create \
 --name "ci-pipeline" \
 --tenant "acme-corp" \
 --roles "admin,ci" \
 --expires-days 365 \
 --store

# List service accounts
motet-cli service-account list

# Revoke service account
motet-cli service-account revoke <token-id>
```

## 🎯 Design Principles

### 1. **Consistency with API**
- CLI structure mirrors API structure (api/v1/)
- Same logical grouping as API
- Easier to find related functionality

### 2. **Single Responsibility**
- Each file has one clear purpose
- Smaller, focused files (100-200 lines vs 600+ lines)
- Clear boundaries between domains

### 3. **Maintainability**
- Easy to find: Know exactly where to look for a command
- Isolated changes: Modifying chat doesn't risk breaking tools
- Better testing: Can test each CLI module independently

### 4. **Scalability**
- Easy to add: New command categories get their own file
- No file bloat: Won't have one massive file
- Clear ownership: Each file has clear domain

## 📝 Notes

- **API-Aligned**: Commands that use APIs are aligned with their API counterparts
- **CLI-Only**: Some commands (traces, database migrations) are local-only. Memory retrieval scoring uses `motet-cli memories retrieval-eval` → `POST /api/v1/memories/search/eval`.
- **Shared Logging**: All modules use `_logging.py` for consistent logging configuration
- **Type Hints**: All modules use Python 3.10+ type hints (e.g., `str | None`)

## 🛠️ Development

### Adding a New Command Group

1. Create a new file in `motet/cli/` (e.g., `workflows.py`)
2. Define your group:
 ```python
 import click
 from._logging import logger

 @click.group("workflows")
 def workflows_group -> None:
 """Workflow operations."""
 pass

 @workflows_group.command("list")
 def list_workflows -> None:
 """List all workflows."""
 # Implementation
 ```
3. Register in `main.py`:
 ```python
 from.workflows import workflows_group
 main_group.add_command(workflows_group)
 ```
4. Export in `__init__.py`:
 ```python
 from.workflows import workflows_group
 __all__ = [..., "workflows_group"]
 ```

### Testing

```bash
# Run CLI tests
pytest tests/unit/cli/ -v

# Manual testing
motet-cli --help
motet-cli command --help
motet-cli chat --help
```

## 📚 Related Documentation

- **API Structure**: `motet/interfaces/api/v1/`

