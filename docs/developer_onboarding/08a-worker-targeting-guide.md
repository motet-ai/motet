# Worker Targeting Guide

## Overview

**Worker targeting** controls which workers execute your commands. It matters for:

- **Command authors** – Declare what capabilities a command needs so the router picks a suitable worker.
- **Tool developers** – Commands that run tools declare capabilities (e.g. `TOOL_EXECUTION`, `MCP_INTEGRATION`); the router sends them to workers that can run those tools.
- **Bundle developers** – Control which workers *load* your bundle (worker IDs/tags) and how commands in the bundle are routed (via `required_capabilities` on each command).

**Datacenter vs device workers:** Most work runs on **datacenter** workers in your deployment. **Device** workers run on a registered machine (laptop, workstation) for host-local tools and paths. See [Worker System & Routing — Datacenter workers and device workers](./08-worker-system-routing.md#datacenter-workers-and-device-workers) and [Local development setup — edge worker](./14-local-development-setup.md#option-3-edge-worker-for-a-remote-motet-deployment).

This guide describes how to use worker targeting when defining commands and bundles. For **scheduled** commands (e.g. `target_worker_id`, `avoid_worker_ids` when creating a schedule), see [Scheduled Commands – Worker Targeting](12-scheduled-commands.md#worker-targeting).

**Note:** The execute API (`POST /api/v1/commands/{command_type}/execute`) and `motet-cli command run` do **not** currently accept targeting parameters (e.g. `target_worker_id`, `required_capabilities`) in the request. Targeting is applied when you **define** commands (capabilities) and when you **schedule** them (worker ID, avoid list, etc.).

---

## Available Worker Capabilities

Workers advertise capabilities; commands declare what they need. The router only sends a command to workers that have all required capabilities.

| Capability | Typical use |
|------------|-------------|
| `MODEL_INFERENCE` | LLM / model calls |
| `MODEL_STREAMING` | Streaming model responses |
| `LOCAL_INFERENCE` | On-device / local LLM inference (advertised only when a usable local model is reachable) |
| `TOOL_EXECUTION` | Running tools |
| `MEMORY_OPERATIONS`, `MEMORY_STORAGE`, `MEMORY_RETRIEVAL` | Memory and storage |
| `VECTOR_OPERATIONS` | Vector search |
| `REASONING` | Reasoning strategies |
| `WEB_SEARCH`, `HTTP_REQUESTS`, `HTTP_OPERATIONS` | Web/HTTP |
| `BROWSER_OPERATIONS` | Browser automation |
| `FILE_OPERATIONS` | File system access |
| `EMBEDDINGS` | Embedding models |
| `MCP_INTEGRATION` | MCP tool integration |
| `TASK_SCHEDULING` | Scheduling |
| `DEPLOYMENT` | Bundle deploy/orchestration (deployer worker only) |
| `WORKER_LIFECYCLE_MANAGEMENT` | Worker lifecycle (lifecycle worker only) |

Values match the `WorkerCapability` enum in `motet.core.commands.distributed`. Use the enum names in code (e.g. `WorkerCapability.TOOL_EXECUTION`).

---

## Command Authors: Declaring Required Capabilities

When you define a command, set **required_capabilities** so the router only sends it to workers that can run it.

### Decorator-based commands

```python
from motet_sdk import motet
from motet.core.commands.distributed import WorkerCapability

@motet.command(
    required_capabilities=[WorkerCapability.MODEL_INFERENCE],
    timeout_seconds=60,
)
def my_llm_command(data: MyData, motet: MotetContext) -> dict:
    ...
```

Examples from the codebase:

- **Reasoning** – `required_capabilities=[WorkerCapability.REASONING]`
- **Tool execution** – `required_capabilities=[WorkerCapability.TOOL_EXECUTION]`
- **Memory** – `required_capabilities=[WorkerCapability.MEMORY_OPERATIONS]`
- **Embeddings** – `required_capabilities=[WorkerCapability.EMBEDDINGS]`
- **Deployment** – `required_capabilities=[WorkerCapability.DEPLOYMENT]` (deployer worker only)

### How it works

1. When the command is invoked, its `required_capabilities` are set on the command's distributed context.
2. The worker router filters to workers that advertise **all** of those capabilities.
3. Among those workers, the configured routing strategy (e.g. least loaded, round robin) picks one.

So **you** decide *which pool* of workers can run the command; the system decides *which one* in that pool.

---

## Tool Developers

Commands that **invoke tools** (including MCP) should declare the right capabilities so they run on workers that have those tools:

- Generic tool execution: `WorkerCapability.TOOL_EXECUTION`
- MCP tools: `WorkerCapability.MCP_INTEGRATION`
- Web/HTTP tools: `WorkerCapability.HTTP_OPERATIONS` or `HTTP_REQUESTS`
- Browser tools: `WorkerCapability.BROWSER_OPERATIONS`

If your command only calls tools, set `required_capabilities` accordingly; the router will then send it only to workers that can execute those tools.

---

## Bundle Developers

Bundles interact with worker targeting in two ways.

### 1. Which workers load the bundle (deploy time)

When you **deploy** a bundle, you can restrict which workers load it using **BundleTargeting**:

- **worker_ids** – Only these worker IDs load the bundle (e.g. `["agent-worker-1"]`).
- **worker_tags** – Only workers that have *all* of these capability tags load the bundle (e.g. `["gpu"]` for GPU-only bundles).
- **motet_ids** / **tenant_ids** – Limit which request context (motet/tenant) can see and use the bundle's commands.

So bundle developers can target "only GPU workers" or "only these three workers" without changing worker code.

### 2. How bundle commands are routed (run time)

Commands inside a bundle are normal `@motet.command` functions (or the `@motet.command` alias). Each one should set **required_capabilities** so that when someone invokes it, the router sends it to a worker that:

1. Has loaded that bundle (per targeting above), and  
2. Has the capabilities the command needs.

Example in a bundle command:

```python
# In your bundle's commands/my_command.py
@motet.command(
    required_capabilities=[WorkerCapability.TOOL_EXECUTION, WorkerCapability.MCP_INTEGRATION],
    timeout_seconds=30,
)
def my_bundle_command(data: MyData, motet: MotetContext) -> dict:
    ...
```

Combining **BundleTargeting** (who loads the bundle) with **required_capabilities** (what capabilities the command needs) gives you precise control over where bundle commands run.

---

## Scheduling and Caller-Controlled Targeting

When a user or system **schedules** a command (rather than running it immediately), they can pass targeting options on the schedule:

- **target_worker_id** – Run on this worker.
- **preferred_worker_ids** – Prefer these workers, in order.
- **avoid_worker_ids** – Do not use these workers.
- **worker_affinity** – Affinity key for stable worker selection.

That flow is documented in [Scheduled Commands – Worker Targeting](12-scheduled-commands.md#worker-targeting). The execute API and `motet-cli command run` do **not** currently accept these parameters; they only apply when creating or updating schedules.

---

## Routing Strategies (Internal)

Once the router has a set of workers that satisfy capabilities (and any schedule-level targeting), it uses a **routing strategy** to choose one. Strategies include:

- **least_loaded** – Prefer worker with lowest load (default).
- **round_robin** – Distribute evenly.
- **random** – Random choice.
- **sticky** – Same worker for a given context (e.g. conversation).

Strategy selection is typically configured at the worker/invoker level, not per request in the public API. For more detail, see [Worker System & Routing](08-worker-system-routing.md).

---

## Summary

| Audience | What you do |
|----------|-------------|
| **Command authors** | Set `required_capabilities` on `@motet.command` (or alias `@motet.command`) so the router only sends the command to workers that can run it. |
| **Tool developers** | Same: use `required_capabilities` (e.g. `TOOL_EXECUTION`, `MCP_INTEGRATION`) on commands that invoke tools. |
| **Bundle developers** | Use **BundleTargeting** (worker_ids, worker_tags) to control which workers load the bundle; use **required_capabilities** on each bundle command for run-time routing. |
| **Scheduling** | Use **target_worker_id**, **avoid_worker_ids**, etc. when creating schedules – see [Scheduled Commands – Worker Targeting](12-scheduled-commands.md#worker-targeting). |

---

## Related Documentation

- [Scheduled Commands – Worker Targeting](12-scheduled-commands.md#worker-targeting) – Schedule-level targeting options.
- [Worker System & Routing](08-worker-system-routing.md) – Worker architecture and routing overview.
- [Building Your First Command](15-building-your-first-command.md) – Command development tutorial.
- [Your First Bundle](15a-your-first-bundle.md) – Bundle development tutorial.
