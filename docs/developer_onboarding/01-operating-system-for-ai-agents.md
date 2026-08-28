# Operating System for AI Agents

Motet is a pre-1.0 **runtime operating system for AI agents**: the layer that schedules work, owns I/O and memory, and runs your agents and bundles.

## How it is put together

Think of Motet the way you think of an OS for agent workloads:

| OS idea | In Motet |
|---|---|
| Processes / syscalls | **Commands** (`@motet.command`) |
| Scheduler | **Workers** (Celery, capability routing) |
| Devices / I/O | **Tools** and **MCP** (manager process + stream proxy) |
| Filesystem | **Memory**, **artifacts**, and **artifact RAG** (opt-in retrieval into the turn) |
| Users | **Principal** and **tenant** context |
| Applications | **Bundles** (and workflows) |
| Kernel API | **MotetContext** (`do`, `join`, tools, memory, …) |
| Cron / daemons | **Scheduled commands** and `motet.dispatch` (work off the chat turn) |
| Included shell | **Chat Explorer** (`/chat-explorer/`) and **Manage** (`/manage/`) |

Work runs as **distributed commands** on Celery workers:

- An **agent turn** is an LLM loop on the agent worker
- **Model calls, tool calls, and workflows** are commands that may run on other workers
- **MCP** servers live in a manager process; workers talk to them over a Redis stream proxy
- **Bundles** hot-load your commands, tools, and workflows without forking core
- **Several agents** coexist in a registry, can hand work to one another, and can run in parallel from a workflow — see [Advanced Motet Concepts](./24-advanced-concepts.md)

A typical compose stack is API + workers + Redis/Valkey + Postgres + identity (Keycloak in the default local setup).

## Ideas you will keep seeing

### Commands
A command is a typed unit of work (`@motet.command`, Pydantic input, `MotetContext`). Tools, memory, model inference, and deploy are commands. The agentic **loop** stays on one worker and dispatches those children.

### Workers
Workers dequeue Celery tasks. They stay relatively thin because MCP servers are not embedded in each process. **Datacenter workers** sit next to the API. **Device (edge) workers** run on a machine you register (`motet-cli device`) for host-local tools. See [Worker System & Routing](./08-worker-system-routing.md#datacenter-workers-and-device-workers).

### MotetContext
Injected into every command. Tools, memory, vault, models, agents, workflows, conversations, and `motet.do` / `motet.join` / `motet.apply` live here.

### Models
Orchestration speaks a **canonical** protocol. Adapters translate at the provider boundary. See [Supported Models](./03a-supported-models.md) for the catalog and [Canonical LLM Protocol](./09a-canonical-llm-protocol.md) for the request shape.

### Tools
Three registries, one namespace: built-in `core.*`, MCP `mcp.server.tool`, bundle `{bundle_id}.*`. See [Tool Ecosystem](./21-tool-ecosystem.md).

### Workflows and bundles
Workflows compose commands. Bundles package agents, tools, workflows, and config. See [Your First Bundle](./15a-your-first-bundle.md) and [SDK Reference](./38-sdk-reference.md). For inspectable planning, see the [`plan-mode`](../../motet-sdk/examples/bundles/plan-mode/) example.

### Reasoning
Every turn runs the agentic loop. When work splits into independent parts, the loop fans out to parallel sub-agents by calling a tool. See [Reasoning](./10-reasoning.md).

## Where to go next

Read **[Why Motet?](./02-why-motet.md)** to decide whether it fits, then **[Quick Start](./04-quick-start-guide.md)** to boot the stack and run a turn. **[Core Concepts](./05-core-concepts-overview.md)** fills in the vocabulary once things are running.

The [documentation home](./00-landing-page.md) is the welcome page; every guide is in the nav by section.

## Navigation

- **[← Back to Documentation Home](./00-landing-page.md)**

---

**Last Updated**: 2026-08-25
