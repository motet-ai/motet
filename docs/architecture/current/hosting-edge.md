# Hosting / edge

Hosting is **`motet-host`**: a standalone deployer for AWS accounts. There is no launcher service. Local development is Docker Compose (`motet-cli local up`); EC2 / AWS uses published images, not a Motet bind-mount.

## Accounts and env

- Per-account files: `.env.<account_id>.aws` plus stack overrides
- Per-account DNS
- Switching accounts is an env/file choice, not a second product

Backup and restore are engine-native (RDS and ElastiCache snapshots) via `motet-host backup` / `restore`. Create keeps 7-day retention. Teardown takes final snapshots.

## Images

Motet-bearing app images split **deps** (apt/pip/Playwright/torch/Vite) from a **thin runtime** (`COPY motet` only).

- Local compose: leave `DEPS_IMAGE` / `FRONTEND_IMAGE` unset (inline stages). `.:/app` mounts still override baked source.
- EC2: publish content-addressed deps tags, then thin `{prefix}/<service>:<MOTET_IMAGE_TAG>` (also aliased `:latest`).
- Convenience images people `docker pull` for local `up` are the **product version** (`vX.Y.Z`). `latest` is an alias of the last published snapshot. Do not publish a new public image from every `main` commit.

`ARG DEPS_IMAGE` and `ARG FRONTEND_IMAGE` must be declared before the first `FROM`. Do not `COPY --from=${VAR}` — alias the stage first.

## Edge

Edge workers attach a registered machine to a remote deployment (`motet-cli device`). They share Valkey through a WireGuard tunnel rather than a second datastore.

- Host-bound tools run on the device worker
- App-builder / identity-scoped edge workers exist for multi-app isolation
- Location-aware routing as a product policy is not shipped (see [workers-redis.md](./workers-redis.md))

## Paths

- Deployer: `hosting/motet-host/`, `hosting/aws/ec2/`
- Image split: `docker/images/*/Dockerfile`, `hosting/aws/ec2/py/deployer/images.py`
- Edge compose / devices API: device-worker compose and `/api/v1` devices routes
- Version identity of those images: [versioning-fsl.md](./versioning-fsl.md)
