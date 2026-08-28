# Why Motet?

Motet is a **runtime operating system for AI agents** that you operate: a scheduler, I/O, memory, identity, and a place to run bundles — with an agent loop on top.

## The honest comparison

If you are building a demo this afternoon, a library wins. `pip install` and twenty lines of Python will produce a tool-using agent faster than Motet's first `local up`, which pulls the published snapshot images (API, workers, embedding server).

What changes is the second week. A library hands you the loop and leaves the rest open: where conversation memory lives, which embedding model runs and where, how spend is measured, who the caller is, where API keys are kept, what happens to work that outlives the request. Each is a fork in the road. None is individually hard. The cost is that you cannot settle one without touching the others, so they arrive as a single knot somewhere between the demo and the first real user.

Motet has already untied it.

## What you do not have to decide

From a clean clone, two decisions stand between you and an agent that calls a tool and remembers the answer: supply a model credential, and log in. Everything below was chosen for you.

| Decision a library leaves open | Motet's answer |
|---|---|
| Where conversation memory lives | Valkey, with vector search on in the local stack |
| Which embedding model, running where | `all-MiniLM-L12-v2`, baked into a sibling service image at build time |
| How responses stream | SSE and WebSocket, both wired, with a canonical event shape |
| How tools are defined and called | `@motet.tool`, MCP, and 47 built-ins already registered |
| How a model reaches hundreds of them | It searches the catalog and calls what it finds, rather than carrying every schema in every request |
| How spend is measured | Automatically, with per-model pricing shipped for registered cloud chat and reasoning models |
| Who the caller is | A Keycloak realm, JWT, and principal and tenant context on every hop |
| Where credentials are kept | An encrypted vault |
| What runs work that outlives a request | Celery workers behind one command API |
| Where it runs in production | Install scripts for EC2 and Fargate |
| How locked in you are to one provider | Seven cloud vendors (OpenAI, Anthropic, Gemini, xAI, Meta, DeepSeek, Moonshot) plus local llama.cpp and mock — [Supported models](./03a-supported-models.md) |

None of this is exotic. It is the list most teams rebuild, in roughly that order, and the reason the second week costs more than the first.

Cost accounting is the one worth singling out, because it is the least expected: metering works with no configuration at all, since per-model pricing ships with the runtime. It is the kind of thing nobody chooses up front and everybody wants by the time the first invoice arrives.

Tool disclosure is the other. Sending every tool's schema on every request is fine at ten tools and untenable at two hundred, and it leaves anything registered at runtime — an MCP server, a tenant's bundle — permanently out of reach. Motet's default is the other way round: the agent is given the means to search and invoke, and finds the rest when it needs them.

## Where the defaults stop

The list above is only useful if you can check it, so here is where it ends:

- These are **stack** defaults. Import the library on its own and memory is in-process with no vector search, tracing is off, and the OpenAI-compatible facade is off.
- **MCP servers are configuration, not magic.** Adding one means editing `mcp_instance_manager.yaml` and supplying credentials or images.
- **Tenancy is scoping, not a hardened boundary.** Tenant IDs are threaded through and keys are namespaced, but several enforcement filters are off by default and bundle visibility is not fully isolated across tenants. Treat it as defense in depth on a shared fleet rather than an isolation guarantee.
- Roughly twenty of the 47 built-in tools work with no further setup. The rest need edge capabilities, a provider key, or MCP.

## What leaves the box

Motet does not phone home. There is no analytics, crash reporting, license check, or update ping in the runtime — a claim worth re-running yourself rather than taking on faith.

More usefully, the data that *accumulates* stays put. Embeddings are computed by a local `all-MiniLM-L12-v2`, in-process or in a sibling container. There is no cloud embedding backend in the codebase at all, so indexing a document or writing to memory never ships text to a third party. The vectors land in your Valkey, artifacts in your object store, traces on your disk.

What does go out is mostly what you would expect, plus one you might not:

| Leaves the stack | When |
|---|---|
| Model inference | Every agent turn, to whichever provider you configured |
| Conversation analysis | Never by default — only if you turn extra analysis on |
| Video transcription | Artifact processing — the compose stack sets `MOTET_VIDEO_TRANSCRIPTION_BACKEND=openai_api` |
| Tools that reach out | `core.web_search`, `core.http_get` and `core.http_post`, the OAuth tools, image generation |
| MCP servers | Whatever the server itself talks to — Google Workspace, weather, a browser |

The transcription row is the one to read twice: it is the only default that reaches a provider you did not choose.

This describes where data comes to rest; it is not a compliance posture, and it is not the tenant boundary described above. If you need a provider to retain nothing, that is your agreement with them — Motet sends `store=false` on OpenAI Responses calls, and there is no equivalent in the other adapters.

## What it is built for

Agent kits that call an LLM and tools in one process are enough for many apps. Motet is for when you also want:

- **MCP servers** that are heavy (browsers) or stateful, shared across workers instead of copied into each one
- **Hot-deployed extensions** (bundles) without rebuilding the runtime
- **Principal and tenant context** on every hop, plus an ops UI and task traces
- **Worker specialization** (GPU for models, CPU for tools) behind one command API
- **An OpenAI-shaped door** so existing clients can talk to the same stack

The stack is Celery commands, Redis/Valkey, Postgres, a canonical LLM protocol, and an MCP manager process.

## What you get

**Distributed commands.** `@motet.command` functions run on workers with typed input and `MotetContext`. Compose with `motet.do`, `motet.join`, `motet.apply`, `motet.maybe`.

**MCP with thin workers.** Servers run in a parent manager. Workers use a Redis stream proxy.

**Provider-agnostic models.** Canonical request/stream types; adapters own vendor wire formats.

**Bundles.** Ship commands, tools, and workflows; lint and reload without forking core.

**Observability.** Task streams, structured events, Chat Explorer, ops UI.

**Tenant-aware data.** Keys and APIs carry `tenant_id` and principal. Isolation is scoped data and API checks on a shared worker fleet.

## What it costs

- A **compose stack** (API, workers, Redis/Valkey, Postgres, identity, MCP manager)
- **Latency per command hop** (milliseconds to tens of milliseconds, plus queue time)
- Operational details: worker warmup, MCP health, cancel across a task tree
- A learning curve: commands, capabilities, MCP name formats

A smaller toolkit is enough if you only need an in-process agent loop.

## When Motet fits

- You want an agent platform you **operate and control**, with a dedicated stack per environment
- You want **MCP, bundles, and traces** in one system
- Several people will write extensions against the SDK

A provider SDK or a lighter agent library is a better starting point for a single-app chatbot, hard real-time paths, or a setup that cannot run Redis and workers.

## Versus common alternatives

Tools and memory are not the dividing line — every agent library has both. The difference is what they are underneath.

| If you want… | Typical pick | What you get here instead |
|---|---|---|
| A graph you draw and run in-process | LangGraph | Steps run on a worker fleet behind one command API, so work can outlive the request that started it |
| To sketch multi-agent roles in a single file | CrewAI / similar | Agents are registry entries — callable over the API, schedulable, and shipped in a bundle you can hot-deploy |
| Durable workflows as the core abstraction | Temporal + an LLM wrapper | The unit is the **command**, with timeouts, capability routing, and traces attached; workflows compose commands on top |
| Tools and memory that behave the same from every process | Wiring a library's stores and tool registry into a queue yourself | Both are services: memory writes carry the verified caller, and heavy MCP servers are hosted once and shared |
| Hosted agents with no ops | A cloud agent product | A runtime you control rather than rent — you run the stack today, and the data and keys stay yours |

## Next steps

- **[What Motet Can Do](./03-what-motet-can-do.md)** — capabilities
- **[Supported Models](./03a-supported-models.md)** — providers and flagship ids
- **[Quick Start](./04-quick-start-guide.md)** — boot the stack
- **[Core Concepts](./05-core-concepts-overview.md)** — vocabulary

## Navigation

- **[← Back to Documentation Home](./00-landing-page.md)**

---

**Last Updated**: 2026-08-25
