"""
Motet - HTTP Interface

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-26

Description:
    FastAPI-based HTTP interface for the Motet distributed framework.
    Provides REST API endpoints, WebSocket support, and web-based UI for
    interacting with the distributed AI system.

Dependencies:
    - fastapi: Web framework and API endpoints
    - websockets: Real-time communication
    - jinja2: Template rendering for UI
    - redis: Distributed state management
    - structlog: Structured logging

Usage:
    uvicorn motet.interfaces.http:app --host 127.0.0.1 --port 8000

Notes:
    - App ``version`` is the Motet product version from package metadata
    - OpenAPI license is FSL-1.1-ALv2 (not a leftover permissive name)
    - Supports both REST API and WebSocket communication
    - Includes web-based UI for system monitoring
    - Integrates with distributed orchestration system
    - Provides comprehensive health and metrics endpoints
    - ``GET /health`` is liveness only (cheap Redis PING + component presence).
      Docker probes this path; a Valkey FT.SEARCH here blocks the asyncio loop
      and stalls Chat Explorer SSE. Use ``GET /health/vector`` for index probes.
    - ``GET /api/v1/version`` reports Motet product versions (API, workers,
      and configured siblings) and a skew flag. Authenticated; not part of
      the liveness probe.
    - /docs and /redoc use relaxed frame headers (SAMEORIGIN, frame-ancestors 'self') so the
      ops dashboard can embed them at /manage/api-docs; other routes keep DENY / frame-ancestors 'none'.
    - /redoc also allows ``worker-src 'self' blob:`` so ReDoc search can index
      the spec (it starts a blob Web Worker). /docs and other routes do not.
    - Custom /docs and /redoc HTML force color-scheme: light and an opaque white page
      background so Swagger/ReDoc stay readable when the ops dashboard is in dark mode
      (iframe transparency would otherwise show the dark parent through light-theme text).
    - Startup registers core command types in this process so /api/v1/commands can
      resolve them on a cold API; bundle commands register only in workers and are
      resolved through the Redis bundle catalog.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import AsyncGenerator, List, Optional, Any
import os
import httpx

from fastapi import FastAPI, HTTPException, Header, Body, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.websockets import WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse, HTMLResponse, Response, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path
from pydantic import BaseModel

from .._version import get_version
from ..core import MotetStack, Message, Config
from ..core.constants import DEFAULT_REDIS_URL
from ..core.models import list_models, model_supports
from ..core.observability.logging import setup_logging
from ..core.observability import trace_store as tracing
import structlog
from ..core.tools import registry as tools
from ..core.workers import global_bus
import redis
import json

logger = structlog.get_logger(__name__)


def _request_is_secure(request: Request) -> bool:
    forwarded_proto = request.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip().lower()
    return request.url.scheme == "https" or forwarded_proto == "https"


def _apply_security_headers(request: Request, response: Response, cfg: Config) -> None:
    if not getattr(cfg, "security_headers_enabled", True):
        return

    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    # FastAPI Swagger/ReDoc are embedded in iframes from /manage/api-docs (same origin).
    # DENY + frame-ancestors 'none' blocks that; SAMEORIGIN + frame-ancestors 'self' allows it.
    _path_norm = request.url.path.rstrip("/") or "/"
    _api_docs_iframe_targets = _path_norm in ("/docs", "/redoc")
    if _api_docs_iframe_targets:
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        _csp = cfg.security_headers_csp
        if "frame-ancestors 'none'" in _csp:
            _csp = _csp.replace("frame-ancestors 'none'", "frame-ancestors 'self'", 1)
        # ReDoc search indexes the spec in a blob: Web Worker. Without
        # worker-src, browsers fall back to script-src (no blob) and search
        # is a no-op. Keep this on /redoc only.
        if _path_norm == "/redoc" and "worker-src" not in _csp:
            _csp = f"{_csp.rstrip('; ')}; worker-src 'self' blob:"
        response.headers.setdefault("Content-Security-Policy", _csp)
    else:
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Content-Security-Policy", cfg.security_headers_csp)
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Permissions-Policy",
        "accelerometer=(), camera=(), geolocation=(), gyroscope=(), microphone=(), payment=(), usb=()",
    )

    if _request_is_secure(request):
        hsts_parts = [f"max-age={int(getattr(cfg, 'security_headers_hsts_max_age_seconds', 31536000) or 31536000)}"]
        if getattr(cfg, "security_headers_hsts_include_subdomains", True):
            hsts_parts.append("includeSubDomains")
        if getattr(cfg, "security_headers_hsts_preload", False):
            hsts_parts.append("preload")
        response.headers.setdefault("Strict-Transport-Security", "; ".join(hsts_parts))









from contextlib import asynccontextmanager


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Startup and shutdown lifecycle for the FastAPI app."""
    # --- startup ---
    # Register core command types in this process. Workers do the same at init
    # (worker_initialization.py); without it the API's local registry is empty on
    # a cold start and /commands endpoints 404 on core types the workers can run.
    # Bundle commands are not covered — those register only in workers, and the
    # command endpoints fall back to the Redis bundle catalog for them.
    try:
        from ..core.commands.distributed import DistributedCommand
        DistributedCommand._ensure_commands_registered()
        logger.info("Core command types registered")
    except Exception as e:
        logger.error("core_command_registration_failed", error=str(e), exc_info=True)
        raise

    from ..core.workers import start_event_observers
    await start_event_observers()
    logger.info("EventObserverManager started")

    from ..core.security.vault_cache_observer import register_vault_cache_observer
    register_vault_cache_observer()
    logger.info("Vault Cache Observer registered")

    from ..core.security.oauth_token_refresher import start_token_refresher
    await start_token_refresher()
    logger.info("OAuth Token Refresher started")

    try:
        cfg = Config()
        if bool(getattr(cfg, "seed_model_profiles_on_startup", False)):
            from ..core.models.profile_seeding import seed_model_profiles_if_configured
            result = await seed_model_profiles_if_configured(cfg)
            logger.info("model_profile_seed_startup_result", result=result)
    except Exception as e:
        logger.error("model_profile_seed_startup_failed", error=str(e), exc_info=True)
        raise

    yield

    # --- shutdown ---
    from ..core.workers import stop_event_observers
    await stop_event_observers()
    logger.info("EventObserverManager stopped")

    from ..core.security.oauth_token_refresher import stop_token_refresher
    await stop_token_refresher()
    logger.info("OAuth Token Refresher stopped")


# Swagger UI / ReDoc are light-theme UIs. Without an opaque page background they
# become unreadable when embedded in the dark-mode ops dashboard (transparent
# iframe body shows the dark parent) or when a dark color-scheme is applied.
_API_DOCS_LIGHT_SCHEME_HEAD = """
<meta name="color-scheme" content="light">
<style>
  html, body { color-scheme: light; background: #ffffff; height: 100%; margin: 0; overflow: hidden; }
  body { background: #ffffff !important; }
  redoc, .redoc-wrap { display: block; height: 100%; }
</style>
"""


def _with_light_api_docs_scheme(response: HTMLResponse) -> HTMLResponse:
    """Inject light color-scheme + opaque white background into docs HTML."""
    html = response.body.decode("utf-8")
    if "</head>" in html:
        html = html.replace("</head>", f"{_API_DOCS_LIGHT_SCHEME_HEAD}</head>", 1)
    else:
        html = f"{_API_DOCS_LIGHT_SCHEME_HEAD}{html}"
    return HTMLResponse(content=html, status_code=response.status_code, media_type=response.media_type)


def create_app() -> FastAPI:
    setup_logging()
    app = FastAPI(
        title="Motet API",
        version=get_version(),
        lifespan=_lifespan,
        # Custom /docs and /redoc below inject an opaque light background for dark-mode embeds.
        docs_url=None,
        redoc_url=None,
        description="HTTP API for the Motet runtime: commands, schedules, execution, vault, and developer tools.",
        contact={
            "name": "Motet",
            "email": "support@motet.dev",
        },
        license_info={
            "name": "Functional Source License, Version 1.1, ALv2 Future License",
            "url": "https://fsl.software/",
        },
        openapi_tags=[
            {
                "name": "commands",
                "description": "Command deployment and hot-loading management",
            },
            {
                "name": "schedules",
                "description": "Scheduled command execution",
            },
            {
                "name": "vault",
                "description": "Secure credential storage and management",
            },
            {
                "name": "debug",
                "description": "Debug and developer tools (debug mode required)",
            },
            {
                "name": "workers",
                "description": "Worker management, health monitoring, and termination",
            },
            {
                "name": "workflows",
                "description": "Workflow creation, execution, and management",
            },
            {
                "name": "tools",
                "description": "Tool discovery, loading, and execution",
            },
            {
                "name": "memories",
                "description": "Memory management and retrieval",
            },
            {
                "name": "conversations",
                "description": "Conversation session management",
            },
            {
                "name": "models",
                "description": "Language model information",
            },
            {
                "name": "chat",
                "description": "Chat completions with streaming support",
            },
            {
                "name": "agents",
                "description": "Agent discovery and metadata",
            },
            {
                "name": "service-accounts",
                "description": "Service account token management",
            },
            {
                "name": "events",
                "description": "Real-time event streaming and statistics",
            },
            {
                "name": "identity",
                "description": "Principal and tenant identity information",
            },
            {
                "name": "oauth",
                "description": "OAuth authentication flows for MCP servers and integrations",
            },
            {
                "name": "artifacts",
                "description": "Artifact upload, metadata, and file management",
            },
            {
                "name": "authentication",
                "description": "Login, callback, refresh, and logout",
            },
            {
                "name": "cost",
                "description": "Usage and cost tracking by tenant and principal",
            },
        ]
    )
    templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))

    from fastapi.openapi.docs import (
        get_redoc_html,
        get_swagger_ui_html,
        get_swagger_ui_oauth2_redirect_html,
    )

    @app.get("/docs", include_in_schema=False)
    async def swagger_ui_html() -> HTMLResponse:
        return _with_light_api_docs_scheme(
            get_swagger_ui_html(
                openapi_url=app.openapi_url,
                title=f"{app.title} - Swagger UI",
                oauth2_redirect_url=app.swagger_ui_oauth2_redirect_url,
            )
        )

    @app.get(app.swagger_ui_oauth2_redirect_url, include_in_schema=False)
    async def swagger_ui_redirect() -> HTMLResponse:
        return get_swagger_ui_oauth2_redirect_html()

    @app.get("/redoc", include_in_schema=False)
    async def redoc_html() -> HTMLResponse:
        return _with_light_api_docs_scheme(
            get_redoc_html(
                openapi_url=app.openapi_url,
                title=f"{app.title} - ReDoc",
            )
        )


    # Serve built Vite SPAs when present (e.g. in API Docker image used by AWS install)
    # Use SPA fallback (serve index.html for non-file paths) so client-side routes work on refresh.
    # Set explicit media types for assets so JS/CSS/fonts load correctly (e.g. on EC2).
    _static_root = Path("/app/static")
    _chat_explorer = _static_root / "chat-explorer"
    _manage = _static_root / "manage"

    def _media_type_for(path: Path) -> str | None:
        suffix = (path.suffix or "").lower()
        return {
            ".js": "application/javascript",
            ".mjs": "application/javascript",
            ".css": "text/css",
            ".json": "application/json",
            ".ico": "image/x-icon",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".svg": "image/svg+xml",
            ".woff": "font/woff",
            ".woff2": "font/woff2",
            ".ttf": "font/ttf",
            ".map": "application/json",
        }.get(suffix)

    if _chat_explorer.is_dir():
        def _serve_chat_explorer_spa(path_relative: str) -> Response:
            if not path_relative or path_relative == "index.html":
                return FileResponse(_chat_explorer / "index.html", media_type="text/html")
            safe = ( _chat_explorer / path_relative ).resolve()
            if not str(safe).startswith(str(_chat_explorer.resolve())):
                return FileResponse(_chat_explorer / "index.html", media_type="text/html")
            if safe.is_file():
                mt = _media_type_for(safe)
                return FileResponse(safe, media_type=mt) if mt else FileResponse(safe)
            return FileResponse(_chat_explorer / "index.html", media_type="text/html")

        @app.get("/chat-explorer", include_in_schema=False)
        def chat_explorer_index():
            return _serve_chat_explorer_spa("")

        @app.get("/chat-explorer/", include_in_schema=False)
        def chat_explorer_index_slash():
            return _serve_chat_explorer_spa("")

        @app.get("/chat-explorer/{full_path:path}", include_in_schema=False)
        def chat_explorer_spa(full_path: str):
            return _serve_chat_explorer_spa(full_path)
    if _manage.is_dir():
        # SPA fallback: serve index.html for any /manage/* path that is not a real file
        # so client-side routing works on refresh (e.g. /manage/workers)
        def _serve_manage_spa(path_relative: str) -> Response:
            if not path_relative or path_relative == "index.html":
                return FileResponse(_manage / "index.html", media_type="text/html")
            safe = ( _manage / path_relative ).resolve()
            if not str(safe).startswith(str(_manage.resolve())):
                return FileResponse(_manage / "index.html", media_type="text/html")
            if safe.is_file():
                mt = _media_type_for(safe)
                return FileResponse(safe, media_type=mt) if mt else FileResponse(safe)
            return FileResponse(_manage / "index.html", media_type="text/html")

        @app.get("/manage", include_in_schema=False)
        def manage_index():
            return _serve_manage_spa("")

        @app.get("/manage/", include_in_schema=False)
        def manage_index_slash():
            return _serve_manage_spa("")

        @app.get("/manage/{full_path:path}", include_in_schema=False)
        def manage_spa(full_path: str):
            return _serve_manage_spa(full_path)

    # Rate limiting structures (memory or redis)
    from collections import defaultdict, deque
    import time
    request_log = defaultdict(deque)
    cfg = Config()

    _cors_origins = [o.strip() for o in (cfg.cors_allowed_origins or "").split(",") if o.strip()]
    _cors_methods = [m.strip() for m in cfg.cors_allowed_methods.split(",") if m.strip()]
    _cors_headers = [h.strip() for h in cfg.cors_allowed_headers.split(",") if h.strip()]
    if _cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=_cors_origins,
            allow_credentials=cfg.cors_allow_credentials,
            allow_methods=_cors_methods,
            allow_headers=_cors_headers,
        )
    from ..core.security.auth import validate_insecure_principal_header_policy

    validate_insecure_principal_header_policy(cfg)
    # Optional OpenTelemetry setup
    try:
        from ..core.observability import setup_tracing as _setup_tracing
        _setup_tracing(cfg.otel_enabled, cfg.otel_exporter, cfg.otel_otlp_endpoint)
    except Exception:
        pass  # optional tracing; proceed without if unavailable
    stack = MotetStack(cfg)
    # Provide runtime stack to tools for store access
    try:
        from ..core.tools import registry as _tools_registry
        _tools_registry.set_runtime_stack(stack)
    except Exception:
        pass  # optional tools registry setup; proceed without
    # expose for background jobs/tests
    try:
        app.state.stack = stack
    except Exception:
        pass  # optional app state; proceed without
    # Optional: MCP tool discovery (uses MotetMCPClient for Option 3.5 architecture)
    # Note: MCP tools are now auto-discovered by MCPInstanceManager in worker processes.
    # This section is kept for backward compatibility but is typically not needed.
    try:
        if getattr(cfg, "mcp_enabled", False):
            print("MCP enabled - tools are managed by MCPInstanceManager in workers")
    except Exception:
        pass  # optional MCP config print; non-critical
    # In-memory session manager (server-side history window)
    try:
        from .sessions import SessionManager
        app.state.sessions = SessionManager(window_messages=int(getattr(cfg, "conversation_context_window", 10)))
    except Exception:
        app.state.sessions = None

    # Initialize state management systems for API process
    try:
        # Initialize state registry using unified Redis manager
        from ..core.distributed.state_registry import initialize_state_registry
        initialize_state_registry()
        
        # Initialize state-aware router
        from ..core.distributed.state_aware_routing import initialize_state_aware_router
        initialize_state_aware_router()
        
        print("✅ State management systems initialized in API process")
    except Exception as e:
        print(f"⚠️  State management initialization failed: {e}")
        # Continue without state management - execution tracking still works

    log = structlog.get_logger()
    # Event handling now uses EventBus (Redis pub/sub) - no need for separate handlers

    # Dev-only Vite proxy for Ant Design X prototype HMR (optional)
    # Enable with MOTET_DEV_VITE_PROXY=true (defaults to http://localhost:5173)
    import os as _os  # ensure local binding for env checks
    if _os.getenv("MOTET_DEV_VITE_PROXY", "").lower() in {"1", "true", "yes"}:
        VITE_TARGET = _os.getenv("MOTET_DEV_VITE_TARGET", "http://localhost:5173")
        VITE_PATHS = ["/chat-explorer", "/@vite", "/src", "/node_modules"]
        
        # Admin Dashboard Vite Dev Server (optional, port 5174)
        ADMIN_VITE_TARGET = _os.getenv("MOTET_DEV_ADMIN_VITE_TARGET", "http://localhost:5174")

        async def proxy_http_base(request: Request):
            """Proxy handler for base paths (e.g., /chat-explorer)."""
            # Map /chat-explorer to Vite's /chat-explorer/
            path = request.url.path
            if path == "/chat-explorer" or path == "/chat-explorer/":
                vite_path = "/chat-explorer/"
            elif path.startswith("/chat-explorer/"):
                # Keep the full path for Vite (it knows about /chat-explorer/ base)
                vite_path = path
            else:
                vite_path = path
            target = f"{VITE_TARGET}{vite_path}"
            if request.url.query:
                target = f"{target}?{request.url.query}"
            headers = dict(request.headers)
            headers.pop("host", None)
            try:
                async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
                    resp = await client.request(
                        request.method,
                        target,
                        headers=headers,
                        content=await request.body(),
                    )
                    # Read the full response body to avoid StreamConsumed errors
                    body = await resp.aread()
                return Response(
                    content=body,
                    status_code=resp.status_code,
                    headers={k: v for k, v in resp.headers.items() if k.lower() not in {"content-encoding", "transfer-encoding", "connection"}},
                    media_type=resp.headers.get("content-type"),
                )
            except Exception as e:
                log.error("Vite proxy error", target=target, error=str(e), exc_info=True)
                return JSONResponse(
                    {"error": "Proxy error", "target": target, "message": str(e)},
                    status_code=502
                )

        async def proxy_http(request: Request, full_path: str):
            """Proxy handler for subpaths (e.g., /chat-explorer/...)."""
            # Keep the full path for Vite (it knows about /chat-explorer/ base)
            path = request.url.path
            if path.startswith("/chat-explorer/"):
                vite_path = path
            else:
                vite_path = path
            target = f"{VITE_TARGET}{vite_path}"
            if request.url.query:
                target = f"{target}?{request.url.query}"
            headers = dict(request.headers)
            headers.pop("host", None)
            try:
                async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
                    resp = await client.request(
                        request.method,
                        target,
                        headers=headers,
                        content=await request.body(),
                    )
                    # Read the full response body to avoid StreamConsumed errors
                    body = await resp.aread()
                return Response(
                    content=body,
                    status_code=resp.status_code,
                    headers={k: v for k, v in resp.headers.items() if k.lower() not in {"content-encoding", "transfer-encoding", "connection"}},
                    media_type=resp.headers.get("content-type"),
                )
            except Exception as e:
                log.error("Vite proxy error", target=target, error=str(e), exc_info=True)
                return JSONResponse(
                    {"error": "Proxy error", "target": target, "message": str(e)},
                    status_code=502
                )

        try:
            import websockets
        except Exception:
            websockets = None  # type: ignore

        async def proxy_ws_base(websocket: WebSocket):
            """WebSocket proxy for base paths (e.g., /chat-explorer)."""
            await proxy_ws(websocket, "")

        async def proxy_ws(websocket: WebSocket, full_path: str = ""):
            if websockets is None:
                await websocket.close(code=1011)
                return
            path = websocket.url.path
            qs = websocket.url.query
            
            # Map paths similar to HTTP proxy - keep full path for Vite
            if path == "/chat-explorer" or path.startswith("/chat-explorer/"):
                # Keep the full path for Vite (it knows about /chat-explorer/ base)
                if path == "/chat-explorer":
                    vite_path = "/chat-explorer/"
                else:
                    vite_path = path
            elif path.startswith("/@vite") or path.startswith("/src") or path.startswith("/node_modules"):
                # HMR WebSocket connections - map directly
                vite_path = path
            else:
                vite_path = path
            
            # Convert http:// to ws:// for WebSocket target
            target = VITE_TARGET.replace("http://", "ws://").replace("https://", "wss://") + vite_path
            if qs:
                target = f"{target}?{qs}"
            
            try:
                await websocket.accept()
            except Exception as e:
                log.warning("WebSocket accept failed", path=path, error=str(e))
                return
            
            try:
                async with websockets.connect(target, ping_interval=None) as target_ws:  # type: ignore
                    async def _client_to_target():
                        try:
                            while True:
                                msg = await websocket.receive_text()
                                await target_ws.send(msg)
                        except (WebSocketDisconnect, Exception) as e:
                            if not isinstance(e, WebSocketDisconnect):
                                log.debug("WebSocket client->target error", path=path, error=str(e))
                            try:
                                await target_ws.close()
                            except Exception:
                                pass  # best-effort close; target may be gone

                    async def _target_to_client():
                        try:
                            while True:
                                msg = await target_ws.recv()
                                if isinstance(msg, bytes):
                                    await websocket.send_bytes(msg)
                                else:
                                    await websocket.send_text(msg)
                        except (WebSocketDisconnect, Exception) as e:
                            if not isinstance(e, WebSocketDisconnect):
                                log.debug("WebSocket target->client error", path=path, error=str(e))
                            try:
                                await websocket.close()
                            except Exception:
                                pass  # best-effort close; client may be gone

                    await asyncio.gather(_client_to_target(), _target_to_client(), return_exceptions=True)
            except Exception as e:
                log.warning("WebSocket proxy connection failed", path=path, target=target, error=str(e))
                try:
                    await websocket.close(code=1011)
                except Exception:
                    pass  # best-effort close on proxy error

        for prefix in VITE_PATHS:
            # Match base path exactly
            app.add_api_route(
                prefix,
                proxy_http_base,
                methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
                include_in_schema=False,
            )
            # Match any subpath
            app.add_api_route(
                f"{prefix}/{{full_path:path}}",
                proxy_http,
                methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
                include_in_schema=False,
            )
            # WebSocket routes for subpaths
            app.add_api_websocket_route(f"{prefix}/{{full_path:path}}", proxy_ws)
            # Also handle base path for WebSocket (e.g., /chat-explorer)
            if prefix == "/chat-explorer":
                app.add_api_websocket_route(prefix, proxy_ws_base)

        # Motet management app (ops-dashboard) proxy routes (ADR-0066); served at /manage
        async def proxy_manage_http_base(request: Request):
            """Proxy handler for Motet management app base path."""
            path = request.url.path
            if path == "/manage" or path == "/manage/":
                vite_path = "/manage/"
            elif path.startswith("/manage/"):
                vite_path = path
            else:
                vite_path = path
            target = f"{ADMIN_VITE_TARGET}{vite_path}"
            if request.url.query:
                target = f"{target}?{request.url.query}"
            headers = dict(request.headers)
            headers.pop("host", None)
            try:
                async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
                    resp = await client.request(
                        request.method,
                        target,
                        headers=headers,
                        content=await request.body(),
                    )
                    body = await resp.aread()
                return Response(
                    content=body,
                    status_code=resp.status_code,
                    headers={k: v for k, v in resp.headers.items() if k.lower() not in {"content-encoding", "transfer-encoding", "connection"}},
                    media_type=resp.headers.get("content-type"),
                )
            except Exception as e:
                log.error("Manage Vite proxy error", target=target, error=str(e), exc_info=True)
                return JSONResponse(
                    {"error": "Proxy error", "target": target, "message": str(e)},
                    status_code=502
                )

        async def proxy_manage_http(request: Request, full_path: str):
            """Proxy handler for Motet management app subpaths."""
            path = request.url.path
            if path.startswith("/manage/"):
                vite_path = path
            else:
                vite_path = path
            target = f"{ADMIN_VITE_TARGET}{vite_path}"
            if request.url.query:
                target = f"{target}?{request.url.query}"
            headers = dict(request.headers)
            headers.pop("host", None)
            try:
                async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
                    resp = await client.request(
                        request.method,
                        target,
                        headers=headers,
                        content=await request.body(),
                    )
                    body = await resp.aread()
                return Response(
                    content=body,
                    status_code=resp.status_code,
                    headers={k: v for k, v in resp.headers.items() if k.lower() not in {"content-encoding", "transfer-encoding", "connection"}},
                    media_type=resp.headers.get("content-type"),
                )
            except Exception as e:
                log.error("Admin Vite proxy error", target=target, error=str(e), exc_info=True)
                return JSONResponse(
                    {"error": "Proxy error", "target": target, "message": str(e)},
                    status_code=502
                )

        # Register Motet management app (ops-dashboard) proxy routes at /manage
        app.add_api_route(
            "/manage",
            proxy_manage_http_base,
            methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
            include_in_schema=False,
        )
        app.add_api_route(
            "/manage/{full_path:path}",
            proxy_manage_http,
            methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
            include_in_schema=False,
        )

    @app.get("/ops", include_in_schema=False)
    @app.get("/ops/{full_path:path}", include_in_schema=False)
    async def ops_retired(full_path: str = ""):
        """Legacy Jinja /ops dashboard is retired; send bookmarks to /manage."""
        target = "/manage/" + full_path if full_path else "/manage"
        return RedirectResponse(url=target, status_code=308)

    @app.get("/", include_in_schema=False)
    async def root():
        return JSONResponse({"name": "Motet API", "version": app.version})

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        # Proxy favicon requests to Vite if proxy is enabled
        if _os.getenv("MOTET_DEV_VITE_PROXY", "").lower() in {"1", "true", "yes"}:
            try:
                VITE_TARGET = _os.getenv("MOTET_DEV_VITE_TARGET", "http://localhost:5173")
                async with httpx.AsyncClient(follow_redirects=True, timeout=5.0) as client:
                    resp = await client.get(f"{VITE_TARGET}/chat-explorer/favicon.ico")
                    if resp.status_code == 200:
                        return Response(
                            content=await resp.aread(),
                            status_code=resp.status_code,
                            media_type=resp.headers.get("content-type", "image/x-icon"),
                        )
            except Exception:
                pass  # optional favicon from Vite; 404 fallback is fine
        return Response(status_code=404)

    # API v1 endpoints (ADR-0053). Load each router individually so one failure doesn't drop all.
    _API_V1_PACKAGE = "motet.interfaces.api.v1"
    _API_V1_ROUTERS = [
        "commands", "schedules", "vault", "debug", "workers", "workflows",
        "tools", "memories", "conversations", "models", "chat",
        "service_accounts", "events", "identity", "oauth", "auth",
        "artifacts", "cost", "developer_docs", "deploy", "agents", "devices",
        "image_stacks", "workspace_containers", "skills", "tenants", "surfaces",
        "tasks", "mcp", "version",
    ]
    import importlib
    _registered = []
    _failed = []
    for _name in _API_V1_ROUTERS:
        try:
            _mod_obj = importlib.import_module(f".{_name}", package=_API_V1_PACKAGE)
            _router = getattr(_mod_obj, "router", None)
            if _router is not None:
                app.include_router(_router)
                _registered.append(_name)
            else:
                logger.warning("API v1 module has no 'router'", module=_name)
                _failed.append(_name)
        except Exception as e:
            logger.warning(
                "Failed to load API v1 router",
                router=_name,
                error=str(e),
                exc_info=True,
            )
            _failed.append(_name)
    # Use print so it appears in docker logs even if log level filters structlog
    _summary = f"API v1: {len(_registered)} routers registered"
    if _failed:
        _summary += f", {len(_failed)} failed: {','.join(_failed)}"
    print(_summary)
    logger.info(
        "API v1 routers loaded",
        registered=len(_registered),
        failed=len(_failed),
        failed_routers=_failed if _failed else None,
    )

    # OpenAI-compatible facade (ADR-0125). Mounted at a drop-in prefix outside
    # /api/v1 because OpenAI clients hard-code the path suffix; this is the
    # documented ADR-0053 exception. Off unless explicitly enabled, since it
    # exposes models (and in deeper modes, tools) to third-party clients.
    if bool(getattr(cfg, "openai_compat_enabled", False)):
        try:
            from .api.openai_compat import router as _openai_compat_router

            _openai_compat_prefix = str(getattr(cfg, "openai_compat_prefix", "/v1") or "/v1")
            app.include_router(_openai_compat_router, prefix=_openai_compat_prefix.rstrip("/"))
            print(f"OpenAI-compatible facade mounted at {_openai_compat_prefix}")
            logger.info(
                "openai_compat_facade_mounted",
                prefix=_openai_compat_prefix,
                default_mode=getattr(cfg, "openai_compat_default_mode", "passthrough"),
            )
        except Exception as e:
            logger.error(
                "openai_compat_facade_mount_failed",
                error=str(e),
                exc_info=True,
            )

    from ..core.security import require_api_key as _sec_require_api_key
    from ..core.security import require_jwt_if_configured as _sec_require_jwt
    from ..core.security import extract_principal as _sec_extract_principal
    def _require_api_key(header_value: Optional[str]) -> None:
        _sec_require_api_key(cfg, header_value)

    def _require_jwt_if_configured(request: Request) -> None:
        _sec_require_jwt(cfg, request)

    from ..core.security import RateLimiter as _RateLimiter
    _rl = _RateLimiter(
        backend=cfg.rate_limit_backend,
        redis_url=cfg.redis_url,
        limit_per_minute=cfg.rate_limit_per_minute,
        window_seconds=cfg.rate_limit_window_seconds or 60,
    )
    def _rate_limit(client_id: str) -> None:
        _rl.check(client_id)

    # request/trace id middleware & basic metrics
    import uuid
    from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST, CollectorRegistry
    from ..core.observability.metrics import set_registry
    REGISTRY = CollectorRegistry(auto_describe=True)
    REQ_COUNTER = Counter("imf_requests_total", "Total requests", ["method", "path", "status"], registry=REGISTRY)
    LATENCY = Histogram("imf_request_latency_seconds", "Request latency", ["method", "path"], registry=REGISTRY)
    # Authentication metrics
    AUTH_ATTEMPTS = Counter("imf_auth_attempts_total", "Total authentication attempts", ["auth_type", "status"], registry=REGISTRY)
    AUTH_LATENCY = Histogram("imf_auth_latency_seconds", "Authentication latency", ["auth_type"], registry=REGISTRY)
    try:
        set_registry(REGISTRY)
        
        # Initialize distributed metrics collector
        from ..core.observability.distributed_metrics import initialize_distributed_metrics
        import redis.asyncio as redis
        import os
        
        redis_url = os.getenv('MOTET_REDIS_URL', DEFAULT_REDIS_URL)
        redis_client = redis.from_url(redis_url)
        initialize_distributed_metrics(redis_client)
        
    except Exception as exc:
        try:
            log.warning("metrics_registry_set_failed", error=str(exc))
        except Exception:
            pass  # logging fallback; must not crash startup

    @app.middleware("http")
    async def add_trace_and_metrics(request: Request, call_next):
        trace_id = request.headers.get("X-Trace-Id") or uuid.uuid4().hex
        request.state.trace_id = trace_id
        try:
            import structlog
            from structlog.contextvars import bind_contextvars

            bind_contextvars(trace_id=trace_id)
        except Exception:
            pass  # optional trace context; proceed without
        # start trace if enabled
        try:
            # Attach identity if present
            principal = None
            auth_type = "none"
            auth_status = "failure"
            auth_start = time.time()
            try:
                principal = _sec_extract_principal(cfg, request)
                if principal:
                    auth_status = "success"
                    # Determine auth type
                    claims = getattr(principal, "claims", {}) or {}
                    if claims.get("type") == "service_account":
                        auth_type = "service_account"
                    elif "sub" in claims or getattr(principal, "id", None):
                        auth_type = "jwt"
                    else:
                        auth_type = "header"
                else:
                    auth_type = "none"
            except Exception as e:
                auth_type = "error"
                auth_status = "failure"
                principal = None
            auth_duration = time.time() - auth_start
            # Track authentication metrics
            try:
                AUTH_ATTEMPTS.labels(auth_type=auth_type, status=auth_status).inc()
                if auth_type != "none":
                    AUTH_LATENCY.labels(auth_type=auth_type).observe(auth_duration)
            except Exception:
                pass  # metrics optional; must not break request
            try:
                request.state.principal = principal
                request.state.tenant_id = getattr(principal, "tenant_id", None) if principal else None
                request.state.motet_id = getattr(principal, "motet_id", None) if principal else None
            except Exception:
                pass  # state assignment optional; proceed with defaults
            try:
                from structlog.contextvars import bind_contextvars as _bind
                _bind(
                    principal_id=(getattr(principal, "id", None) if principal else None),
                    tenant_id=getattr(request.state, "tenant_id", None),
                    motet_id=getattr(request.state, "motet_id", None)
                )
            except Exception:
                pass  # trace context optional; proceed without
            tracing.start_trace(trace_id, {
                "path": str(request.url.path),
                "method": request.method,
                "tenant_id": getattr(request.state, "tenant_id", None),
                "principal_id": getattr(principal, "id", None) if principal else None,
                "motet_id": getattr(request.state, "motet_id", None)
            })
        except Exception as exc:
            try:
                log.warning("trace_start_failed", error=str(exc))
            except Exception:
                pass  # logging fallback; must not break middleware
        # Global rate limiting (per-principal or per-IP)
        # Skip static assets, health checks, and dev proxy paths
        _rl_path = request.url.path
        _skip_rl = (
            _rl_path == "/health"
            or _rl_path.startswith("/chat-explorer/")
            or _rl_path.startswith("/manage/")
            or _rl_path.startswith("/@vite")
            or _rl_path.startswith("/src/")
            or _rl_path.startswith("/node_modules/")
            or _rl_path == "/metrics"
            or _rl_path == "/favicon.ico"
        )
        if not _skip_rl:
            try:
                _principal = getattr(request.state, "principal", None)
                client_id = (getattr(_principal, "id", None) if _principal else None) or (request.client.host if request.client else "unknown")
                _rate_limit(client_id)
            except HTTPException:
                raise
            except Exception:
                pass  # rate limiter failure must not block requests
        start = time.time()
        response = await call_next(request)
        duration = time.time() - start
        try:
            REQ_COUNTER.labels(request.method, request.url.path, str(response.status_code)).inc()
            LATENCY.labels(request.method, request.url.path).observe(duration)
        except Exception as exc:
            try:
                log.warning("metrics_record_failed", error=str(exc))
            except Exception:
                pass  # logging fallback; must not break response
        _apply_security_headers(request, response, cfg)
        response.headers["X-Trace-Id"] = trace_id
        try:
            tracing.end_trace(trace_id, {"status": response.status_code, "duration": duration})
        except Exception as exc:
            try:
                log.warning("trace_end_failed", error=str(exc))
            except Exception:
                pass  # logging fallback; must not break response
        return response




    @app.get("/health", include_in_schema=False)
    async def health():
        # Liveness only. Do not FT.SEARCH here — Docker hits this every few
        # seconds and a hung Valkey Search command blocks the asyncio loop,
        # so Chat Explorer SSE never flushes tokens.
        redis_ok = False
        try:
            from motet.core.distributed.redis_manager import get_redis_client

            redis_ok = bool(await get_redis_client().ping())
        except Exception:
            redis_ok = False
        return JSONResponse(
            {
                "status": "ok",
                "components": {
                    "memory": bool(stack.memory),
                    "redis": redis_ok,
                    "vector": bool(getattr(stack, "vector", None)),
                    "orchestrator": bool(getattr(stack, "orchestrator", None)),
                },
            }
        )


    @app.get("/metrics", include_in_schema=False)
    async def metrics(request: Request):
        if cfg.require_auth_for_ops_endpoints:
            from .api.shared.auth import get_current_principal as _get_principal
            await _get_principal(request)
        from fastapi import Response
        
        # Get local metrics from HTTP server
        local_metrics = generate_latest(REGISTRY).decode('utf-8')
        
        # Get distributed metrics from workers
        try:
            from ..core.observability.distributed_metrics import get_distributed_metrics_collector, format_prometheus_metrics
            
            collector = get_distributed_metrics_collector()
            if collector:
                worker_metrics_samples = await collector.get_all_metrics()
                worker_metrics = format_prometheus_metrics(worker_metrics_samples)
                
                # Combine local and worker metrics
                combined_metrics = local_metrics + worker_metrics
            else:
                combined_metrics = local_metrics
                
        except Exception as e:
            # If distributed metrics fail, just return local metrics
            combined_metrics = local_metrics
        
        return Response(content=combined_metrics.encode('utf-8'), media_type=CONTENT_TYPE_LATEST)

    @app.get("/health/vector", include_in_schema=False)
    async def health_vector():
        vec = getattr(stack, "vector", None)
        if not vec:
            return JSONResponse({"enabled": False, "healthy": False})
        backend = "valkey"
        details: dict[str, Any] = {}
        import time as _t
        start = _t.time()
        try:
            healthy = True
            await asyncio.wait_for(
                asyncio.to_thread(vec.list_by_tag, "_health_probe_", 1),
                timeout=2.0,
            )
            if hasattr(vec, "_index_name"):
                details["index"] = vec._index_name  # type: ignore[attr-defined]
        except Exception as exc:
            return JSONResponse({"enabled": True, "healthy": False, "error": str(exc)})
        latency = (_t.time() - start)
        return JSONResponse({"enabled": True, "healthy": healthy, "backend": backend, "latency_ms": int(latency * 1000), **details})

    # Simple event stream for plan/step/end (debug only)
    # Session list/inspect/clear


    return app


# Instantiate module-level app for convenience
app = create_app()


__all__ = ["create_app", "app"]


