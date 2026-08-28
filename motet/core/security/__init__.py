"""
Motet - Security Module

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Security system for the Motet distributed framework.
    Provides authentication, authorization, rate limiting, and egress controls.

Dependencies:
    - JWT authentication and validation
    - API key management and validation
    - Rate limiting and throttling
    - Egress filtering and controls

Usage:
    from motet.core.security import require_api_key, RateLimiter
    
    # Require API key
    @require_api_key
    async def protected_endpoint():
        pass
    
    # Rate limiting
    limiter = RateLimiter(requests_per_minute=60)

Notes:
    - Supports JWT and API key authentication
    - Includes rate limiting and throttling
    - Provides egress filtering
    - Integrates with distributed architecture
"""

from .auth import require_api_key, require_jwt_if_configured, extract_principal
from .ratelimit import RateLimiter
from .egress import is_host_allowed

__all__ = [
    "require_api_key",
    "require_jwt_if_configured",
    "extract_principal",
    "RateLimiter",
    "is_host_allowed",
]


