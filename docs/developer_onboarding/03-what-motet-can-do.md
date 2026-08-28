# What Motet Can Do for You

This page is the inventory: what the runtime does today, how far each piece goes, and where it stops. Claims here are things you can check in a running stack.

## Commands

A command is a Python function with typed input that runs on a worker. That is the whole unit of work — model inference, a tool call, a memory write, and your own code are the same kind of thing, which is why they compose without glue.

The runtime routes each one by worker capability, so you never address a worker directly. Task, conversation, principal, and tenant context travel along with it, so a command five hops deep still knows who asked. You compose them with `motet.do` for one, `motet.join` for several at once, `motet.apply` for the same command over many inputs, and `motet.maybe` when failure is an expected outcome rather than an error.

What this buys you is that scaling is an operational decision rather than a rewrite: add workers and throughput rises, because nothing in a command body knows how many exist. Worker failures stay contained to the command that was running, and retries and timeouts are per-call parameters rather than something you build.

The cost is a hop. Every command crosses a queue, which is milliseconds to tens of milliseconds plus wait time. For a tight loop of cheap operations, that is the wrong trade — call a function.

## Agents, sub-agents, and reasoning

The default path is the agentic loop: the model gets tools and decides which to call, repeatedly, until it has an answer. That covers most of what people want an agent for.

There is no strategy menu behind that, and nothing guesses at one before the model has read the request. When work splits into parts that do not depend on each other, the loop says so by calling a tool:

```python
core.spawn_agents(tasks=[
    {"instruction": "Find current pricing for the Acme enterprise tier",
     "tools": ["core.http_get_browser"]},
    {"instruction": "Summarize published Acme outage postmortems from 2026",
     "tools": ["core.web_search"]},
])
```

Each task runs as a sub-agent on its own worker, and all the answers come back
together as one result the loop reads like any other. Naming the tools each task
needs matters: a sub-agent works from a small budget, and one left to discover
its own tools tends to spend that budget searching instead of answering. You can do the same thing
from your own code with `motet.join`, which is what the tool uses underneath:

```python
reviews, pricing = motet.join([
    (scrape_reviews, ProductData(product_id=pid)),
    (scrape_pricing, ProductData(product_id=pid)),
])
```

A sub-agent is simply an agent turn invoked by something other than a user: another agent, a workflow step, or a loop fanning out.

By default a sub-agent shares the parent's conversation, so its output lands in the same transcript tagged with its `agent_id` and its spend rolls up to the parent. A workflow step can opt out with `isolate_conversation`, which gives it a child conversation and separately attributable cost. Several agents is therefore the ordinary case rather than a mode you switch on: a registry holds many configurations, one agent can hand work to another, and a workflow can fan several out and combine the results.

See [The agent loop](./07a-agent-loop.md) for the mechanics and [Advanced Concepts](./24-advanced-concepts.md) for the patterns, including a facilitator that picks who speaks next at runtime.

## Models

The loop is vendor-neutral. Seven hosted providers plus llama.cpp on your workers share one request and stream protocol. You pick a model with a provider and a registry key; switching is a credential and a setting, not a rewrite.

What first-class support includes: streaming and tools on chat models, thinking replay where the vendor has a thinking protocol, vision where the spec declares it, native web search on the providers that expose it, and shipped pricing so metering works with no extra tables. OpenAI, Anthropic, Gemini, xAI, Meta, DeepSeek, and Moonshot are registered, along with local GGUF families (Gemma, Hermes, Llama 3, Ministral, Phi, Qwen).

The flagship ids and the live catalog are on [Supported models](./03a-supported-models.md).

Where it stops: a registered id is not a guarantee your vendor account can call it, and local support is the listed GGUF families, not every file on Hugging Face.

## Tools

Tools come from three places and look identical to the model once registered: 47 built-ins under `core.*`, MCP servers under `mcp.server.tool`, and anything you ship in a bundle under `{bundle_id}.*`. Workflows also surface to the model as `workflow_*`, so the LLM can call a multi-step pipeline as if it were a single function.

MCP servers run in a manager process rather than inside each worker, which is why worker memory does not grow with the number of servers you attach. Heavy or stateful servers — a browser, say — exist once and are shared.

Where it stops: roughly twenty of the 47 built-ins work with no further setup. The rest need edge capabilities, a provider key, or an MCP server. Adding an MCP server is configuration — an entry in `mcp_instance_manager.yaml` plus whatever credentials or images it needs — not something the runtime discovers for you.

## Skills

A skill is a folder holding a `SKILL.md` and, optionally, scripts. The model sees only a compact catalog of names and descriptions until it calls `core.activate_skill`, which loads the full instructions. A stack can therefore carry dozens of skills at a cost of a couple of lines of context each and pay for the long-form content only when one turns out to be relevant.

Skills load from bundles under `skills/<name>/SKILL.md` and from `.agents/skills` directories on disk, which is on by default. The format is the public Agent Skills one, and that compatibility is not theoretical: the published vendor skill set — `pdf`, `xlsx`, `pptx`, `docx`, `mcp-builder` and a dozen more — is used unmodified as a test fixture. `motet-cli skills list` shows what is loaded, and `motet-cli bundle lint` checks a `SKILL.md` against the public constraints before you ship it. For a deployable SDK example (Apache reference skills plus a Motet runner), see [`skills-demo`](./26-example-bundles.md#skills-demo--agent-skills).

Script-backed skills go further. After activation the runtime force-includes `core.workspace_shell_exec`, which materializes the skill's files into a workspace container under `/scratch/skills/<skill>/`, installs any Python requirements the bundle declares, bridges artifacts in and out, and returns the exit status alongside whatever files the script produced.

Where it stops: that container path needs Docker, so script-backed skills do not run on a bare worker, though text-only skills do. Provider-native skill hosting is not wired up either — a skill runs through Motet's own activation path rather than being handed to a vendor that hosts skills itself.

## Memory and artifacts

Memory has three tiers. Working memory is the current turn, short-term is the conversation, and long-term is what survives it. Retrieval is hybrid: keyword, semantic, and recency together, rather than vector search alone. Promotion from short-term to long-term is opt-in, so nothing is quietly retained because a conversation ran long.

Artifacts cover uploads and tool output, including images and other non-text context. Artifact RAG chunks and indexes them and pulls citation-ready passages into a turn, scoped to a conversation or a principal. It is off by default (`MOTET_ARTIFACT_RAG_ENABLED`) because indexing every upload is the wrong default for most stacks.

This is what makes "ask questions about the document I just uploaded" a configuration rather than a project.

## Workflows

A workflow declares steps, what each depends on, and when to skip. The runtime derives execution order from the dependencies, runs independent steps in parallel, and passes context between them. Workflows can nest, so a step can be another workflow.

```python
workflow = Workflow(
    workflow_id="lead_qualification",
    steps={
        "analyze_email": WorkflowStep(...),
        "check_crm": WorkflowStep(
            command_type="core.tool_execution",
            command_data={"tool_name": "my_crm.crm_query", ...},
            dependencies=["analyze_email"]
        ),
        "score_lead": WorkflowStep(
            command_type="core.agent_loop",
            command_data={"input": "Score this lead", ...},
            dependencies=["check_crm"]
        ),
        "update_crm": WorkflowStep(
            command_type="core.tool_execution",
            command_data={"tool_name": "my_crm.crm_update", ...},
            dependencies=["score_lead"],
            skip_condition="if_equals:score_lead.qualified:False"
        )
    }
)
```

One sharp edge worth knowing before you design around it: `skip_condition` takes an `operator:path:literal` string and has no numeric comparison. You cannot write "skip when score is under 70." Have the preceding step emit a boolean — `qualified` above — and branch on that.

Use a workflow when the shape of the work is known in advance. When the next step depends on what the model just learned, use the agentic loop instead; a workflow that needs the answer to decide its own steps is the wrong tool.

## Cost accounting and spend budgets

Token usage is converted to estimated USD, aggregated per tenant, and capped by budgets enforced before the model call rather than after.

Usage is captured on streaming and non-streaming calls alike — prompt, output, cache, and reasoning tokens, normalized across providers. Pricing ships with each registered cloud chat and reasoning model, including cache-read discounts, so metering works with no configuration. Budgets are daily and monthly per tenant, and an exceeded limit blocks the request before it reaches the provider. Spend rolls up by tenant, principal, and conversation, and each turn that made a priced call carries a `cost_usd` you can export from an `after_finalize` hook. Operators get `/api/v1/cost/*`, the Cost tab in Manage, and `motet-cli cost`. The model ids those prices attach to are on [Supported models](./03a-supported-models.md).

These are estimates for control and attribution, not billing records. Budget checks fail open when the store is unreachable, and daily aggregates are kept for seven days — treat it as a live control plane, not a ledger. Infrastructure cost is not tracked.

A turn that made no priced model call has **no** `cost_usd` field rather than `0.0`. Absent means unknown, not free, and the difference matters when you aggregate. Locally hosted models are deliberately unpriced and behave the same way. See [Cost and usage API](./28-api-reference.md#cost-and-usage-api).

## Streaming

Turns stream over SSE and WebSocket. Events are plain dicts keyed by `event`, and most clients handle six of them:

```python
async for event in orchestrator.stream_events(stack, messages):
    kind = event["event"]
    if kind == "token":
        ui.append_text(event["data"])          # incremental model output
    elif kind == "tool_execution_started":
        ui.show_tool_running(event["data"])    # surface tool activity
    elif kind == "end":
        ui.finish(event.get("content", ""))    # final answer
```

`token`, `turn`, `thinking`, `tool_execution_started`, `end`, and `error` are the common set; [Streaming Responses](./13-streaming-responses.md) has the rest. Because tool activity is streamed as its own event, a UI can show what the agent is doing rather than an undifferentiated spinner.

## Multi-tenancy

Agents, memory, and artifacts carry principal and tenant context, so one stack can serve several orgs or environments. The tenant comes from the verified JWT, not from anything a caller can set, and Redis keys are namespaced by it.

Be precise about what that is: scoping and defense in depth on a shared fleet, not a hardened isolation boundary. Several enforcement filters are off by default and bundle visibility is not fully isolated across tenants. See [Security & Multi-Tenancy](./22-security-multi-tenancy.md) before you rely on it for untrusted tenants.

## What people build with it

The runtime is aimed at agents that have to survive contact with production — support and sales agents that call real systems, research agents that gather and synthesize, code review agents, and document pipelines that chunk, index, and answer over uploads.

The common thread is that these need more than a loop. They need to know who is asking, remember across sessions, spend within a limit, stream to a UI, and keep running when one worker dies. That is the problem Motet is shaped around. For a single-file chatbot with no operational surface, a smaller toolkit is a better fit — see [Why Motet](./02-why-motet.md) for the honest comparison.

## Where to start

- **[Quick Start](./04-quick-start-guide.md)** — boot the stack and talk to an agent
- **[Supported Models](./03a-supported-models.md)** — providers and flagship ids
- **[Building Your First Command](./15-building-your-first-command.md)** — the end-to-end tutorial
- **[Building Workflows](./17-building-workflows.md)** — when the shape is known in advance
- **[MCP Integration](./09-mcp-integration.md)** — attaching existing tool servers
- **[Reasoning](./10-reasoning.md)** — the agent loop and parallel fan-out

## Navigation

- **[← Back to Documentation Home](./00-landing-page.md)**

---

**Last Updated**: 2026-08-27
