# Bundles / SDK

A bundle is how you extend Motet without forking it: commands, tools, agents, workflows, skills, and optional config in one deployable tree.

```text
manifest.yaml          # name (slug), version, description
commands/              # @motet.command
tools/                 # @motet.tool → {bundle_id}.{name}
agents/                # optional AgentConfig
workflows/             # optional workflow YAML
skills/<name>/SKILL.md
config/                # optional routing, models, MCP
```

`manifest.yaml` `name` is the namespace prefix for everything the bundle registers (`my-bundle.hello`).

## SDK vs runtime

Bundle authors import from **`motet_sdk`**: `motet`, `MotetContext`, `BaseCommandData`, the command error types, manifest helpers, concurrency helpers, and `MockMotetContext` for tests. The SDK has no runtime imports. When the bundle runs on a worker, `bundle_reload.py` injects the real implementations — it builds fresh bridge modules rather than mutating the installed SDK, so restoring `sys.modules` returns to the plain SDK.

**Outside a worker** (unit tests, lint, an author's REPL) bundle code gets the SDK's own behavior: a no-op decorator path, stdlib-threading fallbacks for the concurrency names, and `MockMotetContext` in place of the runtime context. Same imports, both places.

`motet-cli` is implemented in the SDK package; the runtime ships a thin entry point.

There is no `motet.llm` or `motet.agent` on the context. Model work is `motet.do(model_inference, …)` / `model_stream`. An agent turn is `motet.agents.turn` or `agent_turn`.

```python
from motet_sdk import motet, MotetContext, BaseCommandData

@motet.command(timeout_seconds=30)
def hello(data: HelloData, motet: MotetContext) -> dict:
    return {"echo": data.message}

@motet.tool(description="Greet someone", name="hello_tool", schema=HelloToolParams)
def hello_tool(params: dict) -> dict:
    return {"message": f"Hello, {params.get('name', 'World')}!"}
```

## Deploy

`motet-cli bundle init` scaffolds. `motet-cli bundle lint` then deploy via the deploy API / CLI. Publish can build a bundle exec image when `config/exec.yaml` declares requirements and no `oci_image_ref` — that build runs on the **deployer** worker, never on a runtime worker.

Isolation for bundle exec (runc / kata) is an execution-backend choice; see [execution-workspaces.md](./execution-workspaces.md). Runtime and SDK stay lockstep on the same `X.Y.Z` until a compatibility matrix exists ([versioning-fsl.md](./versioning-fsl.md)).

## Paths

- SDK: `motet-sdk/src/motet_sdk/` (`context.py`, `concurrency.py`, `testing.py`, `cli/`)
- Bridge: `motet/core/bundles/bundle_reload.py`
- Onboarding: `docs/developer_onboarding/15a-your-first-bundle.md`, `38-sdk-reference.md`
