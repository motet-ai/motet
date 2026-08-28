"""
Motet - OAuth Login Tool

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Built-in tool for connecting to OAuth services from chat.
    Allows users to initiate OAuth flows using natural language commands like
    "login to google workspace" or "connect my Gmail account".

    This tool checks if the user is already connected first. If not connected,
    it returns an auth_required response so the UI can trigger the OAuth popup
    flow. Vault credential paths are isolated by tenant/motet.

Dependencies:
    - motet.core.tools.registry: Runtime context (principal/tenant/motet)
    - motet.core.tools.mcp_motet.proxy.mcp_instance_manager: OAuth config loading
    - motet.core.security.oauth_manager: OAuth status validation via provider
    - motet.core.utils.async_helpers: Safe async execution in sync tool context

Usage:
    User: "Login to Google Workspace"
    Tool: oauth_login(service_id="google_workspace")

Notes:
    - Part of MCP OAuth Prompt Flow
    - Returns auth_required response format for UI compatibility
"""

from typing import Any, Dict

import structlog
from pydantic import BaseModel, Field

from ..registry import ToolRegistry

logger = structlog.get_logger(__name__)


class Params(BaseModel):
    """Schema for oauth_login tool parameters."""

    service_id: str = Field(
        ...,
        description="Service ID to connect (e.g., 'google_workspace', 'github', 'slack')",
    )


def run(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Connect to an OAuth service. If already connected, returns success.
    If not connected, returns auth_required to trigger OAuth flow in UI.

    Args:
        params: Contains service_id to connect

    Returns:
        Success message if already connected, or auth_required response
    """

    from ...tools.mcp_motet.proxy.mcp_instance_manager import get_oauth_providers_from_config
    from ...security.oauth_manager import OAuthManager
    from ...utils.async_helpers import run_async_safe
    from ..registry import get_runtime_stack

    # Get runtime stack for principal/tenant context (thread-safe via WorkerLocal)
    stack = get_runtime_stack()
    if not stack:
        return {"error": "Runtime stack not available"}

    from ...workers.invoker_context import resolve_current_identity
    identity = resolve_current_identity()
    principal_id = identity.principal_id
    tenant_id = identity.tenant_id
    motet_id = identity.motet_id

    service_id = params.get("service_id")
    if not service_id:
        return {"error": "service_id is required"}

    # Get OAuth providers from config
    providers = get_oauth_providers_from_config()

    if service_id not in providers:
        return {
            "connected": False,
            "error": (
                f"Service '{service_id}' is not configured for OAuth. "
                f"Available OAuth services: {list(providers.keys())}"
            ),
        }

    provider_config = providers[service_id]
    display_name = provider_config.get("display_name", service_id.replace("_", " ").title())

    # Check if already connected AND tokens are valid (validate with provider)
    try:
        oauth_manager = OAuthManager()

        status = run_async_safe(
            oauth_manager.get_oauth_status(
                server_id=service_id,
                principal_id=principal_id,
                tenant_id=tenant_id,
                motet_id=motet_id,
            )
        )

        if status.get("authenticated"):
            logger.info(
                "User already connected to service with valid token",
                service_id=service_id,
                principal_id=principal_id,
            )
            return {
                "connected": True,
                "service_id": service_id,
                "display_name": display_name,
                "message": f"You are already connected to {display_name}. Your credentials are valid.",
            }

        if status.get("needs_reauth"):
            logger.info(
                "User has expired/invalid tokens, prompting re-auth",
                service_id=service_id,
                principal_id=principal_id,
            )
            # Fall through to return auth_required

    except Exception as e:
        logger.debug(
            "Error checking existing tokens, will prompt for auth",
            service_id=service_id,
            error=str(e),
        )

    # Not connected - return auth_required to trigger OAuth flow
    logger.info(
        "Initiating OAuth connect for service",
        service_id=service_id,
        principal_id=principal_id,
    )

    # Note: auth_url is intentionally NOT included - it's just the base OAuth URL
    # without required parameters (client_id, redirect_uri, etc.)
    # The frontend should use authorization_endpoint which handles everything.
    return {
        "auth_required": True,
        "service_id": service_id,
        "provider": provider_config.get("provider", service_id),
        "display_name": display_name,
        "message": f"Please authorize access to {display_name} to continue. A popup window will open for authentication.",
        "authorization_endpoint": f"/api/v1/oauth/{service_id}/initiate",
        "required_scopes": provider_config.get("scopes", []),
        "description": provider_config.get("description", ""),
    }


def _parse(_tool_name: str, params_str: str) -> Dict[str, Any]:
    """Parse parameters from string."""
    import json

    try:
        return json.loads(params_str)
    except Exception:
        return {"service_id": params_str.strip()}


def _fmt(res: Dict[str, Any]) -> str:
    """Format result for observation."""
    if "error" in res:
        return f"oauth_login(error={res['error']})"
    if res.get("connected"):
        return f"oauth_login(connected=True, service={res.get('service_id')})"
    if res.get("auth_required"):
        return f"oauth_login(auth_required=True, service={res.get('service_id')})"
    return f"oauth_login(service={res.get('service_id', 'unknown')})"


def register(registry: ToolRegistry) -> None:
    """Register oauth_login tool."""
    description = (
        "Login, connect, or authenticate to an OAuth service like Google Workspace, Gmail, Google Calendar, "
        "Google Drive, GitHub, Slack, Zoom, or other integrations. Use this tool when the user wants to: "
        "login, sign in, sign-in, log in, authenticate, connect, authorize, link account, grant access, "
        "enable integration, or add a service connection. Supports all configured OAuth providers."
    )

    registry.register(
        name="core.oauth_login",
        description=description,
        func=run,
        tool_schema=Params,
        triggers=["connect:", "login:", "authorize:", "oauth_login:"],
        priority=8,
        parse_params=_parse,
        observation_formatter=_fmt,
        category="oauth",
        keywords=[
            "oauth_login", "login", "sign in", "sign-in", "log in", "authenticate", "connect", 
            "authorize", "oauth", "link", "account", "integration", "google", "gmail", "calendar",
            "drive", "github", "slack", "zoom", "workspace", "grant", "access", "enable"
        ],
    )


__all__ = ["register"]


