## Package: motet_sdk.docker

**Compose files distributed with the SDK** for **`motet-cli local`** and developer bring-up (not the full production stack from the main Motet repository).

### Contents

- **`docker-compose.developer.yml`**: Developer-oriented service set the CLI can launch when no override is provided (see **`motet-cli local`** help and **`MOTET_COMPOSE_FILE`**).

### Notes

Production and integration-test Compose live in the **Motet** repo (`docker-compose*.yml`, `tests/docker-compose.test.yml`). Prefer those for CI and multi-service parity.

### Related

- **`motet-sdk/src/motet_sdk/cli/README.md`**
- Top-level SDK docker folder: **`motet-sdk/docker/README.md`**
