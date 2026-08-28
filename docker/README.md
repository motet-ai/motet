# Docker assets

Supporting **Dockerfiles**, image build contexts, and service-specific dirs used from repository **Compose** files and CI.

## Subdirectories

Each major service often has its own README (for example **`keycloak/`**, **`redis/`**, **`vault/`**).

| Path | Typical role |
|------|----------------|
| `images/` | Base or service image definitions |
| `keycloak/` | Realm/import assets for authentication testing |
| `redis/`, `vault/` | Auxiliary service configs |
| `haproxy/`, `test-seeder/` | Routing or test bootstrap |

Use the Compose files at the repository root (`docker-compose*.yml`) as the canonical way to assemble these pieces.
