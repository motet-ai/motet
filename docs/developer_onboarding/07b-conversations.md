# Conversations

**Conversations** are chat sessions for a principal in a tenant. The system keeps a registry of conversations (id, title, created_at, updated_at, optional agent and surface) so UIs can list, switch, rename, and clear sessions. This section explains what conversations are, how they relate to memory and the chat API, and how to work with them from commands using the **conversations helper** on MotetContext.

## Overview

- **What they are**: A registry of conversation metadata per (motet, tenant, principal). Each entry has an id, display title, timestamps, and optional agent/surface scope. Conversation **history** (messages) and **memory/vector** are stored separately and can be cleared per conversation.
- **Where they live**: Backed by the conversation registry (Redis); list/get/clear/register/rename are exposed as distributed commands and as the **Conversations API** (HTTP) for clients.
- **Relationship to chat**: Starting a chat with a new conversation ID implicitly creates that conversation (register/touch). The Chat API sends messages and streams responses; the Conversations API manages the session list and lifecycle.

## Conversation operations

| Operation | Description |
|-----------|-------------|
| **List** | List conversations for the current principal in the tenant, optionally filtered by agent or surface. Returns id, title, created_at, updated_at, agent_id?, surface_id? sorted by updated_at descending. |
| **Get** | Get one conversation: replay history from conversation-scoped memory plus memory/vector counts. History items can include artifact references for multimodal display. |
| **Register (touch)** | Ensure a conversation exists in the registry; create or update updated_at. Optional title, agent_id, surface_id (set on create). |
| **Rename** | Update a conversation’s display title. |
| **Clear** | Remove the conversation from the registry and clear conversation-scoped memory and vector data. |

New conversations are created implicitly when a client starts a chat with a new conversation ID (the chat path registers or touches the conversation). There is no separate “create conversation” endpoint; use register when you need to ensure a session exists from code.

## Using the conversations helper (from commands)

From inside another command, the preferred way to list, get, clear, register, or rename conversations is the **conversations helper** on MotetContext. It delegates to the conversation commands when task context exists.

- **`motet.conversations.list(limit=100, agent_id=None, surface_id=None)`** – List conversations for the current principal. When you have task context, delegates to the list command; otherwise uses an inner path with motet’s tenant/principal.
- **`motet.conversations.get(conversation_id)`** – Get one conversation (history + counts). Requires task context; delegates to the get command.
- **`motet.conversations.clear(conversation_id)`** – Clear the conversation (registry + memory/vector). Requires task context; delegates to the clear command.
- **`motet.conversations.register(conversation_id, title=None, agent_id=None, surface_id=None)`** – Register or touch a conversation. Requires task context; delegates to the register command.
- **`motet.conversations.rename(conversation_id, title)`** – Rename a conversation. Requires task context; delegates to the rename command.

Example from a command:

```python
@motet.command()
def my_command(data: MyData, motet: MotetContext) -> Dict[str, Any]:
    # List recent conversations (e.g. for a sidebar or export)
    convs = motet.conversations.list(limit=20, agent_id="core.default")

    # Get one conversation’s history and counts
    detail = motet.conversations.get(data.conversation_id)

    # Register a new session when starting a flow
    motet.conversations.register(
        data.conversation_id,
        title="Support session",
        agent_id="core.default",
        surface_id="demo_chat",
    )

    # Rename after user edits title
    motet.conversations.rename(data.conversation_id, "Updated title")

    # Clear when user deletes the conversation
    motet.conversations.clear(data.conversation_id)

    return {"conversations": convs}
```

When you do **not** have task context (e.g. outside a command), use `motet.do(conversations_list, data=ListConversationsData(...))` and the other conversation commands directly. For when to use helpers vs composition, see [Distributed Command System – Resource helpers vs command composition](./07-distributed-command-system.md#resource-helpers-vs-command-composition) and the [SDK Reference](./38-sdk-reference.md).

## When to use what

| Use case | Use |
|----------|-----|
| **From inside a command** (list/get/clear/register/rename) | **`motet.conversations`** helper. Easiest and delegates to the right command. |
| **From an HTTP client** (UI, integration) | **Conversations API** (`GET/PATCH/POST/DELETE /api/v1/conversations`). See [API Reference](./28-api-reference.md#conversations-api). |
| **From CLI** | **Motet CLI** `conversations` command group (list, get, clear, rename, delete). See [Motet CLI Reference](./37-motet-cli-reference.md). |
| **Outside a command** (no MotetContext task_id) | **`motet.do(conversations_list, data=...)`** (and the other conversation commands) with explicit context. |

## Scoping (agent and surface)

Conversations can be scoped by **agent** and **surface** (e.g. which agent and which channel or app the session belongs to). List accepts optional `agent_id` and `surface_id` filters. Register accepts optional `agent_id` and `surface_id`; when provided, they are stored on the conversation and used for filtered listing. This supports multiple agents and surfaces per principal without mixing session lists.

Known surfaces are listed via **`GET /api/v1/surfaces`** (seeded with `demo_chat`, `openai_compat`, `ops_dashboard`, `cli`). Create/update/delete via REST (`POST`/`PATCH`/`DELETE /api/v1/surfaces/...`), CLI (`motet-cli surfaces …`), or manage UI. Bundles may declare additional surfaces in `config/surfaces.yaml`; deploy registers missing ids and no-ops when an id already exists. Agents may restrict which surfaces they can use via `allowed_surface_ids` (null/empty = all catalog surfaces); manage UI can set a Redis overlay with `PUT /api/v1/agents/{qualified_id}/surfaces`.

Common surface ids include `demo_chat` (Chat Explorer default channel), `openai_compat` (OpenAI-compatible `/v1` facade), `ops_dashboard`, and `cli`. Chat Explorer can browse conversations across these surfaces.

## Related documentation

- **[Distributed Command System](./07-distributed-command-system.md)** – Command lifecycle, MotetContext, resource helpers vs composition
- **[API Reference](./28-api-reference.md)** – Conversations API (HTTP), MotetContext helpers
- **[OpenAI-Compatible API](./28-api-reference.md#openai-compatible-api)** – `/v1` facade stamps `surface_id` from the agent's surface allow-list when unambiguous (e.g. cursor bundle → `cursor_ide`), otherwise `openai_compat`
- **[Chat Explorer](./36-chat-explorer.md)** – How the reference UI uses the Conversations API
- **[Memory Management](./20-memory-management.md)** – Conversation-scoped memory and tags

## Navigation

- **[← Agent Loop](./07a-agent-loop.md)** – Agent loop, YAML config, and the agents helper
- **[Distributed Command System](./07-distributed-command-system.md)** – Command system fundamentals
- **[Worker System & Routing →](./08-worker-system-routing.md)** – Worker coordination
- **[Documentation Home](./00-landing-page.md)** – Main documentation hub

---

**Last Updated**: 2026-08-04
