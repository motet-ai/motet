"""
Motet - Interfaces Module

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-05

Description:
    Interface layer for the Motet distributed framework.
    Provides HTTP, CLI, and session management capabilities.

Dependencies:
    - HTTP interface with FastAPI
    - CLI interface with Click (optional; not imported at package load)
    - Session management
    - Operations and monitoring interfaces

Usage:
    from motet.interfaces import http, cli

    # HTTP interface
    app = http.app

    # CLI interface (lazy; requires motet_sdk when cli_app is resolved)
    cli_app = cli.cli_app

Notes:
    - Do not eagerly import ``cli`` here: API images load
      ``motet.interfaces.http`` without installing motet_sdk.
    - Submodule ``cli`` remains importable as ``motet.interfaces.cli``.
    - Supports multiple interface types
    - Includes web and CLI interfaces
    - Provides session management
    - Integrates with distributed architecture
"""

__all__ = ["cli"]
