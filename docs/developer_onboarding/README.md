# Motet Developer Onboarding

Motet is a pre-1.0 runtime operating system for AI agents: commands on workers, MCP, bundles, and an agent loop.

Two decisions stand between a clean clone and an agent that calls a tool and remembers the answer — supply a model credential, and log in. [Why Motet](./02-why-motet.md) covers what the runtime already decided for you, and where those defaults stop.

## Start

1. **[Quick Start](./04-quick-start-guide.md)** — boot the stack and talk to an agent
2. **[Core Concepts](./05-core-concepts-overview.md)** — the vocabulary the rest of these docs assume
3. **[Building Your First Command](./15-building-your-first-command.md)** — end to end, with tests

That is enough to be productive. Everything below is reference — read it when you hit the thing it describes, not before.

## Concepts

- **[Operating System for AI Agents](./01-operating-system-for-ai-agents.md)** — how the runtime is put together
- **[Why Motet?](./02-why-motet.md)** — what is already decided, and when the runtime fits
- **[What Motet Can Do](./03-what-motet-can-do.md)** — capabilities and use cases
- **[Design Principles](./06-design-principles.md)** — the reasoning behind the shape

## Build

- **[Your First Bundle](./15a-your-first-bundle.md)** — create, lint, and deploy an extension
- **[Bundle Scoping and Visibility](./15b-bundle-scoping-and-visibility.md)** — namespaces and execution behavior
- **[Command Composition Patterns](./16-command-composition-patterns.md)** — `do`, `join`, `apply`, `maybe`
- **[Building Workflows](./17-building-workflows.md)** — multi-step compositions
- **[Tool Ecosystem](./21-tool-ecosystem.md)** — use built-ins and write your own
- **[Testing Strategies](./18-testing-strategies.md)** — unit, integration, and bundle tests

## Runtime

- **[Distributed Command System](./07-distributed-command-system.md)** — the unit of work
- **[Agent Loop](./07a-agent-loop.md)** — the tool-using loop
- **[Conversations](./07b-conversations.md)** — chat sessions and scoping
- **[Reasoning](./10-reasoning.md)** — the agent loop and parallel sub-agents
- **[Workflow System](./11-workflow-system.md)** — declarative orchestration
- **[Worker System & Routing](./08-worker-system-routing.md)** — capabilities, load, and placement
- **[Worker Targeting](./08a-worker-targeting-guide.md)** — pinning work to specific workers
- **[MCP Integration](./09-mcp-integration.md)** — the manager process and stream proxy
- **[MCP OAuth & Credentials](./09b-mcp-oauth-credentials.md)** — authenticating MCP servers
- **[Canonical LLM Protocol](./09a-canonical-llm-protocol.md)** — provider-agnostic requests and streams
- **[Supported Models](./03a-supported-models.md)** — providers, flagship ids, and the live catalog
- **[Concurrency Primitives](./19-concurrency-primitives.md)** — pool-agnostic threading
- **[Scheduled Commands](./12-scheduled-commands.md)** — work without a live turn
- **[Streaming Responses](./13-streaming-responses.md)** — SSE, WebSocket, attribution

## State

- **[Memory Management](./20-memory-management.md)** — working, short-term, long-term, and retrieval
- **[Artifacts and Multimodal Context](./20a-artifacts-and-multimodal-context.md)** — uploads, tool outputs, RAG

## Operate

- **[Local Development Setup](./14-local-development-setup.md)** — the Docker workflow
- **[Security & Multi-Tenancy](./22-security-multi-tenancy.md)** — principals, tenants, and scoping
- **[Observability & Debugging](./23-observability-debugging.md)** — traces, events, diagnosis
- **[Configuration Reference](./29-configuration-reference.md)** — every setting
- **[Troubleshooting](./30-troubleshooting-guide.md)** — when something is wrong

## Surfaces

- **[Chat Explorer](./36-chat-explorer.md)** — the reference chat UI and shared component kit
- **[API Reference](./28-api-reference.md)** — REST surface, including the [OpenAI-compatible `/v1` facade](./28-api-reference.md#openai-compatible-api)
- **[CLI Reference](./37-motet-cli-reference.md)** — `motet-cli` command groups
- **[SDK Reference](./38-sdk-reference.md)** — the public API for bundle authors
- **[Extending the CLI](./39-extending-the-cli.md)** — sibling product CLIs

## Guides

- **[Advanced Concepts](./24-advanced-concepts.md)** — capability routing, parallelism, multi-agent patterns
- **[Common Patterns](./25-common-patterns.md)** — reusable shapes
- **[Example Bundles](./26-example-bundles.md)** — complete builds
- **[Best Practices](./27-best-practices.md)** — including retry-safe command design
- **[Architecture Guide](./31-architecture-guide.md)** — the product-level map
- **[Project Structure](./33-project-structure.md)** — navigating the codebase
- **[Resources & Links](./34-resources-links.md)** — everything else
- **[Contributing Guide](./32-contributing-guide.md)** — feedback and pilots welcome; no unsolicited PRs; style and tests for this tree

## Find it by topic

| Looking for | Go to |
|---|---|
| Commands | [Command system](./07-distributed-command-system.md), [First command](./15-building-your-first-command.md), [Composition](./16-command-composition-patterns.md) |
| Agents and chat | [Agent Loop](./07a-agent-loop.md), [Conversations](./07b-conversations.md), [Chat Explorer](./36-chat-explorer.md) |
| Tools and MCP | [Tool ecosystem](./21-tool-ecosystem.md), [MCP integration](./09-mcp-integration.md), [MCP credentials](./09b-mcp-oauth-credentials.md) |
| Models and streaming | [Supported models](./03a-supported-models.md), [Canonical protocol](./09a-canonical-llm-protocol.md), [Streaming](./13-streaming-responses.md) |
| Memory and artifacts | [Memory](./20-memory-management.md), [Artifacts](./20a-artifacts-and-multimodal-context.md) |
| Workers and routing | [Worker system](./08-worker-system-routing.md), [Targeting](./08a-worker-targeting-guide.md) |
| Extending without forking | [First bundle](./15a-your-first-bundle.md), [Scoping](./15b-bundle-scoping-and-visibility.md), [SDK](./38-sdk-reference.md) |
| Security and tenancy | [Security & multi-tenancy](./22-security-multi-tenancy.md) |
| Something is broken | [Troubleshooting](./30-troubleshooting-guide.md), [Observability](./23-observability-debugging.md) |

## Getting help

We are always looking for feedback and folks who want to pilot Motet — email `hello@motet.dev`. Collaborating on Motet itself is invite-only (same address). We are not accepting unsolicited pull requests. To extend the runtime, write a [bundle](./15a-your-first-bundle.md). Style and test conventions for people working in this tree are in the [Contributing Guide](./32-contributing-guide.md).

---

**Last Updated**: 2026-08-25

**Status**: Pre-1.0
