# Bundle Lifecycle (`motet/core/bundles/`)

Everything that happens to a bundle between "someone pushed code" and "a worker
can run it": fetch, validate, publish, deploy, propagate, hot-reload, roll back,
unload, and OCI image build for exec artifacts.

| Module | Runs on | Responsibility |
|--------|---------|----------------|
| `deploy.py` | deployer worker (`WorkerCapability.DEPLOYMENT`) | The deploy pipeline: validate → publish → reload, plus rollback/undeploy/propagate |
| `bundle_lint.py` | deployer (called from validate) | AST / policy lint for bundle sources (issue #158); re-exported from `deploy` |
| `bundle_reload.py` | AI workers | Load/unload a bundle into the live registries; hosts the SDK runtime bridge |
| `bundle_image_build.py` | deployer worker | OCI image builds for bundle exec artifacts |

These are distributed commands, but they don't sequence turns, gather, or
dispatch — bundle lifecycle is its own concern, with its own operational
story, so it has its own package.

## Importing

This package re-exports nothing; import the submodule you need:

```python
from motet.core.bundles.deploy import deploy_bundle, DeployBundleData
from motet.core.bundles.bundle_reload import load_bundles_on_startup
```

`deploy` alone pulls in skills, agents, and the scheduling manager, so keeping
`__init__.py` empty of re-exports keeps the cheap submodules cheap.

## Registration

`@distributed_command` registers a command when its module is imported, and
these modules are imported by `DistributedCommand._ensure_commands_registered`
in `motet/core/commands/distributed.py`.

**Adding a new command module here means adding it to that list.** Skipping it
means workers reject the command type at runtime with "Unknown command type",
and no unit test will catch it.

Custom commands, tools, workflows, and config are deployed as **bundles** from git (or uploaded zip). Bundle deploy is the only supported path.

### Deploy pipeline (deployer worker)

Commands in `deploy.py` run on the **deployer worker** (`WorkerCapability.DEPLOYMENT`):

| Command | Responsibility |
|--------|----------------|
| `core.deploy_bundle` | Orchestrates validate → publish → reload on targeted workers |
| `core.validate_bundle` | Fetch from git + lint; streams `lint_file` / `lint_error` / `lint_complete` to task stream |
| `core.publish_bundle` | Write artifact to store; dispatch `core.reload_bundle` to live targeted workers |
| `core.propagate_bundle` | Retry reload for failed/skipped workers |
| `core.rollback_bundle` | Re-deploy a prior `bundle_version` from artifact store |
| `core.undeploy_bundle` | Unload bundle on targeted workers; cancel affected schedules |

### AI worker commands (bundle load/unload)

In `bundle_reload.py`, run on **AI workers** (dispatched by the deployer):

| Command | Responsibility |
|--------|----------------|
| `core.reload_bundle` | Pull artifact, extract to bundle dir, load config → model spec → MCP → commands/tools/workflows; refresh search index |
| `core.unload_bundle` | Unregister bundle artifacts; remove bundle dir; cancel schedules |

**SDK runtime bridge:** `_load_bundle` calls `_inject_motet_sdk_runtime_bridge` so bundle modules that `from motet_sdk import motet, distributed_command, …` get the worker’s real decorator and context. Inject builds **fresh** `types.ModuleType` package/submodule objects and swaps them into `sys.modules` — it does **not** mutate installed `motet_sdk.*` module objects in place (issue #116). Workers keep the bridge for the process lifetime. Unit tests that need the SDK no-op path again should use `motet_sdk_runtime_bridge` or `snapshot_motet_sdk_runtime_bridge` / `restore_motet_sdk_runtime_bridge` from `bundle_reload.py` (production load paths do not auto-restore).

### Deploy API

All deployment is via **`/api/v1/deploy`** (see `interfaces/api/v1/deploy.py`):

- **POST /api/v1/deploy** — Deploy from git (repo_url, branch, path)
- **POST /api/v1/deploy** (multipart) — Upload zip and deploy
- **GET /api/v1/deploy** — List deployed bundles
- **GET /api/v1/deploy/{bundle_id}/status** — Poll deploy job status
- **POST /api/v1/deploy/validate** — Validate-only (git); SSE lint stream
- **POST /api/v1/deploy/validate-upload** — Validate-only (uploaded zip); SSE lint stream
- **POST /api/v1/deploy/{bundle_id}/propagate** — Retry propagation to failed/skipped workers
- **POST /api/v1/deploy/{bundle_id}/rollback** — Rollback to a prior bundle_version
- **DELETE /api/v1/deploy/{bundle_id}** — Undeploy
- **GET /api/v1/deploy/{bundle_id}/history** — Deploy history for a bundle

List and execute commands (including bundle-contributed ones) remain under **GET/POST /api/v1/commands**.

### Catalog descriptions and schemas

Redis catalogs (`bundle:{bundle_id}:catalog`) carry more than command name lists.
`GET /api/v1/commands` and the manage UI read these fields for bundle rows:

| Field | When populated | Source |
|-------|----------------|--------|
| `command_descriptions` | Deploy / publish (AST extract over `commands/*.py`) | Decorator `description=` or the command function docstring (first line) |
| `command_schemas` | After AI-worker reload / hot-reload / propagate acks | Live `CommandTypeRegistry` → Pydantic `data_class.model_json_schema` (`bundle_reload._command_schemas_from_registry`), merged by `deploy._merge_command_schemas_into_catalog` |

Until workers ack a reload, `command_schemas` may be empty even though commands
are listed. A full **redeploy**, **hot-deploy**, or **propagate** refreshes both
maps for existing installs. Core (non-bundle) commands take description/schema
from the process registry, not this catalog path.

### Adding bundle commands (recommended)

1. Use the **`@motet.command`** or **`@distributed_command`** decorator in your bundle’s `commands/*.py`; prefer `from motet_sdk import motet` then `@motet.command(...)`.
2. Define a Pydantic data class for input; use `motet` for tools, memory, etc.
3. Deploy the bundle via **POST /api/v1/deploy** (git or upload) or **`motet-cli deploy git-deploy`** / **`motet-cli deploy dir-deploy`**.
4. Commands are registered under **`bundle_id.command_type`** (e.g. `calculator.calculate`).
5. **Shared underscore modules**: files starting with `_` (e.g. `commands/_helpers.py`) are not loaded as module files, but the loader registers `bundle.<id>.commands` — and likewise `bundle.<id>.tools` and `bundle.<id>.routing` — as real packages (with `__path__`), so modules in each import shared code with a plain relative import: `from. import _helpers as h`.

 `_load_bundle` purges **every** `bundle.<id>.*` entry from `sys.modules` before any section loads, **including the package parents**, so hot redeploys always execute fresh shared code. The parents matter: importing a submodule binds it as an attribute on its package, and `from. import _helpers` reads that attribute before consulting `sys.modules` or disk — so keeping the parent kept the stale helper and a redeployed command ran against the previous revision. Each package's `__path__` is assigned the directory being loaded rather than appended to, so one bundle id loaded from two directories cannot resolve relative imports against the older tree.

See the developer guide **Your first bundle** for the full flow.

