"""
Motet - Configuration Management

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-26

Description:
    Configuration management system for the Motet distributed framework.
    Provides centralized configuration with environment variable support.

Dependencies:
    - pydantic-settings: Settings management with validation
    - pydantic: Field metadata and validation aliases
    - typing: Type hints and annotations
    - Environment variable loading

Usage:
    from motet.core.config import Config
    
    # Load configuration
    config = Config()
    
    # Access settings
    model_name = config.default_model

Notes:
    - Supports environment variable configuration
    - Includes validation and type checking
    - Provides default values and overrides
    - Integrates with distributed architecture
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .constants import DEFAULT_REDIS_URL, DEFAULT_RATE_LIMIT_PER_MINUTE, REDIS_MAX_CONNECTIONS
from .types import ReasoningEffort


class Config(BaseSettings):
    # Model — default stack routing is OpenAI gpt-4o-mini (cheaper); Kimi K3 available via override.
    model_provider: str = "openai"  # mock|openai|anthropic|gemini|moonshot|deepseek|xai|local
    model_name: str = "gpt-4o-mini"
    model_timeout_seconds: int = 60
    model_max_retries: int = 2
    model_retry_backoff_seconds: float = 0.5
    openai_model_name: str = "gpt-4o-mini"
    anthropic_model_name: str = "claude-3-5-sonnet-latest"
    gemini_model_name: str = "gemini-2.5-flash"

    @model_validator(mode="after")
    def _align_mock_model_name(self) -> "Config":
        """
        When provider is mock but model_name is still an OpenAI leftover default
        (common when only MOTET_MODEL_PROVIDER=mock is set), use mock-small.
        """
        if self.model_provider != "mock":
            return self
        name = (self.model_name or "").strip()
        if not name or name.startswith("gpt-") or name.startswith("o1") or name.startswith("o3"):
            self.model_name = "mock-small"
        return self
    # ADR-0113: default image-generation model. Used when an image_generation request
    # does not specify a provider/model, since the text default model cannot generate images.
    image_model_provider: str = "openai"
    image_model_name: str = "gpt-image-1"
    # ADR-0064: Extended thinking (reasoning) for o-series/gpt-5/Kimi; UI can set via chat overrides
    enable_thinking: bool = False
    reasoning_effort: ReasoningEffort = "medium"
    token_budget: int = 5000
    # Per-turn spend rails for the agent loop. Iteration count alone lets a
    # tool-heavy turn burn hundreds of thousands of prompt tokens before it
    # stops. 0 disables the matching rail.
    agent_max_cost_usd: float = 0.75
    agent_max_prompt_tokens: int = 200000

    # Memory
    enable_memory: bool = True
    memory_backend: str = "inmemory"  # inmemory|redis
    redis_url: str = DEFAULT_REDIS_URL
    redis_max_connections: int = REDIS_MAX_CONNECTIONS
    memory_recent_limit: int = 60
    memory_ttl_seconds: Optional[int] = None
    memory_tags: Optional[str] = None  # comma-separated tags to include
    # Memory semantics
    memory_short_term_tag: str = "stm"
    memory_long_term_tag: str = "ltm"
    memory_working_tag: str = "wm"
    memory_agent_scope_mode: Literal["disabled", "prefer", "strict"] = "prefer"
    memory_agent_tag_prefix: str = "agent:"
    working_memory_reset_each_turn: bool = True
    # Assistant response persistence
    store_assistant_memory: bool = True
    store_assistant_vector: bool = False
    assistant_memory_max_chars: int = 5000
    enable_vector_memory: bool = False
    vector_backend: str = "valkey"  # Valkey only for memory (ADR-0092)
    vector_top_k: int = 3
    chroma_collection: str = "imf_memories"  # Unused for memory; kept for ChromaVectorStore direct use
    embedding_model: str = "sentence-transformers/all-MiniLM-L12-v2"
    embedding_text_model: str = "sentence-transformers/all-MiniLM-L12-v2"
    embedding_topology: Literal["auto", "in_process", "sibling"] = "auto"
    embedding_endpoint: Optional[str] = None
    embedding_request_timeout_seconds: float = 10.0
    embedding_request_max_attempts: int = 3
    embedding_retry_backoff_seconds: float = 0.25
    embedding_circuit_breaker_failure_threshold: int = 3
    embedding_circuit_breaker_recovery_timeout_seconds: float = 30.0
    multimodal_embeddings_enabled: bool = False
    embedding_image_text_model: str = "google/siglip2-base-patch16-256"
    chroma_persist_dir: Optional[str] = None  # Unused for memory; kept for ChromaVectorStore direct use
    vector_tags_filter: Optional[str] = None  # comma-separated tags to filter vector results
    rag_max_chunks: int = 3
    rag_system_prefix: str = "Relevant context:" 
    artifact_rag_enabled: bool = False
    artifact_rag_index_on_derivation: bool = True
    artifact_rag_top_k: int = 5
    artifact_rag_similarity_threshold: float = 0.0
    artifact_rag_hybrid_enabled: bool = True
    artifact_rag_native_text_mode: Literal["auto", "disabled", "required"] = "auto"
    artifact_rag_vector_weight: float = 0.7
    artifact_rag_lexical_weight: float = 0.3
    artifact_rag_candidate_multiplier: int = 4
    artifact_rag_chunk_size: int = 3200
    artifact_rag_chunk_overlap: int = 400
    artifact_rag_token_budget: int = 4000
    # pgvector options (scaffold)
    pgvector_dsn: Optional[str] = None
    pgvector_table: str = "imf_embeddings"
    # Valkey Search LTM vectors (ADR-0092); index name/prefix also via MOTET_MEMORY_VECTOR_VALKEY_*
    memory_vector_valkey_index: Optional[str] = None
    memory_vector_valkey_prefix: Optional[str] = None
    memory_vector_redis_client_id: str = "memory_vector_valkey"
    # Deprecated: LTM indexing is always async via core.memory_vector_index (Valkey-only).
    memory_vector_index_async: Optional[bool] = None

    # Retrieval/caching
    enable_embedding_cache: bool = True
    enable_result_cache: bool = False
    retrieval_vector_weight: float = 0.7

    # Function discovery (ADR-0075 hard cutover)
    # Shared, canonical discovery index settings. All workers should point at the
    # same persistent location and coordinate writes via a distributed lock.
    function_discovery_persist_dir: Optional[str] = None
    function_discovery_writer_lock_key: str = "motet:function_discovery:index_writer"
    function_discovery_writer_lock_ttl_seconds: int = 120
    function_discovery_manifest_file: str = "function_discovery_manifest.json"
    # The manifest lives in Redis alongside the index it describes; the on-disk
    # copy under persist_dir is only a local cache. Workers do not share a
    # filesystem, so a file-only manifest made every worker believe no index
    # existed and rebuild the shared one from its own catalog (#156).
    function_discovery_manifest_redis_key: str = "motet:function_discovery:manifest"
    # How long a worker waits for whichever worker won the writer lock to
    # publish, before giving up and rebuilding itself.
    function_discovery_index_wait_seconds: int = 180

    # Agent Skills (ADR-0073 / agentskills.io progressive disclosure)
    enable_filesystem_skills: bool = True
    project_root: Optional[str] = None
    skill_paths: Optional[str] = None  # comma or os.pathsep separated .agents/skills roots or skill dirs

    # Logging
    log_level: str = "INFO"

    # OpenTelemetry
    otel_enabled: bool = False
    otel_exporter: str = "otlp"  # otlp|memory
    otel_otlp_endpoint: Optional[str] = None

    # Reserved for future providers
    openai_api_key: Optional[str] = None
    anthropic_api_key: Optional[str] = None
    moonshot_api_key: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("MOTET_MOONSHOT_API_KEY", "MOONSHOT_API_KEY"),
    )
    xai_api_key: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("MOTET_XAI_API_KEY", "XAI_API_KEY"),
    )
    deepseek_api_key: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("MOTET_DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY"),
    )
    meta_api_key: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("MOTET_META_API_KEY", "MODEL_API_KEY", "META_API_KEY"),
    )
    gemini_api_key: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("MOTET_GEMINI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"),
    )
    hf_token: Optional[str] = Field(
        default=None,
        description="Hugging Face access token for authenticated model downloads.",
        validation_alias=AliasChoices("MOTET_HF_TOKEN", "HF_TOKEN"),
    )

    # ============================================================================================
    # Provider API modes / rollout flags (ADR-0064)
    # ============================================================================================
    # OpenAI API mode: Chat Completions (legacy) vs Responses (preferred forward path)
    # - chat_completions: use /v1/chat/completions
    # - responses: use /v1/responses
    #
    # NOTE: can be overridden per environment via MOTET_OPENAI_API_MODE for rollback.
    openai_api_mode: str = "responses"  # chat_completions|responses

    # Provider-native built-in tools policy (ADR-0064)
    # When enabled, the system may expose provider built-ins (e.g., OpenAI web search) as virtual tools
    # subject to capability gating and allowlisting.
    enable_tools: bool = False

    # Model profiles (ADR-0064)
    # When enabled, model routing + policy can be overridden per-tenant/per-model from Redis.
    enable_model_profiles: bool = False
    model_profile_name: str = "default"

    # Scheduled command model profile (ADR-0064)
    # When set, scheduled commands can default to a different ModelProfile than interactive turns.
    # This is a policy/routing hint only (adapter + built-in tool policy + default model_settings), not a provider/model selector.
    scheduled_model_profile_name: str = "scheduled-default"

    # Model profile seeding (ADR-0064)
    # Optional: seed model profiles into Redis from a YAML/JSON config file on startup.
    # This is intended for Docker/local/dev and infrastructure-as-code style deployments.
    seed_model_profiles_on_startup: bool = False
    model_profile_seed_file: Optional[str] = None  # e.g. /app/config/model_profiles.yaml
    model_profile_seed_overwrite: bool = False  # if false, only seeds missing profiles

    # Provider stickiness: when true, conversations that opt into provider-native statefulness (e.g., OpenAI previous_response_id)
    # must remain on the same provider until the system explicitly falls back to canonical-history replay.
    enforce_provider_stickiness_for_stateful_sessions: bool = True

    # Tool schema strictness (ADR-0137): reject provider tool dicts; require CanonicalToolSchema.
    strict_canonical_tools: bool = True

    # Google Workspace OAuth Credentials
    google_oauth_client_id: Optional[str] = None
    google_oauth_client_secret: Optional[str] = None

    # API server
    api_key: Optional[str] = None
    jwt_public_key_pem: Optional[str] = None
    jwt_jwks_url: Optional[str] = None
    jwt_jwks_cache_ttl_seconds: int = 300
    jwt_alg_allowlist: str = "RS256,HS256"
    jwt_leeway_seconds: int = 0
    jwt_issuer: Optional[str] = None
    jwt_audience: Optional[str] = None
    keycloak_client_id: Optional[str] = None  # OAuth client ID for Keycloak
    keycloak_public_url: Optional[str] = None  # Public Keycloak URL for browser redirects (e.g., http://localhost:8080)
    
    # Identity mapping
    jwt_sub_claim: str = "sub"
    jwt_roles_claim: str = "roles"
    jwt_tenant_claims: str = "tid,org,tenant,tenant_id,org_id,organization"
    jwt_organization_claim: str = "organization"
    jwt_motet_claims: str = "motet_id,motet,environment,env,deployment"  # Motet/environment identifier claim keys
    deployment_environment: str = "development"  # local|development|test|staging|production
    allow_insecure_principal_headers: bool = False
    allow_insecure_principal_headers_in_non_dev: bool = False
    tenant_id_map_json: Optional[str] = None
    tenant_global_ids: Optional[str] = None
    # Multi-tenancy behavior
    principal_id: Optional[str] = None
    tenant_id: Optional[str] = None
    motet_id: Optional[str] = None  # Motet/environment identifier for multi-environment deployments
    multi_tenant_mode: str = "soft"  # off|soft|enforced
    tenant_enforce_memory_filter: bool = False
    tenant_enforce_trace_filter: bool = False
    require_auth_for_ops_endpoints: bool = False  # gate /metrics behind auth
    cors_allowed_origins: str = ""  # comma-separated origins; empty = same-origin only
    cors_allow_credentials: bool = False
    cors_allowed_methods: str = "GET,POST,PUT,DELETE,OPTIONS,PATCH"
    cors_allowed_headers: str = "Authorization,Content-Type,X-Request-ID,X-Tenant-ID,X-Principal-ID"
    rate_limit_per_minute: int = DEFAULT_RATE_LIMIT_PER_MINUTE
    rate_limit_window_seconds: int = 60
    rate_limit_backend: str = "memory"  # memory|redis
    auth_failure_limit_per_minute: Optional[int] = 10
    auth_failure_window_seconds: int = 60
    security_headers_enabled: bool = True
    security_headers_csp: str = (
        "default-src 'self'; "
        "base-uri 'self'; "
        "frame-ancestors 'none'; "
        "form-action 'self'; "
        "object-src 'none'; "
        "img-src 'self' data: blob: https:; "
        "font-src 'self' data: https:; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' https:; "
        "style-src 'self' 'unsafe-inline' https:; "
        "connect-src 'self' https: http: ws: wss:"
    )
    security_headers_hsts_max_age_seconds: int = 31536000
    security_headers_hsts_include_subdomains: bool = True
    security_headers_hsts_preload: bool = False

    # OpenAI-compatible API facade (ADR-0125)
    # Inbound OpenAI wire (/v1/chat/completions, /v1/models, /v1/responses) for
    # drop-in clients. Disabled by default: enabling it exposes Motet models
    # (and, in hosted_tools/agent modes, tools) to third-party clients.
    openai_compat_enabled: bool = False
    openai_compat_prefix: str = "/v1"
    # Fallback facade mode when a service account carries no explicit policy.
    openai_compat_default_mode: str = "passthrough"  # passthrough|hosted_tools|agent
    # Comma-separated "provider/model" ids allowed when a service account carries
    # no explicit allowlist. Empty means deny-all (ADR-0125 §11a deny-by-default).
    openai_compat_default_allowed_models: str = ""
    # Allow mode selection via request header/body extension. Service-account
    # binding and model aliases take precedence regardless (ADR-0125 §5c).
    openai_compat_allow_request_mode_override: bool = False
    # Comma-separated canonical tool names the facade may execute server-side in
    # hosted_tools mode. Supports "prefix.*" wildcards. Empty means no Motet tool
    # is exposed, so enabling the mode alone grants nothing (ADR-0125 §11b).
    openai_compat_hosted_tools_allowlist: str = ""
    openai_compat_max_tool_iterations: int = 8
    openai_compat_stream_keepalive_seconds: int = 15
    openai_compat_session_ttl_seconds: int = 604800  # response_id -> conversation mapping
    # ADR-0125 §5c.1: in agent mode, honor client-declared tools as handback tools —
    # the agent stack suspends (ADR-0127) and returns OpenAI tool_calls when the model
    # picks one; a follow-up request with the role=tool results resumes the turn.
    # False restores the Motet-owned-loop-only agent mode (client tools ignored).
    openai_compat_agent_client_tools: bool = True
    # Default agent id when facade mode is agent and the request omits
    # motet_agent_id, and the service account carries no agent_id. Empty →
    # MotetStack / agent_turn resolve to core.default. Prefer binding agent_id
    # on the SA for Cursor BYOK; this env is the process-wide fallback.
    openai_compat_default_agent_id: str = ""
    # ADR-0125 §5d: infer conversation continuity for stateless clients (agent mode).
    # Chat Completions clients such as Cursor resend the full transcript each turn
    # with no session header; fingerprinting the transcript prefix rejoins those
    # turns to the same Motet conversation so memory and prompt caching survive.
    openai_compat_infer_session: bool = True
    # ADR-0125 §5d: append a visible session banner carrying the conversation id to
    # agent-mode replies, so a stateless client echoes an explicit reference back
    # instead of relying on the fingerprint alone. Two chat windows whose opening
    # exchange is byte-identical fingerprint the same but banner differently, so
    # this is what keeps them from merging. off | first | every; "every" also
    # survives client-side history compaction that drops the opening messages.
    # Banners are stripped from inbound history, so the model never sees them.
    openai_compat_session_banner: str = "every"
    # Ask the model to preserve the banner when it rewrites a transcript. Cursor
    # BYOK routes its own history summarization back through this endpoint, so a
    # system-prompt line is the only lever the facade has over that rewrite.
    openai_compat_session_banner_guard: bool = True
    # When true (and the SA does not set force_thinking), enable Motet thinking for
    # CAP_REASONING models even if the client omits reasoning opt-in. Useful for
    # Cursor BYOK, which often sends plain Chat Completions with no reasoning fields.
    openai_compat_force_thinking: bool = False
    openai_compat_force_thinking_effort: str = "medium"

    # Tools
    file_read_allowlist: Optional[str] = None  # comma-separated absolute dirs
    file_read_max_bytes: int = 65536
    http_tool_allow_domains: Optional[str] = None  # comma-separated hostnames
    http_tool_deny_domains: Optional[str] = None  # comma-separated hostnames
    # MCP integration (client/server) – optional, behind flags
    mcp_enabled: bool = False
    mcp_server_enabled: bool = False
    # Optional JSON mapping server_id -> {"transport":"stdio|ws", "endpoint":"path|wss://..."}
    mcp_servers_json: Optional[str] = None

    # ADR-0105: MCP Instance Manager sibling deployment (milestone 0 — config plumbing)
    #
    # The MCPInstanceManager runs as a sibling process to the worker, brought up by the
    # same orchestrator (a sidecar pod in cloud k8s, a sibling compose service in edge /
    # dev compose). The in-worker subprocess path is deleted entirely per ADR-0105 §R0.
    #
    # Two env vars wire the worker to its sibling manager when MCP is enabled:
    #   - MOTET_MCP_MANAGER_ENDPOINT : *where* to find the manager. Orchestrator-specific
    #                                  (k8s Service DNS in cloud, compose service name in
    #                                  edge / dev compose). Today the MotetMCPClient still
    #                                  uses the Redis Streams transport, so this is a
    #                                  namespace/discovery hint rather than a network
    #                                  endpoint per se. (ADR-0105 §"Open questions" Q2.)
    #   - MOTET_MCP_MANAGER_ID       : *which* manager — the bus-routing prefix on every
    #                                  MCP stream / PUB-SUB channel / readiness set, and
    #                                  the canonical identity in the ops status surface.
    #                                  Required so multiple managers (multiple edge devices,
    #                                  multiple cloud worker pods) sharing one Redis bus
    #                                  don't claim each other's traffic. (ADR-0105 §R2/R3.)
    #
    # Both are Optional because MCP itself is opt-in (mcp_enabled=False default). When
    # mcp_enabled=True, the worker hard-fails at startup if either is unset, with a clear
    # message pointing at the docker-compose / Helm chart manager service. (Wired in M1.)
    #
    # Endpoint and id are intentionally separate (not derived from each other) so a DNS
    # rename or service-IP change doesn't break dashboard/metrics continuity.
    mcp_manager_endpoint: Optional[str] = None
    mcp_manager_id: Optional[str] = None

    # ADR-0105 (LocalInferenceManager hoist): the local inference manager follows the same
    # sibling-deployment shape as the MCP manager above. It runs as an independent supervised
    # service (the ``local-inference`` compose service in dev / edge, a sibling Deployment in
    # cloud k8s) rather than a per-worker subprocess, so it survives worker restarts while
    # keeping models warm.
    #
    #   - MOTET_LOCAL_INFERENCE_MANAGER_ID : the bus-routing prefix shared between every
    #                                        LocalInferenceClient (in the worker) and the sibling
    #                                        LocalInferenceManager. All request/response Redis
    #                                        Streams are keyed ``local-inference:{manager_id}:...``
    #                                        so one manager serves N workers and no two managers
    #                                        sharing a Redis bus claim each other's traffic
    #                                        (ADR-0105 §R2/§R3). The client/manager read this env
    #                                        var directly; this field documents the contract and
    #                                        gives a typed accessor. Defaults to the same value the
    #                                        client/manager fall back to when unset.
    local_inference_manager_id: str = "local-inference-default"

    # Orchestrator
    orchestrator_max_actions: int = 5
    llm_tool_max_steps: int = 3
    llm_tool_system_prompt: str = (
        "You may choose to use tools. If a tool is needed, respond ONLY with JSON of the form "
        "{\"action\":\"tool\",\"name\":\"<tool_name>\",\"params\":{...},\"thought\":\"why\"}. "
        "Otherwise respond ONLY with JSON {\"action\":\"answer\",\"final\":\"...\"}. No extra text."
    )

    # Tool policies
    tool_allowlist: Optional[str] = None  # comma-separated tool names
    tool_denylist: Optional[str] = None  # comma-separated tool names
    tool_role_policies_json: Optional[str] = None  # JSON mapping role -> [tools]
    tool_default_timeout_seconds: float = 10.0
    # Tool observation persistence policy: removed (ADR-0061 hard cutover)
    
    # Tool invocation transcript storage (ADR-0061)
    store_tool_invocations: bool = True  # Always store ToolInvocation metadata
    # Inline memory cap for ToolInvocation.arguments_json; oversized args are
    # offloaded to ArtifactKind.TOOL_ARGUMENTS (arguments_artifact_id) for replay.
    tool_invocation_arguments_max_bytes: int = 8192

    # Canonical transcript storage/replay (impl-070; ADR-0061/ADR-0064) is always on:
    # finalize_turn persists a TranscriptItem list per turn; prepare_context/conversation_get
    # retrieve history from conversation_transcript memories.
    # Tool artifact storage (raw payloads) - policy-gated
    store_tool_artifacts: bool = True  # Off by default for sensitive tools
    tool_artifact_allowlist: Optional[str] = "oauth_download_url_with_token"  # comma-separated tool names
    tool_artifact_denylist: Optional[str] = None  # comma-separated tool names (OAuth, auth, downloads, binary)
    # When result JSON exceeds this size, store TOOL_ARTIFACT even if the tool is
    # not on the allowlist (still respects denylist + sensitive-name deny). Keeps
    # full payloads out of LLM history while preserving them for artifact_read.
    tool_result_artifact_min_bytes: int = 8192
    # TTL for oversized-result offload artifacts (non-allowlisted). They only need
    # to outlive the cycle that clipped them, so expire to bound Redis growth.
    # Allowlisted tool artifacts keep the original persistent behavior.
    tool_result_artifact_ttl_seconds: int = 604800  # 7 days
    artifact_store_backend: str = "redis"  # redis|s3|postgres (future)
    artifact_store_ttl_seconds: Optional[int] = None  # Deprecated global default; callers should pass TTL explicitly
    artifact_store_max_bytes: int = 25_000_000  # 25MB Redis limit
    artifact_max_video_bytes: int = 536_870_912  # 512MB upload cap for video/* (ADR-0118)
    # ADR-0118 Phase A.2: short-lived HMAC tokens that let <video src> hit the
    # stream endpoint without auth headers. Secret must be set explicitly for
    # multi-process API deployments (ephemeral per-process fallback otherwise).
    artifact_playback_token_secret: Optional[str] = None
    artifact_playback_token_ttl_seconds: int = 300
    video_transcription_enabled: bool = True
    video_transcription_backend: str = "none"  # none|whisper_cli|openai_api (worker config, ADR-0119)
    video_transcription_model: str = "base"  # whisper_cli model name or hosted model id (ADR-0119)
    video_transcription_language: str = ""  # ISO language hint; empty = auto-detect (ADR-0118)
    video_transcription_api_base: str = "https://api.openai.com/v1"  # openai_api backend base URL (ADR-0119)
    video_scene_threshold: float = 0.3  # ffmpeg scene-change threshold for keyframes (ADR-0118)
    worker_media_processing: bool = False  # Force MEDIA_PROCESSING capability advertisement
    artifact_store_encryption: bool = True  # Encryption-at-rest (ADR-0056)
    artifact_store_s3_bucket: Optional[str] = None
    artifact_store_s3_prefix: str = "artifacts"
    # ADR-0118: store video/* payloads as raw range-addressable S3 objects so HTTP
    # Range maps to native ranged GetObject (encryption via S3 SSE instead of the
    # app-layer envelope). Set False to keep the envelope (no efficient seeking).
    artifact_store_s3_raw_video_payloads: bool = True
    artifact_store_s3_sse: str = ""  # ""|AES256|aws:kms — SSE for raw payload objects
    artifact_store_s3_sse_kms_key_id: str = ""  # KMS key for aws:kms SSE
    artifact_store_s3_region: Optional[str] = None
    artifact_store_s3_endpoint_url: Optional[str] = None  # SeaweedFS (local) / AWS S3 / S3-compatible endpoint
    artifact_store_s3_access_key_id: Optional[str] = None
    artifact_store_s3_secret_access_key: Optional[str] = None
    artifact_store_s3_session_token: Optional[str] = None
    artifact_store_s3_force_path_style: bool = False
    artifact_store_s3_use_ssl: bool = True

    # Conversation analysis. The skip/lightweight/full router is always local;
    # these two are an opt-in cheap pin for the LLM dimensions. Unset (None)
    # means inherit the turn's provider/model — do not default analysis_model
    # to gpt-4o-mini, or every turn silently splits vendors.
    analysis_model: Optional[str] = None
    analysis_provider: Optional[str] = None
    turn_gate_skip_simple: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "MOTET_TURN_GATE_SKIP_SIMPLE",
            "MOTET_INTENT_SKIP_REASONING_FOR_SIMPLE",
            "turn_gate_skip_simple",
            "intent_skip_reasoning_for_simple",
        ),
        description="When true, greetings and acknowledgements skip the tool loop.",
    )

    memory_collections_enabled: bool = False
    memory_insights_enabled: bool = False
    memory_event_learning_enabled: bool = False

    conversation_context_window: int = 10

    input_processing_enabled: bool = False
    conversation_collection_name: str = "default"

    # Security & privacy
    pii_allowlist: Optional[str] = None  # comma-separated substrings to preserve

    # Circuit breaker settings
    breaker_tool_failure_threshold: int = 5
    breaker_tool_reset_timeout_seconds: float = 30.0
    breaker_model_failure_threshold: int = 5
    breaker_model_reset_timeout_seconds: float = 60.0

    # Redis command serialization settings
    redis_command_size_threshold_bytes: int = 0  # 0 = always use Redis, >0 = size threshold
    redis_command_complex_object_threshold: int = 5000  # Character threshold for complex object detection

    # Scheduler / Events
    scheduler_max_concurrent_tasks: int = 10
    events_enabled: bool = True

    # Startup validation
    validate_on_startup: bool = False
    startup_strict: bool = False

    # Hosting / deployment (used by install scripts, not core app)
    letsencrypt_email: Optional[str] = None
    imf_hosted_zone_id: Optional[str] = None
    domain_base: Optional[str] = None  # Base domain (e.g., ai.motet.dev) for URLs/cookies

    # Vault
    vault_salt: Optional[str] = None  # hex-encoded salt for PBKDF2; generated per-deployment if unset

    # Local worker — WireGuard tunnel (ADR-0095)
    # Production: set MOTET_WIREGUARD_SERVER_PUBLIC_KEY and _ENDPOINT directly.
    # Local dev: leave unset; devices.py reads the auto-generated key from the
    # mounted wireguard_config volume via wireguard_server_publickey_file.
    wireguard_server_public_key: Optional[str] = None
    wireguard_server_endpoint: Optional[str] = None
    wireguard_peer_subnet: str = "10.0.100.0/24"
    wireguard_allowed_ips: str = "10.0.0.0/16"
    wireguard_server_publickey_file: Optional[str] = None
    wireguard_valkey_url: Optional[str] = None  # tunnel-reachable Valkey URL returned to local workers

    # Pydantic v2 settings configuration
    # extra="ignore" so env vars not on this model (e.g. github_token, MCP) do not break Config()
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="MOTET_",
        validate_assignment=True,
        extra="ignore",
        populate_by_name=True,
    )


__all__ = ["Config"]


