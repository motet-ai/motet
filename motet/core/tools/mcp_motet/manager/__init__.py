"""
Motet - MCP Instance Manager Package

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-13

Description:
    Process-owner package for MCP server instances. Public imports stay on
    ``motet.core.tools.mcp_motet.proxy.mcp_instance_manager`` for back-compat;
    this package holds config, restart budget, per-service status, the
    Redis control plane, and the composed MCPInstanceManager (mixins).

Usage:
    from motet.core.tools.mcp_motet.manager.config import MCPInstanceConfig
    from motet.core.tools.mcp_motet.manager.restart_budget import ServiceRestartBudget
"""

from motet.core.tools.mcp_motet.manager.config import (
    AuthType,
    CredentialCheckResult,
    InstanceManagerConfig,
    MCPInstance,
    MCPInstanceConfig,
    ServiceAuthConfig,
    get_oauth_providers_from_config,
)
from motet.core.tools.mcp_motet.manager.control_commands import (
    MCP_CONTROL_OPS,
    enqueue_mcp_control_command,
    mcp_control_stream_key,
)
from motet.core.tools.mcp_motet.manager.restart_budget import ServiceRestartBudget
from motet.core.tools.mcp_motet.manager.service_status import (
    MCPServiceStatus,
    list_mcp_service_statuses,
    publish_mcp_service_status,
    services_status_key,
)

__all__ = [
    "AuthType",
    "CredentialCheckResult",
    "InstanceManagerConfig",
    "MCPInstance",
    "MCPInstanceConfig",
    "MCPServiceStatus",
    "MCP_CONTROL_OPS",
    "ServiceAuthConfig",
    "ServiceRestartBudget",
    "enqueue_mcp_control_command",
    "get_oauth_providers_from_config",
    "list_mcp_service_statuses",
    "mcp_control_stream_key",
    "publish_mcp_service_status",
    "services_status_key",
]
