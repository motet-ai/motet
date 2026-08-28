## Package: edge

**Edge-worker companion primitives** — device pairing, HTTP vault access, and MCP config filtering for agents running outside the central cluster but attached over a secured tunnel.

### Purpose

- **Device registry**: Track **`DeviceRecord`** instances, auth/session handles (**`DeviceAuthSession`**), and registry queries for provisioned laptops/edge hosts.
- **Secrets without cluster RPC**: **`HttpVaultClient`** supports constrained HTTP-oriented vault retrieval patterns suited to tunnel deployments.
- **Safe MCP configs**: **`mcp_config_filter`** trims or rewrites MCP server definitions so edge shells only see permitted endpoints.

### Core components

#### `device_registry.py`

**`EdgeDeviceRegistry`** and related models for enrolling and authenticating paired devices.
Register writes `motet:edge_device:token:{token}` (and worker / meta / lookup /
index locators) pointing at the tenant so verify and revoke do exact-key
`GET` / `HGETALL` without a keyspace SCAN.

#### `http_vault_client.py`

HTTP client shapes for Vault-style secret reads in edge worker topologies.

#### `mcp_config_filter.py`

Filter pipeline applied before MCP processes start on constrained devices.

### Related

- HTTP **`/api/v1/devices`** surface: **`motet/interfaces/api/v1/devices.py`**
- Compose stack: **`docker-compose.edge-worker.yml`**
- Image: **`docker/images/edge-worker/`** (`motet-edge-worker`)
