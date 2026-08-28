"""
Motet - System Principal Constants

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Shared reserved system principal identifiers and helpers for enforcing
    principal namespace boundaries. This module centralizes principal IDs used
    by internal runtime jobs so user-authenticated paths can reliably reject
    spoofed `system:*` identities.

Dependencies:
    - typing: Type hints for helper signatures

Usage:
    from motet.core.security.system_principals import (
        SYSTEM_NAMESPACE_PREFIX,
        SYSTEM_PRINCIPAL_OAUTH_MANAGER,
        is_reserved_system_principal_id,
    )

    if is_reserved_system_principal_id(principal_id):
        raise ValueError("Reserved principal namespace")

Notes:
    - Reserved namespace prefix is `system:`.
    - User-authenticated inputs must never be allowed to mint system principals.
"""

from __future__ import annotations

SYSTEM_NAMESPACE_PREFIX = "system:"

SYSTEM_PRINCIPAL_OAUTH_MANAGER = "system:oauth-manager"
SYSTEM_PRINCIPAL_OAUTH_REFRESHER = "system:oauth-refresher"
SYSTEM_PRINCIPAL_OAUTH_API = "system:oauth-api"
SYSTEM_PRINCIPAL_OAUTH_LOGOUT = "system:oauth-logout"
SYSTEM_PRINCIPAL_VAULT_CLIENT = "system:vault-client"
SYSTEM_PRINCIPAL_SCHEDULER = "system:scheduler"
SYSTEM_PRINCIPAL_MCP_MANAGER = "system:mcp-manager"
SYSTEM_PRINCIPAL_WORKER_WARMUP = "system:worker-warmup"

SYSTEM_TENANT_ID = "default"
SYSTEM_MOTET_ID = "default"


def is_reserved_system_principal_id(principal_id: str | None) -> bool:
    """Return True when principal_id uses reserved system namespace."""
    normalized = (principal_id or "").strip()
    return normalized.startswith(SYSTEM_NAMESPACE_PREFIX)

