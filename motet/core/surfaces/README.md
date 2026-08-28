# Surfaces Catalog

Redis-backed catalog of conversation **surface** IDs plus
per-agent allow-list overlays for manage-UI and Chat Explorer.

## Concepts

| Concept | Meaning |
|---------|---------|
| Surface | Stable channel id stamped on conversations (`demo_chat`, `openai_compat`, …) |
| Catalog | Explicit allow-list of known surfaces (not auto-created on chat) |
| Agent allow-list | Surfaces an agent *could be on*; empty/missing = all catalog surfaces |
| Overlay | Redis override of `AgentConfig.allowed_surface_ids` for operator edits |

Surface ids are lowercase slugs: start with a letter, then letters/digits/underscores/hyphens
(`^[a-z][a-z0-9_-]{1,62}$`). Snake_case builtins (`demo_chat`) and kebab-case product ids
(`memo-intake`) are both valid; chat still requires the id to exist in the catalog.

## API

- `GET/POST /api/v1/surfaces`
- `GET/PATCH/DELETE /api/v1/surfaces/{id}`
- `PUT /api/v1/agents/{qualified_id}/surfaces` — set/clear allow-list overlay

## Builtins

Seeded by `SurfaceRegistry.ensure_builtins`:

- `demo_chat`, `openai_compat`, `ops_dashboard`, `cli`

## Bundle declaration

Bundles may declare product surfaces in `config/surfaces.yaml` (or
`surfaces/surfaces.yaml`):

```yaml
surfaces:
 - id: partner_portal
 display_name: Partner Portal
 description: Optional description
```

On publish / reload, Motet calls `register_if_absent`. **If the surface id
already exists, deploy is a no-op** (display name / description are not
overwritten). Surface ids are global (not `{bundle}.{id}`).
