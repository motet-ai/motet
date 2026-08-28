# Auth / OAuth

HTTP APIs authenticate a **principal**. Workers trust the `tenant_id` / `principal_id` stamped on the command. A request body or query `tenant_id` is a name, not permission.

## Principal

Every command carries:

- `principal_id` — who
- `tenant_id` — which tenant
- Roles on the `Principal` at the API boundary

`motet.principal_id` and `motet.tenant_id` come from the verified JWT. They are safe to log and attribute against. They are not a `has_permission()` helper — enforce roles at the API.

Identity is never read from `X-Tenant-ID` (CORS allowlist only) or from a body `tenant_id`. Naming another tenant returns **403** unless the caller has global tenant scope. Omitted `tenant_id` means the caller’s tenant.

Reserved system principals live in `system_principals.py`.

## Tenant isolation

Memory operations take **no tenant argument**. Scope is implicit from `MotetContext`. Missing `tenant_id` / `motet_id` / `principal_id` raises rather than falling back to a process default.

Keys are namespaced `{tenant_id}:…`. On managed Redis, RBAC globs (`~{tenant_id}:*`) enforce that at the datastore.

`MOTET_TENANT_ENFORCE_MEMORY_FILTER=true` requires `tenant_id` on memory operations (commands and `/api/v1/memories`). Default is `false`.

## HTTP auth

Shared auth is `get_current_principal` from `motet/interfaces/api/shared/auth.py`. Do not duplicate auth in a new router.

JWT / identity-provider selection is the shipped login path (Keycloak in tests and typical deploys). Management APIs require JWT/admin.

## MCP OAuth

MCP servers that call third-party APIs get tokens from the vault, injected by the MCP manager as env vars. See [mcp.md](./mcp.md) for key shape.

The OAuth proxy (`/api/v1/oauth`) runs the browser login and writes vault keys. Missing-authorization prompt flow is **partial** (core observer / manager path exists; later phases remain open).

There is no pass-through OAuth bridge for desktop MCP. Desktop MCP runs on an edge `mcp-manager`; OAuth is the proxy + vault path above.

## Paths

- Auth: `motet/interfaces/api/shared/auth.py`
- OAuth: `/api/v1/oauth`, oauth manager
- Vault: `motet` vault client, `/api/v1/vault`
- Principals: `system_principals.py`
