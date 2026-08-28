## Package: interfaces.api.shared

**Cross-router FastAPI utilities** — authentication, identity resolution, shared Pydantic models, and **`Depends()`** factories consumed by every **`motet.interfaces.api.v1`** module.

### Purpose

- **One auth story**: JWT / dev-header behavior and **`get_current_principal`** live here so endpoints stay thin.
- **DRY response contracts**: Cross-cutting **`models.py`** types avoid copy-paste between **`v1/*.py`** files.
- **Composable dependencies**: **`dependencies.py`** wires settings, clients, and principal-scoped services for injection.

### Core components

#### `auth.py`

Authentication dependency callables, header parsing compatibility, and integration with **`motet.core.security`** expectations.

**Tenant invariant:** identity comes from the principal, not the body or query. A request `tenant_id` / `motet_id` / `conversation_id` is a name, not permission. Use `require_tenant_access` (and `require_motet_access`) on every endpoint that accepts those fields. Cross-tenant access requires `can_access_all_tenants`; otherwise return 403 — do not silently substitute the caller's tenant.

#### `surfaces.py`

HTTP mapping for conversation surface catalog membership (``require_catalog_surface``). Chat and conversations share this check; agent allow-lists stay in the chat path.

#### `identity.py`

Principal/tenant/motet context helpers shared by resource routers.

#### `scope.py`

Manage-app selector query params (`tenant_id` / `motet_id`) and `matches_scope()` for list filters. Use `Depends(get_manage_app_scope)` on Tasks, Memory, Schedules, and Agents.

#### `memory_ops.py`

Redis SCAN, filter, stats, and scoped-clear helpers for manage-app memory browse. Browse decrypts newest rows; **`count_memory_index`** uses index ``ZCARD`` / ``ZCOUNT``. Used by **`/api/v1/memories/{browse,stats,clear}`** and the debug memory routes.

#### `models.py`

Pydantic bodies and query models reused by multiple **`v1`** routers — extend here when two or more endpoints need the same shape.

#### `dependencies.py`

Reusable **`Depends()`** providers (database clients, feature flags, principal-bound services).

### Notes

When touching auth or shared models, update **all** consumers in **`v1/`** and add regression tests (API tests run in Docker per **`AGENTS.md`**).

### Related

- Resource routers: **`motet/interfaces/api/v1/README.md`**
