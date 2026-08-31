"""
Motet - Agent Turn Package

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-29

Description:
    The turn lifecycle: everything between "a request arrived" and "the
    transcript is persisted" (GitHub issues #146 / #147).

    Commands, each registered via `@motet.command`:

        agent_turn.py           `agent_turn` — one full turn, and the owner of its hooks
        resume_agent_turn.py    `resume_agent_turn` — continue a suspended turn
        phases.py               the phases a turn passes through: memory_reset,
                                prepare_context, finalize_turn, page_context

    Supporting helpers, which are plain functions rather than commands:

    runtime/ Turn Runtime: persist.py writes checkpoints,
                        start.py is fresh-turn + budget Continue, resume.py is
                        private re-entry, result.py is TurnResult
        prepare.py      input shaping before reasoning (messages, model policy,
                        tool schema) — not the `prepare_context` command
        gate.py         always-on local turn gate (auto vs no_tools)
        no_tools.py     one model call with an empty tool list
        hooks.py        pre-reasoning / finalize hook runners (analysis,
                        context inject, skills, context_prepare / finalize)
        complete.py     completion path after reasoning (media, response text,
                        usage) — not the `finalize_turn` command
        outcome.py      TurnOutcome / HandedBackToolCall classifier and gates
        budget_continue.py  issue #188 Continue contract after budget stops
        (orthogonal to resume)

    Importing this package registers every command above.

Usage:
    from motet.core.orchestration.turn import (
        agent_turn, resume_agent_turn, classify_loop_outcome, TurnOutcomeKind,
    )

Notes:
    - `prepare.py` vs `prepare_context`, and `complete.py` vs `finalize_turn`,
      are the two name collisions worth knowing about here. The modules are
      helpers called *by* the turn; the commands are separately routable units
      that happen to cover related ground.
    - ``resume_turn`` is a private Turn Runtime function
      (``runtime/resume.py``), not a Celery command. Prefer
      ``resume_agent_turn`` when the suspended turn was agent_turn-owned.
"""

from motet.core.orchestration.turn.agent_turn import (
    agent_turn,
    get_motet_context,
    _inherit_parent_context,
    _build_child_metadata,
    _resolve_transcript_primary,
    _suspended_turn_response,
)
from motet.core.conversations.trivial_message import (
    is_trivial_message,
    last_user_message,
)
from motet.core.orchestration.turn.gate import (
    TurnMode,
    TurnModeDecision,
    normalize_turn_mode,
    resolve_turn_mode,
    turn_gate,
)
from motet.core.orchestration.turn.hooks import (
    _leading_system_insert_at,
)
from motet.core.orchestration.turn.phases import (
    finalize_turn,
    memory_reset,
    page_context,
    prepare_context,
)
from motet.core.orchestration.turn.complete import (
    _collect_generated_media,
    _iter_tool_result_dicts,
    _media_type_for_content_type,
    _validate_and_enrich_media,
    complete_agent_turn,
    extract_response_text,
    extract_thinking_text,
    extract_tool_summaries,
    extract_spawn_children,
    extract_turn_cost,
    extract_turn_usage,
    resolve_turn_model,
)
from motet.core.orchestration.turn.outcome import (
    HandedBackToolCall,
    TurnOutcome,
    TurnOutcomeKind,
    apply_turn_outcome_gate,
    classify_loop_outcome,
    parse_handed_back_tool_calls,
)
from motet.core.orchestration.turn.prepare import (
    _coerce_reasoning_effort,
)
from motet.core.orchestration.turn.resume_agent_turn import (
    ResumeAgentTurnData,
    resume_agent_turn,
)

__all__ = [
    "agent_turn",
    "resume_agent_turn",
    "memory_reset",
    "prepare_context",
    "finalize_turn",
    "page_context",
    "normalize_turn_mode",
    "resolve_turn_mode",
    "turn_gate",
    "TurnMode",
    "TurnModeDecision",
    "is_trivial_message",
    "last_user_message",
    "ResumeAgentTurnData",
    "TurnOutcome",
    "TurnOutcomeKind",
    "HandedBackToolCall",
    "classify_loop_outcome",
    "apply_turn_outcome_gate",
    "parse_handed_back_tool_calls",
    "get_motet_context",
    "_leading_system_insert_at",
    "_inherit_parent_context",
    "_build_child_metadata",
    "_resolve_transcript_primary",
    "_suspended_turn_response",
    "_collect_generated_media",
    "_iter_tool_result_dicts",
    "_media_type_for_content_type",
    "_validate_and_enrich_media",
    "complete_agent_turn",
    "extract_response_text",
    "extract_thinking_text",
    "extract_tool_summaries",
    "extract_spawn_children",
    "extract_turn_cost",
    "extract_turn_usage",
    "resolve_turn_model",
    "_coerce_reasoning_effort",
]
