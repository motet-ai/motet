"""
Motet - Utils Module

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Re-exports async helpers used by external MCP servers.

Dependencies:
    - Core async helpers from motet.core.utils

Usage:
    from motet.utils import run_async_safe

Notes:
    - ``run_async_safe`` is the pool-aware entry for running async code from
      sync worker contexts.
"""

from motet.core.utils.async_helpers import run_async_safe

__all__ = ['run_async_safe']
