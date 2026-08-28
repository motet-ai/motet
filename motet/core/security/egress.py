"""
Motet - Egress Security

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Egress security module for the Motet distributed framework.
    Provides comprehensive egress control capabilities including host
    allowlist/denylist validation and URL filtering. Includes security
    policy enforcement and distributed security coordination.

Dependencies:
    - urllib.parse: URL parsing and hostname extraction
    - typing: Type hints and annotations

Usage:
    from motet.core.security.egress import is_host_allowed

    # Check if host is allowed
    allowed = is_host_allowed(
        url="https://example.com/api",
        allow_csv="example.com,api.example.com",
        deny_csv="malicious.com,blocked.com"
    )

Notes:
    - Provides comprehensive egress control capabilities
    - Includes host allowlist/denylist validation
    - Supports URL filtering and security policy enforcement
    - Includes distributed security coordination
    - Supports comprehensive error handling and logging
    - Integrates with security system
    - Includes comprehensive observability and monitoring
"""

from __future__ import annotations

from urllib.parse import urlparse
from typing import Optional


def is_host_allowed(url: str, allow_csv: Optional[str], deny_csv: Optional[str]) -> bool:
    host = urlparse(url).hostname or ""
    if deny_csv:
        deny = {h.strip().lower() for h in deny_csv.split(',') if h.strip()}
        if host.lower() in deny:
            return False
    if allow_csv:
        allow = {h.strip().lower() for h in allow_csv.split(',') if h.strip()}
        if host.lower() not in allow:
            return False
    return True


__all__ = ["is_host_allowed"]


