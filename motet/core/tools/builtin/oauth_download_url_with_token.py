"""
Motet - OAuth Authenticated URL Download Tool

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Built-in tool for downloading a URL that requires authentication, using the
    caller's OAuth token stored in Vault and isolated by tenant/motet.

    This tool is intended for "follow-up fetches" after an API returns a download
    URL (e.g., transcript/recording file URLs). It avoids pushing raw access tokens
    through LLM-visible parameters by resolving tokens from Vault using the current
    runtime context.

Dependencies:
    - httpx: HTTP client for downloading content
    - pydantic: Parameter validation
    - motet.core.security.oauth_manager: OAuth token retrieval from Vault
    - motet.core.tools.registry: Runtime context (principal/tenant/motet)
    - motet.core.security.auth: Domain allow/deny checks

Usage:
    # Download a URL using the user's stored Zoom OAuth token
    oauth_download_url_with_token(
        url="https://api.zoom.us/v2/...",
        service_id="zoom",
        return_as="text"
    )

Notes:
    - Does NOT accept raw tokens as parameters (reduces leakage risk).
    - Returns either decoded text or base64-encoded bytes (configurable).
    - Intended for relatively small payloads; use max_bytes to cap downloads.
"""

from __future__ import annotations

import base64
from typing import Any, Dict, Optional, Literal
from urllib.parse import urlparse, urlencode, urlunparse, parse_qsl

import httpx
import structlog
from pydantic import BaseModel, Field

from ...config import Config
from ...constants import HTTP_MAX_CONNECTIONS, HTTP_MAX_KEEPALIVE_CONNECTIONS
from ...workers.concurrency_primitives import WorkerLocal
from ..protocol import err
from ..registry import ToolRegistry, get_runtime_stack

logger = structlog.get_logger(__name__)

HTTP_LIMITS = httpx.Limits(max_connections=HTTP_MAX_CONNECTIONS, max_keepalive_connections=HTTP_MAX_KEEPALIVE_CONNECTIONS)
_http_client_local = WorkerLocal()


def _get_http_client() -> httpx.Client:
    """Get or create a sync httpx client per worker/greenlet."""
    if not hasattr(_http_client_local, "client") or _http_client_local.client is None:
        _http_client_local.client = httpx.Client(limits=HTTP_LIMITS, follow_redirects=True, timeout=60.0)
    elif _http_client_local.client.is_closed:
        try:
            _http_client_local.client.close()
        except Exception:
            pass  # best-effort cleanup before recreate
        _http_client_local.client = httpx.Client(limits=HTTP_LIMITS, follow_redirects=True, timeout=60.0)
    return _http_client_local.client


class Params(BaseModel):
    """Schema for oauth_download_url_with_token tool parameters."""

    url: str = Field(..., description="The URL to download", examples=["https://api.zoom.us/v2/users/me/recordings"])
    service_id: str = Field(
        ...,
        description="OAuth service_id to resolve an access token from Vault (e.g., 'zoom')",
        examples=["zoom"],
    )
    token_in: Literal["auto", "authorization_header", "query_param"] = Field(
        default="auto",
        description=(
            "Where to place the OAuth access token when service_id is provided. "
            "If set to auto, the tool will use the provider's configured token placement policy "
            "(from mcp_instance_manager.yaml) when available."
        ),
        examples=["auto"],
    )
    token_query_param: str = Field(
        default="access_token",
        description="Query parameter name to use when token_in=query_param (Zoom uses access_token)",
        examples=["access_token"],
    )
    timeout_seconds: float = Field(
        default=60.0,
        ge=0.5,
        le=300.0,
        description="HTTP timeout in seconds",
        examples=[60.0],
    )
    max_bytes: int = Field(
        default=5_000_000,
        ge=1,
        le=25_000_000,
        description="Maximum bytes to download (hard cap)",
        examples=[5000000],
    )
    return_as: Literal["auto", "text", "base64"] = Field(
        default="auto",
        description="How to return content: auto (text when content-type is text/* or */json), text, or base64",
        examples=["auto"],
    )
    mime_type: Optional[str] = Field(
        default=None,
        description="Optional override for returned content_type (otherwise derived from response headers)",
        examples=["text/plain"],
    )
    filename: Optional[str] = Field(
        default=None,
        description="Optional filename hint for downstream consumers (UI/download handling)",
        examples=["transcript.vtt"],
    )


def _is_textual_content_type(content_type: str, filename: Optional[str] = None) -> bool:
    """
    Determine if content type (and optionally filename) indicates a text file.
    
    Checks both content type and filename extension for common text/transcript formats.
    """
    ct = (content_type or "").lower()
    
    # Check content type
    if ct.startswith("text/") or "json" in ct or "xml" in ct or "javascript" in ct:
        return True
    
    # WebVTT is a text format (used for video transcripts)
    if ct == "video/vtt" or ct == "text/vtt":
        return True
    
    # Check filename extension if provided (for cases where content-type is wrong/missing)
    if filename:
        filename_lower = filename.lower()
        # Common text/transcript file extensions
        text_extensions = [
            ".txt", ".md", ".markdown", ".log", ".csv", ".tsv",
            ".vtt", ".srt", ".smi", ".ass", ".ssa",  # Subtitle/transcript formats
            ".json", ".xml", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
            ".html", ".htm", ".css", ".js", ".ts", ".jsx", ".tsx",
            ".py", ".java", ".cpp", ".c", ".h", ".go", ".rs", ".rb", ".php",
            ".sh", ".bash", ".zsh", ".fish", ".ps1", ".bat", ".cmd",
            ".sql", ".r", ".m", ".pl", ".pm", ".lua", ".vim", ".el",
        ]
        if any(filename_lower.endswith(ext) for ext in text_extensions):
            return True
    
    return False


def _safe_filename_from_url(url: str) -> Optional[str]:
    try:
        path = urlparse(url).path or ""
        name = path.rsplit("/", 1)[-1].strip() or None
        return name
    except Exception:
        return None


def _append_query_param(url: str, key: str, value: str) -> str:
    """Append or replace a query parameter in a URL."""
    parts = urlparse(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query[key] = value
    new_query = urlencode(query, doseq=True)
    return urlunparse((parts.scheme, parts.netloc, parts.path, parts.params, new_query, parts.fragment))


def _get_token_transport_policy(service_id: str) -> tuple[Optional[str], Optional[str]]:
    """
    Load token placement hints for a service_id from mcp_instance_manager.yaml.

    Returns:
        (token_transport, token_query_param)
        - token_transport: "authorization_header" | "query_param" | None
        - token_query_param: e.g. "access_token" | None
    """
    try:
        from ...tools.mcp_motet.proxy.mcp_instance_manager import get_oauth_providers_from_config

        providers = get_oauth_providers_from_config()
        cfg = providers.get(service_id) or {}
        return cfg.get("token_transport"), cfg.get("token_query_param")
    except Exception:
        return None, None


def _looks_like_html_document(text: str) -> bool:
    """Cheap check for HTML documents (used to flag unexpected HTML downloads)."""
    t = (text or "").lstrip().lower()
    return t.startswith("<!doctype html") or t.startswith("<html") or "<html" in t[:2000]


def run(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Download a URL, optionally using an OAuth bearer token resolved from Vault.

    The runtime stack is required for resolving the OAuth token (principal/tenant/motet).
    """
    url = params.get("url")
    if not url:
        return err("url is required")

    timeout_seconds = float(params.get("timeout_seconds", 60.0))
    max_bytes = int(params.get("max_bytes", 5_000_000))
    return_as = params.get("return_as", "auto")
    mime_type_override = params.get("mime_type")
    filename_hint = params.get("filename")
    service_id = params.get("service_id")
    if not service_id:
        return err("service_id is required")
    token_in = params.get("token_in", "auto")
    token_query_param = params.get("token_query_param", "access_token") or "access_token"

    # If token placement is left as auto, prefer per-service config hints.
    cfg_transport, cfg_query_param = _get_token_transport_policy(service_id)
    if cfg_query_param:
        token_query_param = cfg_query_param

    effective_token_in = token_in
    if token_in == "auto":
        effective_token_in = cfg_transport or "authorization_header"

    # Domain allow/deny enforcement (same family of checks as http_get)
    try:
        cfg = Config()
        from ...security import is_host_allowed

        if not is_host_allowed(url, cfg.http_tool_allow_domains, cfg.http_tool_deny_domains):
            return err("domain not allowed" if cfg.http_tool_allow_domains else "domain denied")
    except Exception:
        # If config/auth checks aren't available, proceed (consistent with existing http_get behavior).
        pass

    headers: Dict[str, str] = {"User-Agent": "imf-download/1.0"}

    # Resolve bearer token from Vault via OAuthManager
    stack = get_runtime_stack()
    if not stack:
        return {"error": "Runtime stack not available"}

    from ...workers.invoker_context import resolve_current_identity
    identity = resolve_current_identity()
    principal_id = identity.principal_id
    tenant_id = identity.tenant_id
    motet_id = identity.motet_id

    from ...security.oauth_manager import get_oauth_manager
    from ...utils.async_helpers import run_async_safe

    oauth_manager = get_oauth_manager()
    tokens = run_async_safe(
        oauth_manager.get_tokens(
            server_id=service_id,
            principal_id=principal_id,
            tenant_id=tenant_id,
            motet_id=motet_id,
        )
    )
    if not tokens or not isinstance(tokens, dict) or not tokens.get("access_token"):
        return {
            "auth_required": True,
            "service_id": service_id,
            "message": f"Please authorize {service_id} to download this URL.",
            "authorization_endpoint": f"/api/v1/oauth/{service_id}/initiate",
        }

    access_token = str(tokens["access_token"])
    if effective_token_in == "authorization_header":
        headers["Authorization"] = f"Bearer {access_token}"
    elif effective_token_in == "query_param":
        url = _append_query_param(url, token_query_param, access_token)
    else:
        # Fall back safely
        headers["Authorization"] = f"Bearer {access_token}"

    client = _get_http_client()
    try:
        with client.stream("GET", url, headers=headers, timeout=timeout_seconds) as resp:
            content_type = resp.headers.get("content-type", "") or ""
            effective_content_type = mime_type_override or content_type or "application/octet-stream"

            chunks = []
            total = 0
            for chunk in resp.iter_bytes():
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    return err(f"download too large: {total} bytes > {max_bytes}")
                chunks.append(chunk)

            body = b"".join(chunks)

            if resp.status_code >= 400:
                # Return a small snippet for debugging; do not return full payload.
                snippet = ""
                filename_from_url = filename_hint or _safe_filename_from_url(url)
                if _is_textual_content_type(effective_content_type, filename_from_url):
                    try:
                        snippet = body[:512].decode("utf-8", errors="replace")
                    except Exception:
                        snippet = ""
                return err(f"HTTP {resp.status_code} {snippet}".strip())

            # Some providers return an HTML page (HTTP 200) when a download URL is accessed
            # without valid authorization (or when the wrong token placement is used).
            # Flag this as an auth_required-style response when the caller asked for OAuth-backed
            # download (service_id provided), so the UI can prompt re-authorization.
            if service_id and "text/html" in (effective_content_type or "").lower():
                try:
                    html_text = body[:200_000].decode("utf-8", errors="replace")
                    if _looks_like_html_document(html_text):
                        return {
                            "auth_required": True,
                            "service_id": service_id,
                            "message": (
                                f"Unexpected HTML response when downloading a file. "
                                f"Please re-authorize {service_id} and retry."
                            ),
                            "authorization_endpoint": f"/api/v1/oauth/{service_id}/initiate",
                        }
                except Exception:
                    # If decoding fails, fall through and return the raw payload representation.
                    pass

        # Determine filename for text detection
        filename_from_url = filename_hint or _safe_filename_from_url(url)
        
        # Check if we should return as text
        wants_text = return_as == "text" or (
            return_as == "auto" and _is_textual_content_type(effective_content_type, filename_from_url)
        )

        result: Dict[str, Any] = {
            "status": resp.status_code,
            "url": url,
            "content_type": effective_content_type,
            "bytes": len(body),
            "filename": filename_hint or _safe_filename_from_url(url),
        }

        if wants_text:
            try:
                result["text"] = body.decode("utf-8")
            except UnicodeDecodeError:
                result["base64"] = base64.b64encode(body).decode("ascii")
        else:
            result["base64"] = base64.b64encode(body).decode("ascii")

        return result

    except httpx.HTTPStatusError as exc:
        return err(f"HTTP error {exc.response.status_code}")
    except Exception as exc:
        # Avoid logging headers (may contain Authorization)
        logger.error("oauth_download_url_with_token failed", url=url, error=str(exc), exc_info=True)
        return err(f"download failed: {exc}")


def register(registry: ToolRegistry) -> None:
    """Register oauth_download_url_with_token tool."""
    description = (
        "Download a URL and return text or base64. Requires service_id to resolve an OAuth bearer token "
        "from Vault (recommended for authenticated downloads)."
    )

    registry.register(
        name="core.oauth_download_url_with_token",
        description=description,
        func=run,
        tool_schema=Params,
        priority=8,
        category="oauth",
        # IMPORTANT: This tool's primary purpose is to return the downloaded content.
        # The default context manager will drop large `text`/`base64` fields (and also
        # cannot reconstruct non-standard metadata fields like `bytes`/`filename`/`content_type`),
        # which results in tool responses that only include `status` + _context_* markers.
        # Observation policy:
        # - Do not inject into the LLM context directly (contextualize_observation=False) because
        #   downstream context managers may drop the payload fields.
        # - We DO allow storing a capped observation when the download is textual; the observation
        #   formatter will never store base64/binary payloads (preview + metadata only).
        contextualize_observation=False,
        keywords=[
            "oauth_download_url_with_token",
            "download",
            "fetch",
            "file",
            "transcript",
            "recording",
            "url",
            "oauth",
            "bearer",
            "zoom",
        ],
    )


__all__ = ["register"]


