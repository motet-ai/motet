"""
Motet - OAuth Logout Tool

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Built-in tool for disconnecting from OAuth services by revoking stored
    credentials. This uses tenant/motet isolation for vault paths
    and triggers downstream handling via mcp.auth_revoked events.

Dependencies:
    - motet.core.tools.registry: Runtime context (principal/tenant/motet)
    - motet.core.tools.mcp_motet.proxy.mcp_instance_manager: OAuth config loading
    - motet.core.security.oauth_manager: Token revocation and event emission
    - motet.core.utils.async_helpers: Safe async execution in sync tool context

Usage:
    User: "Disconnect from Google Workspace"
    Tool: oauth_logout(service_id="google_workspace")

Notes:
    - Part of MCP OAuth Prompt Flow
"""

from typing import Any, Dict

import structlog
from pydantic import BaseModel, Field

from ..registry import ToolRegistry

logger = structlog.get_logger(__name__)


class Params(BaseModel):
    """Schema for oauth_logout tool parameters."""

    service_id: str = Field(
        ...,
        description="Service ID to disconnect (e.g., 'google_workspace', 'github', 'slack')",
    )


def run(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Disconnect from an OAuth service by revoking credentials.

    Args:
        params: Contains service_id to disconnect

    Returns:
        Success or error message
    """
    from ...tools.mcp_motet.proxy.mcp_instance_manager import get_oauth_providers_from_config
    from ...security.oauth_manager import get_oauth_manager
    from motet.core.security.system_principals import (
        SYSTEM_PRINCIPAL_OAUTH_LOGOUT,
        SYSTEM_TENANT_ID,
        SYSTEM_MOTET_ID,
    )
    from ...utils.async_helpers import run_async_safe
    from ..registry import get_runtime_stack

    stack = get_runtime_stack()
    if not stack:
        return {"error": "Runtime stack not available"}

    from ...workers.invoker_context import resolve_current_identity
    from ...workers.invoker_context import IdentityContext
    identity = resolve_current_identity(
        system_defaults=IdentityContext(
            principal_id=SYSTEM_PRINCIPAL_OAUTH_LOGOUT,
            tenant_id=SYSTEM_TENANT_ID,
            motet_id=SYSTEM_MOTET_ID,
        ),
    )
    principal_id = identity.principal_id
    tenant_id = identity.tenant_id
    motet_id = identity.motet_id

    service_id = params.get("service_id")
    if not service_id:
        return {"error": "service_id is required"}

    providers = get_oauth_providers_from_config()
    if service_id not in providers:
        return {"disconnected": False, "error": f"Service '{service_id}' is not configured for OAuth."}

    provider_config = providers[service_id]
    display_name = provider_config.get("display_name", service_id.replace("_", " ").title())

    try:
        oauth_manager = get_oauth_manager()

        result = run_async_safe(
            oauth_manager.revoke_credentials(
                server_id=service_id,
                principal_id=principal_id,
                tenant_id=tenant_id,
                motet_id=motet_id,
                revoke_at_provider=False,
            )
        )

        if result.get("success"):
            logger.info(
                "Disconnected from OAuth service",
                service_id=service_id,
                principal_id=principal_id or SYSTEM_PRINCIPAL_OAUTH_LOGOUT,
                tenant_id=tenant_id or SYSTEM_TENANT_ID,
                motet_id=motet_id or SYSTEM_MOTET_ID,
            )
            return {
                "disconnected": True,
                "service_id": service_id,
                "display_name": display_name,
                "message": f"Successfully disconnected from {display_name}. Your credentials have been revoked.",
            }

        # "Not found" case
        if "No OAuth credentials found" in result.get("message", ""):
            return {
                "disconnected": False,
                "service_id": service_id,
                "display_name": display_name,
                "message": f"You are not currently connected to {display_name}.",
            }

        return {
            "disconnected": False,
            "service_id": service_id,
            "display_name": display_name,
            "error": result.get("message", f"Failed to revoke credentials for {display_name}."),
        }

    except Exception as e:
        logger.error(
            "Error disconnecting from service",
            service_id=service_id,
            error=str(e),
            exc_info=True,
        )
        return {"disconnected": False, "service_id": service_id, "error": f"Error disconnecting from {display_name}: {e}"}


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
        return f"oauth_logout(error={res['error']})"
    if res.get("disconnected"):
        return f"oauth_logout(disconnected=True, service={res.get('service_id')})"
    return f"oauth_logout(disconnected=False, service={res.get('service_id', 'unknown')})"


def register(registry: ToolRegistry) -> None:
    """Register oauth_logout tool."""
    description = (
        "Logout, disconnect, or revoke credentials from an OAuth service like Google Workspace, Gmail, "
        "Google Calendar, Google Drive, GitHub, Slack, Zoom, or other integrations. Use this tool when "
        "the user wants to: logout, log out, sign out, sign-out, disconnect, revoke, unlink, remove access, "
        "disable integration, or remove a service connection. Permanently removes stored credentials."
    )

    registry.register(
        name="core.oauth_logout",
        description=description,
        func=run,
        tool_schema=Params,
        triggers=["disconnect:", "logout:", "revoke:", "oauth_logout:"],
        priority=8,
        parse_params=_parse,
        observation_formatter=_fmt,
        category="oauth",
        keywords=[
            "oauth_logout", "logout", "log out", "sign out", "sign-out", "disconnect", "revoke", 
            "oauth", "deauthorize", "unlink", "remove", "access", "disable", "integration",
            "google", "gmail", "calendar", "drive", "github", "slack", "zoom", "workspace"
        ],
    )


__all__ = ["register"]


