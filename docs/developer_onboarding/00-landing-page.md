# Welcome to Motet Developer Documentation

> **Motet** is a pre-1.0 **runtime operating system for AI agents**. You run the stack (API, workers, Redis/Valkey, Postgres, identity) and extend it with bundles.

## What is Motet?

Motet is the layer agents run *on*. It schedules **commands** on workers, provides tools (including MCP), memory, artifacts, and identity, and loads **bundles** as applications. An agent turn is an LLM loop that calls those commands. Workers dequeue work and route by capability.

Agents are not a special case in that picture — they are one more thing the runtime schedules. That is what makes **several agents** straightforward: many agent configurations coexist in a registry, one agent can hand work to another, and a workflow can run several in parallel.

### What you can build

- **Commands** and **tools** with `@motet.command` / `@motet.tool` in a bundle
- An **agent** that uses MCP servers, built-in tools, and workflows
- **Conversations**, **memory**, and **artifacts** with principal and tenant context
- Streaming turns over SSE, or OpenAI `/v1` for existing clients
- **Chat Explorer** (`/chat-explorer/`) and **Manage** (`/manage/`) — the included chat and operator UIs

## Get started

1. **[Operating System for AI Agents](./01-operating-system-for-ai-agents.md)** — how the runtime is put together
2. **[Why Motet?](./02-why-motet.md)** — what the runtime already decided for you, and when it fits
3. **[Supported Models](./03a-supported-models.md)** — providers and flagship ids
4. **[Quick Start Guide](./04-quick-start-guide.md)** — `motet-cli local up` and a first turn
5. **[Your First Bundle](./15a-your-first-bundle.md)** or **[Building Your First Command](./15-building-your-first-command.md)**

The documentation nav lists every guide by section: Start, Concepts, Build, Runtime, State, Operate, Surfaces, and Guides.

## Prerequisites

- Python 3.11 and Docker
- Comfort with a terminal, REST APIs, and environment variables
- A model provider API key ([Quick Start](./04-quick-start-guide.md#model-api-key))

---

**Last Updated**: 2026-08-26
