## Package: motet_sdk.cli

**`motet-cli` implementation** shipped inside **`motet-sdk`**: bundle lifecycle, local stack, deploy, chat, artifacts, models, tools, workers, vault, identity, cost, skills, device bridges, and operational subcommands.

### Purpose

- **Author bundles** (`bundle init`, `lint`, `upload`, `hot-deploy`) against a running Motet API.
- **Run local stacks** (`local up` / `down` / `status`) using SDK-bundled Compose or **`MOTET_COMPOSE_FILE`** overrides. `local up` and `local recreate` write `tls/` when it is missing so the `redis-tls` proxy can start on a clean clone.
- **Operate deployments** with the same verbs as HTTP (`command`, `chat`, `deploy`, `workers`, `workflows`, `conversations`, etc.) over the public API.
- **Edge / device helpers** (`device`, clipboard/shell/process-control bridges) aligned with edge worker documentation in the main **`motet`** tree.
- **AWS hosting** is a separate package: ``motet-host`` under ``hosting/motet-host/`` (not part of ``motet-cli``).

### Structure

- **`main.py`**: Typer/Click entry wiring and command registration.
- **`_api.py`**, **`_auth.py`**, **`_config.py`**, **`_logging.py`**: HTTP client, auth headers, config resolution, logging.
- Resource modules mirror API areas: **`artifacts.py`**, **`chat.py`**, **`commands.py`**, **`skills.py`**, **`vault.py`**, **`workers.py`**, **`version.py`**, **`tenants.py`**, … — each maps CLI flags to **`/api/v1/...`** calls. ``motet-cli version`` inspects the running stack (API, workers, configured siblings); ``motet-cli --version`` is the local package version.

### Related

- CLI quickstart in package README: **`motet-sdk/README.md`**
- REST layout (runtime): **`motet/interfaces/api/v1/README.md`** (Motet repo)
