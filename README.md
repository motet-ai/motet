# Motet

**Pre-1.0** runtime operating system for AI agents.

**License (split):** The runtime under `motet/` is **Functional Source License, Version 1.1 (FSL-1.1-ALv2)** or a commercial license — see [LICENSE-FSL](LICENSE-FSL) and [COMMERCIAL_LICENSE.md](COMMERCIAL_LICENSE.md). The **Motet Developer Kit** in `motet-sdk/` is **Apache 2.0** — see [motet-sdk/LICENSE](motet-sdk/LICENSE). Third-party licenses: [NOTICES](NOTICES).

Motet is the layer agents run *on*. Commands are the unit of work, workers are the scheduler, built-in tools and MCP are I/O, memory and artifacts are storage, and **bundles are how you extend it**: installable packages of commands, tools, and workflows, written against the Apache-licensed SDK and deployed onto the running stack (API, workers, Redis/Valkey, Postgres, identity) without touching the runtime.

Motet is built to coordinate agents working together. **Multi-agent orchestration is a first-class promise of the runtime**, not a special mode: a registry holds many agent configurations, any agent can hand work to another, and a workflow can fan several out in parallel and synthesize the results. See [Advanced Motet Concepts](docs/developer_onboarding/24-advanced-concepts.md).

## What you do not have to decide

A library will beat Motet to a demo — the first `local up` pulls the published snapshot images. What Motet saves is the second week. From a clean clone, two decisions stand between you and an agent that calls a tool and remembers the answer: supply a model credential, and log in.

These were already made for you: where memory lives (Valkey, with vector search), which embedding model runs and where (baked into a sibling image at build time), how responses stream (SSE and WebSocket), how tools are defined (`@motet.tool`, MCP, 47 built-ins registered), how spend is measured (automatic, with pricing for 54 cloud models), who the caller is (Keycloak realm and JWT), where credentials live (an encrypted vault), what runs work outliving a request (Celery workers), where it runs in production (install scripts for EC2 and Fargate), and how portable you are across providers (eight adapters — six cloud vendors plus local and mock — switched by environment variable).

These are stack defaults rather than library defaults, and tenancy is scoping rather than a hardened boundary. [Why Motet](docs/developer_onboarding/02-why-motet.md) has the full table and where it stops.

## Getting started

Python 3.11 and Docker required. Put `OPENAI_API_KEY` in `.env` before `local up`. Details: [Quick Start](docs/developer_onboarding/04-quick-start-guide.md#model-api-key).

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
pip install -e motet-sdk && pip install -e .

motet-cli local up
```

Convenience images live at `ghcr.io/motet-ai` and use the product version tag. An eval (invite-only) snapshot needs `docker login ghcr.io`. A public Motet release uses the same tags without login. Use `motet-cli local up --build` when you are changing Motet itself.

- **[Developer onboarding](docs/developer_onboarding/README.md)** — concepts, commands, bundles, MCP
- **[Architecture](docs/architecture/current/README.md)** — topology and runtime invariants; read the index plus the one chapter you need
- **[Quick start](docs/developer_onboarding/04-quick-start-guide.md)** — env, first command, local chat
- **[Local development](docs/developer_onboarding/14-local-development-setup.md)** — Docker workflow
- **[Your first bundle](docs/developer_onboarding/15a-your-first-bundle.md)** — write an extension: package commands and tools, deploy to the running stack, no core changes

With the stack up:

| App | URL | Role |
|---|---|---|
| **Chat Explorer** | `/chat-explorer/` | Reference chat: agents, conversations, artifacts, streaming |
| **Manage** | `/manage/` | Full admin console: live worker fleet, bundle deploys, schedules, artifacts, spend, and per-task trace visualization |

Administration and monitoring are part of the runtime, not an add-on: Manage gives you a comprehensive window into what your agents are doing — which workers are healthy, what a task actually executed (rendered as a command graph), where the time and money went — from the first `local up`. Both apps ship with the runtime. Build your own surfaces on the same APIs (`@motet/ui-common` is the shared UI kit). Details: [Chat Explorer](docs/developer_onboarding/36-chat-explorer.md), [Observability](docs/developer_onboarding/23-observability-debugging.md).

## Capabilities

### Agents and conversations
- **Agent command**: tool-using loop for chat and sub-agents. The loop runs on the agent worker; model, tool, and workflow steps are distributed commands.
- **Conversations**: list, rename, clear, register; scope by agent and surface.
- **Details:** [Agent Loop](docs/developer_onboarding/07a-agent-loop.md), [Conversations](docs/developer_onboarding/07b-conversations.md)

### Commands and workers
- **Commands** are the unit of work. Authors write `@motet.command` functions; the runtime routes them to Celery workers with task, conversation, and principal context.
- **Routing** uses worker capabilities and load. The agentic loop stays on one worker; children are dispatched as commands.
- **Details:** [Distributed command system](docs/developer_onboarding/07-distributed-command-system.md)

### Tools and MCP
- **MCP** servers run in a dedicated manager process. Workers talk to them over a Redis stream proxy, so each worker stays thin.
- **Tools** come from three places: built-in (`core.*`), MCP (`mcp.server.tool`), and bundles (`{bundle_id}.*`).
- **Details:** [MCP integration](docs/developer_onboarding/09-mcp-integration.md), [Tool ecosystem](docs/developer_onboarding/21-tool-ecosystem.md)

### Models
- Orchestration uses a **canonical, provider-agnostic** request/stream protocol. Adapters map OpenAI, Anthropic, Gemini, and local APIs.
- Your agentic flows are **model-agnostic**: swap providers by configuration and A/B model performance with little to no additional effort — no rewriting commands.
- **Details:** [Canonical LLM protocol](docs/developer_onboarding/09a-canonical-llm-protocol.md)

### Cost and budgets
- **Usage accounting**: token counts normalized across providers and converted to estimated USD from per-model pricing, on streaming and non-streaming calls alike.
- **Spend budgets**: daily and monthly per-tenant limits checked *before* the provider call, so an over-budget tenant is blocked rather than billed and warned afterward.
- **Aggregation**: Redis-backed rollups by tenant, principal, and conversation; per-turn `cost_usd` is handed to `after_finalize` hooks for export.
- **Operator surface**: `/api/v1/cost/*`, the Cost tab in Manage, and `motet-cli cost`.
- Costs are estimates for control and attribution, not billing records. Infrastructure cost and long-term analytics are not tracked.
- **Details:** [Cost and usage API](docs/developer_onboarding/28-api-reference.md#cost-and-usage-api)

### Memory, artifacts, workflows
- **Memory**: working / short-term / long-term with hybrid retrieval. Consolidation is opt-in (command or API).
- **Artifacts**: uploads and tool outputs, including multimodal context.
- **Artifact RAG**: chunk and index artifacts, then retrieve citation-ready context into the turn (conversation- or principal-scoped). Off by default (`MOTET_ARTIFACT_RAG_ENABLED`).
- **Workflows**: declarative multi-step command compositions; some built-ins are exposed to the LLM as `workflow_*` tools.
- **Details:** [Memory](docs/developer_onboarding/20-memory-management.md), [Artifacts](docs/developer_onboarding/20a-artifacts-and-multimodal-context.md), [Workflows](docs/developer_onboarding/11-workflow-system.md)

### Reasoning
- Every turn runs one **agentic loop**. Modes are `auto` (default), `agentic`, and `no_tools`. Parallel work is `core.spawn_agents` from inside the loop — not a second executor.
- **Details:** [Reasoning](docs/developer_onboarding/10-reasoning.md)

### Bundles, schedules, streaming
- **Bundles** are Motet's extensibility mechanism — think extensions for the runtime. A bundle packages commands, tools, workflows, and config, and installs onto the running stack with lint, deploy, reload, and rollback. New capabilities reach every worker through this first-class path; you never rewrite workers to get a need met.
- **Schedules**: cron, delayed, and recurring commands — work that continues without a live chat turn (`motet.schedules`, `motet.dispatch`).
- **Streaming**: SSE for chat and UI events.
- **Details:** [First bundle](docs/developer_onboarding/15a-your-first-bundle.md), [Schedules](docs/developer_onboarding/12-scheduled-commands.md), [Streaming](docs/developer_onboarding/13-streaming-responses.md)

### OpenAI-compatible API
- Optional `/v1` facade (`chat/completions`, `responses`, `models`) for Cursor, Open WebUI, and OpenAI SDKs.
- Off by default (`MOTET_OPENAI_COMPAT_ENABLED`). Modes: `passthrough`, `hosted_tools`, or full Motet `agent`.
- **Details:** [API reference — OpenAI-compatible API](docs/developer_onboarding/28-api-reference.md#openai-compatible-api)

### Security, tenancy, observability
- JWT / principal context on commands. Tenant IDs scope data in Redis and APIs.
- Task streams, structured events, and the Manage app for traces and worker state.
- **Details:** [Security & multi-tenancy](docs/developer_onboarding/22-security-multi-tenancy.md), [Observability](docs/developer_onboarding/23-observability-debugging.md)

## MCP

Configure servers in `mcp_instance_manager.yaml`. The manager process owns the servers; workers stay thin.

```yaml
services:
  - service_id: "playwright"
    transport: "stdio"
    command: "npx"
    args: ["-y", "@playwright/mcp"]
    env:
      PLAYWRIGHT_HEADLESS: "true"
    instances: 1
    is_stateful: true
```

Bundle and command authors call tools by name through `motet.tools` or `tool_execution`. See [MCP integration](docs/developer_onboarding/09-mcp-integration.md).

## Deploying

`motet-cli local up` is for your machine. To run the stack somewhere else, the
deploy targets live in [hosting/](hosting/README.md):

| Target | Notes |
|--------|-------|
| **AWS EC2** | EC2 + Docker Compose with ElastiCache and RDS. `./hosting/aws/ec2/py/install.sh` |
| **AWS Fargate** | ECS Fargate + ECR, same data layer. `./hosting/aws/fargate/install.sh` |

For AWS specifically, **`motet-host`** is a separate CLI that manages installs
across multiple AWS accounts — environment switching, per-account DNS, and
backup/restore — wrapping the EC2 deployer:

```bash
pip install -e hosting/motet-host
motet-host setup            # register an environment
motet-host use <env>        # switch the active account/stack
motet-host install --yes    # deploy and wait for the API to come up
```

It is intentionally separate from `motet-cli` so deploy tooling can move at its
own pace. See [hosting/motet-host/](hosting/motet-host/README.md) and the
[multi-account deploy guide](hosting/aws/multi-account-deploy-guide.md).

## Project layout

```
.
├── motet/                      # Runtime (FSL / commercial)
│   ├── core/                   # Commands, workers, models, tools, memory, …
│   ├── interfaces/             # FastAPI + Chat Explorer / ops frontend
│   ├── cli/                    # Thin motet-cli entry (implementation in SDK)
│   └── services/
├── motet-sdk/                  # Bundle-author SDK + CLI (Apache 2.0)
├── hosting/                    # Deploy targets (AWS) + motet-host CLI
├── tests/
├── docs/developer_onboarding/  # Developer guides
├── config/
├── docker/
├── docker-compose.distributed.yml
├── requirements.txt
└── pyproject.toml
```

## Development

Python 3.11. Format with Black and isort; lint with flake8; type-check with mypy.

We are always looking for feedback and folks who want to pilot Motet — email `hello@motet.dev`. Collaborating on Motet itself is invite-only (same address). We are not accepting unsolicited pull requests. To extend the runtime, write a [bundle](docs/developer_onboarding/15a-your-first-bundle.md). Style and tests for people working in this tree: [Contributing](docs/developer_onboarding/32-contributing-guide.md).

```bash
black motet/
isort motet/
flake8 motet/
mypy motet/
```

## License and docs

- **Onboarding:** [docs/developer_onboarding/](docs/developer_onboarding/README.md)
- **Architecture:** [docs/architecture/current/](docs/architecture/current/README.md)
- **Security:** [SECURITY.md](SECURITY.md)
- **Commercial terms:** [COMMERCIAL_LICENSE.md](COMMERCIAL_LICENSE.md)

Runtime (`motet/`): FSL-1.1-ALv2 or commercial. SDK (`motet-sdk/`): Apache 2.0. See [NOTICES](NOTICES) for third-party licenses.
