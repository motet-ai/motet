# Tools / discovery

Tools come from three places and look identical to the model once registered:

| Source | Canonical name | Example |
|---|---|---|
| Built-in | `core.<name>` | `core.web_search`, `core.http_get` |
| MCP | `mcp.<server_id>.<name>` | `mcp.playwright.browser_navigate` |
| Bundle | `{bundle_id}.<name>` | `my_bundle.lookup_customer` |

Workflows also surface as `workflow_<id>` — a multi-step pipeline as one function. Motet admin tools use `motet_admin.<name>`.

Inside Motet (registry, `command_data`, `motet.tools.execute`) always use **canonical** names. Wire form (`mcp__server_id__tool_name`) exists only at the LLM provider boundary. See [llm-protocol.md](./llm-protocol.md).

## How the loop sees tools

The agentic loop does **not** send the full catalog each iteration. The model gets a frozen sticky prefix:

- Always: `core.help`, `core.tools_search`, `core.tool_call`
- Plus keyword pins from the request

Catalog reachability is `tools_search` → `tool_call`. `tools_search` returns matches **with full JSON schemas** (the catalog may rank by embedding; that is on-demand, not a per-turn shortlist). `tool_call` invokes any tool or workflow by canonical name whether or not it was in the request schemas. See [embeddings-rag.md](./embeddings-rag.md).

`core.agent_turn` stays out of function discovery. Agent-to-agent from the model is `core.handoff`. Parallel work is `core.spawn_agents`.

## Built-ins worth knowing

- HTTP: `core.http_get`, `core.http_post`, `core.http_get_browser`, `core.web_search`. Usable successes may attach `cache_control=same-turn`; a fresh same-signature hit replays a short notice instead of refetching. Default for other tools is `no-store`.
- Files: `core.file_read`, `core.file_write`, `core.file_search` run on a **device worker**, not a datacenter worker.
- Memory: `core.memory_store`, `core.memory_recall`, … — see [memory-artifacts.md](./memory-artifacts.md).
- Skills: `core.activate_skill` — see [skills.md](./skills.md).

Large or binary tool output is stored as an artifact; the conversation holds a `ToolInvocation` reference, not the bytes.

## Writing a tool

Bundle tools: `@motet.tool(description=..., name=...)` in `tools/*.py`. Registration as `{bundle_id}.{name}` is automatic when the bundle loads. Import from `motet_sdk`.

## Paths

- Registry / execution: `motet/core/tools/`
- Loop discovery: `motet/core/reasoning/react/loop_discovery.py`, `tool_shortlist.py`
- Meta tools: `core.tools_search`, `core.tool_call`
- Onboarding: `docs/developer_onboarding/21-tool-ecosystem.md`
