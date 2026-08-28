"""
Motet - MCP Instance Manager (compatibility shim)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Public import path and ``python -m`` entry for the sibling MCP process
    owner. Implementation lives in ``motet.core.tools.mcp_motet.manager``.

Dependencies:
    - motet.core.tools.mcp_motet.manager.instance_manager: composed manager
    - motet.core.tools.mcp_motet.manager.config: Pydantic models

Usage:
    from motet.core.tools.mcp_motet.proxy.mcp_instance_manager import (
        MCPInstanceManager,
        MCPInstanceConfig,
    )
    python -m motet.core.tools.mcp_motet.proxy.mcp_instance_manager

Notes:
    - Docker / compose still launch this module path.
    - Replica pooling and metric auto-scale are not implemented.
"""

from motet.core.tools.mcp_motet.manager.config import (
    AuthType,
    CredentialCheckResult,
    InstanceManagerConfig,
    MCPInstance,
    MCPInstanceConfig,
    ServiceAuthConfig,
    apply_stdio_discovery_bearer_placeholder,
    get_oauth_providers_from_config,
)
from motet.core.tools.mcp_motet.manager.instance_manager import (
    MCPInstanceManager,
    get_instance_manager,
    get_service_config,
    main,
    set_instance_manager,
)

_apply_stdio_discovery_bearer_placeholder = apply_stdio_discovery_bearer_placeholder

__all__ = [
    "AuthType",
    "CredentialCheckResult",
    "InstanceManagerConfig",
    "MCPInstance",
    "MCPInstanceConfig",
    "MCPInstanceManager",
    "ServiceAuthConfig",
    "_apply_stdio_discovery_bearer_placeholder",
    "apply_stdio_discovery_bearer_placeholder",
    "get_instance_manager",
    "get_oauth_providers_from_config",
    "get_service_config",
    "main",
    "set_instance_manager",
]


if __name__ == "__main__":
    main()
