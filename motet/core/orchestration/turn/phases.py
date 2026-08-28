"""
Motet - Turn Phase Commands

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-26

Description:
    The distributed commands that surround an agent turn, each a phase the turn
    passes through:

        memory_reset       clear working / conversation memory before the turn
    prepare_context assemble context via the provider pipeline
        finalize_turn      persist the canonical transcript and memory writes
        page_context       manage-app page context

    These are separate distributed commands rather than inline steps so each can
    be routed, retried, observed, and called on its own — `resume_agent_turn`
    calls `finalize_turn` directly, for instance, without replaying a turn.

    Forced mode and the turn gate live in ``turn/gate.py``
    (``resolve_turn_mode``).

    The turn that drives them is `agent_turn` in this package's `agent_turn.py`.
    Implements unified task-level streaming: every command in a task
    writes to the same `task:{task_id}:response` stream.

Dependencies:
    - motet.core.commands: @motet.command, capabilities, payload models
    - motet.core.workers.observers: EventPriority for stream event ordering

Usage:
    from motet.core.orchestration.turn.phases import (
        memory_reset, prepare_context,
    )
    from motet.core.commands.command_data_classes import (
        MemoryResetData, PrepareContextData,
    )

    # Reset memory (decorator-based API)
    reset_command = memory_reset(
        data=MemoryResetData(reset_working_memory=True, reset_conversation_memory=True),
        task_id="task_123", conversation_id="conv_123",
    )
    result = await reset_command.execute()

    # Prepare context (decorator-based API)
    context_command = prepare_context(
        data=PrepareContextData(messages=[Message(role="user", content="Hello")]),
        task_id="task_123", conversation_id="conv_123",
    )
    context = await context_command.execute()

Notes:
    - Uses the composition helpers (`motet.maybe`, `motet.dispatch`).
    - This module owns the single `get_motet_context` binding for the turn
      package. `agent_turn.py` resolves the context *through* this module rather
      than importing it from the decorator directly, so patching
      `turn.phases.get_motet_context` in a test affects the whole turn path.
      That indirection is the reason it looks redundant; keep it.
    - The suspension gate is not here — it lives with the turn itself
      (`turn/outcome.py` classifies, `agent_turn.py` acts on the result).
"""


import time
from typing import Any, Dict, List, Optional
from uuid import uuid4
import structlog

from motet import motet
from motet.core.commands.capabilities import WorkerCapability
from motet.core.commands.decorator import get_motet_context  # noqa: F401  (re-exported for turn.py/tests)
from motet.core.commands.command_data_classes import (
    FinalizeTurnData,
    MemoryResetData,
    PageContextData,
    PrepareContextData,
)
from motet.core.workers.observers import EventPriority

logger = structlog.get_logger(__name__)


def _extract_analysis_metadata(analysis_data: Any) -> Dict[str, Any]:
    """Normalize conversation_analysis output into the declared result shape.

    Does not choose a turn mode. Routing uses ``turn_gate`` on the last
    user message.
    """
    from motet.core.orchestration.turn.hook_models import (
        analysis_as_dict,
        parse_analysis_result,
    )

    parsed = parse_analysis_result(analysis_data)
    return analysis_as_dict(parsed)


# ==================== DECORATOR-BASED COMMANDS (NEW) ====================

@motet.command(
    description="Reset working memory and/or conversation-scoped memory for a clean slate before or during a turn.",
    timeout_seconds=30,
    priority=EventPriority.HIGH,
    required_capabilities=[WorkerCapability.MEMORY_OPERATIONS]
)
def memory_reset(data: MemoryResetData) -> Dict[str, Any]:
    """
    Reset memory (working memory and/or conversation memory).
    
    Clears working memory and optionally conversation-specific memory for cleanup
    and initialization scenarios. High priority for initialization operations.
    
    Args:
        data: Memory reset configuration
        motet: Motet context for resource access
        
    Returns:
        Dict with reset count and configuration
    """
    reset_count = 0
    
    motet = get_motet_context()

    if data.reset_working_memory:
        # Reset working memory (temporary items for this session)
        if hasattr(motet.memory, 'clear_working_memory'):
            reset_count += motet.memory.clear_working_memory()
        elif hasattr(motet.memory, 'clear_by_tag'):
            reset_count += motet.memory.clear_by_tag('working')
    
    if data.reset_conversation_memory and motet.conversation_id:
        # Issue #139: never clear another principal's conversation memory.
        from motet.core.conversations.ownership import authorize_motet_conversation_access

        authorize_motet_conversation_access(motet, bind_if_unclaimed=False)
        if hasattr(motet.memory, 'clear_by_tag'):
            from motet.core.memory.constants import CONVERSATION_SCOPE_TAG_PREFIX
            reset_count += motet.memory.clear_by_tag(f"{CONVERSATION_SCOPE_TAG_PREFIX}{motet.conversation_id}")
    
    return {
        "reset_count": reset_count,
        "reset_working_memory": data.reset_working_memory,
        "reset_conversation_memory": data.reset_conversation_memory
    }


@motet.command(
    description="Prepare model context for a turn: assemble messages, memories, artifacts, and provider-pipeline context before inference.",
    timeout_seconds=45,
    priority=EventPriority.HIGH,
    required_capabilities=[WorkerCapability.MEMORY_OPERATIONS, WorkerCapability.VECTOR_OPERATIONS]
)
def prepare_context(data: PrepareContextData) -> Dict[str, Any]:
    """
    Prepare context for turn execution through the ADR-0109 provider pipeline.

    The distributed command remains the execution and audit boundary. Ordered
    providers handle conversation history replay, memory recall, artifact
    content parts, and token budgeting before the command serializes the final
    prepared messages.
    
    Args:
        data: Context preparation configuration
        motet: Motet context for resource access
        
    Returns:
        Dict with prepared messages and context info
    """
    motet = get_motet_context()
    
    # Import logger for structured logging
    import structlog
    logger = structlog.get_logger(__name__)

    # Issue #139: authorize before replaying conversation history into the prompt.
    # Binds like agent_turn (which normally claims first) so a standalone call on
    # a fresh id is not a false denial; a foreign-owned id is rejected either way.
    from motet.core.conversations.ownership import authorize_motet_conversation_access

    authorize_motet_conversation_access(motet, bind_if_unclaimed=True)

    from motet.core.orchestration.context import run_context_pipeline

    state = run_context_pipeline(data=data, motet=motet, logger=logger)
    logger.info(
        "prepare_context_timings",
        **state.timings,
        conversation_id=motet.conversation_id,
        message_count=len(state.messages),
        history_messages=state.context_info.get("conversation_history_retrieved", 0),
    )

    return {
        "prepared_messages": [
            msg.__dict__ if hasattr(msg, "__dict__") else msg
            for msg in state.messages
        ],
        "context_info": state.context_info,
    }




@motet.command(
    description="Finalize a completed turn: persist conversation transcript, update memory, and close turn bookkeeping.",
    timeout_seconds=30,
    priority=EventPriority.NORMAL,
    required_capabilities=[WorkerCapability.MEMORY_OPERATIONS]
)
def finalize_turn(data: FinalizeTurnData) -> Dict[str, Any]:
    """
    Finalize turn execution (conversation storage, memory updates).
    
    Stores the canonical conversation_transcript (impl-070) for this turn so
    prepare_context and conversation_get can replay history. Records
    ``agent_id`` on the final assistant message and transcript memory metadata
    when resolvable (ADR-0083). Optionally stores assistant_response for long-term
    memory (with ``agent_id`` in metadata when known). Normal priority for post-execution
    finalization.
    
    Args:
        data: Turn finalization configuration
        motet: Motet context for resource access
        
    Returns:
        Dict with storage results
    """

    motet = get_motet_context()

    # Also holds "conversation_error"/"memory_error" strings on failure.
    results: Dict[str, Any] = {
        "conversation_stored": False,
        "memory_updated": False,
        "items_stored": 0,
        "canonical_transcript_stored": False,
    }

    # Issue #139: authorize before writing transcript rows into a conversation.
    from motet.core.conversations.ownership import authorize_motet_conversation_access

    authorize_motet_conversation_access(motet, bind_if_unclaimed=True)
    
    # Store conversation (canonical transcript only; legacy conversation_turn removed)
    if data.store_conversation and hasattr(motet.memory, "store"):
        try:
            from motet.core.conversations.transcript_storage import store_turn_transcript

            store_result = store_turn_transcript(
                motet,
                data.messages,
                data.assistant_response,
                agent_id=data.agent_id,
                root_turn=data.root_turn,
                root_agent_id=data.root_agent_id,
                transcript_sequence=data.transcript_sequence,
                pending_action_carry=data.pending_action_carry,
            )
            results.update(store_result)
        except Exception as e:
            results["conversation_error"] = str(e)
    
    # Update memory with assistant response
    if data.update_memory and data.assistant_response and hasattr(motet.memory, "store"):
        try:
            # Store assistant response
            from motet.core.conversations.transcript_storage import resolve_transcript_agent_id
            from motet.core.memory.constants import CONVERSATION_SCOPE_TAG_PREFIX

            response_tags = [f"{CONVERSATION_SCOPE_TAG_PREFIX}{motet.conversation_id}"] if motet.conversation_id else []
            resolved_author = resolve_transcript_agent_id(motet, explicit=data.agent_id)
            response_meta: Dict[str, Any] = {
                "task_id": motet.task_id,
                "conversation_id": motet.conversation_id,
                "timestamp": time.time(),
            }
            if resolved_author:
                response_meta["agent_id"] = resolved_author

            motet.memory.store(
                content=data.assistant_response,
                type="assistant_response",
                tags=response_tags,
                metadata=response_meta,
                item_id=str(uuid4()),
                working=True  # Store in working memory for auto-detection
            )
            results["memory_updated"] = True
            results["items_stored"] += 1
        except Exception as e:
            results["memory_error"] = str(e)
    
    return results


@motet.command(
    description="Resolve manage-app page context into a compact system message for UI-aware agent turns.",
    timeout_seconds=30,
    required_capabilities=[WorkerCapability.TOOL_EXECUTION],
)
def page_context(data: PageContextData) -> Dict[str, Any]:
    """
    Resolve manage-app page context into a compact system message.

    Maps `current_page` to a read-only `motet_admin.*` tool and returns additive
    context-injection payload (`system_messages`, optional `context_patch`).
    """
    import json

    motet = get_motet_context()
    context = dict(data.context or {})
    page_ctx = context.get("page_context") or context.get("page") or context.get("ui_context") or {}
    if not isinstance(page_ctx, dict):
        page_ctx = {"raw": str(page_ctx)}

    current_page = str(page_ctx.get("current_page") or "unknown").strip().lower()

    page_to_tool = {
        "workers": "motet_admin.get_worker_summary",
        "tasks": "motet_admin.get_task_history",
        "schedules": "motet_admin.get_schedule_summary",
        "bundles": "motet_admin.get_deploy_summary",
        "deploy": "motet_admin.get_deploy_summary",
        "cost": "motet_admin.get_cost_summary",
        "vault": "motet_admin.get_vault_summary",
        "conversations": "motet_admin.get_conversation_summary",
    }

    tool_name = page_to_tool.get(current_page)
    if not tool_name:
        payload = json.dumps(page_ctx, ensure_ascii=True, default=str)
        return {
            "system_messages": [f"Current page context:\n{payload}"],
            "context_patch": {"page_context_source": "raw"},
        }

    result = motet.tools.execute(
        tool_name,
        {},
        role=getattr(motet, "role", None),
        persist_observation=False,
    )
    if not isinstance(result, dict) or result.get("status") != "success":
        error_text = result.get("error", "unavailable") if isinstance(result, dict) else "unavailable"
        return {
            "system_messages": [f"Current page context ({current_page}): unavailable ({error_text})"],
            "context_patch": {"page_context_source": tool_name, "page_context_error": str(error_text)},
        }

    rendered = json.dumps(result.get("result"), ensure_ascii=True, default=str)
    if len(rendered) > 4096:
        rendered = rendered[:4080] + "... (truncated)"
    return {
        "system_messages": [f"Current page context ({current_page}) from {tool_name}:\n{rendered}"],
        "context_patch": {"page_context_source": tool_name},
    }




__all__ = [
    # Decorator-based functions (ADR-0030)
    "memory_reset",
    "prepare_context",
    "finalize_turn",
    "page_context",
    # Data classes
    "MemoryResetData",
    "PrepareContextData",
    "FinalizeTurnData",
    "PageContextData",
]

# Decorator-based commands auto-register themselves via @motet.command
