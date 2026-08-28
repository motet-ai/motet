# Tenancy

Operator-managed catalog of **tenants** (organizations) and **Motets** (per-tenant deployment environments such as `prod` / `staging` / `dev`).

## Components

| Module | Role |
|--------|------|
| `tenant_registry.py` | Redis-backed CRUD for tenants and nested Motets |

## Redis keys

| Key | Type | Purpose |
|-----|------|---------|
| `{tenant_id}:tenant:meta` | hash | Tenant metadata |
| `motet:tenant:index` | set | All tenant ids (shared product prefix) |
| `{tenant_id}:tenant:motet:{motet_id}` | hash | Motet metadata |
| `{tenant_id}:tenant:motet:index` | set | Motet ids for a tenant |

Client id for `get_sync_redis_client`: `tenant_registry`.

## API / CLI

- REST: `/api/v1/tenants` (see `motet/interfaces/api/v1/tenants.py`)
- CLI: `motet-cli tenants …`

## Boundaries

- **Not** JWT remapping (`MOTET_TENANT_ID_MAP_JSON` / `MOTET_TENANT_GLOBAL_IDS`)
- **Not** `ScopedRegistry` (in-process visibility grants)
- **Not** bundle content catalogs (`bundle:{id}:catalog`)

Tenant ids `motet`, `imf`, and other shared first segments (`celery`,
`worker`, `lock`, …) are reserved. Those prefixes are the shared control plane
(`motet:events:…`) and an ElastiCache glob `~motet:*` would match them.
`motet-global` is allowed (different first segment).
