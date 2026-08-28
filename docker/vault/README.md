# Vault Initialization

This directory contains scripts for initializing the distributed vault service in the Motet.

## Files

- **`vault_init.py`** - Script to initialize the vault with default credentials and configuration

## Usage

### Automatic Initialization (Docker)

The vault is automatically initialized when Docker Compose starts via the `vault-init` service:

```bash
docker-compose -f docker-compose.distributed.yml up -d
```

Check initialization logs:
```bash
docker-compose -f docker-compose.distributed.yml logs vault-init
```

### Manual Initialization

```bash
# From within a Docker container
docker-compose -f docker-compose.distributed.yml exec motet-api python docker/vault/vault_init.py

# With environment file
docker-compose -f docker-compose.distributed.yml exec motet-api python docker/vault/vault_init.py --env-file .env

# Dry run (preview changes without applying)
docker-compose -f docker-compose.distributed.yml exec motet-api python docker/vault/vault_init.py --dry-run

# Verify only (check if vault is initialized)
docker-compose -f docker-compose.distributed.yml exec motet-api python docker/vault/vault_init.py --verify-only
```

## What It Initializes

The script initializes:

1. **Default API Keys** (from environment variables):
   - OpenAI API key (`OPENAI_API_KEY`)
   - Anthropic API key (`ANTHROPIC_API_KEY`)
   - Google API key (`GOOGLE_API_KEY`)

2. **MCP Server Credentials**:
   - Google Workspace OAuth tokens
   - Google Service Account keys
   - GitHub Personal Access Token
   - AccuWeather API key

3. **System Configuration**:
   - Vault initialization metadata
   - System version and environment info

## Environment Variables

The script reads credentials from environment variables. See `docker-compose.distributed.yml` for the full list of supported variables.

## Integration

The vault initialization is integrated into Docker Compose:

- **Service**: `vault-init` runs once during startup
- **Dependency**: `motet-api` service waits for `vault-init` to complete
- **Restart Policy**: `no` (runs once and exits)

## Related Documentation

- [Vault Docker Setup Guide](../../docs/operations/VAULT_DOCKER_SETUP_GUIDE.md)
- [Distributed Vault Service](../../motet/core/security/vault_service.py)

