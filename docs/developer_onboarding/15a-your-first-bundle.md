# Your First Bundle

Bundles are the standard way to deploy custom commands, tools, and configuration to Motet. This guide walks you through creating, linting, and deploying a bundle using the CLI and deploy API.

## Prerequisites

- Motet CLI (`motet-cli`) installed and configured
- Access to a Motet API (e.g. `http://localhost:8000`) with deploy permissions
- Python 3.11+ for local linting

## 1. Scaffold a new bundle

Create a new bundle directory with the expected layout:

```bash
motet-cli bundle init my-bundle
cd my-bundle
```

This creates:

- `manifest.yaml` — bundle name, version, description
- `commands/` — Python modules for custom commands
- `tools/` — Python modules for custom tools
- `agents/` — optional agent definitions/configuration
- `workflows/` — optional workflow YAML definitions
- `config/` — optional routing, models, or MCP config

Edit `manifest.yaml` to set `name` (slug), `version`, and `description`. The `name` is the bundle identifier and the namespace prefix for all artifacts (e.g. `my-bundle.my_command`).

## 2. Add a command

Add a Python file under `commands/` that defines a `@motet.command` function. Use the **Motet SDK** for types and decorators so your bundle works with or without the full runtime. Example `commands/hello.py`:

```python
from motet_sdk import motet, MotetContext, BaseCommandData
from pydantic import Field
from typing import Any, Dict

class HelloData(BaseCommandData):
    """Input for the hello command."""
    message: str = Field(..., description="Message to echo back")

@motet.command(timeout_seconds=30)
def hello(data: HelloData, motet: MotetContext) -> Dict[str, Any]:
    """Echo the given message. Use for testing bundle deployment."""
    return {"echo": data.message}
```

For bundle tools, use `@motet.tool` in `tools/*.py`:

```python
from motet_sdk import motet
from pydantic import BaseModel, Field

class HelloToolParams(BaseModel):
    name: str = Field(default="World", description="Name to greet")

@motet.tool(description="Greet someone", name="hello_tool", schema=HelloToolParams)
def hello_tool(params: dict) -> dict:
    return {"message": f"Hello, {params.get('name', 'World')}!"}
```

### 2.1 Using `@motet.tool` effectively

Recommended kwargs:

- `description` (required): short, user-facing summary of what the tool does.
- `name` (optional): explicit tool name; defaults to function name.
- `schema` (recommended): Pydantic model for parameters.
- Optional metadata: `category`, `keywords`, `cost_class`, `observation_formatter`.

Example with richer metadata:

```python
from motet_sdk import motet
from pydantic import BaseModel, Field

class MathParams(BaseModel):
    a: float = Field(..., description="First operand")
    b: float = Field(..., description="Second operand")

@motet.tool(
    description="Add two numbers",
    name="math_add",
    schema=MathParams,
    category="calculator",
    keywords=["math", "add", "arithmetic"],
    cost_class="low",
)
def math_add(params: dict) -> dict:
    return {"result": params["a"] + params["b"]}
```

Common pitfalls:

- Do not include the bundle prefix in `name`. Use `math_add`, not `my-bundle.math_add`.
- Keep `description` and `schema` accurate; they affect discovery and usability.
- Prefer SDK imports in bundles (`from motet_sdk import motet`) instead of runtime-only imports.
- If you omit `name`, the function name becomes the tool name.

Ensure every command has a docstring and that data classes have descriptions; the linter uses these for AI discovery. For the full list of SDK types (MotetContext, WorkerCapability, MockMotetContext, manifest validation), see [Motet SDK reference](./38-sdk-reference.md).

### 2.2 Sharing code between modules

Files whose names start with `_` are **not** loaded as command or tool modules, so use them for shared code. Each directory (`commands/`, `tools/`, `routing/`) is a real Python package, so import them with a plain relative import:

```python
# commands/_client.py — shared helper, not loaded as a command
import httpx


def fetch(url: str) -> dict:
    return httpx.get(url, timeout=10).json()
```

```python
# commands/report.py
from . import _client


@motet.command()
def report(data: ReportData, motet: MotetContext) -> dict:
    return {"payload": _client.fetch(data.url)}
```

Notes:

- Prefer sharing **within** a directory. Reaching across (`from ..commands import _client` inside a `tools/` module) does work, but only because `commands/` loads before `tools/` by default — it breaks if you reorder `load_order` in `manifest.yaml`. Keep a helper next to each caller, or publish shared code as a real dependency.
- Redeploys pick up edited helpers, so you can change a helper's signature and its callers together in one deploy.
- Helpers are ordinary modules — the linter does not require tool/command metadata on them.

## 3. Lint locally

Run the same linter that the deploy pipeline uses, without deploying:

```bash
motet-cli bundle lint .
```

Fix any reported errors (syntax, safety, missing descriptions). Lint must pass before deploy is accepted.

## 4. Deploy

Deploy from the current directory (zip and upload) or from git:

```bash
# Zip and upload (works from any machine)
motet-cli deploy dir-deploy . --api-url http://localhost:8000

# Or from git (server clones the repo)
motet-cli deploy git-deploy --repo-url https://github.com/org/repo --branch main --path bundles/my-bundle --api-url http://localhost:8000
```

Or call the API directly:

```bash
curl -X POST http://localhost:8000/api/v1/deploy \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"repo_url": "https://github.com/org/repo", "branch": "main", "path": "bundles/my-bundle"}'
```

The API returns `202 Accepted` with a `deploy_job_id` and `status_url`. Poll the status URL to see per-worker ack and any errors.

## 4.1 Fast local iteration (hot deploy)

Use hot deploy for rapid local development loops:

```bash
# Hot deploy (Mutagen sync)
motet-cli bundle hot-deploy .
```

Use `motet-cli deploy dir-deploy .` or `git-deploy` for full deployment; use `motet-cli bundle hot-deploy .` for speed while iterating locally.

## 5. Verify

- **List commands:** `GET /api/v1/commands` — your bundle’s commands appear with namespaced types (e.g. `my-bundle.hello`).
- **List agents:** `GET /api/v1/agents` — your bundle’s agents appear with namespaced IDs (e.g. `my-bundle.research_agent`).
- **Execute:** `POST /api/v1/commands/my-bundle.hello/execute` with body `{"data": {"message": "hi"}}`.

## Next steps

- Add tools under `tools/`, workflows under `workflows/` (YAML), and agents via `agents/agents.yaml` as needed.
- **Agents:** Create `agents/agents.yaml` with a top-level `agents:` list. Each entry defines an agent with `agent_id`, `system_prompt`, and optional `display_name`, `description`, `tool_filter`, `turn_hooks`, model settings, etc. Agents are namespaced as `{bundle_name}.{agent_id}` when loaded. See [Agent Loop – Configuring agents in a bundle (YAML)](./07a-agent-loop.md#configuring-agents-in-a-bundle-yaml) for the full YAML format and example.
- **Workflows:** Create one or more `.yaml` files in `workflows/` (e.g. `workflows/my_workflow.yaml`). Each file defines a single workflow with `workflow_id`, `name`, `description`, `steps`, and optional `required_inputs`, `input_parameters`, and `use_for`. The workflow id is namespaced as `{bundle_name}.{workflow_id}` when loaded. See [Workflow System – Bundle workflows (YAML)](./11-workflow-system.md#bundle-workflows-yaml) for the full YAML format and examples.
- Use `config/models.yaml` to add model profiles and `config/mcp.yaml` for MCP server config. Deploy applies bundle MCP config to the running MCP manager (add/remove services without restarting workers).
- **Surfaces:** Optional `config/surfaces.yaml` with a top-level `surfaces:` list. Each entry needs `id` (global channel slug) and may include `display_name` / `description`. Deploy registers new ids into the surfaces catalog; existing ids are left unchanged.
- Use `motet-cli deploy` for full API options (targeting, validate-only, rollback, undeploy).
- Read [Bundle Scoping and Visibility](./15b-bundle-scoping-and-visibility.md) for visibility, namespacing, and execution routing guidance.
