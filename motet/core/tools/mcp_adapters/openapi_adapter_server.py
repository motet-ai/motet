"""
Motet - OpenAPI to MCP Adapter Server

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Generic adapter that creates an MCP server from an OpenAPI specification.
    Run as a subprocess by MCPInstanceManager. For Docker MCP stdio, use image
    ``docker/images/openapi-adapter-mcp/Dockerfile`` (tag e.g.
    ``motet-openapi-adapter-mcp:local``) so the full Motet worker image is not
    required inside the sidecar.

    Features:
    - Loads OpenAPI spec from URL or local file
    - Generates MCP tools automatically using FastMCP
    - Injects authentication headers from environment variables
    - Runs over stdio by default (compatible with MotetMCPProxy)

Dependencies:
    - fastmcp
    - httpx
    - pyyaml
    - Optional: motet Redis client for URL spec caching (disabled in lightweight Docker image)

Usage:
    python motet/core/tools/mcp_adapters/openapi_adapter_server.py \
        --openapi-url https://api.example.com/openapi.json \
        --base-url https://api.example.com/v1 \
        --tool-prefix my_service

    # Disable response validation (workaround for OpenAPI spec drift)
    python motet/core/tools/mcp_adapters/openapi_adapter_server.py \
        --openapi-url https://api.example.com/openapi.json \
        --base-url https://api.example.com/v1 \
        --tool-prefix my_service \
        --disable-output-validation

    # SSRF / payload guardrails (ADR-0060)
    python motet/core/tools/mcp_adapters/openapi_adapter_server.py \
        --openapi-url https://api.example.com/openapi.json \
        --base-url https://api.example.com/v1 \
        --allowed-hosts api.example.com \
        --timeout-seconds 30 \
        --max-response-bytes 1048576
"""

import argparse
import asyncio
import fnmatch
import hashlib
import importlib
import json
import logging
import os
import pickle
import sys
from typing import Any, Dict, Optional, cast

import httpx
import yaml

# Redis import will be done lazily in load_spec() to avoid stdout pollution
# from package initialization logs
REDIS_AVAILABLE = None  # Will be determined on first use

# ------------------------------------------------------------------------------
# Logging Configuration
# ------------------------------------------------------------------------------
# Configure logging to stderr (Python defaults to stderr, but we're explicit
# to ensure stdout remains clean for JSON-RPC communication in stdio transport).
# Using force=True to override any existing config (defensive).
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stderr,
    force=True
)

logger = logging.getLogger("openapi_adapter")

# Imported lazily in main() for CLI-only paths; imported here so unit tests
# and load_spec() can share the same policy type without a circular import.
try:
    from motet.core.tools.mcp_adapters.openapi_request_safety import (
        DEFAULT_MAX_RESPONSE_BYTES,
        DEFAULT_TIMEOUT_SECONDS,
        RequestSafetyPolicy,
        build_httpx_event_hooks,
        read_response_bounded,
        validate_request_url,
    )
except ImportError:
    # Lightweight Docker image may copy only this file; fall back to a
    # relative import when the package path is unavailable.
    from openapi_request_safety import (  # type: ignore[no-redef]
        DEFAULT_MAX_RESPONSE_BYTES,
        DEFAULT_TIMEOUT_SECONDS,
        RequestSafetyPolicy,
        build_httpx_event_hooks,
        read_response_bounded,
        validate_request_url,
    )


def _glob_path_to_regex(path_glob: str) -> str:
    """Convert a glob pattern (fnmatch) to a regex pattern string."""
    # fnmatch.translate returns a fully-anchored regex ending in \\Z; FastMCP expects a regex string.
    # NOTE: FastMCP applies regex matching in a way that can behave like "search" rather than
    # "match" depending on implementation details. Add a start anchor to ensure our allowlist
    # patterns are not applied as substring matches (e.g. "/users/*" unintentionally matching
    # "/phone/users/*").
    return "^" + fnmatch.translate(path_glob)


def _parse_allow_endpoint(value: str) -> tuple[Optional[str], str]:
    """Parse '{METHOD} {PATH}' allow-endpoint values into (method, path_glob)."""
    raw = (value or "").strip()
    if not raw:
        return None, ""
    if " " not in raw:
        # Treat as path glob with any method.
        return None, raw
    method, path = raw.split(None, 1)
    return method.upper(), path.strip()


def build_openapi_route_maps(
    *,
    allow_operation_ids: list[str],
    allow_tags: list[str],
    allow_endpoints: list[str],
) -> tuple[list[Any] | None, Any | None, Dict[str, int]]:
    """Build FastMCP OpenAPI route_maps + route_map_fn implementing a deny-by-default allowlist."""
    allow_operation_ids = [s for s in (allow_operation_ids or []) if s]
    allow_tags = [s for s in (allow_tags or []) if s]
    allow_endpoints = [s for s in (allow_endpoints or []) if s]

    any_allowlist = bool(allow_operation_ids or allow_tags or allow_endpoints)
    if not any_allowlist:
        return None, None, {"allowlists_enabled": 0}

    # Dynamic imports: keeps Pyright happy when the editor venv omits fastmcp, and matches
    # version-specific export paths for HTTPRoute.
    openapi_mod = importlib.import_module("fastmcp.server.openapi")
    MCPType = getattr(openapi_mod, "MCPType")
    RouteMap = getattr(openapi_mod, "RouteMap")
    try:
        HTTPRoute = getattr(openapi_mod, "HTTPRoute")
    except AttributeError:
        models_mod = importlib.import_module("fastmcp.utilities.openapi.models")
        HTTPRoute = getattr(models_mod, "HTTPRoute")

    route_maps: list[Any] = []

    # Allow by endpoint glob: "{METHOD} {PATH}" → RouteMap(methods=[METHOD], pattern=<regex>)
    for raw in allow_endpoints:
        method, path_glob = _parse_allow_endpoint(raw)
        if not path_glob:
            continue
        methods = None if not method else [method]
        route_maps.append(
            RouteMap(
                methods=cast(Any, methods),
                pattern=_glob_path_to_regex(path_glob),
                mcp_type=MCPType.TOOL,
            )
        )

    # Allow by tag.
    for tag in allow_tags:
        route_maps.append(RouteMap(tags={tag}, mcp_type=MCPType.TOOL))

    # Catch-all: exclude everything not matched by prior maps.
    route_maps.append(RouteMap(mcp_type=MCPType.EXCLUDE))

    allow_op_id_set = set(allow_operation_ids)

    def route_map_fn(route: Any, mcp_type: Any) -> Any:
        # Allow by operationId even if the route would otherwise be excluded by the catch-all.
        if route.operation_id and route.operation_id in allow_op_id_set:
            return MCPType.TOOL
        return None

    stats = {
        "allowlists_enabled": 1,
        "allow_operation_ids": len(allow_operation_ids),
        "allow_tags": len(allow_tags),
        "allow_endpoints": len(allow_endpoints),
        "route_maps": len(route_maps),
    }
    return route_maps, route_map_fn, stats


def load_spec(
    url: Optional[str],
    file_path: Optional[str],
    policy: Optional[RequestSafetyPolicy] = None,
) -> Dict[str, Any]:
    """
    Load OpenAPI spec from URL or file with optional Redis caching.
    
    For URLs, caches the parsed spec in Redis to avoid re-downloading and re-parsing
    on subsequent adapter startups. Cache is invalidated based on ETag/Last-Modified headers.
    """
    # File path - no caching needed (local file)
    if file_path:
        logger.info(f"Loading OpenAPI spec from file: {file_path}")
        with open(file_path, "r") as f:
            if file_path.endswith((".yaml", ".yml")):
                return yaml.safe_load(f)
            return json.load(f)
    
    # URL - use caching if Redis is available
    if url:
        safety = policy or RequestSafetyPolicy()
        validate_request_url(url, safety)
        # Lazy import Redis to avoid stdout pollution from package initialization
        global REDIS_AVAILABLE
        if REDIS_AVAILABLE is None:
            try:
                # Suppress structlog output to stdout before importing Redis
                # (Redis manager uses structlog which can log to stdout)
                try:
                    import structlog
                    # Temporarily configure structlog to use stderr
                    structlog.configure(
                        logger_factory=structlog.PrintLoggerFactory(sys.stderr),
                    )
                except (ImportError, Exception):
                    pass
                
                # Import here (lazy) to avoid stdout pollution during module import
                from motet.core.distributed.redis_manager import get_sync_redis_client
                REDIS_AVAILABLE = True
            except ImportError:
                REDIS_AVAILABLE = False
                logger.warning("Redis not available - OpenAPI spec caching disabled")
        
        if not REDIS_AVAILABLE:
            # Fallback to direct download if Redis not available
            logger.info(f"Loading OpenAPI spec from URL (no cache): {url}")
            return _download_and_parse_spec(url, safety)
        
        # Try to load from cache
        try:
            # Ensure structlog is configured to stderr before Redis operations
            try:
                import structlog
                structlog.configure(
                    logger_factory=structlog.PrintLoggerFactory(sys.stderr),
                )
            except (ImportError, Exception):
                pass
            
            from motet.core.distributed.redis_manager import get_sync_redis_client
            redis_client = get_sync_redis_client("openapi_adapter_cache")
            cache_key = f"openapi:parsed:{hashlib.md5(url.encode()).hexdigest()}"
            etag_key = f"{cache_key}:etag"
            
            # Check cache
            #
            # IMPORTANT: The Redis client may be configured with decode_responses=True,
            # which will try to UTF-8 decode values on read. That breaks binary payloads
            # (e.g., pickles) and results in:
            #   UnicodeDecodeError: 'utf-8' codec can't decode byte 0x80 ...
            #
            # To keep caching robust across client configurations, we store the parsed
            # OpenAPI spec as a JSON string (not a pickle). If we encounter undecodable
            # legacy cache entries, we clear them and re-download.
            try:
                cached_spec_data = redis_client.get(cache_key)
                cached_etag = redis_client.get(etag_key)
            except UnicodeDecodeError as decode_error:
                logger.warning(f"Cached spec not decodable (clearing cache): {decode_error}")
                try:
                    redis_client.delete(cache_key, etag_key)
                except Exception:
                    pass  # Redis cache miss; fall through to download
                cached_spec_data = None
                cached_etag = None

            cached_etag_value = None
            if cached_etag is not None:
                cached_etag_value = (
                    cached_etag.decode(errors="replace") if isinstance(cached_etag, (bytes, bytearray)) else str(cached_etag)
                )
            
            if cached_spec_data and cached_etag_value:
                # Validate cache with ETag check
                logger.info(f"Checking cache validity for {url}")
                with httpx.Client(
                    timeout=min(10.0, safety.timeout_seconds),
                    event_hooks=build_httpx_event_hooks(safety),
                ) as client:
                    # HEAD request to check ETag without downloading
                    head_response = client.head(url)
                    current_etag = head_response.headers.get("etag") or head_response.headers.get("last-modified")
                    
                    if current_etag and cached_etag_value == current_etag:
                        # Cache hit - deserialize and return
                        try:
                            logger.info(f"Cache hit for {url} - using cached parsed spec")
                            if isinstance(cached_spec_data, (bytes, bytearray)):
                                try:
                                    spec = json.loads(cached_spec_data.decode("utf-8"))
                                except Exception:
                                    # Backward-compatibility: older cache entries may be pickled
                                    spec = pickle.loads(cached_spec_data)
                            else:
                                spec = json.loads(str(cached_spec_data))
                            return spec
                        except Exception as unpickle_error:
                            # Corrupted cache data - log and fall through to download
                            logger.warning(f"Failed to unpickle cached spec (corrupted cache), downloading fresh: {unpickle_error}")
                            # Clear corrupted cache entry
                            try:
                                redis_client.delete(cache_key, etag_key)
                            except Exception:
                                pass  # unpickle failed; fall through to download
                    else:
                        logger.info(f"Cache invalidated (ETag changed) for {url}")
            
            # Cache miss or invalid - download and parse
            logger.info(f"Loading OpenAPI spec from URL: {url}")
            spec = _download_and_parse_spec(url, safety)
            
            # Store in cache
            try:
                with httpx.Client(
                    timeout=min(10.0, safety.timeout_seconds),
                    event_hooks=build_httpx_event_hooks(safety),
                ) as client:
                    head_response = client.head(url)
                    etag = head_response.headers.get("etag") or head_response.headers.get("last-modified")
                    
                    if etag:
                        # Cache for 24 hours (specs don't change often)
                        redis_client.setex(cache_key, 86400, json.dumps(spec))
                        redis_client.setex(etag_key, 86400, etag)
                        logger.info(f"Cached parsed spec for {url}")
            except Exception as cache_error:
                # Non-fatal - log and continue
                logger.warning(f"Failed to cache spec: {cache_error}")
            
            return spec
            
        except Exception as e:
            # Redis error - fallback to direct download
            logger.warning(f"Redis cache unavailable, downloading directly: {e}")
            return _download_and_parse_spec(url, safety)
            
    raise ValueError("Must provide either --openapi-url or --openapi-file")


def _download_and_parse_spec(
    url: str, policy: Optional[RequestSafetyPolicy] = None
) -> Dict[str, Any]:
    """Download and parse OpenAPI spec from URL with size and host limits."""
    safety = policy or RequestSafetyPolicy()
    validate_request_url(url, safety)
    with httpx.Client(
        timeout=safety.timeout_seconds,
        event_hooks=build_httpx_event_hooks(safety),
        follow_redirects=True,
    ) as client:
        with client.stream("GET", url) as response:
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            body = read_response_bounded(response, safety.max_response_bytes)
        text = body.decode("utf-8")
        if "yaml" in content_type or url.endswith((".yaml", ".yml")):
            return yaml.safe_load(text)
        return json.loads(text)


def get_auth_headers() -> Dict[str, str]:
    """
    Construct auth headers from environment variables.
    
    Supports:
    1. OAuth Bearer Token: env var specified by config (e.g. ZOOM_ACCESS_TOKEN)
       - We look for any env var that looks like a token if explicit one isn't passed?
       - Actually, the best pattern for a generic adapter is to just check specific
         well-known env vars or look for an injected "AUTH_TOKEN" or "MCP_AUTH_TOKEN".
         
       HOWEVER, mcp_instance_manager.yaml configures `env_var: ZOOM_ACCESS_TOKEN`.
       So inside this process, `os.environ["ZOOM_ACCESS_TOKEN"]` will be set.
       
       But this script is GENERIC. It doesn't know *which* env var holds the token.
       
       Solution: The caller (MCPInstanceManager) sets the specific env var.
       But we need to know WHICH one to map to the Authorization header.
       
       We can add a CLI arg `--auth-env-var` or just look for a standard one.
       Let's stick to a standard pattern: The adapter expects `MCP_AUTH_TOKEN` 
       to be set if Bearer auth is used. The YAML config can map `ZOOM_ACCESS_TOKEN`
       to `MCP_AUTH_TOKEN` in the `env` section? No, that's messy.
       
       Better: Pass `--auth-header "Authorization: Bearer {ZOOM_ACCESS_TOKEN}"`?
       Or simply generic behavior:
       If `MCP_ACCESS_TOKEN` is present -> `Authorization: Bearer <val>`
       If `MCP_API_KEY` is present -> `X-API-Key: <val>` (or configurable)
       
       Let's try to detect ANY env var that looks like a token provided by our vault integration?
       
       Actually, standardizing on `MCP_ACCESS_TOKEN` for the bearer token inside the container/process
       is a clean interface for a generic adapter.
       
       So in YAML:
       env:
         MCP_ACCESS_TOKEN: "${ZOOM_ACCESS_TOKEN}"
    """
    headers = {}
    
    # Standard Bearer Token support
    token = os.environ.get("MCP_ACCESS_TOKEN")
    if token:
        # Log token presence (but not the actual token value for security)
        token_preview = f"{token[:10]}...{token[-4:]}" if len(token) > 14 else "***"
        logger.info(f"Found MCP_ACCESS_TOKEN (length={len(token)}, preview={token_preview}), adding Authorization header")
        headers["Authorization"] = f"Bearer {token}"
        return headers
    else:
        # During discovery mode, tokens may not be available - this is OK
        # Tools are generated from OpenAPI spec, not from API calls
        # Log all env vars that might contain tokens for debugging
        token_env_vars = [k for k in os.environ.keys() if "TOKEN" in k.upper() or "AUTH" in k.upper()]
        logger.warning(f"MCP_ACCESS_TOKEN not found - will proceed without authentication. Available token-related env vars: {token_env_vars}")

    # Api Key support (generic)
    api_key = os.environ.get("MCP_API_KEY")
    if api_key:
        # Default to X-API-Key, but this varies wildly.
        # Maybe we need an arg for the header name?
        header_name = os.environ.get("MCP_API_KEY_HEADER", "X-API-Key")
        logger.info(f"Found MCP_API_KEY, adding {header_name} header")
        headers[header_name] = api_key
        
    return headers


def main():
    parser = argparse.ArgumentParser(description="OpenAPI to MCP Adapter")
    parser.add_argument("--openapi-url", help="URL to OpenAPI specification")
    parser.add_argument("--openapi-file", help="Path to local OpenAPI specification file")
    parser.add_argument("--base-url", required=True, help="Base URL for the REST API")
    parser.add_argument("--tool-prefix", default="api", help="Prefix for generated tools")
    parser.add_argument("--name", default="OpenAPI Adapter", help="Name of the MCP server")
    parser.add_argument(
        "--allow-operation-id",
        action="append",
        default=[],
        help="Allowlist an OpenAPI operationId (repeatable). When any allowlist is provided, tool generation is deny-by-default.",
    )
    parser.add_argument(
        "--allow-tag",
        action="append",
        default=[],
        help="Allowlist operations containing a given OpenAPI tag (repeatable).",
    )
    parser.add_argument(
        "--allow-endpoint",
        action="append",
        default=[],
        help='Allowlist an endpoint using glob matching on "{METHOD} {PATH}" (repeatable). Example: \'GET /users/*\'.',
    )
    parser.add_argument(
        "--disable-output-validation",
        action="store_true",
        help="Disable response validation against OpenAPI output schemas (workaround for spec drift)",
    )
    parser.add_argument(
        "--allowed-hosts",
        default="",
        help="Comma-separated hostname allowlist for --base-url and --openapi-url (ADR-0060).",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"HTTP timeout in seconds for spec download and generated tools (default: {DEFAULT_TIMEOUT_SECONDS}).",
    )
    parser.add_argument(
        "--max-response-bytes",
        type=int,
        default=DEFAULT_MAX_RESPONSE_BYTES,
        help=f"Maximum response body size in bytes (default: {DEFAULT_MAX_RESPONSE_BYTES}).",
    )
    parser.add_argument(
        "--allow-http",
        action="store_true",
        help="Allow http:// URLs. HTTPS is required by default; use only in non-production.",
    )
    
    args = parser.parse_args()
    
    try:
        FastMCP = getattr(importlib.import_module("fastmcp"), "FastMCP")

        policy = RequestSafetyPolicy.from_cli(
            allowed_hosts=args.allowed_hosts,
            allow_http=args.allow_http,
            timeout_seconds=args.timeout_seconds,
            max_response_bytes=args.max_response_bytes,
        )
        # Fail fast before any network I/O so misconfig is a startup error.
        validate_request_url(args.base_url, policy)
        if args.openapi_url:
            validate_request_url(args.openapi_url, policy)
        logger.info(
            "Request safety policy applied: allow_http=%s allowed_hosts=%s "
            "timeout_seconds=%s max_response_bytes=%s",
            policy.allow_http,
            sorted(policy.allowed_hosts) or "(any https host)",
            policy.timeout_seconds,
            policy.max_response_bytes,
        )

        # 1. Load Spec
        spec = load_spec(args.openapi_url, args.openapi_file, policy=policy)
        route_maps, route_map_fn, route_stats = build_openapi_route_maps(
            allow_operation_ids=args.allow_operation_id,
            allow_tags=args.allow_tag,
            allow_endpoints=args.allow_endpoint,
        )
        if route_stats.get("allowlists_enabled"):
            logger.info("Applied OpenAPI allowlist route maps", extra=route_stats)
        
        # 2. Setup Client with Auth Headers
        # Note: Headers are set at startup from environment variables
        # If token is updated after login, the MCP auth observer will restart
        # this instance to pick up the new token (see mcp_auth_observer.py)
        headers = get_auth_headers()
        # Add a user agent
        headers["User-Agent"] = "Motet-MCP-Adapter/1.0"
        
        client = httpx.AsyncClient(
            base_url=args.base_url,
            headers=headers,
            timeout=policy.timeout_seconds,
            follow_redirects=True,
            event_hooks=build_httpx_event_hooks(policy),
        )
        
        # 3. Create FastMCP Server
        # FastMCP.from_openapi parses the spec and generates tools
        logger.info(f"Creating FastMCP server from OpenAPI spec (paths: {len(spec.get('paths', {}))})")

        disabled_output_schema_count = 0

        def _mcp_component_fn(_route: Any, component: Any) -> Any:
            """
            Optional FastMCP component hook.

            Used to disable output schema validation on generated OpenAPI tools when the
            upstream OpenAPI spec does not match real API payloads (e.g., Zoom spec drift).
            """
            nonlocal disabled_output_schema_count

            # Prefer an exact type check when available (dynamic import: submodule may move between FastMCP releases).
            try:
                mod = importlib.import_module("fastmcp.server.openapi.components")
                OpenAPITool = getattr(mod, "OpenAPITool")
                if isinstance(component, OpenAPITool):
                    if args.disable_output_validation:
                        component.output_schema = None
                        disabled_output_schema_count += 1
                    return component
            except Exception:
                # Fall back to duck-typing below.
                pass

            # Fallback: if the component has an output_schema attribute, clear it.
            if args.disable_output_validation and hasattr(component, "output_schema"):
                try:
                    setattr(component, "output_schema", None)
                    disabled_output_schema_count += 1
                except Exception:
                    pass  # output_schema clear optional; component may be immutable

            return component

        mcp = FastMCP.from_openapi(
            openapi_spec=spec,
            client=client,
            name=args.name,
            route_maps=route_maps,
            route_map_fn=route_map_fn,
            mcp_component_fn=_mcp_component_fn,
        )

        if args.disable_output_validation:
            logger.warning(
                "Output validation disabled for generated tools "
                f"(cleared output_schema on {disabled_output_schema_count} components)"
            )
        
        # Log tool count for debugging
        # FastMCP doesn't expose tools directly, but we can check if server was created
        logger.info(f"FastMCP server '{args.name}' created successfully, ready to accept requests")
        
        # 4. Run Server
        # FastMCP runs on stdio by default if no transport args are given, 
        # but let's be explicit if possible. FastMCP.run() defaults to stdio.
        logger.info(f"Starting MCP server '{args.name}' for {args.base_url}")
        mcp.run()
        
    except Exception as e:
        logger.error(f"Failed to start adapter: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
