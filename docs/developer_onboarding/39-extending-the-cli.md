# Extending the CLI

Motet’s `motet-cli` stays **generic** (auth, bundle deploy, command execute,
local stack, device workers). Product-specific verbs belong in a **sibling
CLI** that reuses Motet SDK helpers — not in Motet core. AWS hosting / deploy
ops similarly live in the sibling **`motet-host`** package
(`hosting/motet-host/`), not under `motet-cli`.

The [app-builder](../../motet-sdk/examples/bundles/app-builder/) example shows
the full pattern.

## When to extend

Ship a sibling CLI when your bundle needs:

- Host lifecycle (`docker compose up/down`, workspace setup)
- Short aliases for long `motet-cli command run` / `workflow_execution` payloads
- Product defaults (repo owner, labels, worker IDs)

Do **not** add those commands to `motet-cli` itself.

## Pattern (worked example: app-builder)

```text
examples/bundles/app-builder/
├── .bundleignore              # host-only: cli/, deploy/, install.sh
├── install.sh                 # pip install -e cli/ (+ optional up/deploy)
├── deploy/
│   ├── docker-compose.yml     # product compose (run with --project-directory=repo)
│   ├── app-builder.sh         # host/compose implementation
│   └── setup_workspace.sh
├── cli/                       # installable Click package → `app-builder` on PATH
│   ├── pyproject.toml
│   └── src/app_builder_cli/
└── commands/ … workflows/ …   # Motet bundle (hot-deployed to workers)
```

Put host packaging under `cli/` / `deploy/` and list them in `.bundleignore`
so Motet lint / zip / watch fingerprints skip them.

Install:

```bash
./motet-sdk/examples/bundles/app-builder/install.sh
# requires Motet SDK: pip install -e motet-sdk
```

Use:

```bash
motet-cli local up          # Motet runtime
motet-cli auth login
app-builder up              # product edge worker
app-builder deploy
app-builder cycle
```

## How the product CLI talks to Motet

Reuse the same helpers `motet-cli` uses:

```python
from motet_sdk.cli._api import api_request, api_url_option, normalize_base_url
from motet_sdk.cli._auth import get_api_headers

# Authenticated execute (JWT from motet-cli auth login / store-token)
api_request(
    "POST",
    f"{base}/api/v1/commands/{command_type}/execute",
    headers=get_api_headers(),
    json={"data": data, "timeout_seconds": timeout},
)
```

Host ops stay in a shell script next to compose; the Click CLI shells out to
that script. That keeps Docker/`compose` concerns out of Motet’s Python
packages.

## Checklist for your own bundle

1. Put compose + host scripts under `your-bundle/deploy/`.
2. Add `your-bundle/cli/` with a `pyproject.toml` entry point
   (`your-tool = "your_pkg.main:main"`).
3. Depend on `motet-sdk`; call `_api` / `_auth` (or shell out to
   `motet-cli command run` if you prefer zero Python coupling).
4. Ship `install.sh` that `pip install -e`’s the CLI and documents Motet
   prerequisites (`motet-cli local up`, `motet-cli auth login`).
5. Keep Motet runtime free of your product’s labels, paths, and compose
   project names.

## What Motet owns vs what the product owns

| Motet (`motet-cli` / runtime) | Product (e.g. `app-builder`) |
|-------------------------------|------------------------------|
| Auth, workers, hot-deploy API | Compose project + edge mounts |
| Generic `command` / `bundle` | Workflow aliases (`cycle`, …) |
| Edge file/exec tools | GitHub labels, workspace clone |
| Cost / conversations | Installer + docs for operators |
