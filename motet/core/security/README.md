## Package: security

**Distributed security infrastructure** with authentication, authorization, and multi-tenancy for distributed AI operations.

### Purpose
- **Principal-Based Authentication**: JWT/JWKS validation with principal and tenant extraction
- **Distributed Authorization**: Role-based access control across distributed commands and workers
- **Multi-Tenancy**: Tenant isolation and data segregation in distributed operations
- **Rate Limiting**: Distributed rate limiting with Redis backend support
- **Egress Control**: Domain policies and PII redaction for distributed tool execution

### Components
- `auth.py`: JWT/JWKS utilities, API key checks.
- `service_accounts.py`: Long-lived `sa_*` tokens, including OpenAI facade policy.
  Create writes `motet:auth:service_account:{token}` → tenant so verify is
  exact-key `GET` then `HGETALL`. Tokens created before locators: run
  `scripts/backfill_valkey_locators.py --only service_account`.
- `facade_policy.py`: Per-credential mode, model allowlist, and force_thinking for the OpenAI-compatible facade.
- `ratelimit.py`: Token bucket/Leaky bucket primitives and FastAPI dependencies.
- `egress.py`: Domain allow/deny helpers for HTTP tools.
- `encryption_service.py`: Tenant AES-256-GCM. Unwrap tries the current KEK, then
  `encryption:tenant:{tid}:previous` so rows written before a rotation still open.
- `encrypted_payload_store.py`: Memory/artifact Redis hashes. Encrypt binds the
  collapsed logical key; decrypt retries older AAD names. Re-seal leftovers with
  `scripts/backfill_encrypted_payload_aad.py`.
- `vault_service.py`: Credential store. Key resolve uses the tenant key,
  `motet:vault:locate:{id}` → tenant, `motet:vault:…` for platform rows,
  then leftover names (`None:vault:…`, unprefixed `vault:…`, `imf:vault:…`).
  List reads `{tid}:vault:index` / `motet:vault:index` (SET of ids). Store
  and delete update that SET. Rows created before the index:
  `scripts/backfill_valkey_vault_index.py`. Store and delete SCAN cache
  hashes for that credential id.

### Implemented
- API key and JWT/JWKS validation with alg allowlist and leeway; cache TTL.
- In-memory and Redis-backed rate limiting.
- Dedicated auth-failure throttling for repeated invalid JWT/service-account attempts.
- HTTP egress policies (allow/deny) applied to tools.
- Principal and tenant extraction from JWT (with configurable claim names) and optional dev headers.
- Fail-closed protection for insecure principal headers outside local/test environments.
- Service account tokens carry OpenAI facade policy (`facade_mode`, `allowed_models`,
 `force_thinking`, `agent_id`), surfaced on `Principal.claims` so the facade authorizes
 without re-verifying the token.

### OpenAI Facade Policy
Clients such as Cursor can only supply a base URL, an API key, and a model string, so facade
authorization is bound to the credential rather than to request headers.

- **Execution mode**: `passthrough`, `hosted_tools`, or `agent`. The bound mode is also the
 ceiling — a request may select a weaker mode but never escalate.
- **Model allowlist**: `provider/model`, `provider/*`, or `*` entries. Deny-by-default, so a
 credential without policy can call nothing.
- **Force thinking**: optional SA/config flag to enable Motet thinking for `CAP_REASONING`
 models when the client omits reasoning opt-in (common for Cursor BYOK).
- **Agent id**: optional SA/config default Motet agent for `agent` mode when the client
 omits `motet_agent_id` (e.g. `cursor.backend`).
- **Resolution**: `resolve_facade_policy(principal, cfg)` prefers service account claims and
 falls back to `MOTET_OPENAI_COMPAT_DEFAULT_*` / `MOTET_OPENAI_COMPAT_FORCE_THINKING*`
 configuration.

### Principal-Based Identity (Updated for Distributed Architecture)
- **Principal ID**: All distributed commands include `principal_id` for consistent identity tracking
- **Environment Configuration**:
 - `MOTET_JWT_SUB_CLAIM` (default: `sub`) - Maps to `principal_id` in distributed commands
 - `MOTET_JWT_ROLES_CLAIM` (default: `roles`) - Principal roles for authorization
 - `MOTET_JWT_TENANT_CLAIMS` (default: `tenant_id,tid,org_id`) - Additional tenant claim names to accept
 - `MOTET_JWT_ORGANIZATION_CLAIM` (default: `organization`) - Canonical Keycloak organization claim
 - `MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS=false` - Dev mode header acceptance
- **Development Headers**: `X-Principal-Id`, `X-Tenant-Id`, `X-Roles`
- **Distributed Integration**: `extract_principal(cfg, request)` → `Principal` used in command context

### Planned
- RBAC expansion and OAuth/JWT integration paths; CORS policy validation.
- PII redaction pipeline expansion and configurable masking policies.

