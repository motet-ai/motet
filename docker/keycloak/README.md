# Keycloak 26.x + Organizations Bootstrap

This directory contains a thin wrapper around the official Keycloak 26.x image.
It pre-loads the **motet** realm (clients, roles, basic users) and starts
Keycloak with the preview **Organizations** feature enabled so that each tenant
can be modeled as a first-class organization instead of ad-hoc groups.

Highlights:

- Realm `motet` with default admin/user accounts
- Organizations feature enabled via `--features organization`
- Client `motet-ai-stack` (public PKCE) ready for demo chat / CLI
- Also includes the `orgs` / `org_hierarchy` scopes for group-based claims
  (useful for environment-level segmentation under each org)
- Adds an `organization` scope that emits canonical org metadata (id/slug/
  attributes) so Motet can resolve tenants without manual group assignments

## Bootstrapping Organizations

1. Start the stack: `docker-compose -f docker-compose.distributed.yml up keycloak`
2. Sign in to the admin console at `http://localhost:8080/admin` (`admin/admin`)
3. Create organizations under the **Organizations** nav:
   - `acme` → add environments (e.g., `prod`, `dev`)
   - `globex`
   - `motet-global` (assign admins/operators)
4. Assign users to the appropriate organization/environment
5. Tokens now include the `organization` claim which Motet maps to `tenant_id`

The `/orgs/<tenant>/<env>` group hierarchy remains for fallback and for
scopes that still expect groups.

### Automation via bootstrap script

Instead of clicking through the admin console you can run the helper script,
which shells into the Keycloak container and issues the appropriate `kcadm.sh`
calls:

```bash
python docker/keycloak/bootstrap_orgs.py
# customize realm / compose file / org definitions
python docker/keycloak/bootstrap_orgs.py \
    --compose-file docker-compose.distributed.yml \
    --realm motet \
    --org demo-org:"Demo Org":"Sample tenant" \
    --org motet-global:"Motet Global":"Cross-tenant operators"
```

The helper prefers the Organizations REST API: it creates/updates real
organizations (setting the `displayName` / `description` attributes) and then
mirrors the same slug under the `/orgs/<tenant>` group hierarchy so the
existing `org_hierarchy` and `orgs` token mappers keep working. If the preview
feature is disabled in a build, it falls back to group creation only.
It also turns on the realm-level toggle `organizationsEnabled=true` (required in
addition to starting Keycloak with the `organization` feature).
It also ensures the dedicated `organization` client scope (with the
`oidc-organization-membership-mapper`) exists and is attached to both the realm
defaults and the `motet-ai-stack` client so OAuth flows always request the
canonical tenant claim.

By default, the script also assigns the seeded demo users to the corresponding
organizations:

- `demo@acme.localhost` → `demo-org`
- `root@motet.localhost` → `motet-global`

The script is idempotent and safe to run multiple times. It reads the admin
credentials from `KEYCLOAK_ADMIN` / `KEYCLOAK_ADMIN_PASSWORD` (falling back to
`admin/admin`).

Build locally:

```bash
docker build -t motet-keycloak:dev docker/keycloak
```

Runtime command (configured automatically by docker-compose):

```
kc.sh start-dev --import-realm --features organization,scripts --http-enabled=true --hostname-strict=false
```

On first boot the realm import seeds the base configuration. Additional
organization bootstrap (creating sample orgs, assigning members) is handled by
the companion `keycloak-bootstrap` service that runs `kcadm.sh` against this
server once it becomes healthy.

## Local dev: faster “healthy” in Compose

Keycloak still needs a full JVM + Quarkus boot (often **1–3 minutes** on Docker
Desktop, especially first run or after a fresh volume). To reduce how long
Compose waits before marking the service **healthy**:

- The distributed and test compose files use **`GET /health/ready` on port
  8080** (with **`KC_HTTP_MANAGEMENT_HEALTH_ENABLED=false`**) instead of
  **`kcadm.sh`**, so the probe tracks process readiness rather than admin-CLI
  readiness.
- **`JAVA_OPTS_APPEND=-XX:TieredStopAtLevel=1`** speeds JIT warmup a bit in dev.
- **`interval: 10s`** notices success sooner than a 30s poll.

Other levers (not in compose): reuse the **`keycloak_data`** volume so later
starts skip heavy first-import work; give the VM more CPU/RAM; try **OrbStack**
/ **Colima** if Docker Desktop is slow. For a one-off session that does not need
Organizations/preview features, a slimmer `KC_FEATURES` list would also shorten
startup.

