FROM motet-edge-worker:latest AS builder-worker

# ADR-0122 app-builder worker image.
#
# Layers the self-hosted development agent's dependencies on top of the edge
# device image (docker/images/edge-worker/Dockerfile). These are deliberately
# NOT in the shared edge-worker image: user devices don't need git, the docker
# CLI stack, or — critically — the safe.directory '*' loosening below.
#
# Build via app-builder (or deploy/app-builder.sh) build (ensures the base image exists).

# git for the agent working in its isolated clone at /srv/app-builder/imf.
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
 && rm -rf /var/lib/apt/lists/*

# docker CLI + compose + buildx so core.edge_exec (subprocess backend)
# can run the Docker test suite through the mounted /var/run/docker.sock.
# CLI only — no daemon. Buildx is required for test-runner Dockerfiles that use
# BuildKit ``RUN --mount=type=cache`` (Docker Desktop / compose builds).
COPY --from=docker:cli /usr/local/bin/docker /usr/local/bin/docker
# Pinned to the versions validated with the ADR-0122 test gate.
COPY --from=docker/compose-bin:v5.3.1 /docker-compose /usr/local/bin/docker-compose
COPY --from=docker/buildx-bin:0.35.0 /buildx /usr/local/lib/docker/cli-plugins/docker-buildx
RUN mkdir -p /usr/local/lib/docker/cli-plugins \
 && ln -sf /usr/local/bin/docker-compose /usr/local/lib/docker/cli-plugins/docker-compose \
 && chmod +x /usr/local/lib/docker/cli-plugins/docker-buildx

# The app-builder clone is bind-mounted with host ownership; without this
# git refuses all operations with "detected dubious ownership". Builder-only:
# do not add this to the shared local-worker image.
RUN git config --system --add safe.directory '*'
