# Chat Explorer & Shared UI Library

**Chat Explorer** is the reference chat UI for Motet — a multi-surface conversation
browser and debugging tool. Use it to try the platform, inspect conversations from
other channels, and see how a client uses the REST API, streaming, conversations,
artifacts, and auth.

> **App vs surface:** Chat Explorer is the **application**. The default channel it
> stamps on new chats is `surface_id=demo_chat` (one surface among others such as
> `openai_compat`, `ops_dashboard`, `cli`). Switching the surface in the header lists
> and scopes conversations by `(agent_id, surface_id)`.

Both Chat Explorer and the ops dashboard share a common UI library —
**`@motet/ui-common`** — that provides the hooks, components, types, API clients,
and chat protocol logic that any Motet frontend application needs.

## What It Is

Chat Explorer is a React application (Ant Design + Ant Design X) that provides:

- **Chat**: Send messages and get streamed AI responses (Agent command, reasoning strategies, tools).
- **Conversations**: List, switch, rename, and delete conversations (backed by the Conversations API), scoped by agent and surface.
- **Agent and surface pickers**: In the header. Surface options come from `GET /api/v1/surfaces`, filtered by the selected agent’s `allowed_surface_ids` (null/empty = all catalog surfaces).
- **Model picker**: One searchable select below the composer. Each option is `provider : model`; Auto leaves routing to the backend. A key icon marks providers with an API key configured (environment or vault). Models that need a key and do not have one stay in the list but cannot be selected. Enable thinking sits to the right; when it is on, reasoning effort appears to the right of the switch.
- **Attachments**: Upload files (images, PDFs, documents) as artifacts; attach them to messages and preview in the thread.
- **Retrieval**: Search-icon popover on the composer — This chat, My files, or Workspace. Advanced file IDs and tags stay collapsed. A chip next to the icon shows the current scope; clearing it returns to This chat.
- **Auth**: JWT, API key, service account, or SSO (OAuth) via the auth and OAuth APIs. When you are signed out, Chat Explorer and Administration show a full-page sign-in landing; the app shell is not on screen. On a local stack, sign in with a seeded Keycloak user — `motet-admin` / `RootPassword1!` or `acme-user` / `AcmeUser1!` (see [Quick Start — Log in](./04-quick-start-guide.md#log-in)).
- **Multi-agent reasoning**: Right-hand panel shows per-agent reasoning chains, thinking traces, tool executions, and workflow steps — each attributed to the agent that produced them.
- **Observability**: Optional event stream panel for debugging task and stream events.

It is the main “try it” / explore UI for the stack and a practical reference for building your own chat or agent UIs on top of Motet.

## How to Run It

With the API server and workers running (e.g. via Docker or [Quick Start](./04-quick-start-guide.md)):

```bash
# From repo root; frontend monorepo
cd motet/interfaces/frontend
npm install
npm run dev
# Or: npm run dev:chat  (workspace apps/chat-explorer)
```

**URL:** served at **`/chat-explorer/`** (Vite base and API static/proxy mount).
In local Docker compose, open the API origin and navigate to `/chat-explorer/`
(or the Vite dev proxy path). On AWS EC2: `https://<public-ip>/chat-explorer/`.

Configure base URL and auth as needed for your environment. On a local stack,
sign in with the seeded users from [Quick Start — Log in](./04-quick-start-guide.md#log-in).

## Frontend Monorepo Structure

The frontend is an npm workspace monorepo:

```
motet/interfaces/frontend/
├── apps/
│   ├── chat-explorer/         # Chat Explorer (reference chat UI)
│   └── ops-dashboard/         # Operations and admin UI
└── packages/
    └── motet-ui-common/       # Shared library (@motet/ui-common)
```

Both apps consume `@motet/ui-common` as a workspace dependency. The shared package is never published — it is resolved via Vite aliases at build time.

## `@motet/ui-common` — Shared UI Library

`@motet/ui-common` is the shared package that contains all framework-aware UI primitives. It was extracted from the former demo-chat-x app so that multiple frontend applications (Chat Explorer, ops dashboard, and any future apps) share a single implementation of chat protocol handling, conversation management, auth, attachments, and rendering.

### What It Provides

| Category | Exports | Purpose |
|----------|---------|---------|
| **Hooks** | `useAuth`, `useConversationManager`, `useAttachments`, `useRequestContext`, `useEventBus`, `useThrottle` | React hooks for auth state, conversation CRUD, file uploads, model/behavior overrides, SSE event subscriptions, and throttled updates. |
| **Components** | `AuthModal`, `SignedOutPage`, `RenameModal`, `MermaidBlock`, `renderMarkdownWithMermaid`, `RagControls` | Auth dialogs, full-page sign-in landing, conversation rename modal, Markdown with live Mermaid, and retrieval scope chips (This chat / My files / Workspace). |
| **API Clients** | `listConversations`, `getConversation`, `updateConversationTitle`, `deleteConversation`, `mapHistoryToMessages` | Typed fetch wrappers for the Conversations API (`/api/v1/conversations`). |
| **Chat Protocol** | `reduceChatEvent`, `streamAgentKeyFromData`, `withAgentStream` | Framework-agnostic SSE event reducer that processes chat stream events into structured state (content, thinking, tool calls, per-agent attribution). |
| **Types** | `AuthState`, `SSEvent`, `AgentStreamSlice`, `AgentReasoningPanel`, `AttachmentState`, `Overrides`, `ConversationEntry`, etc. | Shared type definitions for auth, events, multi-agent streaming, attachments, and request overrides. |
| **Utilities** | `randomId`, `debugLog`, `parseSseBuffer`, `resolveAgentDisplayName`, `shortAgentLabel`, `formatExecutionStatusLine` | SSE buffer parsing, agent display name resolution from registry, and formatting helpers. |

### Package Exports

The package exposes subpath exports so consumers can import from the root or from specific modules:

```typescript
// Root import (most common)
import { useAuth, AuthModal, reduceChatEvent } from "@motet/ui-common";

// Subpath imports for tree-shaking or clarity
import { listConversations } from "@motet/ui-common/api";
import { useAttachments } from "@motet/ui-common/hooks";
import { MermaidBlock } from "@motet/ui-common/components";
import type { AuthState, Overrides } from "@motet/ui-common/types";
import { parseSseBuffer } from "@motet/ui-common/utils";
```

### Key Hook Details

**`useConversationManager`** — Full conversation lifecycle management. Handles list/get/rename/delete against the Conversations API, maintains a local `ConversationStore` keyed by conversation ID, supports agent and surface scoping, and persists the active conversation to localStorage. Apps receive a `ConversationStore` and action callbacks.

**`useAttachments`** — Manages the full attachment lifecycle: file selection, upload to the Artifacts API (`/api/v1/artifacts`), progress tracking, preview URLs, and cleanup on send or discard. Handles image, PDF, and document types with content-type detection and preset icon inference.

**`useRequestContext`** — Manages model and behavior overrides (model provider/name, reasoning effort, and related chat override keys) that are sent alongside chat messages. Persists user preferences to localStorage.

**`useAuth`** — Auth state management supporting JWT, API key, service account, and dev-mode header authentication. Builds the correct `Authorization` or `X-*` headers for API requests based on the active auth method.

### Chat Protocol Reducer

The `reduceChatEvent` function is the core of the streaming chat experience. It takes a parsed SSE event and the current `ChatOutput` state and returns the next state. This is a pure reducer — framework-agnostic, no React dependency — so it can be used in any JavaScript runtime.

The reducer handles:
- **Content streaming** — Accumulating text deltas into the response.
- **Extended thinking** — Tracking `thinking_delta` events and thinking completion.
- **Tool executions** — Recording tool call start, progress, and completion.
- **Reasoning steps** — ReAct-style thought/action/observation events.
- **Workflow steps** — Workflow step start, completion, and error events.
- **Per-agent attribution** — Bucketing all of the above by qualified agent ID (e.g. `core.default`, `expert-panel.researcher`) when the backend sends `metadata.agent_id` on stream events.
- **Stream lifecycle** — Processing `[DONE]` markers, error events, and usage metadata.
- **Budget stops** — When `end.stop_reason` is `max_iterations` or `max_model_calls`, Chat Explorer shows a **Continue** control that sends `continue_after_budget: true` (a new turn with a fresh budget). This is not the same as resuming a suspended tool-handback turn. See [Agent Loop — Turn budgets and Continue](./07a-agent-loop.md#turn-budgets-and-continue).

### How Apps Consume It

Chat Explorer and ops-dashboard import from `@motet/ui-common` and add app-specific wiring. In chat-explorer, many modules are thin re-export wrappers:

```typescript
// chat-explorer/src/hooks/useAttachments.ts
export { useAttachments } from "@motet/ui-common";

// chat-explorer/src/hooks/useConversation.ts — wraps useConversationManager
// with app-specific defaults and derives ConversationStore
import { useConversationManager, computeInitialConversations } from "@motet/ui-common";
```

The chat provider (`chatProvider.ts`) uses `reduceChatEvent` and `withAgentStream` from `@motet/ui-common` to drive the SSE streaming loop, then dispatches the reduced state into React component state.

## APIs It Uses

Chat Explorer shows how a client uses Motet's public APIs:

| API | Usage in Chat Explorer |
|-----|------------------------|
| **Conversations** (`/api/v1/conversations`) | List conversations (sidebar), get history when switching conversation, rename (PATCH), delete (DELETE). Filtered by `agent_id` + `surface_id`. |
| **Chat** (`/api/v1/chat`) | POST to send a message and receive a streamed response (SSE). Drives the main chat thread and streaming UX. |
| **Artifacts** (`/api/v1/artifacts`) | Upload files (multipart POST), associate with `conversation_id`, get preview (GET `/{id}/preview`) for images, delete when user removes attachment. |
| **Models** (`/api/v1/models`) | List available models for the composer model selector, including `requires_api_key` and `has_api_key` so the picker can mark keyed providers and disable the rest. Catalog: [Supported models](./03a-supported-models.md). |
| **Auth** (`/api/v1/auth`) | Login, logout, refresh, identity-provider logout. |
| **OAuth** (`/api/v1/oauth`) | SSO flows (e.g. initiate) when using an identity provider. |
| **Events** (`/api/v1/events`) | Optional SSE of the caller’s tenant command/turn events for debugging (right panel). Chat tokens still come from `/api/v1/chat`. |
| **Vault** (`/api/v1/vault`) | Not called directly by Chat Explorer. When MCP or other tools need API keys, the backend resolves them from the vault in commands. The manage UI can use vault list/stats/health. See [API Reference](./28-api-reference.md) for credential store/retrieve and MCP environment endpoints. |

Together, these show how to:

- Use the **Conversations API** for multi-session chat (list, get, rename, delete, clear).
- Use the **Chat API** for sending input and consuming streamed output (SSE).
- Use the **Artifacts API** for uploads, previews, and conversation-scoped files.
- Use **Auth/OAuth** for secure, tenant- and principal-aware access.

## Multi-Agent Stream Attribution

When a conversation involves multiple agents (e.g. an expert-panel bundle with researcher, critic, and synthesizer agents), the backend attaches `agent_id` to SSE stream events (and `parent_agent_id` on nested loops). The frontend uses this to:

1. **Bucket streaming content by agent** — Each agent's tokens, thinking trace, tool executions, and reasoning steps are tracked in a separate `AgentStreamSlice`.
2. **Render per-agent reasoning panels** — The right sidebar shows collapsible panels for each agent, with its display name resolved from the agent registry via `resolveAgentDisplayName`.
3. **One assistant bubble per turn** — The selected chat agent owns the bubble. Spawned children (`{parent}.spawn-N`) are labeled Sub-agent N and sit in a scrollable stack (collapsed when the turn finishes) so their write-ups do not push the parent's thinking and synthesis off screen. Reloading a conversation groups consecutive attributed assistant rows from that turn the same way, including spawn children whose write-ups were stored on the parent transcript (reply text only — thinking traces are live-session).

The `streamAgentKeyFromData` utility extracts the agent key from an SSE event payload, and `withAgentStream` is a reducer helper that routes updates into the correct agent bucket.

## Where It Lives in the Codebase

### Shared library (`@motet/ui-common`)

- **Package root**: `motet/interfaces/frontend/packages/motet-ui-common/`
- **Hooks**: `motet-ui-common/src/hooks/` — `useAuth`, `useConversationManager`, `useAttachments`, `useRequestContext`, `useEventBus`, `useThrottle`
- **Components**: `motet-ui-common/src/components/` — Auth modal, signed-out landing, `RenameModal`, `MermaidBlock`
- **API clients**: `motet-ui-common/src/api/` — `conversations.ts` (CRUD), `chat.ts` (SSE reducer + protocol types)
- **Types**: `motet-ui-common/src/types/` — Auth, SSE events, agent streams, attachments, overrides
- **Utilities**: `motet-ui-common/src/utils/` — SSE parsing, agent display helpers, formatting

### Chat Explorer app

- **App root**: `motet/interfaces/frontend/apps/chat-explorer/`
- **URL mount**: `/chat-explorer/`
- **Chat provider**: `chat-explorer/src/chatProvider.ts` — SSE streaming loop using `reduceChatEvent` from `@motet/ui-common`.
- **Chat processing**: `chat-explorer/src/hooks/useChatProcessing.tsx` — Wires reduced SSE state into React component state, builds per-agent reasoning panels.
- **Multi-agent chat**: `chat-explorer/src/hooks/useMotetChat.tsx` — Renders one assistant bubble per turn, with sub-agent sections nested inside.
- **Conversations**: `chat-explorer/src/hooks/useConversation.ts` — Wraps `useConversationManager` from `@motet/ui-common` with app-specific defaults.
- **Right sidebar**: `chat-explorer/src/components/RightSidebar.tsx` — Per-agent collapsible reasoning panels.
- **Storage**: localStorage keys use the `chat_explorer_*` prefix.

### Ops dashboard

- **App root**: `motet/interfaces/frontend/apps/ops-dashboard/`
- Consumes `useAuth`, `AuthModal`, `SignedOutPage`, `parseSseBuffer`, `buildHeaders`, and `renderMarkdownWithMermaid` from `@motet/ui-common`.

## Building a New Frontend App

To create a new Motet frontend application that shares the common infrastructure:

1. Add a new app under `motet/interfaces/frontend/apps/`.
2. Add `"@motet/ui-common": "workspace:*"` to its `package.json` dependencies.
3. Configure a Vite alias so `@motet/ui-common` resolves to the source directory (see `chat-explorer/vite.config.ts` for the pattern).
4. Import hooks, components, and API clients from `@motet/ui-common`.

The shared package has `react`, `react-dom`, `antd`, `@ant-design/x`, and `@ant-design/x-markdown` as peer dependencies — your app must provide these.

## Next Steps

- **[API Reference](./28-api-reference.md)** – Endpoint details for conversations, chat, artifacts, cost, and more.
- **[Streaming Responses](./13-streaming-responses.md)** – How streaming and SSE work.
- **[Artifacts and Multimodal Context](./20a-artifacts-and-multimodal-context.md)** – Artifact model and API.
- **[Security & Multi-Tenancy](./22-security-multi-tenancy.md)** – Auth and tenant context.

## Navigation

- **[← Back to Documentation Home](./00-landing-page.md)** – Main documentation hub

---

**Last Updated**: 2026-08-28

**See the framework in action.** Run Chat Explorer at `/chat-explorer/` and use the APIs it demonstrates to build your own chat or agent UIs.
