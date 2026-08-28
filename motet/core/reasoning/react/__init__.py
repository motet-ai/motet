"""
Motet -   Init

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-27

Description:
    Package init for ReAct agentic-loop: agentic_loop (in-process iteration),
    agent_loop, the loop driver (``run_agentic_loop``,), and shared loop
    helpers (discovery, execution, observations, skills/sidecars, state
    snapshot). Resume re-entry lives in Turn Runtime
    (``motet.core.orchestration.turn.runtime``).

Dependencies:
    - agentic_loop: In-process iteration body (not a distributed command)
    - agent_loop: In-process builder plus Celery wrapper for spawn_agents children and hosted_tools
    - LoopStateSnapshot: loop-state ↔ checkpoint codec
    - motet.core.checkpoints: TurnCheckpoint store (turn-lifecycle state, imported
      directly from `core.checkpoints` rather than re-exported here)

Usage:
    from motet.core.reasoning.react import (
        agentic_loop, agent_loop, run_agentic_loop, LoopStateSnapshot,
    )

Notes:
    - Issue #147 factorization: discovery/execution/observations/skills/results/
      state live in dedicated modules; agentic_loop remains the iteration
      conductor and imports only the helpers it calls. Import helper names from
      their owning module, not through agentic_loop.
    - The react package is an acyclic import graph: agentic_loop depends on the
      helper modules, never the reverse. loop_results.py is a leaf holding the
      terminal-result / usage helpers both the conductor and loop_execution need.
    - `agentic_loop` below is the iteration function, which shadows the
      same-named submodule on this package: `import
      motet.core.reasoning.react.agentic_loop as m` binds the function, not
      the module. Use `importlib.import_module` when you need the module.
"""


from .agentic_loop_data import (
    AgenticLoopData  # Recursive tool chaining data
)
from .agentic_loop import agentic_loop  # one in-process iteration
from .agent_data import AgentData  # AgentData for agent_loop / run_agent
from .agent import agent_loop, run_agent, build_agent_loop_data
from .loop_driver import run_agentic_loop  # in-process loop driver
from .loop_state_snapshot import LoopStateSnapshot  # Issue #147: loop ↔ checkpoint codec

__all__ = [
    "AgenticLoopData",  # Recursive tool chaining data
    "agentic_loop",  # one in-process iteration; use run_agentic_loop for a turn
    "AgentData",  # input for agent_loop / run_agent
    "agent_loop",  # Celery entry for parallel sub-agents
    "run_agent",  # in-process turn path
    "build_agent_loop_data",
    "run_agentic_loop",  # in-process loop driver
    "LoopStateSnapshot",  # Issue #147: shared loop-state codec
]

