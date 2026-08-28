# Configuration Reference

Reference for Motet configuration options commonly used in development and deployment. Configuration is primarily via environment variables with the `MOTET_` prefix. Some worker/exec/MCP knobs are read directly from the environment; the authoritative field list for the Pydantic settings model is `motet/core/config.py`.

## Environment Variables

### Model Configuration

Registered providers and flagship ids: [Supported models](./03a-supported-models.md). Local key setup: [Quick Start](./04-quick-start-guide.md#model-api-key).

```bash
# Model Provider (stack default: OpenAI gpt-4o-mini; Kimi K3 via override)
MOTET_MODEL_PROVIDER=openai                  # mock|openai|anthropic|gemini|moonshot|deepseek|xai|meta|local
MOTET_MODEL_NAME=gpt-4o-mini                 # Model name
MOTET_MODEL_TIMEOUT_SECONDS=60               # Model timeout
MOTET_MODEL_MAX_RETRIES=2                    # Max retries
MOTET_MODEL_RETRY_BACKOFF_SECONDS=0.5        # Retry backoff

# Provider-Specific
MOTET_OPENAI_MODEL_NAME=gpt-4o-mini
MOTET_ANTHROPIC_MODEL_NAME=claude-3-5-sonnet-latest
MOTET_OPENAI_API_KEY=your-openai-key
MOTET_ANTHROPIC_API_KEY=your-anthropic-key
MOTET_GEMINI_API_KEY=your-gemini-key         # or GEMINI_API_KEY / GOOGLE_API_KEY
MOTET_MOONSHOT_API_KEY=your-moonshot-key     # or MOONSHOT_API_KEY (when using moonshot/kimi-k3)
MOTET_DEEPSEEK_API_KEY=your-deepseek-key     # or DEEPSEEK_API_KEY (deepseek-v4-flash / deepseek-v4-pro)
MOTET_XAI_API_KEY=your-xai-key               # or XAI_API_KEY
MOTET_META_API_KEY=your-meta-key             # or MODEL_API_KEY / META_API_KEY (muse-spark-1.2)

# Opt-in live adapter capability matrix (incurs provider spend).
# Default picks one canary per provider: newest release month, then cheapest input price.
# MOTET_LIVE_ADAPTER_MATRIX=1
# MOTET_LIVE_ADAPTER_CASES=deepseek:deepseek-v4-pro,openai:gpt-5.5

# Reasoning (Kimi K3 always thinks when selected; effort currently only max)
MOTET_ENABLE_THINKING=false
MOTET_REASONING_EFFORT=medium                # low|medium|high|xhigh|max
MOTET_TOKEN_BUDGET=5000                      # Token budget
```

### Memory Configuration

```bash
# Memory Backend
MOTET_ENABLE_MEMORY=true                      # Enable memory
MOTET_MEMORY_BACKEND=inmemory                # inmemory|redis
MOTET_REDIS_URL=redis://localhost:6379/0      # Redis URL
MOTET_MEMORY_RECENT_LIMIT=60                  # Recent memory limit
MOTET_MEMORY_TTL_SECONDS=                     # Memory TTL (optional)

# Memory Tags
MOTET_MEMORY_TAGS=                            # Comma-separated tags
MOTET_MEMORY_SHORT_TERM_TAG=stm               # Short-term memory tag
MOTET_MEMORY_LONG_TERM_TAG=ltm               # Long-term memory tag
MOTET_MEMORY_WORKING_TAG=wm                  # Working memory tag
MOTET_WORKING_MEMORY_RESET_EACH_TURN=true    # Reset working memory each turn

# Vector Memory (LTM uses Valkey Search)
MOTET_ENABLE_VECTOR_MEMORY=false             # Enable vector memory
MOTET_VECTOR_BACKEND=valkey                  # valkey (memory); chroma/pgvector for migrate-pgvector
MOTET_VECTOR_TOP_K=3                         # Top K results
MOTET_EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L12-v2
MOTET_EMBEDDING_TEXT_MODEL=                  # Text-embedding override; takes precedence over MOTET_EMBEDDING_MODEL when set

# Valkey Search (LTM index)
MOTET_MEMORY_VECTOR_VALKEY_INDEX=           # Index name (default in valkey_vector_store.py)
MOTET_MEMORY_VECTOR_VALKEY_PREFIX=          # Key prefix (default in valkey_vector_store.py)

# Unused for memory (migrate-pgvector CLI, tests)
MOTET_CHROMA_COLLECTION=imf_memories        # Chroma collection
MOTET_CHROMA_PERSIST_DIR=                   # Chroma persist directory
MOTET_PGVECTOR_DSN=postgresql://user:pass@host:5432/dbname
MOTET_PGVECTOR_TABLE=imf_embeddings

# Retrieval
MOTET_ENABLE_EMBEDDING_CACHE=true            # Enable embedding cache
MOTET_ENABLE_RESULT_CACHE=false             # Enable result cache
MOTET_RETRIEVAL_VECTOR_WEIGHT=0.7           # Vector weight
```

### Distributed System Configuration

```bash
# Redis
MOTET_REDIS_URL=redis://localhost:6379/0
MOTET_PURE_DISTRIBUTED_INVOKER_REDIS_URL=redis://localhost:6379/1
MOTET_VALKEY_CLIENT=redis                    # Application client (redis-py). Set glide to opt in; Celery stays on redis-py
# MOTET_VALKEY_GLIDE_TIMEOUT_MS=30000        # GLIDE per-command timeout (ms)
# MOTET_VALKEY_GLIDE_INFLIGHT=128            # Concurrent GLIDE commands per process

# Command Serialization
MOTET_REDIS_COMMAND_SIZE_THRESHOLD_BYTES=0   # 0=always use Redis
MOTET_REDIS_COMMAND_COMPLEX_OBJECT_THRESHOLD=5000

# Scheduler/Events
MOTET_SCHEDULER_MAX_CONCURRENT_TASKS=10
MOTET_EVENTS_ENABLED=true
```

### Authentication & Security

```bash
# API Authentication
MOTET_API_KEY=your-api-key                   # API key authentication

# JWT Authentication
MOTET_JWT_PUBLIC_KEY_PEM=                    # JWT public key (PEM)
MOTET_JWT_JWKS_URL=                          # JWKS URL
MOTET_JWT_JWKS_CACHE_TTL_SECONDS=300        # JWKS cache TTL
MOTET_JWT_ALG_ALLOWLIST=RS256,HS256         # Allowed algorithms
MOTET_JWT_LEEWAY_SECONDS=0                   # JWT leeway
MOTET_JWT_ISSUER=                            # JWT issuer
MOTET_JWT_AUDIENCE=                          # JWT audience

# Keycloak OAuth
MOTET_KEYCLOAK_CLIENT_ID=                    # OAuth client ID
MOTET_KEYCLOAK_PUBLIC_URL=http://localhost:8080  # Public Keycloak URL

# Identity Mapping
MOTET_JWT_SUB_CLAIM=sub                      # Subject claim
MOTET_JWT_ROLES_CLAIM=roles                 # Roles claim
MOTET_JWT_TENANT_CLAIMS=tid,org,tenant,tenant_id,org_id,organization  # Tenant claim names (fallback order)
MOTET_JWT_ORGANIZATION_CLAIM=organization             # Canonical organization claim
MOTET_JWT_MOTET_CLAIMS=motet_id,motet,environment,env,deployment  # Motet/environment claim keys
MOTET_DEPLOYMENT_ENVIRONMENT=development    # local|development|test|staging|production
MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS=false # Dev mode headers
MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS_IN_NON_DEV=false # Explicit non-dev escape hatch

# Multi-Tenancy
MOTET_TENANT_ID_MAP_JSON=                    # Tenant ID mapping (JSON)
MOTET_TENANT_GLOBAL_IDS=                     # Global tenant IDs
MOTET_MULTI_TENANT_MODE=soft                 # off|soft|enforced; declared in config but currently read by no runtime code
MOTET_TENANT_ENFORCE_MEMORY_FILTER=false     # Enforce memory filter
MOTET_TENANT_ENFORCE_TRACE_FILTER=false      # Enforce trace filter

# Rate Limiting & HTTP hardening
MOTET_RATE_LIMIT_PER_MINUTE=300              # Rate limit per minute
MOTET_RATE_LIMIT_WINDOW_SECONDS=60           # Rate limit window
MOTET_RATE_LIMIT_BACKEND=memory              # memory|redis
MOTET_AUTH_FAILURE_LIMIT_PER_MINUTE=10       # Failed JWT/service-account attempts per window
MOTET_AUTH_FAILURE_WINDOW_SECONDS=60         # Failed-auth throttle window
MOTET_SECURITY_HEADERS_ENABLED=true          # Emit CSP/HSTS/basic security headers
MOTET_SECURITY_HEADERS_CSP=                  # Override default Content-Security-Policy. /redoc also adds worker-src 'self' blob: so ReDoc search can run.
MOTET_SECURITY_HEADERS_HSTS_MAX_AGE_SECONDS=31536000  # HSTS max-age
MOTET_SECURITY_HEADERS_HSTS_INCLUDE_SUBDOMAINS=true   # Add includeSubDomains to HSTS
MOTET_SECURITY_HEADERS_HSTS_PRELOAD=false    # Add preload to HSTS
MOTET_CORS_ALLOWED_ORIGINS=                  # Comma-separated; empty = same-origin only
MOTET_CORS_ALLOW_CREDENTIALS=false
```

### Tool Configuration

```bash
# File Tools
MOTET_FILE_READ_ALLOWLIST=                   # Comma-separated allowed directories
MOTET_FILE_READ_MAX_BYTES=65536              # Max file read size

# Developer docs (ops-dashboard HTTP + core.docs_read)
MOTET_DEVELOPER_DOCS_DIR=                    # Override onboarding markdown root (default: docs/developer_onboarding next to the repo / image root)

# Worker exec (core.worker_exec — backend from MOTET_EXEC_BACKEND; the LLM-facing schema does not accept cwd)
MOTET_WORKER_EXEC_CWD_ALLOWLIST=             # Comma-separated cwd prefixes the tool may pick from (recommended: /var/motet/worker-exec, mode 0700, in Docker images)
MOTET_WORKER_EXEC_DEFAULT_CWD_ROOT=          # Default root the tool uses for the generated per-call run dir; must be under MOTET_WORKER_EXEC_CWD_ALLOWLIST. Falls back to first allowlisted prefix, then /var/motet/worker-exec.
MOTET_WORKER_EXEC_DEFAULT_TIMEOUT=120        # Default timeout seconds when ExecutionRequest.timeout_seconds is unset (read by backends, not by the tool)
MOTET_EXEC_BACKEND=subprocess                # subprocess (default) | docker | container | kata | kata-fc (Engine API + HostConfig.Runtime; Linux + Kata-registered daemon). Local compose often docker.
MOTET_KATA_DOCKER_RUNTIME=io.containerd.kata.v2   # Docker Engine runtime id; consulted only when EXEC_BACKEND is kata or kata-fc (daemon /etc/docker/daemon.json or containerd config)
MOTET_DOCKER_CONTAINER_RUNTIME=                # Optional HostConfig.Runtime override; consulted only when EXEC_BACKEND=docker (e.g. runsc, or a Kata runtime to force Kata isolation without changing backend id)
MOTET_DOCKER_HOST=                           # Optional. Default: /var/run/docker.sock; use unix:///path for another socket. tcp/http URLs are not supported yet.
MOTET_DOCKER_API_VERSION=v1.44               # Engine API path version (default v1.44; daemons may require ≥1.44)
MOTET_WORKER_EXEC_DOCKER_IMAGE=python:3.11-slim   # Image when ExecutionRequest.oci_image_ref is unset (docker backend)
MOTET_WORKER_EXEC_DOCKER_AUTO_PULL=1         # If 1, pull missing image via Engine API then retry container create (like docker run)
MOTET_WORKER_EXEC_DOCKER_WORKDIR=/work       # Container workdir and bind-mount target for host cwd (docker backend)
MOTET_WORKER_EXEC_DOCKER_NETWORK=            # Docker network when network policy is restricted (default bridge if empty)
# Host exec (core.host_exec via shell bridge — LLM-facing schema does not accept cwd)
MOTET_HOST_EXEC_DEFAULT_CWD_ROOT=            # Default host root for the tool's generated per-call run dir; the bridge must allowlist this path. Falls back to first MOTET_SHELL_BRIDGE_CWD_ALLOWLIST prefix.
# Bundle exec (optional config/exec.yaml in the bundle): published catalog may include oci_image_ref, exec_artifact_digest, base_image_stack, requirements_path (bundle-relative).
# Validate computes requirements_sha256 from the file at requirements_path for CI alignment (scripts/bundle_exec_docker_build.sh builds an image from a requirements.txt; publish does not run docker build).
# With MOTET_EXEC_BACKEND=docker|kata|kata-fc, core.worker_exec may pass bundle_id so oci_image_ref merges from the catalog when not set explicitly.

# Deployer-worker bundle exec image build.
# When enabled, the deployer worker (DEPLOYMENT capability) runs scripts/bundle_exec_docker_build.sh during publish_bundle for any bundle that declared config/exec.yaml with requirements_path but NO oci_image_ref. Bundle-supplied oci_image_ref always wins. Build failures fail the publish (no half-publish). Runtime workers are STILL forbidden from building images; only the deployer is permitted.
MOTET_DEPLOYER_BUILD_ENABLED=true            # true (current early-stage default — flip to false once SaaS deploys with separate CI become common) | false. When true, deployer-worker container needs Docker Engine access (mount /var/run/docker.sock or DinD).
MOTET_DEPLOYER_BUILD_REGISTRY=               # Required when ENABLED=true. Destination registry/namespace for built images, e.g. "registry.example.com/acme". Image is tagged "{registry}/{prefix}-{bundle_id}:{bundle_version}", pushed, then pinned to "image@sha256:..." in the catalog.
MOTET_DEPLOYER_BUILD_TAG_PREFIX=motet-bundle-exec  # Tag prefix used when composing the build image ref. Lowercased and OCI-tag-sanitized.
MOTET_DEPLOYER_BUILD_TIMEOUT_SECONDS=600     # Hard timeout on the build subprocess (capped to keep a stuck builder from wedging the deployer worker).
MOTET_DEPLOYER_PUSH_TIMEOUT_SECONDS=300      # Hard timeout on the docker push subprocess.

# When true, publish_bundle hard-fails any catalog row whose exec.oci_image_ref is set but not in name@sha256:<64hex> form. Default false (UI shows a "mutable tag" warning). Recommended true for production / SaaS-shaped deployments. Tier-only bundles (no oci_image_ref at all) are NOT rejected — this gate only catches set-but-mutable refs.
MOTET_REQUIRE_DIGEST_PINNED_PUBLISH=false    # true | false (default). Recommend "true" in prod alongside MOTET_DEPLOYER_BUILD_ENABLED=true so Motet pins automatically when bundles don't.

# Platform image-stack registry.
# A "stack" is Motet's term for the curated base image layer a bundle's exec image is built on top of (analogous to a Cloud Native Buildpacks "stack"). Isolation tiers (runc / runsc / kata-fc) are a separate setting.
# Three stacks are builtin: python-minimal (pre-pinned to python:3.11-slim), python-office, python-browser. Operators register additional stacks or override builtins via env. Settings here are read by the API, the deployer worker, and the validate-time lint.
# Naming: env var "MOTET_IMAGE_STACK_PYTHON_DATA_SCIENCE" registers stack name "python-data-science" (uppercase + underscores → lowercase + dashes). An optional companion "..._DESCRIPTION" sets the human-readable summary shown in the ops UI. Recommend digest-pinned refs (image@sha256:...).
MOTET_IMAGE_STACK_PYTHON_OFFICE=             # OCI ref for the python-office builtin (LibreOffice + python-docx/openpyxl/pypdf). Empty = unpinned (lint warns; deployer falls back to Dockerfile default). Local build: ./scripts/build-python-office-stack.sh → motet/python-office:dev (see docker/images/python-office/README.md).
MOTET_IMAGE_STACK_PYTHON_OFFICE_DESCRIPTION= # Optional override for the builtin description string.
MOTET_IMAGE_STACK_PYTHON_BROWSER=            # OCI ref for the python-browser builtin (Playwright + Chromium headless). Empty = unpinned.
MOTET_IMAGE_STACK_PYTHON_BROWSER_DESCRIPTION=
# Operator-defined stacks follow the same pattern, e.g.:
#   MOTET_IMAGE_STACK_PYTHON_DATA_SCIENCE=registry.example.com/motet/python-ds@sha256:...
#   MOTET_IMAGE_STACK_PYTHON_DATA_SCIENCE_DESCRIPTION=Numpy, pandas, scikit-learn

# HTTP Tools
MOTET_HTTP_TOOL_ALLOW_DOMAINS=               # Comma-separated allowed domains
MOTET_HTTP_TOOL_DENY_DOMAINS=                # Comma-separated denied domains

# Tool Policies
MOTET_TOOL_ALLOWLIST=                        # Comma-separated allowed tools
MOTET_TOOL_DENYLIST=                         # Comma-separated denied tools
MOTET_TOOL_ROLE_POLICIES_JSON=               # Role-based tool policies (JSON)
MOTET_TOOL_DEFAULT_TIMEOUT_SECONDS=10.0      # Default tool timeout

# Tool invocation / artifact transcripts (replaces removed tool_observation persistence)
MOTET_STORE_TOOL_INVOCATIONS=true            # Store ToolInvocation metadata
MOTET_TOOL_INVOCATION_ARGUMENTS_MAX_BYTES=8192  # Oversized args offload to artifacts
MOTET_STORE_TOOL_ARTIFACTS=true              # Store raw tool payloads when policy allows
MOTET_TOOL_ARTIFACT_ALLOWLIST=oauth_download_url_with_token  # Comma-separated tool names
MOTET_TOOL_ARTIFACT_DENYLIST=                # Comma-separated denylist
MOTET_TOOL_RESULT_ARTIFACT_MIN_BYTES=8192    # Auto-offload large results (respects denylist)
MOTET_TOOL_RESULT_ARTIFACT_TTL_SECONDS=604800  # TTL for non-allowlisted result offloads
```

### MCP Configuration

```bash
# MCP Integration
MOTET_MCP_ENABLED=false                      # Enable MCP
MOTET_MCP_SERVER_ENABLED=false               # Enable MCP server
MOTET_MCP_SERVERS_JSON=                      # MCP servers JSON mapping
MCP_INSTANCE_MANAGER_CONFIG=config/mcp_instance_manager.yaml

# MCP process backend (stdio servers and HTTP start_server)
# Default: subprocess. If unset and MOTET_EXEC_BACKEND is docker | kata | kata-fc, MCP uses Docker Engine for sidecars too (same Runtime selection as worker).
MOTET_MCP_EXEC_BACKEND=                      # subprocess | docker (optional; inherits MOTET_EXEC_BACKEND when unset)
MOTET_MCP_DOCKER_CONTAINER_RUNTIME=          # Optional override for MCP sidecar HostConfig.Runtime (checked before KATA/DOCKER_CONTAINER_RUNTIME inheritance)
MOTET_MCP_DOCKER_IMAGE=node:20-bookworm-slim # Default image when service YAML has no exec_image (stdio / HTTP sidecar)
# Per-service exec_image in mcp_instance_manager.yaml overrides this (e.g. Playwright → mcr.microsoft.com/playwright).
MOTET_MCP_DOCKER_WORKDIR=                    # In-container cwd; default /work when host working_dir is set else /
MOTET_MCP_DOCKER_NETWORK=bridge              # Docker NetworkMode for MCP containers
MOTET_MCP_INITIALIZE_TIMEOUT_SECONDS=120     # Stdio: seconds per stdout read waiting for initialize JSON-RPC (5–600; heavy MCPs may need 180)
MOTET_MCP_WATCHER_POLL_TIMEOUT_SECONDS=      # Optional; default max(300, N_services×MOTET_MCP_PER_SERVICE_INIT_TIMEOUT_SECONDS+120)
MOTET_MCP_PER_SERVICE_INIT_TIMEOUT_SECONDS=240 # Per-service init cap used by the poll default above
# HTTP MCP start_server + MOTET_MCP_EXEC_BACKEND=docker: published port bind + client URL (worker in Docker)
MOTET_MCP_HTTP_PORT_BIND_HOST=0.0.0.0        # Docker port-publish HostIp (127.0.0.1 = host-only; breaks in-container worker)
MOTET_MCP_HTTP_CLIENT_HOST=host.docker.internal # Reach sidecar from worker container; Linux: set to gateway IP if needed

# MCP Docker containers get labels motet.mcp=1, motet.worker_id=<manager id>, and
# motet.mcp.service_id=<service>. Startup sweep matches the raw manager id and the cloud_
# form. HTTP start also reclaims leftovers for the same service_id or published host port.
# The manager is a sibling process (not a child of Celery workers); worker restart does not
# stop MCP sidecars. Leftovers without labels: stop manually.
MOTET_MCP_MANAGER_ID=                        # Stream / status prefix (local compose: mcp-local-default)
MOTET_MCP_RESTART_MAX_PER_HOUR=3             # Per-service restart budget
MOTET_MCP_RESTART_WINDOW_SECONDS=3600        # Restart budget window
```

### Orchestrator Configuration

```bash
# Orchestrator
MOTET_ORCHESTRATOR_MAX_ACTIONS=5             # Max actions

# LLM tool loop helpers
MOTET_LLM_TOOL_MAX_STEPS=3                   # Max tool steps
```

### Reasoning Configuration

An agent turn runs one loop, so there is no strategy to select between.
`MOTET_REASONING_EFFORT` is listed under Model above. The turn gate can
skip tools on greetings:

```bash
MOTET_TURN_GATE_SKIP_SIMPLE=true             # Greetings and acks skip the tool loop
```

Per-request behavior is set on the request rather than the environment: see
[Reasoning](10-reasoning.md) for forcing a `no_tools` reply,
and [Agent Loop](07a-agent-loop.md) for per-agent tuning.

The agent loop also has two spend rails. Iteration count alone lets a
tool-heavy turn keep calling the model until `max_iterations` (20) even after
the transcript is already huge. These stop the turn instead of inviting
another twenty steps:

```bash
MOTET_AGENT_MAX_COST_USD=0.75           # Stop when accumulated model cost hits this. 0 disables.
MOTET_AGENT_MAX_PROMPT_TOKENS=200000    # Stop when accumulated prompt tokens hit this. 0 disables.
```

Per-agent overrides live on `AgentConfig.max_cost_usd` / `max_prompt_tokens`.
`core.spawn_agents` children use a tighter set ($0.20 / 80,000 tokens /
ten iterations / 60 seconds of tool time) so a wide fan-out cannot
multiply the parent ceiling. The 60-second cap is child-only; the
parent turn leaves it off. A child that hits those rails returns
`incomplete` and does not write a Continue checkpoint — Continue is for
the user turn, not a sub-agent.

### Conversation Analysis

```bash
# Opt-in cheap pin. Unset inherits the turn's provider/model.
# MOTET_ANALYSIS_MODEL=gpt-4o-mini
# MOTET_ANALYSIS_PROVIDER=openai
```

Analysis is turned on **per agent**, by the `conversation_analysis` turn hook rather than by an enable flag. `core.default` sets it to `core.conversation_analysis`; `core.motet_admin` leaves it unset. The hook itself is local: skip, lightweight, and the empty default dimension list make no model call. LLM dimensions (`intent`, `context`, `complexity`, `tone`, `user_profile`) are opt-in; when one runs it inherits that turn's provider and model — the same pair `core.spawn_agents` sub-agents inherit — unless `MOTET_ANALYSIS_MODEL` is set. A model pin without a provider keeps the turn's vendor; set `MOTET_ANALYSIS_PROVIDER` only to send analysis to a different one. Pin `analysis_model` / `analysis_provider` on the command data to override both for a single call.

Those two variables are the only analysis settings. Any `MOTET_*` variable this reference does not list is ignored rather than rejected.

### Observability

```bash
# Logging
MOTET_LOG_LEVEL=INFO                         # Log level: DEBUG|INFO|WARNING|ERROR

# OpenTelemetry
MOTET_OTEL_ENABLED=false                    # Enable OpenTelemetry
MOTET_OTEL_EXPORTER=otlp                    # otlp|memory
MOTET_OTEL_OTLP_ENDPOINT=                   # OTLP endpoint

# Tracing
MOTET_TRACE_ENABLED=false                   # Enable distributed tracing

# Metrics
MOTET_METRICS_ENABLED=true                  # Set in the distributed compose file; currently read by no runtime code

# Debug Mode
MOTET_DEBUG_MODE=false                      # Enable debug mode

# Convenience images (compose / motet-cli local up)
MOTET_IMAGE_REGISTRY=ghcr.io/motet-ai       # Registry prefix for Motet-bearing images
MOTET_IMAGE_TAG=v0.1.1                      # Product version tag; `local up` pulls, `--build` rebuilds
```

### Circuit Breakers

```bash
# Circuit Breaker Settings
MOTET_BREAKER_TOOL_FAILURE_THRESHOLD=5      # Tool failure threshold
MOTET_BREAKER_TOOL_RESET_TIMEOUT_SECONDS=30.0  # Tool reset timeout
MOTET_BREAKER_MODEL_FAILURE_THRESHOLD=5     # Model failure threshold
MOTET_BREAKER_MODEL_RESET_TIMEOUT_SECONDS=60.0  # Model reset timeout
```

### OpenAI-Compatible API

Serves the OpenAI HTTP API so clients such as Cursor and the OpenAI SDKs can use Motet. Off by default; both model access and hosted tool exposure are deny-by-default, so enabling the flag alone grants nothing. See [API Reference](./28-api-reference.md).

```bash
MOTET_OPENAI_COMPAT_ENABLED=false                   # Mount the OpenAI-compatible API
MOTET_OPENAI_COMPAT_PREFIX=/v1                      # Mount point
MOTET_OPENAI_COMPAT_DEFAULT_MODE=passthrough        # Mode for credentials without a binding
MOTET_OPENAI_COMPAT_DEFAULT_ALLOWED_MODELS=         # Fallback allowlist; empty denies all
MOTET_OPENAI_COMPAT_HOSTED_TOOLS_ALLOWLIST=         # Tools runnable in hosted_tools mode
MOTET_OPENAI_COMPAT_ALLOW_REQUEST_MODE_OVERRIDE=false  # Honor per-request mode selection
MOTET_OPENAI_COMPAT_MAX_TOOL_ITERATIONS=8           # Hosted tool loop bound
MOTET_OPENAI_COMPAT_AGENT_CLIENT_TOOLS=true         # Honor client-declared tools in agent mode
MOTET_OPENAI_COMPAT_DEFAULT_AGENT_ID=               # Fallback agent id in agent mode (prefer SA agent_id)
MOTET_OPENAI_COMPAT_STREAM_KEEPALIVE_SECONDS=15     # SSE keepalive interval
MOTET_OPENAI_COMPAT_SESSION_TTL_SECONDS=604800      # Response-id / fingerprint record lifetime
MOTET_OPENAI_COMPAT_INFER_SESSION=true              # Rejoin conversations via transcript fingerprints (agent)
MOTET_OPENAI_COMPAT_SESSION_BANNER=every            # Visible session footer: off | first | every
MOTET_OPENAI_COMPAT_SESSION_BANNER_GUARD=true       # Ask the model to preserve banners when summarizing
MOTET_OPENAI_COMPAT_FORCE_THINKING=false            # Force thinking for CAP_REASONING models
MOTET_OPENAI_COMPAT_FORCE_THINKING_EFFORT=medium    # Default effort when force_thinking applies
```

### Startup

```bash
# Startup Validation
MOTET_VALIDATE_ON_STARTUP=false             # Validate on startup
MOTET_STARTUP_STRICT=false                  # Strict startup validation
```

## MCP Configuration File

`config/mcp_instance_manager.yaml`:

```yaml
services:
  - service_id: "playwright"
    transport: "stdio"
    # Optional Docker sidecar image (stdio/http); default MOTET_MCP_DOCKER_IMAGE if omitted
    exec_image: "mcr.microsoft.com/playwright:v1.58.2-noble"
    command: "npx"
    args:
      - "-y"
      - "@playwright/mcp"
    env:
      PLAYWRIGHT_HEADLESS: "true"
      # Match Microsoft Playwright Docker image (see playwright.dev/docs/docker)
      PLAYWRIGHT_BROWSERS_PATH: "/ms-playwright"

    # Instance sharing and lifecycle
    state_model: "stateful"
    credential_scope: "motet"
    visibility: "motet"
    lifecycle_duration: "permanent"
    instances: 1
  
  - service_id: "weather"
    transport: "stdio"
    command: "npx"
    args:
      - "-y"
      - "@modelcontextprotocol/server-weather"

    # Instance sharing and lifecycle
    state_model: "stateless"
    credential_scope: "motet"
    visibility: "motet"
    lifecycle_duration: "permanent"
    instances: 1
  
  - service_id: "custom_server"
    transport: "http"
    start_server: false
    base_url: "https://mcp-server.example.com/mcp"

    # Instance sharing and lifecycle
    state_model: "stateless"
    credential_scope: "motet"
    visibility: "motet"
    lifecycle_duration: "permanent"
    instances: 1
```

## Configuration Best Practices

### 1. Use Environment Files

```bash
# ✅ CORRECT: Use .env file
# .env
MOTET_REDIS_URL=redis://localhost:6379/0
MOTET_MODEL_PROVIDER=openai
MOTET_OPENAI_API_KEY=your-key
```

### 2. Secure Sensitive Values

```bash
# ✅ CORRECT: Use environment variables, not hardcoded
export MOTET_OPENAI_API_KEY=your-key

# ❌ WRONG: Don't hardcode in config files
# MOTET_OPENAI_API_KEY=your-key  # In code
```

### 3. Use Appropriate Defaults

```bash
# ✅ CORRECT: Use sensible defaults
# Most configs have good defaults, only override when needed
```

## Configuration Validation

Motet validates configuration on startup:

```python
# Configuration is validated via Pydantic
from motet.core.config import Config

config = Config()  # Validates all environment variables
```

## Next Steps

- **[Supported Models](./03a-supported-models.md)** — providers, flagship ids, and the live catalog
- **[Troubleshooting Guide](./30-troubleshooting-guide.md)** - Solve problems
- **[Contributing Guide](./32-contributing-guide.md)** — feedback and pilots welcome at `hello@motet.dev`
- **[Project Structure](./33-project-structure.md)** - Understand codebase

## Navigation

- **[← Back to Documentation Home](./00-landing-page.md)** - Main documentation hub

---

**Last Updated**: 2026-08-28
