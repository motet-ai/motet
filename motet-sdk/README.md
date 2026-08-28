# Motet Developer Kit (SDK)

SDK for building Motet bundles (commands, tools, workflows) that run on a Motet runtime. Use this package when you do not have access to the full Motet source tree.

## Install

```bash
pip install motet-sdk
```

## CLI

The **entire** `motet-cli` lives in this package. The SDK ships the full CLI (bundle, local, command, deploy, chat, artifacts, models, tools, memories, traces, database, schedules, vault, workers, workflows, conversations, events, identity, cost, auth, setup, debug, agents, etc.):

```bash
motet-cli --version                # SDK / product version (same number as motet)
motet-cli version                  # Running-stack versions (API + workers + siblings; requires auth)
motet-cli bundle init my-bundle     # Scaffold a new bundle
motet-cli bundle lint my-bundle    # Validate manifest and commands/
motet-cli bundle hot-deploy .      # POST to /api/v1/deploy/hot (requires running stack)
motet-cli local up                 # Pull published images (use --build to rebuild from this tree)
motet-cli local down               # Stop local stack; also removes MCP Docker sidecars (label motet.mcp), not Compose-managed
motet-cli local status             # Container status + workers readiness
motet-cli command list             # List deployed commands (requires running stack)
motet-cli chat --message "Hello"   # Chat with AI (requires running stack)
motet-cli artifacts indexing-status <artifact_id>
# ... and all other motet-cli commands
```

When using the full Motet repo, the repo's `motet-cli` entry point delegates to this package; install both (`pip install -e .` and `pip install -e motet-sdk`) so the full stack and CLI are available.

## Usage

```python
from motet_sdk import distributed_command, MotetContext, WorkerCapability
from pydantic import BaseModel, Field
from typing import Any, Dict

class MyCommandData(BaseModel):
    input_value: str = Field(..., description="Input")

@distributed_command(
    timeout_seconds=60,
    required_capabilities=[WorkerCapability.TOOL_EXECUTION],
)
def my_command(data: MyCommandData, motet: MotetContext) -> Dict[str, Any]:
    result = motet.tools.execute("some_tool", {"arg": data.input_value})
    return {"result": result}
```

## Testing

Use `MockMotetContext` in unit tests, injecting the resources your command
touches (`memory`, `tools`, `models`, `agents`, `workflows`, `schedules`,
`commands`, `conversations`, `vault`, `event_bus`, `artifact_store`):

```python
from unittest.mock import Mock

from motet_sdk import MockMotetContext

def test_my_command():
    motet = MockMotetContext(tools=Mock(execute=Mock(return_value={"ok": True})))
    result = my_command(data=MyCommandData(input_value="x"), motet=motet)
    assert "result" in result
```

Resources are exposed as read-only properties, so pass them to the constructor
rather than assigning afterwards. Methods like `do`, `join`, `apply`, and
`maybe` raise `NotImplementedError` by default — override them on the instance
to stub sub-command calls.

## Bundle manifest

Validate a bundle directory:

```python
from pathlib import Path
from motet_sdk import load_manifest, validate_manifest

manifest = load_manifest(Path("my-bundle"))
err = validate_manifest(Path("my-bundle"))  # None if valid
```

## How to test (Phase 1)

With the full Motet repo:

1. **Unit test (SDK bridge):** A bundle that imports only from `motet_sdk` is loaded by the runtime; the bridge injects the real decorator so the command registers.
   ```bash
   pytest tests/unit/test_bundle_loading.py::TestLoadBundle::test_sdk_demo_bundle_registers_echo_command -v
   ```

2. **Run all bundle loading tests:**
   ```bash
   pytest tests/unit/test_bundle_loading.py -v
   ```

3. **SDK-only (no runtime):** Install the SDK and run bundle code in tests using `MockMotetContext`; the decorator is a no-op so you can call the function directly.

## Example: Run `get-news.news_aggregation`

With the full Motet repo checked out, you can run the new browser-assisted
workflow example end-to-end.

1. **Initialize local fixture repos:**
   ```bash
   bash tests/bundles/setup_repos.sh
   ```

2. **Deploy the example bundle:**
   ```bash
   curl -s -X POST http://localhost:8000/api/v1/deploy \
     -H 'Content-Type: application/json' \
     -d "{\"repo_url\":\"file://$(pwd)/tests/bundles/.repos/get-news\",\"ref\":\"main\",\"path\":\".\"}"
   ```

3. **Execute the workflow via `core.workflow_execution`:**
   ```bash
   curl -s -X POST http://localhost:8000/api/v1/commands \
     -H 'Content-Type: application/json' \
     -d '{
       "command_type": "core.workflow_execution",
       "data": {
         "workflow_id": "get-news.news_aggregation",
         "context": {
           "topic": "AI regulation",
           "max_sources": 4,
           "fetch_tool_name": "core.http_get_browser",
           "max_chars": 2500,
           "min_overlap_terms": 2,
           "include_source_links": true,
           "max_items": 4
         }
       }
     }'
   ```

The workflow returns structured step outputs and a final digest payload from
`get-news.build_digest`.

For broad prompts like "get news", you can call the no-arg workflow alias:

```json
{
  "command_type": "core.workflow_execution",
  "data": {
    "workflow_id": "get-news.top_headlines"
  }
}
```

## Source layout

Module map of the installed package: **`src/motet_sdk/README.md`**. CLI internals: **`src/motet_sdk/cli/README.md`**. Example bundles: **`examples/README.md`**.

## License

Apache-2.0
