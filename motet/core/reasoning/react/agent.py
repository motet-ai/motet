"""
Motet - Agent Loop Builder

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-26

Description:
    Builds LoopContext + AgenticLoopData from AgentData and runs the loop via
    Turn Runtime ``start``.

    The turn path (``agent_turn``) calls ``run_agent``
    in-process so it does not park a second Celery slot. The ``@motet.command``
    wrapper remains for (1) ``core.spawn_agents`` children that need overlapping
    workers (``motet.do(agent_loop)`` / ``motet.join``) and (2) the OpenAI-compat
    ``hosted_tools`` hop (allowlist + handback, no ``agent_turn`` hooks).

Dependencies:
    - motet.command, get_motet_context: Command decorator and context
    - build_loop_context, resolve_conversation_history: Loop isolation (strategies)
    - LoopStateSnapshot: Shared loop-state codec (issue #147)
    - turn.runtime.start: in-process loop owner
    - AgentData: Command input model
    - WorkerCapability, EventPriority: Distributed config

Usage:
    from motet.core.reasoning.react.agent import run_agent, AgentData

    # Turn path — in-process (no extra slot)
    result = run_agent(motet, AgentData(agent_id="core.default", input="..."))

    # Parallel sub-agent or hosted_tools hop — Celery hop is intentional
    result = motet.do(agent_loop, data=AgentData(agent_id="core.default.spawn-1", input="..."))

Notes:
    - tools (optional): mapped to AgenticLoopData.tools; when provided, skip discovery.
    - Return shape matches agentic_loop (final_response, tool_results, iterations_used).
    - agent_turn must call run_agent / runtime.start, not motet.do(agent_loop).
    - spawn_agents children set use_task_stream and stamp metadata.agent_id
      so tokens and thinking land on the parent task stream with the child id.
"""

from typing import Any, Dict

from motet import motet
from motet.core.commands.decorator import get_motet_context
from motet.core.commands.capabilities import WorkerCapability
from ...workers.observers import EventPriority
from ..loop_context import build_loop_context, resolve_conversation_history
from .agent_data import AgentData
from .agentic_loop_data import AgenticLoopData
from .loop_state_snapshot import LoopStateSnapshot


def _resolve_spend_rails(data: AgentData) -> tuple[float, int, int]:
    """Resolve dollar, prompt-token, and tool-time ceilings; 0 disables a rail.

    AgentData None inherits Config for dollars and prompt tokens
    (MOTET_AGENT_MAX_COST_USD / MOTET_AGENT_MAX_PROMPT_TOKENS). Tool time
    does not inherit a parent default — None and 0 both disable — so a
    coding turn is not cut off at the spawn-child 60s rail. Explicit 0 on
    cost or tokens also turns that rail off.
    """
    from motet.core.config import Config

    cfg = Config()
    if data.max_cost_usd is None:
        cost = float(getattr(cfg, "agent_max_cost_usd", 0.0) or 0.0)
    else:
        cost = float(data.max_cost_usd)
    if data.max_prompt_tokens is None:
        tokens = int(getattr(cfg, "agent_max_prompt_tokens", 0) or 0)
    else:
        tokens = int(data.max_prompt_tokens)
    if data.max_tool_time_ms is None:
        tool_time = 0
    else:
        tool_time = int(data.max_tool_time_ms)
    return max(cost, 0.0), max(tokens, 0), max(tool_time, 0)


def _stamp_stream_agent_identity(motet_ctx: Any, data: AgentData) -> None:
    """Copy command metadata and set this run's agent_id for stream frames.

    ``motet.join`` shares the parent's metadata dict with children. Mutating
    it in place would retag the parent. Stream events read ``motet.metadata``
    (``_resolve_stream_agent_id_plaintext``), not ``AgentData.metadata``.
    Hosted_tools hops leave metadata alone (``inject_meta_tools=False``).
    """
    if not data.inject_meta_tools:
        return
    aid = (data.agent_id or "").strip()
    if not aid:
        return
    current = getattr(motet_ctx, "metadata", None)
    stamped = dict(current) if isinstance(current, dict) else {}
    stamped["agent_id"] = aid
    parent_aid = (data.parent_agent_id or "").strip()
    if parent_aid:
        stamped["parent_agent_id"] = parent_aid

    command = getattr(motet_ctx, "_command", None)
    distributed = getattr(command, "distributed_context", None) if command is not None else None
    if distributed is not None:
        distributed.metadata = stamped
        return
    if hasattr(motet_ctx, "_metadata_fallback"):
        motet_ctx._metadata_fallback = stamped


def build_agent_loop_data(motet_ctx: Any, data: AgentData) -> AgenticLoopData:
    """Map AgentData to AgenticLoopData. Does not run the loop."""
    base_stream_key = data.base_stream_key
    if base_stream_key is None:
        from motet.core.distributed.tenant_keys import task_response_stream_for

        base_stream_key = task_response_stream_for(motet_ctx)

    loop_context = build_loop_context(
        loop_id=data.agent_id,
        base_stream_key=base_stream_key,
        conversation_history=data.conversation_history or None,
        parent_agent_id=data.parent_agent_id,
        metadata=data.metadata,
    )

    history = resolve_conversation_history(loop_context, data.conversation_history)
    stream_key = base_stream_key if data.use_task_stream else (loop_context.stream_key or base_stream_key)

    max_model_calls = data.max_model_calls
    if max_model_calls is None:
        max_model_calls = max(int(data.max_iterations) * 3, 30)

    max_cost_usd, max_prompt_tokens, max_tool_time_ms = _resolve_spend_rails(data)

    return LoopStateSnapshot(
        input=data.input,
        tools=data.tools,
        tool_filter_metadata=data.tool_filter_metadata,
        max_iterations=data.max_iterations,
        remaining_iterations=data.max_iterations,
        max_model_calls=max_model_calls,
        max_cost_usd=max_cost_usd,
        max_prompt_tokens=max_prompt_tokens,
        max_tool_time_ms=max_tool_time_ms,
        model_calls_used=0,
        max_tools=data.max_tools,
        model_provider=data.model_provider,
        model_name=data.model_name,
        model_profile_name=data.model_profile_name,
        temperature=data.temperature,
        enable_thinking=data.enable_thinking,
        reasoning_effort=data.reasoning_effort or "medium",
        enable_prompt_caching=data.enable_prompt_caching,
        skill_refs=data.skill_refs,
        handback_tool_names=data.handback_tool_names,
        handback_tools=data.handback_tools,
        agent_id=data.agent_id if data.inject_meta_tools else None,
        parent_agent_id=data.parent_agent_id,
        inject_meta_tools=data.inject_meta_tools,
    ).to_loop_data(
        conversation_history=history or [],
        stream_key=stream_key,
        prefilled_tool_calls=data.prefilled_tool_calls,
    )


def run_agent(motet_ctx: Any, data: AgentData) -> Dict[str, Any]:
    """In-process agent run: build loop data and call Turn Runtime start."""
    from motet.core.orchestration.turn.runtime import start

    _stamp_stream_agent_identity(motet_ctx, data)
    if data.use_task_stream:
        motet_ctx.ensure_stream(ttl_seconds=3600)
    result = start(motet_ctx, build_agent_loop_data(motet_ctx, data))
    return dict(result.payload) if result.payload else {
        "final_response": result.final_response,
        "stop_reason": result.stop_reason,
    }


@motet.command(
    description="Run a configured agent end-to-end: build loop context and invoke the agentic tool-calling loop for the current turn.",
    timeout_seconds=300,
    priority=EventPriority.HIGH,
    required_capabilities=[WorkerCapability.REASONING, WorkerCapability.TOOL_EXECUTION],
    streaming_enabled=True,
)
def agent_loop(data: AgentData) -> Dict[str, Any]:
    """
    Celery entry for ``core.spawn_agents`` children and the OpenAI-compat
    hosted_tools hop (allowlist + handback, no agent_turn hooks). Turn
    owners call ``run_agent`` in-process.
    """
    return run_agent(get_motet_context(), data)
