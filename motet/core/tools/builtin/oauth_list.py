"""
Motet - OAuth List Tool

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Built-in tool for listing available OAuth services and their connection status.
    This validates tokens with providers when possible, and uses tenant/motet
    isolation for credential access.

Dependencies:
    - motet.core.tools.registry: Runtime context (principal/tenant/motet)
    - motet.core.tools.mcp_motet.proxy.mcp_instance_manager: OAuth config loading
    - motet.core.security.oauth_manager: OAuth status validation via provider
    - motet.core.utils.async_helpers: Safe async execution in sync tool context

Usage:
    User: "What services can I connect to?"
    Tool: oauth_list()

Notes:
    - Part of MCP OAuth Prompt Flow
"""

from typing import Any, Dict

import structlog
from pydantic import BaseModel, Field

from ..registry import ToolRegistry

logger = structlog.get_logger(__name__)


class Params(BaseModel):
    """Schema for oauth_list tool parameters."""

    include_non_oauth: bool = Field(
        default=False,
        description="Include services that don't use OAuth (api_key, service_account)",
    )


def run(_params: Dict[str, Any]) -> Dict[str, Any]:
    """
    List all available OAuth services and their connection status.

    Returns:
        List of services with their connection status
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

    providers = get_oauth_providers_from_config()
    oauth_manager = OAuthManager()

    services = []
    for service_id, config in providers.items():
        display_name = config.get("display_name", service_id.replace("_", " ").title())
        description = config.get("description", "")

        connected = False
        needs_reauth = False
        try:
            status = run_async_safe(
                oauth_manager.get_oauth_status(
                    server_id=service_id,
                    principal_id=principal_id,
                    tenant_id=tenant_id,
                    motet_id=motet_id,
                )
            )
            connected = status.get("authenticated", False)
            needs_reauth = status.get("needs_reauth", False)

            logger.debug(
                "Checked OAuth status for service",
                service_id=service_id,
                connected=connected,
                needs_reauth=needs_reauth,
                principal_id=principal_id,
            )
        except Exception as e:
            logger.warning(
                "Error checking OAuth status for service",
                service_id=service_id,
                principal_id=principal_id,
                tenant_id=tenant_id,
                error=str(e),
            )

        service_info = {
            "service_id": service_id,
            "display_name": display_name,
            "description": description,
            "auth_type": config.get("type", "oauth2"),
            "connected": connected,
            "scopes": config.get("scopes", []),
        }
        if needs_reauth:
            service_info["needs_reauth"] = True

        services.append(service_info)

    # Sort: connected first, then alphabetically
    services.sort(key=lambda x: (not x["connected"], x["display_name"]))

    connected_count = sum(1 for s in services if s["connected"])
    disconnected_count = len(services) - connected_count

    logger.info(
        "Listed OAuth services",
        total=len(services),
        connected=connected_count,
        principal_id=principal_id,
    )

    # Build detailed message with connection status
    if len(services) == 0:
        message = "No OAuth services are configured."
    else:
        connected_names = [s.get("display_name", s.get("service_id", "unknown")) 
                          for s in services if s.get("connected")]
        message_parts = [f"Found {len(services)} OAuth service(s)."]
        
        if connected_count > 0:
            if connected_count <= 3:
                message_parts.append(f"Connected: {', '.join(connected_names)}.")
            else:
                message_parts.append(f"Connected ({connected_count}): {', '.join(connected_names[:3])} and {connected_count - 3} more.")
        
        if disconnected_count > 0:
            message_parts.append(f"{disconnected_count} service(s) not connected.")
        
        message = " ".join(message_parts)

    return {
        "services": services,
        "total_count": len(services),
        "connected_count": connected_count,
        "disconnected_count": disconnected_count,
        "message": message,
    }


def _parse(_tool_name: str, params_str: str) -> Dict[str, Any]:
    """Parse parameters from string."""
    import json

    try:
        return json.loads(params_str)
    except Exception:
        return {}


def _fmt(res: Dict[str, Any]) -> str:
    """Format result for observation."""
    if "error" in res:
        return f"oauth_list(error={res['error']})"
    
    total = res.get("total_count", 0)
    connected = res.get("connected_count", 0)
    services = res.get("services", [])
    
    if total == 0:
        return "oauth_list(total=0, no services configured)"
    
    # Build summary of connected/disconnected services
    connected_services = [s.get("display_name", s.get("service_id", "unknown")) 
                         for s in services if s.get("connected")]
    disconnected_services = [s.get("display_name", s.get("service_id", "unknown")) 
                            for s in services if not s.get("connected")]
    
    parts = [f"oauth_list(total={total}, connected={connected})"]
    
    if connected_services:
        parts.append(f"Connected: {', '.join(connected_services[:5])}")
        if len(connected_services) > 5:
            parts.append(f"(+{len(connected_services) - 5} more)")
    
    if disconnected_services and len(disconnected_services) <= 5:
        parts.append(f"Not connected: {', '.join(disconnected_services)}")
    elif disconnected_services:
        parts.append(f"Not connected: {', '.join(disconnected_services[:3])} (+{len(disconnected_services) - 3} more)")
    
    return " | ".join(parts)


def register(registry: ToolRegistry) -> None:
    """Register oauth_list tool."""
    description = (
        "List all available OAuth services and integrations, showing which ones the user is connected to. "
        "Shows connection status for Google Workspace, Gmail, Google Calendar, Google Drive, GitHub, Slack, "
        "Zoom, and other configured OAuth providers. Use when the user asks: what services are available, "
        "what am I connected to, show integrations, list connections, check OAuth status, or view accounts."
    )

    registry.register(
        name="core.oauth_list",
        description=description,
        func=run,
        tool_schema=Params,
        triggers=["list:", "show:", "services:", "oauth_list:"],
        priority=8,
        parse_params=_parse,
        observation_formatter=_fmt,
        category="oauth",
        keywords=[
            "oauth_list", "list", "show", "services", "oauth", "integrations", "connections",
            "status", "connected", "available", "accounts", "google", "gmail", "calendar",
            "drive", "github", "slack", "zoom", "workspace", "check", "view"
        ],
    )


__all__ = ["register"]


