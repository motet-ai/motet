# Motet Frontend Monorepo

This monorepo contains the frontend applications for the Motet.

## Structure

```
frontend/
├── package.json # Workspace root
├── tsconfig.base.json # Shared TypeScript config
├── packages/
│ └── motet-ui-common/ # Shared UI components, hooks, and utilities
└── apps/
 ├── chat-explorer/ # Chat Explorer (`/chat-explorer/`)
 └── ops-dashboard/ # Ops/admin UI (`/manage/`)
```

## Quick Start

```bash
# Install all dependencies
npm install

# Start chat-explorer development server
npm run dev

# Build all packages
npm run build
```

## Packages

### @motet/ui-common

Shared UI components, hooks, types, and utilities.

**Hooks:**
- `useAuth` - Authentication management (JWT, SSO, API keys)
- `useEventBus` - SSE event subscription
- `useThrottle` - Value throttling for streaming

**Components:**
- `AuthModal` - Authentication credential entry
- `SignedOutPage` - Full-page sign-in landing (welcome or after logout)
- `RagControls` - Retrieval scope chips (This chat / My files / Workspace)

**Utilities:**
- `randomId` - Generate random IDs
- `debugLog` - Development-only logging
- `parseSseBuffer` - Parse SSE event streams
- `buildHeaders` - Construct auth headers

**Usage:**
```typescript
import { useAuth, AuthModal, AuthState } from "@motet/ui-common";

function App {
 const { auth, handleSsoLogin, isAuthenticated } = useAuth;
 //...
}
```

## Apps

### chat-explorer (Chat Explorer)

Reference multi-surface chat UI at `/chat-explorer/`. Default channel
`surface_id=demo_chat`. Features:
- Real-time streaming chat with AI
- Conversation list scoped by agent and surface
- Agent and surface pickers in the header
- Model picker on the composer (`provider : model`, or Auto); key icon when the provider has an API key, unselectable when it does not
- File attachments
- Retrieval popover (This chat / My files / Workspace) on the composer
- Mermaid diagram rendering
- Reasoning / thinking visualization
- Full-page sign-in landing when signed out

See [Chat Explorer & Shared UI Library](../../../docs/developer_onboarding/36-chat-explorer.md).

### ops-dashboard

Operations dashboard for monitoring workers, tasks, memory, and costs
(`/manage/`).

Features:
- Three-column layout (nav, content, AI chat)
- Tenant/Motet scope selector (`src/api/scope.ts` `scopedUrl` / `applyScopeParams`)
- Shared auth header helper (`src/api/http.ts` `getAuthHeaders` / `setLiveAuth`; uses live `useAuth` state, storage fallback)
- Pages: Workers, Instance Managers, MCP Servers, Tasks (Flow/JSON plus Cancel on running rows), Schedules, Memory (newest-window browse, contains/semantic search, tag, forget, scoped clear), Vault, Workflows, Models, Cost, API (ReDoc), Documentation (grouped onboarding nav with lexical search)
- Dark/light mode support
- Signed-out landing while unauthenticated (dashboard shell is not shown)

## Development

```bash
# Start chat-explorer (default)
npm run dev

# Start ops-dashboard (runs on port 5174)
npm run dev:ops

# Build specific apps
npm run build:chat
npm run build:ops

# Lint all packages
npm run lint
```

### Running Ops Dashboard Locally

The ops-dashboard runs on port 5174 by default:

```bash
cd motet/interfaces/frontend
npm run dev:ops
```

Then access at: http://localhost:5174/manage/

Note: The ops-dashboard proxies API requests to `http://localhost:8000` by default.
Make sure the FastAPI backend is running.

## Architecture

This monorepo uses npm workspaces for dependency management. The shared package
`@motet/ui-common` is linked automatically during `npm install`.

TypeScript project references are used for faster builds and better IDE support.
Each app extends `tsconfig.base.json` and references the shared package.
