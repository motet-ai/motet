"""
Motet - Reasoning Constants

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-26

Description:
    Reasoning constants and type definitions for the Motet distributed framework.
    Provides the complexity vocabulary used by conversation analysis.

Dependencies:
    - typing: Type hints and literal types

Usage:
    from motet.core.reasoning.constants import Complexity

    complexity: Complexity = "moderate"

Notes:
    - Defines complexity levels (simple, moderate, complex)
    - Turn modes are auto / agentic / no_tools.
"""

from __future__ import annotations

from typing import Literal

# Type aliases for clarity across the reasoning module
# Use lowercase to match analysis output (simple, moderate, complex)
Complexity = Literal["simple", "moderate", "complex"]

__all__ = [
    "Complexity",
]
