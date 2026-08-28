## Package: interfaces.api.v1

**Versioned REST routers** mounted under **`/api/v1/*`**. Each resource module defines **`router = APIRouter(prefix="/api/v1/<resource>", tags=["<resource>"])`** so paths stay explicit in code review.

### Purpose

- **Consistent URL map**: Resource name in the path matches the module name where practical (**`commands.py`** → **`/api/v1/commands`**).
- **Composable surface**: **`motet/interfaces/http.py`** imports every submodule’s **`router`** and **`include_router`s** without a single brittle mega-import (partial failures log warnings).
- **Shared auth/models**: Depends + Pydantic types come from **`motet.interfaces.api.shared`** (**`auth`**, **`identity`**, **`scope`**, **`models`**, **`dependencies`**).
- **Artifact enrichment**: **`PATCH /api/v1/artifacts/{artifact_id}/metadata`** merges custom metadata and normalized **`artifact_tags`** for upload-driven enrichment workflows.

### Conventions

- Export **`router`** (never **`commands_router`**-only modules without a **`router`** alias at module level usable by **`http.py`** loaders).
- Use **`Depends(get_current_principal)`** (and siblings) from **`shared.auth`** rather than bespoke JWT parsing per file.
- A request **`tenant_id`** is a name, not permission. Call **`require_tenant_access`** (schedules, service accounts, deploy/skills lists, cost, vault). Omitted tenant resolves to the caller; a foreign tenant is 403 unless the principal has global scope.
- **`/api/v1/debug`** requires **`MOTET_DEBUG_MODE=true`** and an admin principal. Leave debug off on hosted stacks.
- New endpoints need **`Field(description=...)`** on Pydantic models and **`responses={...}`** on routes per project standards — see **`AGENTS.md`** API checklist.

### Registered modules

**`http.py`** loads (in order): **`commands`**, **`schedules`**, **`vault`**, **`debug`**, **`workers`**, **`workflows`**, **`tools`**, **`memories`**, **`conversations`**, **`models`**, **`chat`**, **`service_accounts`**, **`events`**, **`identity`**, **`oauth`**, **`auth`**, **`artifacts`**, **`cost`**, **`developer_docs`**, **`deploy`**, **`agents`**, **`devices`**, **`image_stacks`**, **`workspace_containers`**, **`skills`**, **`tenants`**, **`surfaces`**, **`tasks`**, **`mcp`**, **`version`**.

**`debug`**: developer tools including **`GET /api/v1/debug/commands`** and **`/memory/{stats,search,clear}`**. Optional **`tenant_id`** / **`motet_id`** query params filter the manage-app Tasks view; omitted means all tenants/motets. Memory SCAN helpers are shared with **`memories`**.

**`memories`**: list/find/tag/store plus manage-app **`GET /browse`**, **`GET /stats`**, and **`POST /forget`**. The ops Memory page uses these product routes (not debug). Optional **`tenant_id`** / **`motet_id`** query params filter browse/stats/clear. Browse also accepts **`agent`** (qualified id, short name, or `agent:` tag). Browse decrypts a caller-chosen newest window (up to 5000); stats totals come from the Redis index.

**`schedules`** / **`agents`** / **`workspace-containers`**: the same optional scope query params filter the manage-app lists (workspace containers are tenant-only).

**`mcp`**: per-service MCP health for the ops **MCP Servers** page — **`GET /api/v1/mcp/servers`** (monitoring auth, no JWT) plus enqueue **`POST.../restart|disable|enable|register`** and **`DELETE /servers/{service_id}`** onto the sibling manager control stream. Does not scrape YAML; records come from Redis `imf:manager_status:{manager_id}:mcp:services`.

**`tasks`**: live orchestration tasks — **`GET /live`**, **`GET /{task_id}`**, **`POST /{task_id}/cancel`** (owning principal; sticky cancel + push wake).

**`developer_docs`**: list + get markdown from `docs/developer_onboarding/` (override `MOTET_DEVELOPER_DOCS_DIR`), plus lexical `GET /search?q=`. Shared corpus with `core.docs_read`; the HTTP list returns exclusive nav sections (Home, Start, Concepts, Build, Runtime, State, Operate, Surfaces, Guides), a flattened `items` array in that order, and the Motet product `version`. Home is the landing page. The agent tool stays allowlisted. `/search` is registered before `/{doc_id}`.

**`version`**: **`GET /api/v1/version`** (authenticated) returns this API process Motet product version, each registered worker's stamped version, configured sibling versions (embedding-server, mcp-manager), and ``skew`` when any worker or configured sibling is unreachable, missing a version, or disagrees with the API. Unconfigured siblings are omitted. ``GET /health`` stays liveness-only.

### Adding a router

1. Create **`motet/interfaces/api/v1/<resource>.py`** with **`router`** and prefix **`/api/v1/<resource>`**.
2. Export it from **`motet/interfaces/api/v1/__init__.py`** (optional but used by tests and explicit imports).
3. Append **`<resource>`** to **`_API_V1_ROUTERS`** in **`motet/interfaces/http.py`**.
4. Run through the **Pre-Commit API Checklist** in **`AGENTS.md`**.

### Related

- Shared helpers: **`motet/interfaces/api/shared/README.md`**
- Broader interface layer: **`motet/interfaces/README.md`**
