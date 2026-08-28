"""
Motet - Agent Listing Commands

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-26

Description:
    Distributed command support for agent discovery. This module
    exposes `core.agent_list`, which returns role-filtered visible agent
    configurations using shared discovery services from `motet.core.agents`.

Dependencies:
    - motet.core.agents.discovery: Shared agent listing/sync helpers
    - motet.core.commands.decorator: distributed command wrapper

Usage:
    from motet.core.commands.builtin.agents import agent_list, AgentListData

    data = AgentListData(principal_roles=["admin"])
    command = agent_list(task_id="t1", conversation_id="", data=data)
    # execute through global_invoker

Notes:
    - Returned `qualified_id` values follow `core.<id>` or `<bundle>.<id>`.
    - Plural on purpose. This is the *agents domain* — it wraps
      `motet.core.agents` and backs the `motet.agents` helper. The singular
      `core.agent_loop` command is a different
      thing and lives in `motet/core/reasoning/react/agent.py`. Naming this
      `agent.py` made people open it looking for that command.
    - Named for the domain rather than the command it currently holds, matching
      the rest of `builtin/` (`artifacts.py` holds only `create_artifact`).
      A second agent command belongs here rather than in a new module.
"""

from __future__ import annotations

from typing import Any, Dict

import structlog

from motet import motet
from motet.core.commands.command_data_classes import AgentListData
from motet.core.commands.decorator import get_motet_context
from motet.core.commands.capabilities import WorkerCapability
from motet.core.agents.discovery import list_visible_agents

logger = structlog.get_logger(__name__)


@motet.command(
    description="List available agents the current user can see, with ids, names, and config summaries for agent selection and routing.",
    timeout_seconds=30,
    required_capabilities=[WorkerCapability.TOOL_EXECUTION],
)
def agent_list(data: AgentListData) -> Dict[str, Any]:
    """List visible agent configs from synchronized local registry state."""
    motet = get_motet_context()
    agents = list_visible_agents(principal_roles=list(data.principal_roles or []))
    logger.info(
        "agent_list_success",
        total_agents=len(agents),
        conversation_id=getattr(motet, "conversation_id", None),
        task_id=getattr(motet, "task_id", None),
    )
    return {
        "agents": agents,
        "total": len(agents),
    }


__all__ = [
    "agent_list",
    "AgentListData",
]

