# Motet CLI Reference

Complete reference for `motet-cli` commands and subcommands. AWS deploy tooling
lives in a separate CLI — see [AWS hosting](#aws-hosting-motet-host-separate-package).

## Quick Start

```bash
# Top-level help
motet-cli --help

# Group help
motet-cli <group> --help

# Nested group help (example)
motet-cli debug memory --help
```

## Common Workflows

### Start local stack

```bash
motet-cli local up            # pull published images (login only for a private/eval registry)
motet-cli local up --build    # rebuild Motet images from this tree
motet-cli local status
motet-cli local doctor
motet-cli local manage
```

### Run an edge worker against a remote deployment

```bash
motet-cli auth login   # or store-token, if not already authenticated
motet-cli device register --device-name my-laptop --read-path ~/work --write-path ~/work/out
motet-cli device start
motet-cli device doctor
```

See [Local development setup](./14-local-development-setup.md#option-3-edge-worker-for-a-remote-motet-deployment) for flags, bridges, and troubleshooting.

### Fast local bundle iteration

```bash
# Hot deploy (Mutagen sync)
motet-cli bundle hot-deploy .
```

### Standard bundle deployment

```bash
motet-cli bundle lint .
# From local dir: zip and upload (works from any machine)
motet-cli deploy dir-deploy .
# Or from git: server clones repo
motet-cli deploy git-deploy --repo-url https://github.com/org/repo --branch main --path bundles/my-bundle
motet-cli deploy list
```

---

## Quick reference: I want to…

| Goal | Command |
|------|---------|
| Run the local stack | `motet-cli local up` |
| Chat with the agent | `motet-cli chat --message "…"` |
| Chat with artifact RAG | `motet-cli chat --message "…" --artifact-id <id>` (optional `--artifact-rag-scope`, `--artifact-tag`, `--artifact-collection-id`, `--allow-broader-artifact-rag-scope`; model: `--provider`, `--model-name`, `--model-profile`) |
| List or run commands | `motet-cli command list` / `motet-cli command run` |
| List tools | `motet-cli tools list` / `motet-cli tools describe` |
| Deploy a bundle (zip local dir) | `motet-cli deploy dir-deploy .` |
| Deploy a bundle from git | `motet-cli deploy git-deploy --repo-url URL --branch BRANCH --path PATH` |
| Hot-reload a bundle | `motet-cli bundle hot-deploy .` |
| Log in / check auth | `motet-cli auth login` / `motet-cli auth status` |
| Inspect traces | `motet-cli traces list` / `motet-cli traces show` |
| Manage vault / MCP | `motet-cli vault list` / `motet-cli vault mcp-servers` |
| Manage workers | `motet-cli workers health` / `motet-cli workers readiness` |
| Inspect stack Motet versions | `motet-cli version` (`motet-cli --version` is the local package only) |
| Attach a device (edge) worker | `motet-cli device register` / `motet-cli device start` — [details](./14-local-development-setup.md#option-3-edge-worker-for-a-remote-motet-deployment) |
| List schedules | `motet-cli schedules list` |
| Manage surfaces catalog | `motet-cli surfaces list` / `create` / `delete` |
| List or cancel live tasks | `motet-cli tasks live` / `motet-cli tasks cancel <task_id>` (`--include-cancelled` on live/list) |
| Configure CLI | `motet-cli setup set` / `motet-cli setup show` |

---

## Table of contents

- [Local development](#local-development)
- [Edge worker (`device`)](#edge-worker-device)
- [Deployment & bundles](#deployment--bundles)
- [Auth & identity](#auth--identity)
- [Agent, commands & tools](#agent-commands--tools)
- [Conversations & memory](#conversations--memory)
- [Observability & debug](#observability--debug)
- [Configuration & infrastructure](#configuration--infrastructure)
- [All groups (alphabetical)](#all-groups-alphabetical)

---

## Local development

| Group | Description | Subcommands |
|-------|-------------|-------------|
| **local** | Manage the local distributed Docker stack (API, workers, dependencies on one machine). | `up`, `down`, `restart`, `status`, `logs`, `doctor`, `manage` |

## AWS hosting (`motet-host`, separate package)

AWS multi-account deploy tooling lives in the standalone **`motet-host`** CLI
(`hosting/motet-host/`), not under `motet-cli`. Install with
`pip install -e hosting/motet-host`. See
[hosting/motet-host/README.md](../../hosting/motet-host/README.md).

```bash
motet-host setup
motet-host add prod-eu --profile my-aws-profile --prefix prod-eu \
  --domain-base example.com --hosted-zone-id <ROUTE53_ZONE_ID>
motet-host status
motet-host logs api
motet-host doctor
```

## Edge worker (`device`)

| Group | Description | Subcommands |
|-------|-------------|-------------|
| **device** | Register and run a **device (edge) worker** on this machine so a remote Motet deployment can route host-local work (allowed paths, clipboard/shell/process bridges). Uses Docker Compose on the device. | `register`, `configure`, `list`, `revoke`, `start`, `stop`, `status`, `logs`, `doctor`, `build`, `update` |

Conceptual overview: [Worker System & Routing](./08-worker-system-routing.md#datacenter-workers-and-device-workers). Setup walkthrough: [Local development setup](./14-local-development-setup.md#option-3-edge-worker-for-a-remote-motet-deployment).

---

## Deployment & bundles

| Group | Description | Subcommands |
|-------|-------------|-------------|
| **bundle** | Bundle developer experience: init, lint, hot deploy (sync). | `init`, `lint`, `hot-deploy` |
| **deploy** | Bundle deployment (POST/GET /api/v1/deploy). | `git-deploy`, `dir-deploy`, `list`, `status`, `validate`, `propagate`, `rollback`, `undeploy`, `history` |

---

## Auth & identity

| Group | Description | Subcommands |
|-------|-------------|-------------|
| **auth** | Obtain and manage principal (user) authentication for the CLI. | `login`, `store-token`, `check`, `logout`, `status` |
| **identity** | Current principal and tenant (API: /api/v1/identity). | `me`, `tenant` |
| **tenants** | Tenant / Motet (environment) catalog (API: /api/v1/tenants). | `list`, `get`, `create`, `update`, `delete`, `ensure-defaults`, `motets list\|get\|create\|update\|delete` |
| **service-account** | Manage service account tokens for automation. `create` also binds OpenAI-compatible API policy via `--facade-mode` and `--allowed-models`. | `create`, `list`, `revoke` |

---

## Agent, commands & tools

| Group | Description | Subcommands |
|-------|-------------|-------------|
| **chat** | Single command: chat with the agent. | `--message`, `--stream`, `--new`, `--provider`, `--model-name`, `--model-profile`, `--artifact-rag-scope`, `--artifact-id`, `--artifact-tag`, `--artifact-collection-id`, `--allow-broader-artifact-rag-scope`, `--api-url` |
| **command** | Commands API: list, info, run (commands are deployed via bundles). | `list`, `info`, `run` |
| **models** | Single command: list models. See [Supported models](./03a-supported-models.md). | `--provider`, `--api-url` |
| **tools** | Tools/MCP utilities. | `list`, `describe`, `call` |
| **workflows** | Workflow management (API: /api/v1/workflows). Templates via `list`/`create`/`execute`; checkpointed runs via `runs`. | `list`, `create`, `execute`, `runs list\|get\|pause\|cancel\|resume` |
| **tasks** | Live orchestration tasks (API: /api/v1/tasks). List in-flight work and request cooperative cancel for a task tree. | `live`, `list`, `get`, `cancel` |

---

## Conversations & memory

| Group | Description | Subcommands |
|-------|-------------|-------------|
| **conversations** | Manage conversations (API: /api/v1/conversations). | `list`, `get`, `clear`, `rename`, `delete` |
| **surfaces** | Surfaces catalog (API: /api/v1/surfaces). Mutations require admin. | `list`, `get`, `create`, `update`, `delete` |
| **memories** | Memory operations (API: /api/v1/memories). | `list`, `find`, `tag`, `clear`, `vector-list`, `store`, `store-dir`, `inspect`, `consolidate`, `retrieve`, `retrieval-eval` |
| **artifacts** | Artifact storage management (API: /api/v1/artifacts). | `ls`, `put`, `get`, `rm`, `rm-all`, `info`, `metadata`, `indexing-status`, `reindex`, `reindex-task`, `indexing-policy`, `strategies`, `plan` |

---

## Observability & debug

| Group | Description | Subcommands |
|-------|-------------|-------------|
| **traces** | Trace operations. | `list`, `show`, `watch`, `replay` |
| **events** | Event stream and stats (API: /api/v1/events). | `stream`, `stats` |
| **cost** | Cost tracking and budget (API: /api/v1/cost). | `summary`, `summary-by-principal`, `usage`, `budget`, `budget-set`, `events` |
| **debug** | Debug API: commands, task flow, memory, traces (requires `MOTET_DEBUG_MODE=true` for many). | `commands list`, `command`, `task-flow`, `task-events`, `flow-analysis`, `memory stats`, `memory search`, `traces list`, `trace` |

---

## Configuration & infrastructure

| Group | Description | Subcommands |
|-------|-------------|-------------|
| **setup** | Configure default API URL and other CLI settings (~/.motet/config.json). | `set`, `show`, `doctor` |
| **vault** | Manage vault credentials and MCP (API: /api/v1/vault). | `list`, `get`, `store`, `retrieve`, `delete`, `mcp-env`, `mcp-servers`, `health`, `stats` |
| **workers** | Manage workers (API: /api/v1/workers). Admin actions require motet-admin role. | `readiness`, `health`, `terminate`, `start`, `stop`, `restart`, `terminate-unhealthy`, `termination-history` |
| **version** | Inspect Motet product versions on the running stack (API, workers, configured siblings via /api/v1/version). Distinct from `motet-cli --version`. | *(no subcommands)* |
| **schedules** | Manage scheduled commands (API: /api/v1/schedules). | `list`, `command-types`, `stats`, `get`, `create`, `cancel`, `delete`, `suspend`, `resume` |
| **database** | Database operations. | `migrate-pgvector` |

---

## All groups (alphabetical)

For quick scanning, all top-level groups in alphabetical order:

| Group | Subcommands |
|-------|-------------|
| **artifacts** | `ls`, `put`, `get`, `rm`, `rm-all`, `info`, `metadata`, `indexing-status`, `reindex`, `reindex-task`, `indexing-policy`, `strategies`, `plan` |
| **auth** | `login`, `store-token`, `check`, `logout`, `status` |
| **bundle** | `init`, `lint`, `hot-deploy` |
| **chat** | `--message`, `--stream`, `--new`, `--provider`, `--model-name`, `--model-profile`, `--artifact-rag-scope`, `--artifact-id`, `--artifact-tag`, `--artifact-collection-id`, `--allow-broader-artifact-rag-scope`, `--api-url` |
| **command** | `list`, `info`, `run` |
| **conversations** | `list`, `get`, `clear`, `rename`, `delete` |
| **cost** | `summary`, `summary-by-principal`, `usage`, `budget`, `budget-set`, `events` |
| **database** | `migrate-pgvector` |
| **debug** | `commands list`, `command`, `task-flow`, `task-events`, `flow-analysis`, `memory stats`, `memory search`, `traces list`, `trace` |
| **deploy** | `deploy`, `list`, `status`, `validate`, `propagate`, `rollback`, `undeploy`, `history` |
| **device** | `register`, `configure`, `list`, `revoke`, `start`, `stop`, `status`, `logs`, `doctor`, `build`, `update` |
| **events** | `stream`, `stats` |
| **identity** | `me`, `tenant` |
| **local** | `up`, `down`, `restart`, `status`, `logs`, `doctor`, `manage` |
| **memories** | `list`, `find`, `tag`, `clear`, `vector-list`, `inspect`, `consolidate`, `retrieve`, `store`, `store-dir`, `retrieval-eval` |
| **models** | `--provider`, `--api-url` |
| **schedules** | `list`, `command-types`, `stats`, `get`, `create`, `cancel`, `delete`, `suspend`, `resume` |
| **service-account** | `create`, `list`, `revoke` |
| **setup** | `set`, `show`, `doctor` |
| **surfaces** | `list`, `get`, `create`, `update`, `delete` |
| **tasks** | `live`, `list`, `get`, `cancel` |
| **tenants** | `list`, `get`, `create`, `update`, `delete`, `ensure-defaults`, `motets list\|get\|create\|update\|delete` |
| **tools** | `list`, `describe`, `call` |
| **traces** | `list`, `show`, `watch`, `replay` |
| **vault** | `list`, `get`, `store`, `retrieve`, `delete`, `mcp-env`, `mcp-servers`, `health`, `stats` |
| **version** | *(no subcommands — GET /api/v1/version)* |
| **workers** | `readiness`, `health`, `terminate`, `start`, `stop`, `restart`, `terminate-unhealthy`, `termination-history` |
| **workflows** | `list`, `create`, `execute`, `runs list\|get\|pause\|cancel\|resume` |

---

## Notes

- This page reflects the currently registered CLI commands in `motet.cli.main`.
- For option-level details, run `motet-cli <group> --help`.
- For nested debug groups, use `motet-cli debug <group> --help`.

## Navigation

- **[← Back to Documentation Home](./00-landing-page.md)** - Main onboarding hub
- **[Quick Start Guide](./04-quick-start-guide.md)** - Fast setup path
- **[Local Development Setup](./14-local-development-setup.md)** - Detailed local environment guidance

---

**Last Updated**: 2026-08-28
