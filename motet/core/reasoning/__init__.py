"""
Motet - Reasoning Module

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-26

Description:
    Reasoning system for the Motet distributed framework.
    Reasoning constants and the agent loop package.

    Packages under this one: `react/` (agentic loop), with
    `loop_context.py` and `reasoning_events.py` alongside as shared
    utilities. Fan-out is the ordinary tool `core.spawn_agents`, not a
    sibling strategy package.

    Inspectable planning is a bundle concern (structured plans/todos); durable
    multi-step command DAGs remain workflows.

Dependencies:
    - Reasoning constants

Usage:
    Prefer distributed commands for reasoning:
        - `agent_turn` → `run_agent` → `agentic_loop`

Notes:
    - Public modes are auto, agentic, and no_tools.
    - The loop asks for missing facts from its own system brief.
"""

from .constants import Complexity

__all__ = [
    "Complexity",
]
