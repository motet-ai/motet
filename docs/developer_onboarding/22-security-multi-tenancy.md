# Security & Multi-Tenancy

Motet's security model provides principal-based access control, tenant isolation, and comprehensive authentication. This section covers security architecture, authentication, authorization, and best practices.

## Principal-Based Architecture

### Principal Concept

Every command includes principal context:

- **Principal ID**: Who is executing the command
- **Principal Type**: User, service, application, system
- **Roles**: What permissions the principal has

### Principal Context

```python
@motet.command()
def my_command(data: MyData, motet: MotetContext) -> Dict[str, Any]:
    # Principal context automatically available
    principal_id = motet.principal_id
    tenant_id = motet.tenant_id
    
    # Both come from the verified JWT and are safe to log and attribute against
    logger.info("command_started", principal_id=principal_id, tenant_id=tenant_id)
    
    return {"result": "success"}
```

Note what this context is *for*. `principal_id` and `tenant_id` are trustworthy identity, so they are the right thing to scope, attribute, and audit against. They are not a permission check — Motet has no `has_permission()` helper. Roles live on the `Principal` at the API boundary; see [Authorization](#authorization) for where to enforce them.

## Tenant Isolation

### Data isolation is structural, not a convention

Isolation is not something you remember to do. Memory operations take **no
tenant argument at all**, so there is no parameter through which a command could
reach another tenant's data:

```python
# Scoping is implicit and unavoidable
motet.memory.store(content="data", tags=["tenant_data"])
# Scoped to motet.tenant_id, which came from the verified principal
```

Three layers hold that up:

1. **Identity comes from the token.** `tenant_id` is read from the authenticated
   `Principal` and carried on the command's distributed context. A `tenant_id`
   in a request body is never consulted, and the `X-Tenant-ID` header appears
   only in the CORS allowlist — nothing reads it as identity.
2. **The memory manager refuses ambiguity.** Every memory operation resolves
   identity from `MotetContext` and raises `ValueError` if `tenant_id`,
   `motet_id`, or `principal_id` is empty, rather than falling back to a
   process-wide default. There is no override parameter to pass.
3. **Keys are physically namespaced.** Records live under `{tenant_id}:mem:…`,
   so a cross-tenant read would require a different key prefix. On managed Redis
   this is backed by RBAC globs (`~{tenant_id}:*`), enforcing separation at the
   datastore rather than only in application code.

So the failure mode worth guarding against is not a malicious tenant argument —
there is nowhere to put one. It is a command running with **no** identity
context, such as a background job constructed outside a request. That is why the
manager hard-fails instead of defaulting, and why
`MOTET_TENANT_ENFORCE_MEMORY_FILTER=true` is worth setting on any multi-tenant
deployment: it requires `tenant_id` to be present on memory operations and
raises when it is missing, instead of letting the operation land on default
context and read a different key prefix than it wrote. It defaults to `false`
and covers both the memory commands and the `/api/v1/memories` endpoints. The
drift it prevents is silent, so nothing will tell you it was needed.

### HTTP API tenant fields

The HTTP API is the tenant security boundary. Workers trust the `tenant_id` stamped on the command, so a request body or query `tenant_id` is a **name**, not permission.

1. Identity comes from the authenticated principal, not the body or query.
2. A request `tenant_id`, `motet_id`, or `conversation_id` does not grant access.
3. Naming another tenant returns **403** unless the caller has global tenant scope. The API does not silently substitute the caller's tenant.

Omitted `tenant_id` means the caller's tenant only (including list endpoints). This applies to schedules, service accounts, deploy and skills catalogs, cost, vault, and workspace containers. The OAuth popup `tenant_id` query parameter is insecure-header simulation only and cannot override a real JWT.

### Tenant and Motet catalog

A **Motet** in this sense is a deployment environment under a tenant (for example `prod`, `staging`, or `dev`) — not the Motet product name alone. Request identity still comes from JWT / service-account claims (`tenant_id`, `motet_id`). Separately, Motet keeps an **operator-managed catalog** of which tenants and Motets exist, used by the manage-app scope selector and admin tooling.

| Concern | Source |
|---------|--------|
| Who is calling (identity) | JWT / service account / headers |
| Which tenants & Motets appear in ops UI | Catalog API (`/api/v1/tenants`) |

```bash
# Seed local/dev defaults (motet-global, default, demo + Motets)
motet-cli tenants ensure-defaults

# Create and inspect
motet-cli tenants create acme --name "Acme Corp"
motet-cli tenants motets create acme prod --name Production
motet-cli tenants list --include-motets
```

- Mutations require an admin role (`admin` or `motet-admin`).
- Non-admins may list/get only their own tenant and its Motets.
- Prefer `status=disabled` to hide an entry from the scope selector without deleting it.
- Deleting a tenant fails while Motets remain unless you pass `--force` (CLI) or `?force=true` (API).
- The catalog does **not** replace JWT tenant remapping (`MOTET_TENANT_ID_MAP_JSON`) or global-tenant allowlists (`MOTET_TENANT_GLOBAL_IDS`).

#### `motet-global` (the platform tenant)

`motet-global` is the usual **platform/operator** tenant. It is seeded with the display name **Motet Platform**, because it is one tenant among many — not a synonym for “all tenants”:

| Layer | Behavior |
|-------|----------|
| Keycloak | Organization seeded by org bootstrap for operator users |
| Config | Listed in `MOTET_TENANT_GLOBAL_IDS` (e.g. `["motet-global"]`) so JWT resolution sets `tenant_scope=global` |
| Catalog | Included in `ensure-defaults` so the manage-app scope selector can target it |
| Cost | Platform/model cost bucket, selectable like any other tenant |

Keep three things separate:

| Concept | How it is expressed |
|---------|---------------------|
| A specific tenant | `tenant_id=acme`, or `tenant_id=motet-global` for platform activity |
| All tenants | The **All Tenants** option in the scope selector; `tenant_id=__all__` on APIs that aggregate |
| Global scope | A capability of the caller (`tenant_scope=global`, `admin`, or `motet-admin`) — it is never a selection |

Having `tenant_scope=global` lets admins **list the whole catalog** and aggregate across it; it does not by itself invent catalog rows — seed or create `motet-global` (and other tenants) explicitly. Passing `tenant_id=__all__` without global scope simply returns your own tenant.

See [API Reference](./28-api-reference.md#tenants-and-motets-catalog-api) and [CLI Reference](./37-motet-cli-reference.md).

## Authentication

Motet supports multiple authentication methods with a hybrid approach.

### JWT Authentication (Production)

JWT authentication provides cryptographically verified identity:

```bash
# JWT token in Authorization header
Authorization: Bearer <jwt_token>

# Token structure:
{
  "iss": "https://auth.example.com",      # Issuer (Keycloak, Auth0, etc.)
  "sub": "user-abc-123",                  # Principal ID (verified)
  "aud": "motet-ai-stack",               # Audience
  "exp": 1732147200,                       # Expiration
  "iat": 1732143600,                       # Issued at
  "tenant_id": "tenant-001",              # Tenant ID (verified)
  "roles": ["user", "admin"],              # Principal roles
  "email": "alice@example.com",           # User email
  "name": "Alice Smith"                   # User name
}
```

**Configuration**:
```bash
# JWKS URL (Keycloak, Auth0, etc.)
export MOTET_JWT_JWKS_URL=https://auth.example.com/.well-known/jwks.json

# JWT validation settings
export MOTET_JWT_JWKS_CACHE_TTL_SECONDS=300    # Cache JWKS for 5 minutes
export MOTET_JWT_ALG_ALLOWLIST=RS256,HS256     # Allowed algorithms
export MOTET_JWT_LEEWAY_SECONDS=0              # Clock skew tolerance
export MOTET_JWT_ISSUER=https://auth.example.com
export MOTET_JWT_AUDIENCE=motet-ai-stack

# Identity mapping
export MOTET_JWT_SUB_CLAIM=sub                 # Subject claim name
export MOTET_JWT_ROLES_CLAIM=roles             # Roles claim name
export MOTET_JWT_TENANT_CLAIMS=tid,org,tenant,tenant_id,org_id  # Tenant claim names
export MOTET_DEPLOYMENT_ENVIRONMENT=development
export MOTET_AUTH_FAILURE_LIMIT_PER_MINUTE=10
export MOTET_AUTH_FAILURE_WINDOW_SECONDS=60
export MOTET_SECURITY_HEADERS_ENABLED=true
```

**How it works**:
1. **Token Verification**: JWT signature verified using JWKS
2. **Claim Extraction**: Principal, tenant, roles extracted from verified claims
3. **Context Propagation**: Claims propagated to `MotetContext`
4. **Access Control**: Authorization based on verified claims

### Keycloak Integration

Keycloak is the recommended identity provider:

```bash
# Keycloak configuration
export MOTET_KEYCLOAK_CLIENT_ID=motet-client
export MOTET_KEYCLOAK_PUBLIC_URL=http://localhost:8080
export MOTET_JWT_JWKS_URL=http://localhost:8080/realms/motet/.well-known/jwks.json
export MOTET_JWT_ISSUER=http://localhost:8080/realms/motet
```

**Keycloak Setup**:
1. **Install Keycloak**: Use Docker Compose or standalone
2. **Create Realm**: Create "motet" realm
3. **Configure Client**: Create OAuth client
4. **Set Up Mappers**: Configure organization/tenant mappers
5. **Test Authentication**: Verify JWT issuance

**Keycloak Features**:
- **Organization Hierarchy**: Support for multi-level organizations
- **User Federation**: LDAP, Active Directory integration
- **Social Login**: Google, GitHub, etc.
- **MFA Support**: Multi-factor authentication
- **Session Management**: Centralized session control

### OAuth login flow (browser and web apps)

For web UIs (e.g. Chat Explorer), users sign in via OAuth and receive a JWT. The Auth API provides the endpoints:

1. **Start login**: Redirect the user to `GET /api/v1/auth/login`. Optionally pass `redirect_uri` so after login the user is sent to your app (e.g. `https://myapp.example.com`). The server redirects to the identity provider (Keycloak) with PKCE and state.
2. **Callback**: After the user signs in, the IdP redirects to `GET /api/v1/auth/callback?code=...&state=...`. The server exchanges the code for tokens and returns an HTML page that passes the JWT to your frontend (e.g. via `postMessage` or redirect). Your app stores the token and uses it for API calls.
3. **API calls**: Send `Authorization: Bearer <jwt_token>` on every request to protected endpoints (chat, conversations, artifacts, etc.).
4. **Refresh**: When the access token expires, call `POST /api/v1/auth/refresh` with the current (possibly expired) JWT. The server uses the stored refresh token to return new tokens.
5. **Logout**: Call `GET /api/v1/auth/logout` to clear the server-side session. For IdP logout (single sign-out), use `GET /api/v1/auth/identity-provider-logout` with the same query params your IdP expects.

**Requirements**: JWT must be configured (`MOTET_JWT_JWKS_URL`, Keycloak client, etc.). Chat Explorer implements this flow; see its auth handling for a reference.

### Auth and OAuth API endpoints

| Endpoint | Purpose |
|----------|---------|
| `GET /api/v1/auth/login` | Start OAuth login (redirects to IdP). Query: `redirect_uri` (optional). |
| `GET /api/v1/auth/callback` | OAuth callback (IdP redirects here with `code` and `state`). Returns HTML that delivers JWT to the client. |
| `POST /api/v1/auth/refresh` | Refresh JWT using stored refresh token. Send current JWT in `Authorization` header. |
| `GET /api/v1/auth/logout` | Log out and clear server-side session. |
| `GET /api/v1/auth/identity-provider-logout` | Initiate IdP logout (single sign-out). |
| `GET /api/v1/oauth/*` | OAuth proxy for **MCP tools** (e.g. tool needs to call a third-party API with user OAuth). Not used for user login. |

For programmatic or script access, use **JWT** (from your IdP) or **service accounts** (`X-API-Key: sa_...`) instead of the login flow.

### Service Accounts

Service accounts provide long-lived tokens for automation:

```http
GET /api/v1/conversations HTTP/1.1
X-API-Key: sa_<service_account_key>
```

A service account carries a principal ID (prefixed `sa_`), a tenant ID, roles,
scopes, and a TTL, so requests made with its key are subject to the same
tenant isolation and role checks as a human principal.

**Creating Service Accounts**:
```python
from motet.core.security.service_accounts import ServiceAccountManager
from motet.core.distributed.redis_manager import get_sync_redis_client

# Create service account
redis_client = get_sync_redis_client("service_accounts")
sa_manager = ServiceAccountManager(redis_client)

token = sa_manager.create_service_account(
    name="automation-bot",
    tenant_id="tenant-001",
    motet_id="production",  # Required: motet/environment identifier
    roles=["automation"],
    created_by="user@example.com",
    expires_days=365
)

# Returns token string (format: sa_*)
# Example: "sa_20251126_abc123xyz_automation-bot"
```

**Service Account Management**:
```bash
# Create via CLI
motet-cli service-account create \
    --name automation-bot \
    --tenant tenant-001 \
    --motet production \
    --roles automation \
    --expires-days 365 \
    --store

# Create a token scoped for the OpenAI-compatible API (deny-by-default models)
motet-cli service-account create \
    --name cursor-desktop \
    --tenant tenant-001 \
    --motet production \
    --roles member \
    --facade-mode passthrough \
    --allowed-models openai/gpt-4o-mini

# List service accounts
motet-cli service-account list

# List filtered by tenant/motet
motet-cli service-account list --tenant tenant-001 --motet production

# Revoke service account
motet-cli service-account revoke sa_abc123...
```

### Hybrid Authentication Mode

Motet supports both JWT and header-based authentication:

```bash
# Production: JWT only
export MOTET_JWT_JWKS_URL=https://auth.example.com/.well-known/jwks.json
export MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS=false  # Default

# Development: Headers allowed
export MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS=true
```

**Authentication priority**, in the order `extract_principal` actually tries them:

1. **`Authorization: Bearer sa_...`** — a service account token, verified against
   the service account store. The `sa_` prefix is what distinguishes it.
2. **`Authorization: Bearer <jwt>`** — any other bearer token is treated as a JWT
   and its signature is verified.
3. **`X-API-Key`** — when a static `api_key` is configured and the header matches
   it, the request authenticates as a synthetic principal.
4. **`X-Principal-Id` / `X-Tenant-Id`** — only when
   `MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS` is on.
5. **Reject** — no principal, so the endpoint returns 401.

Two behaviors matter more than the order:

**A failed JWT does not fall through.** If JWT verification is configured and the
presented token does not verify, extraction stops and returns no principal — it
does not continue on to the API key or the header path. Without that, presenting
a deliberately invalid token would be a way to downgrade to whatever weaker
method is still enabled.

**A principal without a tenant is rejected**, unless insecure headers are on.
That closes the gap where an identity with no tenant would other­wise land on
default context and read a different key namespace than it wrote.

### The insecure-header flag fails closed

`MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS=true` is not merely discouraged outside
development — it **refuses to boot**. `validate_insecure_principal_header_policy`
runs once at startup and raises when the flag is on outside a local or test
environment:

```
RuntimeError: MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS is only allowed in
local/test environments unless MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS_IN_NON_DEV=true
```

So the checklist item below is a belt-and-braces check rather than the only thing
standing between you and header-spoofable auth in production. Environment is read
from `MOTET_DEPLOYMENT_ENVIRONMENT`. The escape hatch,
`MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS_IN_NON_DEV=true`, exists for staging
environments that are not marked local; it logs a warning every time it is used,
and it should not be set in production.

Two more protections apply at the HTTP boundary, both on by default:

**Failed authentication is rate limited.** After 10 failures in 60 seconds the
response becomes `429` with a `Retry-After` header instead of `401`. The counter
is keyed on auth type, client IP, and a hash of the presented token, so one
client hammering a bad token cannot lock out a different client or a different
token from the same address. Tune with `MOTET_AUTH_FAILURE_LIMIT_PER_MINUTE` and
`MOTET_AUTH_FAILURE_WINDOW_SECONDS`.

**Security headers are sent by default.** `MOTET_SECURITY_HEADERS_ENABLED`
defaults to true and adds CSP plus the baseline set to browser and API responses.

## Authorization

### Role-Based Access Control

Roles live on the **`Principal`**, which is resolved at the API boundary from the request's token. That is where role checks belong:

```python
from motet.interfaces.api.shared.auth import get_current_principal
from motet.core.types import Principal

@router.post("/admin/rebuild")
async def rebuild(principal: Principal = Depends(get_current_principal)):
    if "admin" not in principal.roles:
        raise HTTPException(status_code=403, detail="Admin role required")
    ...
```

### Command-Level Authorization

`MotetContext` carries **identity** (`motet.principal_id`, `motet.tenant_id`) but **not roles** — a command runs on a worker, detached from the request that authorized it. So a command that needs to make a role decision must be handed the roles it should decide on, as ordinary command data:

```python
class AdminData(BaseCommandData):
    principal_roles: List[str] = Field(
        default_factory=list,
        description="Roles of the calling principal, resolved at the API boundary",
    )

@motet.command()
def admin_command(data: AdminData, motet: MotetContext) -> Dict[str, Any]:
    if "admin" not in data.principal_roles:
        raise PermissionError("Admin role required")
    return {"result": "success"}
```

This is the pattern the built-in commands use — `agent_list` takes `principal_roles` and filters visible agents against it rather than reading roles from context.

Because the caller supplies the roles, a command-level check is **defense in depth, not the gate**. Anything that must not be bypassed belongs at the API boundary, where the token is verified. Tenant isolation is the exception: `motet.tenant_id` comes from the verified context and is applied by the runtime, so scoping does not depend on the caller being honest.

## Getting It Right

Four rules, each paired with the mistake it prevents. Tenant isolation is absent
from this list on purpose — it is structural, covered above, and not something
you can get wrong by forgetting.

### Use verified identity, never client-supplied identity

```python
# ✅ From the verified token, via context
principal_id = motet.principal_id

# ❌ From the request body — trivially spoofed
principal_id = request.json["principal_id"]
```

### Validate input with a typed model

Unvalidated paths and identifiers are where injection and traversal get in.
Declaring the shape is also the cheapest validation you will ever write:

```python
from motet.core.commands.base_command_data import BaseCommandData
from pydantic import Field

class FileData(BaseCommandData):
    file_path: str = Field(..., pattern="^/allowed/")

# ❌ Path traversal: read_file(request.json["file_path"])
# ✅ Validated before it reaches the filesystem
data = FileData(**request.json)
```

### Gate on roles at the API boundary

The boundary is where the token is verified, so it is the only place a role check
is a *gate*. A command re-check is worth having, but it catches an internal
caller that composed the command wrongly — it is not the thing stopping an
attacker, because `principal_roles` arrives as data:

```python
# ✅ The gate — at the boundary, against the verified principal
if "admin" not in principal.roles:
    raise HTTPException(status_code=403, detail="Admin role required")

# ✅ Defense in depth — inside the command
@motet.command()
def sensitive_operation(data: SensitiveData, motet: MotetContext):
    if "admin" not in data.principal_roles:
        raise PermissionError("Admin role required")
    return perform_sensitive_operation(data)
```

### Read credentials from the vault, never from source

```python
# ❌ Committed to the repository, and now in every clone and every backup
api_key = "sk-abc123"

# ✅ Tenant and principal come from the command context
api_key = motet.vault.get_api_key("openai", motet.distributed_context)
```

## Encryption at Rest

Motet encrypts sensitive data at rest in Redis using a phased encryption strategy.

### Why Encryption Matters

**Without encryption**, Redis compromise exposes all data:

```bash
# Attacker gains Redis access
redis-cli GET "acme:cmd:data:abc123"
# Returns: {"api_key": "sk-secret-xyz", "user_data": {...}}
```

**With encryption**, data is protected even if Redis is compromised:

```bash
redis-cli GET "acme:cmd:data:abc123"
# Returns: {"encrypted": true, "encrypted_data": "...", "dek": {...}}
# Useless without tenant encryption key
```

### Encryption Architecture

Motet uses **envelope encryption** for maximum security:

1. **Data Encryption Key (DEK)**: Unique random key per command
2. **Key Encryption Key (KEK)**: Tenant-specific master key in vault
3. **DEK wraps data**: AES-256-GCM encrypts command data with DEK
4. **KEK wraps DEK**: Tenant KEK encrypts the DEK before storage

```python
# Automatic encryption - no code changes needed!
@motet.command()
def process_sensitive_data(data: MyData, motet: MotetContext):
    # Data automatically encrypted before Redis storage
    # Decrypted automatically when read
    return {"result": "success"}
```

### What Gets Encrypted

| Data Type | Encryption Status | Notes |
|-----------|------------------|-------|
| Vault credentials | ✅ Always encrypted (AES-256) | Highest security |
| Command data | ✅ Encrypted with envelope | Contains API keys, PII |
| Command results | ✅ Encrypted with envelope | May contain sensitive data |
| Memory items | ✅ Encrypted with envelope | User conversations, content |
| Schedule data | ✅ Encrypted with envelope | Scheduled command payloads |
| Command metadata | ⚠️ Plaintext | Non-sensitive: timestamps, IDs |

### Tenant Isolation Through Encryption

Each tenant has a separate encryption key:

```python
# Tenant A's data encrypted with Tenant A's key
tenant_a_key = vault.get_encryption_key("tenant-a")

# Tenant B CANNOT decrypt Tenant A's data
# Even if Tenant B compromises Redis
```

**Benefits**:
- ✅ Tenant key compromise doesn't affect other tenants
- ✅ Per-tenant key rotation possible
- ✅ Cryptographic isolation (not just access control)

### Key Management

#### Automatic Key Generation

Encryption keys are automatically generated for new tenants:

```python
# First command for tenant creates key
result = motet.do(my_command, data=MyData(value="test"))
# Vault automatically generates tenant encryption key
# Key cached in memory for performance
```

#### Key Storage

Each tenant's key lives in the vault under `encryption:tenant:{tenant_id}`, as a
base64 `key` field holding 32 bytes for AES-256. `EncryptionService` caches it in
memory after first use, and a mismatch in length is rejected rather than
truncated.

In local worker mode the key is fetched from the cloud vault's resolve endpoint
over the WireGuard tunnel rather than read directly, and a local worker may only
request its own tenant's key — `MOTET_EDGE_TENANT_ID` is enforced on the way in.

#### Key Rotation

**There is no key rotation today.** `EncryptionService` exposes `encrypt`,
`decrypt`, `get_tenant_key`, `wrap_key`, `unwrap_key`, and `clear_key_cache` —
there is no rotate operation and no key versioning, so stored data is encrypted
under a single per-tenant key with no second version to decrypt against.

Plan around it: rotating a tenant key today means re-encrypting that tenant's
data yourself, and there is no built-in window in which both the old and new key
work. If rotation is a compliance requirement for your deployment, treat it as
work to schedule rather than a setting to switch on.

### Performance Impact

Encryption is highly optimized:

- **Encryption overhead**: < 1ms per command
- **Key caching**: Keys cached in memory (no vault lookup per command)
- **Batch operations**: Multiple encryptions parallelized

```python
# Performance metrics automatically tracked
{
    "dek_gen_time_ms": 0.2,      # Generate DEK
    "encryption_time_ms": 0.5,    # Encrypt data
    "dek_wrap_time_ms": 0.3,      # Wrap DEK with KEK
    "total_time_ms": 1.0          # Total overhead
}
```

### Compliance

Encryption at rest helps meet compliance requirements:

- ✅ **GDPR**: Article 32 (Security of processing)
- ✅ **HIPAA**: 164.312(a)(2)(iv) (Encryption and decryption)
- ✅ **SOC 2**: CC6.7 (Encryption of data at rest)
- ✅ **ISO 27001**: A.10.1.1 (Cryptographic controls)
- ✅ **PCI DSS**: 3.4 (Render PAN unreadable)

## Transport Security (TLS)

### Redis TLS

TLS is enabled by the **URL scheme**, not a separate flag. Use `rediss://`
and Motet applies SSL parameters to the connection pool; with plain
`redis://` the SSL settings below are ignored entirely.

```bash
# Enable Redis TLS by using the rediss:// scheme
export MOTET_REDIS_URL=rediss://redis.example.com:6380/0

# Optional: CA bundle for verifying the server certificate
export MOTET_REDIS_CA_CERT=/path/to/ca.pem

# Certificate validation: NONE | OPTIONAL | REQUIRED
export MOTET_REDIS_SSL_CERT_REQS=REQUIRED
```

Setting `MOTET_REDIS_SSL_CERT_REQS=NONE` also disables hostname checking, which
is intended for self-signed certificates in development — never production.

### HTTP API TLS

Always use HTTPS in production:

```bash
# ✅ CORRECT: HTTPS
curl https://motet.example.com/api/v1/chat

# ❌ WRONG: HTTP (unencrypted)
curl http://motet.example.com/api/v1/chat
```

**Reverse Proxy Configuration** (nginx):

```nginx
server {
    listen 443 ssl http2;
    server_name motet.example.com;
    
    # TLS certificates
    ssl_certificate /etc/ssl/certs/motet.crt;
    ssl_certificate_key /etc/ssl/private/motet.key;
    
    # Strong TLS configuration
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers 'ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256';
    ssl_prefer_server_ciphers off;
    
    # HSTS header
    add_header Strict-Transport-Security "max-age=31536000" always;
    
    location / {
        proxy_pass http://localhost:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

### mTLS (Mutual TLS)

Motet has **no built-in mTLS layer** and reads no mTLS environment variables.
Terminate mutual TLS at the infrastructure boundary instead:

- **HTTP API**: require client certificates at the reverse proxy (nginx
  `ssl_client_certificate` + `ssl_verify_client on`) or your load balancer.
- **Redis**: use a `rediss://` URL with a managed Redis that enforces client
  certificates, as shown under [Redis TLS](#redis-tls).
- **Service mesh**: let the mesh (Istio, Linkerd) handle peer authentication.

Motet authenticates callers with JWT bearer tokens; mTLS is a transport
control layered underneath, not a replacement for it.

## Network Security

### Firewall Rules

Restrict network access to Motet services:

```bash
# Allow only necessary ports
iptables -A INPUT -p tcp --dport 8000 -j ACCEPT  # HTTP API
iptables -A INPUT -p tcp --dport 6379 -j DROP    # Block external Redis
iptables -A INPUT -p tcp --dport 5672 -j DROP    # Block external RabbitMQ

# Allow internal network only
iptables -A INPUT -s 10.0.0.0/8 -p tcp --dport 6379 -j ACCEPT
```

### VPC/Network Isolation

Deploy Motet in isolated network:

```yaml
# AWS VPC example
VPC:
  CIDR: 10.0.0.0/16
  
  PublicSubnet:
    - CIDR: 10.0.1.0/24
      Resources: [LoadBalancer, BastionHost]
  
  PrivateSubnet:
    - CIDR: 10.0.10.0/24
      Resources: [Workers, Redis]
  
  SecurityGroups:
    LoadBalancer:
      Ingress: [443/tcp from 0.0.0.0/0]
      Egress: [8000/tcp to Workers]
    
    Workers:
      Ingress: [8000/tcp from LoadBalancer]
      Egress: [6379/tcp to Redis, 5672/tcp to RabbitMQ]
    
    Redis:
      Ingress: [6379/tcp from Workers only]
      Egress: [none]
```

### Service Mesh

For advanced deployments, use service mesh:

```yaml
# Istio example
apiVersion: security.istio.io/v1beta1
kind: PeerAuthentication
metadata:
  name: motet-mtls
spec:
  mtls:
    mode: STRICT  # Require mTLS for all traffic

---
apiVersion: security.istio.io/v1beta1
kind: AuthorizationPolicy
metadata:
  name: motet-authz
spec:
  rules:
  - from:
    - source:
        principals: ["cluster.local/ns/motet/sa/worker"]
    to:
    - operation:
        methods: ["POST"]
        paths: ["/api/v1/*"]
```

## Secrets Management

### Distributed Vault

Motet's distributed vault stores credentials securely. For **REST API** usage (UIs, scripts, MCP environment), see [API Reference — Vault](./28-api-reference.md#vault-api).

Inside a command, reach the vault through `motet.vault`. You never pass a
tenant id: the vault derives tenant and principal from the command context,
which is what makes isolation automatic rather than a caller responsibility.

```python
from motet.core.security.vault_service import CredentialType

@motet.command()
def use_provider_key(data: MyData, motet: MotetContext) -> Dict[str, Any]:
    context = motet.distributed_context

    # Store credential (encrypted at rest, scoped to this tenant/principal)
    motet.vault.store_credential(
        credential_key="openai_api_key",
        credential_data={"api_key": "sk-secret-xyz"},
        context=context,
        credential_type=CredentialType.API_KEY,
    )

    # Retrieve credential (decrypted on read); returns None when absent
    credential = motet.vault.get_credential("openai_api_key", context)
    api_key = (credential or {}).get("api_key")

    # Convenience for the common provider-key case
    api_key = motet.vault.get_api_key("openai", context)

    return {"has_key": bool(api_key)}
```

`get_credential` returns the stored **dict**, not a bare string, so read the
field you stored. `store_credential` returns a bool indicating success.

**Security Features**:
- ✅ AES-256-GCM encryption
- ✅ Tenant isolation
- ✅ Access logging
- ✅ Credential rotation
- ✅ TTL expiration

### Vault Configuration

The vault has a single backend — Motet's own encrypted store — and there is no
pluggable backend selector for AWS Secrets Manager, HashiCorp Vault, or Google
Secret Manager. It is configured with:

```bash
export MOTET_VAULT_ENABLED=true
export MOTET_VAULT_MASTER_KEY=<32-byte base64 key>   # required; never auto-generated
export MOTET_VAULT_SALT=<salt>
export MOTET_VAULT_TIMEOUT_SECONDS=10
```

Motet refuses to start the vault service without `MOTET_VAULT_MASTER_KEY`
rather than generating one, so that credentials never become unreadable after
a restart. To source that key from an external secret manager, inject it as an
environment variable at deploy time.

### Environment Variable Security

**Never** commit secrets to code:

```python
# ✅ CORRECT: Use environment variables or the vault
api_key = os.getenv("OPENAI_API_KEY")
# OR, inside a command, via the context helper
api_key = motet.vault.get_api_key("openai", motet.distributed_context)

# ❌ WRONG: Hardcoded secret
api_key = "sk-secret-xyz"  # NEVER DO THIS!
```

**Use secret scanning**:

```bash
# Pre-commit hook to detect secrets
pip install detect-secrets
detect-secrets scan --baseline .secrets.baseline
```

## Security Monitoring

### What the runtime already logs

Authentication failures are logged for you, with the fields you need to chase
them, so this is not something you have to add:

```
Authentication failed     auth_type=jwt client_ip=… token_fingerprint=… reason=jwt_verification_failed
Authentication throttled  auth_type=jwt client_ip=… token_fingerprint=… reason=…
```

The token is recorded as a truncated SHA-256 fingerprint rather than the token
itself, which is what lets you correlate repeated attempts without putting
credentials in your logs. `reason` distinguishes a bad signature from an expired
token from a malformed header.

For your own security-relevant events, use structured logging with the same
identifiers so they join up with the above:

```python
import structlog

logger = structlog.get_logger(__name__)

logger.warning(
    "authorization_denied",
    principal_id=principal_id,
    tenant_id=tenant_id,
    resource=resource_id,
    action=action,
)
```

### Metrics

**Authentication is the only part of this page with metrics.** Two exist, both
on the `motet_` prefix:

| Metric | Labels | Notes |
|--------|--------|-------|
| `motet_auth_attempts_total` | `auth_type`, `status` | `auth_type` is `jwt`, `service_account`, `header`, `none` or `error`; `status` is `success` or `failure` |
| `motet_auth_latency_seconds` | `auth_type` | Histogram of verification time |

There are **no vault, encryption, or authorization metrics**. Access to a
credential and a failed role check are visible in logs but are not counted, so
alerting on them means log-based alerting rather than Prometheus.

A failed-authentication alert against the metric that exists:

```yaml
groups:
- name: security
  rules:
  - alert: HighFailedAuthRate
    expr: rate(motet_auth_attempts_total{status="failure"}[5m]) > 10
    annotations:
      summary: "High failed authentication rate"
      description: "{{ $value }} failed auth/sec over 5 minutes"

  - alert: HeaderAuthInProduction
    expr: increase(motet_auth_attempts_total{auth_type="header"}[5m]) > 0
    annotations:
      summary: "Insecure header authentication used"
      description: "Header auth should not succeed in production"
```

The second one is worth having: header auth succeeding in production means
`MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS` is on somewhere it should not be, and
the metric will show it even if the startup guard was bypassed with the non-dev
override.

## Security Configuration Checklist

### Production Checklist

**Authentication & Authorization**:
- [ ] JWT authentication enabled (`MOTET_JWT_JWKS_URL`)
- [ ] `MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS=false`, and
      `..._IN_NON_DEV` **not** set
- [ ] JWT issuer and audience validated (`MOTET_JWT_ISSUER`, `MOTET_JWT_AUDIENCE`)
- [ ] `MOTET_JWT_ALG_ALLOWLIST` restricted to the algorithms you actually issue
- [ ] `MOTET_TENANT_ENFORCE_MEMORY_FILTER=true` on any multi-tenant deployment
- [ ] Service accounts used for automation, with least-privilege roles
- [ ] Static `X-API-Key` unset unless something genuinely needs it

> `MOTET_MULTI_TENANT_MODE` is declared but read by no runtime code, so setting
> it to `enforced` does nothing. Tenant isolation comes from the verified
> principal and key namespacing, not from that flag.

**Encryption at Rest**:
- [ ] `MOTET_VAULT_MASTER_KEY` supplied and secured
- [ ] `MOTET_ALLOW_EPHEMERAL_MASTER_KEY` **not** set in production
- [ ] Tenant keys resolvable for every tenant you serve
- [ ] Rotation plan owned **outside** Motet — there is no rotation support, so
      rotating means re-encrypting that tenant's data yourself

> The vault refuses to auto-generate a master key: without
> `MOTET_VAULT_MASTER_KEY` it raises unless `MOTET_ALLOW_EPHEMERAL_MASTER_KEY=true`.
> An ephemeral key is lost on restart, which means every credential encrypted
> under it becomes unreadable — fine for a test run, silent data loss in
> production.

**Transport Security**:
- [ ] HTTPS enforced (reverse proxy with TLS)
- [ ] Redis TLS enabled
- [ ] Certificate validation enabled
- [ ] Strong TLS protocols only (TLS 1.2+)
- [ ] HSTS header configured

**Network Security**:
- [ ] Firewall rules configured
- [ ] VPC/network isolation enabled
- [ ] Redis not publicly accessible
- [ ] Internal services on private network

**Secrets Management**:
- [ ] Vault master key secured
- [ ] Environment variables protected
- [ ] No hardcoded secrets in code
- [ ] Secret scanning enabled (pre-commit hooks)

**Monitoring & Tool Restrictions**:
- [ ] `MOTET_FILE_READ_ALLOWLIST` set to the directories tools may read
- [ ] `MOTET_HTTP_TOOL_ALLOW_DOMAINS` / `..._DENY_DOMAINS` configured
- [ ] Alerting on `motet_auth_attempts_total{status="failure"}`
- [ ] Alerting on `motet_auth_attempts_total{auth_type="header"}` — should be zero
      in production
- [ ] Log aggregation captures `Authentication failed` / `Authentication
      throttled` events, since vault and authorization have no metrics

### Development Checklist

- [ ] Headers allowed for local development
- [ ] Test service accounts created
- [ ] Local Keycloak configured (optional)
- [ ] Security testing performed
- [ ] Input validation tested
- [ ] Authorization checks verified

## Next Steps

- **[Observability & Debugging](./23-observability-debugging.md)** - Learn debugging
- **[Advanced Motet Concepts](./24-advanced-concepts.md)** - Advanced concepts
- **[Best Practices](./27-best-practices.md)** - Learn from experience

## Navigation

- **[← Back to Documentation Home](./00-landing-page.md)** - Main documentation hub

---

**Last Updated**: 2026-08-29
