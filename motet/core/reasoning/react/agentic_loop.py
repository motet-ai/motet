"""
Motet - Agentic Loop Iteration

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-23

Description:
    One in-process ReAct iteration (model → tools → observation). Not a Celery
    command. ``run_agentic_loop`` in loop_driver.py runs this until a terminal
    result; model/tool/workflow work stays distributed via ``motet.do``. Do not
    ``motet.do(agentic_loop)``. Loop-control policy lives here: budgets, stall,
    trailing wrap-up notice, forced finalize on rail stop, suspend/handback,
    system prompt, meta/handback schema injection.

Dependencies:
    - AgenticLoopData: in-process continuation payload
    - loop_driver.agentic_loop_continue: next Motet-tool iteration (lazy import)
    - loop_discovery / loop_execution / loop_skills: phase helpers
    - loop_results: terminal result contract (leaf; keeps react/ acyclic)
    - model_stream: distributed LLM call

Usage:
    from motet.core.reasoning.react.loop_driver import run_agentic_loop

    result = run_agentic_loop(motet, AgenticLoopData(...))

Notes:
    - Import phase helpers from their owning modules, not through this file.
    - Shared helpers belong in a leaf (e.g. loop_results), not a deferred
      import back into this conductor.
    - Continuations and suspend use LoopStateSnapshot so loop fields cannot
      drift vs agent entry / resume_turn.
    - Hosted_tools sets ``inject_meta_tools=False`` (allowlist only) and does
      not stamp an owning ``agent_id``. Cursor's ``cursor.backend`` is a real
      agent in facade agent mode, not a hosted_tools sentinel.
    - On the last two Motet-tool rounds the loop appends a trailing user
      wrap-up so the model sees remaining budget without rewriting the
      cached system prefix. Spawn children's static rails stay on their
      worker brief; live remaining counts stay here for every Motet-owned
      turn. A rail stop then asks for one tools-off write-up so partial
      findings survive instead of scaffolding text.
"""

import json
import structlog
import os
import time
from typing import Dict, Any, List, Optional

from motet.core.commands.decorator import get_motet_context
from .agent_data import DEFAULT_MODEL_NAME, DEFAULT_MODEL_PROVIDER
from .agentic_loop_data import AgenticLoopData
from .loop_discovery import (
    build_context_query,
    ensure_tool_filter_required_tools,
    merge_sticky_tool_schemas,
)
from .loop_execution import (
    ToolCallBuildResult,
    execute_tools_and_append_results,
    build_unique_tool_calls,
    maybe_fast_path_return,
    prefilled_stream_data,
    validate_prefilled_tool_calls,
)
from .loop_results import accumulate_usage, build_loop_result
from .loop_skills import (
    ATTACHMENT_TOOL_NAMES,
    conversation_has_attachments,
    evict_stale_artifact_view_sidecars,
    expose_activated_skill_runner_tools,
)
from ...models.adapters.providers.message_history_sanitizer import needs_user_turn
from ...types import CanonicalToolSchema, Message, RequestContext, tool_schema_name
from ..reasoning_events import emit_reasoning_event

logger = structlog.get_logger(__name__)

# Consecutive iterations of repeat-only tool calls before the turn is stopped.
# Two is normal (a re-read after an edit, a status poll); a sustained run means the
# model is circling. Tune with MOTET_MAX_STALLED_ITERATIONS.
MAX_STALLED_ITERATIONS = max(int(os.getenv("MOTET_MAX_STALLED_ITERATIONS", "3")), 1)

# Trailing user notice on the last N Motet-tool rounds. Lives off the system
# prefix so ADR-0124 cache stays intact; the model does not otherwise see
# remaining_iterations. Applies to every Motet-owned turn, not only spawn
# children — a parent burning 18 of 20 rounds has the same blind spot.
BUDGET_WRAP_UP_REMAINING = 2
BUDGET_WRAP_UP_PREFIX = "[budget wrap-up]"
BUDGET_FINALIZE_PREFIX = "[budget finalize]"
RAIL_FINALIZE_REASONS = frozenset(
    {
        "max_iterations",
        "max_model_calls",
        "max_cost",
        "max_prompt_tokens",
        "max_tool_time",
        "stalled",
    }
)


def _is_budget_notice(msg: Message, prefix: str) -> bool:
    """True when *msg* is a harness budget notice, not a user utterance."""
    content = getattr(msg, "content", None)
    return (
        getattr(msg, "role", None) == "user"
        and isinstance(content, str)
        and content.startswith(prefix)
    )


def _is_budget_wrap_up(msg: Message) -> bool:
    """True when *msg* is a harness wrap-up, not a user utterance."""
    return _is_budget_notice(msg, BUDGET_WRAP_UP_PREFIX)


def _is_budget_finalize(msg: Message) -> bool:
    """True when *msg* is the tools-off write-up request after a rail stop."""
    return _is_budget_notice(msg, BUDGET_FINALIZE_PREFIX)


def _strip_budget_notices(history: List[Message]) -> None:
    """Drop wrap-up / finalize notices so they cannot stack or linger past Continue."""
    kept = [
        msg
        for msg in history
        if not _is_budget_wrap_up(msg) and not _is_budget_finalize(msg)
    ]
    if len(kept) != len(history):
        history[:] = kept


def _budget_wrap_up_text(data: AgenticLoopData) -> str:
    """Concrete remaining-budget notice. Numbers come from this iteration's rails."""
    iteration = data.current_iteration
    total = int(data.max_iterations or 0)
    left = int(data.remaining_iterations or 0)
    if left <= 1:
        return (
            f"{BUDGET_WRAP_UP_PREFIX} Iteration {iteration} of {total}. "
            "Last round. Do not call tools. Write up what you have now."
        )
    return (
        f"{BUDGET_WRAP_UP_PREFIX} Iteration {iteration} of {total}. "
        f"{left} rounds left. Stop gathering and write up what you have. "
        "A partial answer beats another tool call."
    )


def _maybe_append_budget_wrap_up(data: AgenticLoopData) -> bool:
    """Tell the model its remaining rounds when the budget is almost gone.

    Trailing user message, never a system rewrite: the cached prefix is the
    system prompt plus tools. Hosted_tools leaves the client's messages
    alone. A leftover notice from a prior Continue is stripped when the
    budget is refreshed so it cannot keep saying "last round".
    """
    if not _is_motet_owned_turn(data):
        return False

    history = data.conversation_history
    _strip_budget_notices(history)

    if int(data.remaining_iterations or 0) > BUDGET_WRAP_UP_REMAINING:
        return False

    history.append(Message(role="user", content=_budget_wrap_up_text(data)))
    logger.info(
        "agentic_loop_budget_wrap_up",
        iteration=data.current_iteration,
        max_iterations=data.max_iterations,
        remaining_iterations=data.remaining_iterations,
    )
    return True


def _model_request_context(motet: Any, data: AgenticLoopData) -> RequestContext:
    """RequestContext for a loop model call (main iteration or rail finalize)."""
    dctx = getattr(motet, "distributed_context", None)
    return RequestContext(
        tenant_id=motet.tenant_id or None,
        principal_id=motet.principal_id or None,
        motet_id=motet.motet_id or None,
        task_id=motet.task_id or None,
        command_id=motet.command_id or None,
        parent_command_id=getattr(dctx, "parent_command_id", None) if dctx else None,
        trace_id=getattr(dctx, "trace_id", None) if dctx else None,
        conversation_id=getattr(motet, "conversation_id", None) or None,
        model_profile_name=(data.model_profile_name or None),
        tenant_isolation_required=(
            getattr(dctx, "tenant_isolation_required", True) if dctx else True
        ),
        worker_security_level=(
            getattr(dctx, "worker_security_level", "standard") if dctx else "standard"
        ),
    )


def _finalize_writeup_text(stop_reason: str) -> str:
    """Trailing notice for the tools-off write-up after a rail stop."""
    return (
        f"{BUDGET_FINALIZE_PREFIX} This turn's budget is exhausted "
        f"({stop_reason}). Do not call tools. Write up your findings from "
        "the results above, with sources. A partial answer is better than silence."
    )


def _try_finalize_writeup(
    motet: Any,
    data: AgenticLoopData,
    *,
    stop_reason: str,
    accumulated_usage: Dict[str, Any],
) -> Optional[str]:
    """One tools-off model call so a rail stop can return findings, not scaffolding.

    Does not consume ``remaining_iterations``. Hosted_tools turns are left
    alone. On failure the finalize notice is popped so Continue does not
    inherit a 'budget exhausted' user message.
    """
    if not _is_motet_owned_turn(data):
        return None
    if stop_reason not in RAIL_FINALIZE_REASONS:
        return None

    history = data.conversation_history
    _strip_budget_notices(history)
    history.append(Message(role="user", content=_finalize_writeup_text(stop_reason)))

    from motet.core.commands.builtin.model import model_stream
    from motet.core.commands.command_data_classes import ModelStreamData
    from motet.core.commands.response_models import CommandExecutionError

    try:
        enable_prompt_caching = _resolve_enable_prompt_caching(
            enable_prompt_caching=data.enable_prompt_caching,
            model_provider=data.model_provider,
            model_name=data.model_name,
        )
        stream_data = motet.do(
            model_stream,
            data=ModelStreamData(
                messages=history,
                stream_key=data.stream_key,
                tools=[],
                model_settings={
                    "provider": data.model_provider or DEFAULT_MODEL_PROVIDER,
                    "model_name": data.model_name or DEFAULT_MODEL_NAME,
                    "temperature": data.temperature,
                    "enable_thinking": data.enable_thinking,
                    "reasoning_effort": data.reasoning_effort or "medium",
                    "enable_prompt_caching": enable_prompt_caching,
                },
                request_context=_model_request_context(motet, data),
                skill_refs=data.skill_refs,
            ),
        )
    except CommandExecutionError as exc:
        logger.warning(
            "agentic_loop_finalize_failed",
            stop_reason=stop_reason,
            error_type=exc.error_type,
            error=exc.message,
        )
        if history and _is_budget_finalize(history[-1]):
            history.pop()
        return None
    except Exception as exc:
        logger.warning(
            "agentic_loop_finalize_failed",
            stop_reason=stop_reason,
            error=str(exc),
            exc_info=True,
        )
        if history and _is_budget_finalize(history[-1]):
            history.pop()
        return None

    data.model_calls_used = int(data.model_calls_used or 0) + 1
    accumulate_usage(accumulated_usage, stream_data or {})
    content = str((stream_data or {}).get("final_content") or "").strip()
    _append_assistant_message(history, content=content, tool_calls=[])
    if not content:
        logger.warning(
            "agentic_loop_finalize_empty",
            stop_reason=stop_reason,
        )
        return None

    logger.info(
        "agentic_loop_finalized",
        stop_reason=stop_reason,
        content_length=len(content),
    )
    motet.stream_event(
        "agentic_loop_finalized",
        reason=stop_reason,
        stream_key=data.stream_key,
    )
    return content


def _forced_finalize_message(
    motet: Any,
    data: AgenticLoopData,
    *,
    fallback: str,
    stop_reason: str,
    accumulated_usage: Dict[str, Any],
) -> tuple[str, bool]:
    """Return (write-up or fallback, whether the write-up succeeded)."""
    text = _try_finalize_writeup(
        motet,
        data,
        stop_reason=stop_reason,
        accumulated_usage=accumulated_usage,
    )
    if text:
        return text, True
    return fallback, False


def _replace_stop_with_finalize(
    motet: Any,
    data: AgenticLoopData,
    result: Dict[str, Any],
    *,
    iterations_used: int,
    accumulated_usage: Dict[str, Any],
    accumulated_media: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Rebuild a rail-stop result after the tools-off write-up attempt."""
    message, finalized = _forced_finalize_message(
        motet,
        data,
        fallback=str(result.get("final_response") or ""),
        stop_reason=str(result.get("stop_reason") or ""),
        accumulated_usage=accumulated_usage,
    )
    return build_loop_result(
        message,
        list(result.get("tool_results") or []),
        iterations_used,
        str(result.get("stop_reason") or ""),
        accumulated_usage,
        media=accumulated_media,
        finalized=finalized,
    )


def _is_motet_owned_turn(data: AgenticLoopData) -> bool:
    """False for hosted_tools allowlist turns (not a registry agent).

    Gates Motet's fallback system prompt. The checkpoint field is still
    spelled ``inject_meta_tools``; it means this is a Motet-owned turn.
    """
    return bool(getattr(data, "inject_meta_tools", True))


def _sort_tool_schemas_for_caching(tool_schemas: Optional[List[Any]]) -> List[Any]:
    """
    Sort tool schemas deterministically for better prompt caching.

    Stable sort by name so the tools prefix stays cacheable (ADR-0124).
    ``core.spawn_agents`` is an ordinary registry tool and sorts with the rest.
    """
    if not tool_schemas:
        return []
    return sorted(tool_schemas, key=tool_schema_name)


def _resolve_enable_prompt_caching(
    *,
    enable_prompt_caching: Optional[bool],
    model_provider: Optional[str],
    model_name: Optional[str],
) -> bool:
    """
    ADR-0124: agentic-loop default for provider prompt caching.

    - Explicit True/False from the caller wins.
    - When unset (None), default True iff the resolved ModelSpec has CAP_PROMPT_CACHING.
    - Missing specs / incapable models → False (no-op).
    """
    if enable_prompt_caching is not None:
        return bool(enable_prompt_caching)
    from ...models.registry import get_model_spec
    from ...models.specs import CAP_PROMPT_CACHING

    provider = (model_provider or DEFAULT_MODEL_PROVIDER).strip()
    name = (model_name or DEFAULT_MODEL_NAME).strip()
    spec = get_model_spec(provider, name)
    if spec is None:
        return False
    return CAP_PROMPT_CACHING in (spec.capabilities or set())


def _all_provider_executed(tool_calls: List[Dict[str, Any]]) -> bool:
    """Return True if tool_calls is non-empty and every call has kind='provider'."""
    return bool(tool_calls) and all(
        isinstance(tc, dict) and tc.get("kind") == "provider" for tc in tool_calls
    )


def _merge_tool_result_media(
    accumulator: List[Dict[str, Any]],
    tool_results: List[Dict[str, Any]],
) -> None:
    """Merge artifact-backed media from tool_results into accumulator in place (ADR-0113).

    Scans successful tool results for a canonical ``media`` list (e.g. produced by
    ``core.image_generation``) and appends each part, de-duplicating by ``artifact_id``.
    """
    if not tool_results:
        return
    seen: set = {
        str(p.get("artifact_id"))
        for p in accumulator
        if isinstance(p, dict) and p.get("artifact_id")
    }
    for entry in tool_results:
        if not isinstance(entry, dict) or entry.get("status") != "success":
            continue
        result = entry.get("result")
        if not isinstance(result, dict):
            continue
        parts = result.get("media")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if isinstance(part, dict) and part.get("artifact_id"):
                artifact_id = str(part["artifact_id"])
                if artifact_id in seen:
                    continue
                seen.add(artifact_id)
                accumulator.append(part)


def _append_assistant_message(
    history: List[Message],
    content: str = "",
    tool_calls: Optional[List[Any]] = None,
    reasoning_content: Optional[Any] = None,
    reasoning_blocks: Optional[Any] = None,
) -> None:
    """Append an assistant message with optional tool calls and reasoning (ADR-0064 R10)."""
    from motet.core.models.adapters.tool_call_codec import tool_call_requests_from_unknown

    canonical = tool_call_requests_from_unknown(tool_calls) if tool_calls else None
    history.append(
        Message(
            role="assistant",
            content=content,
            tool_calls_canonical=canonical or None,
            reasoning_content=reasoning_content,
            reasoning_blocks=reasoning_blocks,
        )
    )


def _build_agentic_system_prompt(
    handback_tool_names: Optional[List[str]] = None,
) -> str:
    """
    Build the agentic system prompt for agentic_loop (ADR-0059).

    Used only when conversation history has no ``role=system`` message yet
    (bare ``agent`` / ``agentic_loop`` entry). Normal ``agent_turn`` paths
    already supply ``AgentConfig.system_prompt``, so this fallback is skipped.

    This prompt includes:
    - Motet-only identity ("You are Motet's assistant") — provider/model
      personas (Claude, Kimi, etc.) belong in agent ``system_prompt`` configs
    - Default behavior guidance (answer directly, use tools when needed, and
      ask for a missing required fact rather than guessing — ADR-0138 decision
      9 replaced the pre-turn clarification classifier with this standing line,
      which sits with the model that holds the tool schemas)
    - Client-provided tool preference directive when the turn carries handback
      tools (ADR-0125 §5c.1 / ADR-0127): work in the client's environment should
      use the client's own tools rather than functionally-overlapping Motet tools
    - Parallel work is ``core.spawn_agents``, found through discovery like any
      other registry tool

    Current datetime is intentionally omitted (issue #131) so the system prefix stays
    cache-stable across days. For wall-clock / timezone needs, call ``core.current_time``.
    For relative delayed schedules, use ``delay_seconds`` on ``core.schedule_command``.

    Args:
        handback_tool_names: Names of externally-owned (client-provided) tools,
            enumerated verbatim so the directive matches the model's tool list.

    Returns:
        System prompt string
    """
    client_tools_section = ""
    if handback_tool_names:
        names_line = ", ".join(sorted(handback_tool_names))
        client_tools_section = f"""

Client-provided tools (important):
- The client supplied its own tools for this conversation: {names_line}.
- These run in the client's environment (its files, shell, workspace). For any task in the client's environment — reading, editing, or searching files, running commands — PREFER these client tools over similar Motet tools (e.g. prefer them over `core.file_search`, `core.file_read`, `core.host_exec`).
- Use Motet tools for server-side capabilities the client tools do not cover: scheduling, web fetch/search, admin/history, workflows."""

    return f"""You are Motet's assistant. Produce helpful, correct answers.{client_tools_section}

Default behavior:
- Answer directly if you already have enough information.
- Use tools only when needed.
- Prefer the minimum number of tool/workflow calls.
- If a fact you need to act is missing, ask for exactly those items and do not start tool calls or guess a value. Ask once, listing what you need together. If the request is answerable as asked, answer it.

Tool calling rules (important):
- Only provide parameters that exist in the tool schema.
- Always include all required parameters.
- NEVER pass empty strings for required string parameters. If you don't have a meaningful value, ask the user a clarifying question or choose a different tool that doesn't require that value.
- Use sane defaults when a parameter is optional; do not invent placeholder values.
- Date/time:
  - Do NOT guess the current date or wall-clock time.
  - Call `core.current_time` when you need "now", today's date, an absolute ISO timestamp, or timezone conversion.
  - For one-shot delayed Motet schedules ("in N seconds/minutes"), prefer `core.schedule_command` with `delay_seconds` instead of computing `scheduled_at`.
- Skill/bundle execution path rules:
  - `bundle_id` is the bundle slug (e.g. `basic-skill-example`), not a skill id.
  - If you have a skill id like `bundle.skill`, derive `bundle_id` as the text before the first dot.
  - For `core.worker_exec`, prefer bundle-relative script paths like `skills/...` (not host absolute paths).
  - If you see `/work/skills/...` or `${{MOTET_PLUGIN_ROOT:-/tmp/imf_bundles}}/<bundle_id>/skills/...`, normalize to `skills/...`.
  - Do NOT assume `/work` is the runtime root unless a tool result explicitly confirms it.
- For web requests, you MUST call `http_get_browser` to fetch the content before answering.
- Built-in web tools may be blocked or restricted (robots/bot protection). If you suspect they are blocked, try `http_get_browser` and report the result; do not refuse without attempting the fetch.
  - For bundled skill script execution, prefer `core.worker_exec`; `core.host_exec` is for host/edge domain commands, not deployed worker bundle paths.
- If you want to use a workflow tool, confirm with the user first and execute it in the next turn.

When unsure which tool/command to use:
- Call the 'help' tool FIRST when you don't know which tool/command/workflow to use for a task.
- The 'help' tool searches internal registries (tools, commands, workflows) and returns ranked recommendations.
- Prefer 'help' over external web search for internal system operations (e.g., "how to delete a schedule", "list scheduled commands").
- NEVER invent or hallucinate tool/command names - only use tools/commands that are actually returned by 'help', 'tools_list', or 'commands_list'.
- If 'commands_list' shows total > limit, call it again with limit=500 to get all commands. Do NOT invent command names to fill gaps.

Constraints:
- If you call tools/workflows, wait for results before finalizing."""


def _handback_tool_names(data: "AgenticLoopData") -> set:
    """All externally-owned tool names: explicit names plus handback schema names (ADR-0127)."""
    names = set(data.handback_tool_names or [])
    for schema in data.handback_tools or []:
        name = tool_schema_name(schema)
        if name:
            names.add(name)
    return names


def _ensure_handback_tools_in_schemas(
    tool_schemas: Optional[List[Any]],
    handback_tools: Optional[List[Any]],
) -> List[Any]:
    """
    Append externally-owned (handback) tool schemas to the model tool list (ADR-0125 §5c.1).

    Handback tools exist only on the external owner (e.g. an OpenAI facade
    client's IDE tools), so embedding discovery can never surface them — they
    must be injected every iteration. On a name collision with a discovered
    Motet tool, the external schema wins: client-declared ⇒ handback is the
    default ownership rule (ADR-0125 §5c.1), enforced here by replacing the
    registry schema so the suspension gate sees exactly what the model saw.
    """
    if not handback_tools:
        return list(tool_schemas or [])
    handback_names = {
        tool_schema_name(s) for s in handback_tools if tool_schema_name(s)
    }
    shadowed = [
        name for s in (tool_schemas or [])
        if (name := tool_schema_name(s)) in handback_names
    ]
    if shadowed:
        logger.info(
            "agentic_loop_handback_tool_shadows_registry_tool",
            shadowed_tool_names=sorted(shadowed),
        )
    kept = [s for s in (tool_schemas or []) if tool_schema_name(s) not in handback_names]
    return [*kept, *handback_tools]



def _metadata_agent_id(motet: Any) -> Optional[str]:
    """Qualified agent id stamped into command metadata by agent_turn (ADR-0083)."""
    try:
        command = getattr(motet, "_command", None)
        metadata = getattr(getattr(command, "distributed_context", None), "metadata", None) or {}
        agent_id = metadata.get("agent_id")
        return str(agent_id) if agent_id else None
    except Exception:
        return None


def _maybe_suspend_turn(
    motet: Any,
    data: AgenticLoopData,
    unique_tool_calls: List[Dict[str, Any]],
    content: str,
    iterations_used: int,
    accumulated_usage: Dict[str, Any],
    accumulated_media: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """
    ADR-0127: Suspend the turn when the model requests an externally-owned tool.

    When any call names a handback tool, the loop executes NONE of the turn's
    calls here and hands the FULL call list back (ADR-0125 deviation 5): the
    wire assistant message must declare every call or a caller-supplied resume
    transcript forks. Motet-owned calls in a mixed turn are executed by
    ``resume_turn`` when the client's observations come back (issue #159
    execute-at-resume).

    Returns a handback *intent* (Turn Runtime writes the checkpoint), or None
    when no suspension applies. Does not import ``motet.core.checkpoints``.
    """
    from motet.core.reasoning.react.loop_intents import (
        INTENT_HANDBACK,
        calls_require_handback,
        turn_intent,
    )

    handback_names = _handback_tool_names(data)
    if not calls_require_handback(unique_tool_calls, external_names=handback_names):
        return None
    # Same-iteration handbacks (ADR-0127): remaining_iterations is not
    # decremented. max_model_calls bounds Read↔model loops.
    return turn_intent(
        INTENT_HANDBACK,
        unique_tool_calls=unique_tool_calls,
        content=content,
        iterations_used=iterations_used,
        accumulated_usage=dict(accumulated_usage),
        accumulated_media=list(accumulated_media),
    )


def _suspend_for_nested_workflow(
    motet: Any,
    data: AgenticLoopData,
    nested: Dict[str, Any],
    content: str,
    iterations_used: int,
    accumulated_usage: Dict[str, Any],
    accumulated_media: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Suspend the agent turn because a nested workflow paused (issue #149).

    Facade stays dumb: handed_back_tool_calls are the workflow's pending client
    tools. Graph truth lives in WorkflowCheckpoint; TurnCheckpoint stores only
    workflow_run_id plus Motet loop state and nested_resume_history.

    Only ``handback_tools`` is agent-path consumable. Elicitation / confirmation /
    OAuth must resume via ``resume_workflow`` (or HTTP) until a Motet-native UI
    consumer exists — synthesizing fake tool calls fails late and confusingly.
    """
    from motet.core.reasoning.react.loop_intents import INTENT_NESTED_WORKFLOW, turn_intent
    from motet.core.workflow.checkpoint import WorkflowSuspendNotConsumable

    suspend_reason = str(nested.get("suspend_reason") or "").strip()
    pending = list(nested.get("pending_tool_calls") or [])
    if suspend_reason and suspend_reason != "handback_tools":
        raise WorkflowSuspendNotConsumable(
            f"Nested workflow suspended for '{suspend_reason}' which has no "
            f"agent-path consumer; resume via resume_workflow / "
            f"POST /api/v1/workflows/runs/{{id}}/resume "
            f"(workflow_run_id={nested.get('workflow_run_id')})"
        )
    if not pending:
        raise WorkflowSuspendNotConsumable(
            "Nested workflow suspended without handback tool_calls; "
            f"suspend_reason={suspend_reason or 'unknown'}; "
            f"resume via resume_workflow "
            f"(workflow_run_id={nested.get('workflow_run_id')})"
        )

    return turn_intent(
        INTENT_NESTED_WORKFLOW,
        nested=nested,
        content=content,
        iterations_used=iterations_used,
        accumulated_usage=dict(accumulated_usage),
        accumulated_media=list(accumulated_media),
    )


def _maybe_stop_for_spend(
    motet: Any,
    data: AgenticLoopData,
    *,
    iterations_used: int,
    accumulated_usage: Dict[str, Any],
    accumulated_media: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Hard-stop when the turn has spent its dollar, token, or tool-time budget.

    Unlike max_iterations this is not a Continue snapshot: inviting another
    twenty steps after a cost cap is how the spend continues. 0 on a rail
    means that rail is off. Tool time is join wall clock already on
    usage.tool_time_ms; a cache hit adds 0. Checked after each tool batch
    (this function runs at the start of the next iteration), not mid-call.
    """
    cost = float(accumulated_usage.get("cost_usd") or 0.0)
    prompt_tokens = int(accumulated_usage.get("prompt_tokens") or 0)
    tool_time_ms = int(accumulated_usage.get("tool_time_ms") or 0)
    max_cost = float(getattr(data, "max_cost_usd", 0.0) or 0.0)
    max_prompt = int(getattr(data, "max_prompt_tokens", 0) or 0)
    max_tool_time = int(getattr(data, "max_tool_time_ms", 0) or 0)

    if max_cost > 0 and cost >= max_cost:
        logger.warning(
            "agentic_loop_max_cost_reached",
            cost_usd=cost,
            max_cost_usd=max_cost,
        )
        motet.stream_event(
            "agentic_loop_stopped",
            reason="max_cost",
            cost_usd=cost,
            max_cost_usd=max_cost,
            stream_key=data.stream_key,
        )
        return build_loop_result(
            (
                f"Stopped: this turn reached its cost ceiling "
                f"(${cost:.2f} of ${max_cost:.2f}). "
                "Say what you have, or ask to continue with a narrower goal."
            ),
            [],
            iterations_used,
            "max_cost",
            accumulated_usage,
            media=accumulated_media,
        )

    if max_prompt > 0 and prompt_tokens >= max_prompt:
        logger.warning(
            "agentic_loop_max_prompt_tokens_reached",
            prompt_tokens=prompt_tokens,
            max_prompt_tokens=max_prompt,
        )
        motet.stream_event(
            "agentic_loop_stopped",
            reason="max_prompt_tokens",
            prompt_tokens=prompt_tokens,
            max_prompt_tokens=max_prompt,
            stream_key=data.stream_key,
        )
        return build_loop_result(
            (
                f"Stopped: this turn reached its prompt-token ceiling "
                f"({prompt_tokens} of {max_prompt}). "
                "Say what you have, or ask to continue with a narrower goal."
            ),
            [],
            iterations_used,
            "max_prompt_tokens",
            accumulated_usage,
            media=accumulated_media,
        )

    if max_tool_time > 0 and tool_time_ms >= max_tool_time:
        logger.warning(
            "agentic_loop_max_tool_time_reached",
            tool_time_ms=tool_time_ms,
            max_tool_time_ms=max_tool_time,
        )
        motet.stream_event(
            "agentic_loop_stopped",
            reason="max_tool_time",
            tool_time_ms=tool_time_ms,
            max_tool_time_ms=max_tool_time,
            stream_key=data.stream_key,
        )
        elapsed_s = tool_time_ms / 1000.0
        ceiling_s = max_tool_time / 1000.0
        return build_loop_result(
            (
                f"Stopped: this turn reached its tool-time ceiling "
                f"({elapsed_s:.1f}s of {ceiling_s:.0f}s). "
                "Say what you have, or ask to continue with a narrower goal."
            ),
            [],
            iterations_used,
            "max_tool_time",
            accumulated_usage,
            media=accumulated_media,
        )
    return None


def _budget_stop_result(
    motet: Any,
    data: AgenticLoopData,
    *,
    message: str,
    stop_reason: str,
    iterations_used: int,
    accumulated_usage: Dict[str, Any],
    accumulated_media: List[Dict[str, Any]],
    finalized: bool = False,
) -> Dict[str, Any]:
    """Return a budget-stop intent; Turn Runtime persists the Continue snapshot."""
    from motet.core.reasoning.react.loop_intents import INTENT_BUDGET_STOP, turn_intent

    return turn_intent(
        INTENT_BUDGET_STOP,
        message=message,
        stop_reason=stop_reason,
        iterations_used=iterations_used,
        accumulated_usage=dict(accumulated_usage),
        accumulated_media=list(accumulated_media),
        finalized=finalized,
    )


def _maybe_stop_for_stall(
    motet: Any,
    data: AgenticLoopData,
    filter_result: ToolCallBuildResult,
    iterations_used: int,
    accumulated_usage: Dict[str, Any],
    accumulated_media: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Stop the turn when it stops asking for anything new.

    Replaces the per-call duplicate veto. Repeating a call is legitimate — the file
    was just edited, the job status moved on — and identical parameters are exactly
    what a re-read of the same target looks like, so a veto cannot separate that
    from a stuck model. Worse, a veto is escapable: nudge an offset and the "same"
    call goes through, which is how refused re-reads turned into windows that crept
    a byte at a time.

    Progress is judged per iteration instead: any novel call clears the counter, so
    a re-read beside new work costs nothing, while a model that only asks for what
    it already has trips the rail. Stopping the turn is not escapable by perturbing
    parameters. Mutates data.stalled_iterations.
    """
    data.stalled_iterations = (
        0 if filter_result.had_novel_tool_call else data.stalled_iterations + 1
    )
    if data.stalled_iterations < MAX_STALLED_ITERATIONS:
        return None

    repeated_names = sorted({
        str(tc.get("tool_name") or "")
        for tc in filter_result.unique_tool_calls
        if tc.get("is_repeat")
    })
    logger.warning(
        "agentic_loop_stalled",
        stalled_iterations=data.stalled_iterations,
        max_stalled_iterations=MAX_STALLED_ITERATIONS,
        repeated_tools=repeated_names,
    )
    motet.stream_event(
        "agentic_loop_stopped",
        reason="stalled",
        stalled_iterations=data.stalled_iterations,
        stream_key=data.stream_key,
    )
    # Non-empty message: budget/stop paths must say something on the wire (ADR-0127).
    return build_loop_result(
        f"Stopped: the last {data.stalled_iterations} steps requested only "
        "information already gathered in this turn, so the task is not progressing. "
        "Please continue with a different approach.",
        [], iterations_used, "stalled", accumulated_usage,
        media=accumulated_media,
    )


def agentic_loop(data: AgenticLoopData) -> Dict[str, Any]:
    """
    Execute one iteration of the agentic loop pattern (ADR-0050, ADR-0059).

    In-process (ADR-0132): not a distributed command. Further Motet-tool
    iterations are requested by returning a continuation for
    ``run_agentic_loop``. Streams all events to the unified task-level stream.
    
    Args:
        data: AgenticLoopData with conversation history, tool schemas, and iteration state
        
    Returns:
        Dict with final response, tool results, and loop metadata
    """
    motet = get_motet_context()
    
    # Performance timing instrumentation
    timings = {
        "embedding_ms": 0.0,   # ADR-0074: embedding-only schema resolution (replaces discovery_ms)
        "llm_ms": 0.0,
        "tool_execution_ms": 0.0,
    }
    
    # Accumulated usage tracking (ADR-0064 R9: canonical usage envelope)
    accumulated_usage = dict(data.usage_accumulator or {})
    accumulated_usage.setdefault("prompt_tokens", 0)
    accumulated_usage.setdefault("completion_tokens", 0)
    accumulated_usage.setdefault("total_tokens", 0)
    accumulated_usage.setdefault("cache_read_tokens", 0)
    accumulated_usage.setdefault("cache_creation_tokens", 0)
    accumulated_usage.setdefault("reasoning_tokens", 0)
    accumulated_usage.setdefault("tool_time_ms", 0)

    # ADR-0113: media (e.g. generated images) accumulated across recursive iterations.
    # Seeded from the caller so earlier iterations' media survive to the terminal return.
    accumulated_media: List[Dict[str, Any]] = list(data.media_accumulator or [])

    # Motet-tool iteration index (unchanged across client handback suspend/resume).
    current_iteration = data.current_iteration
    iterations_used = current_iteration  # count the current Motet iteration as "used" once it runs
    # First model call of the turn (not the same as Motet iteration 1 after handbacks).
    is_first_model_call = data.model_calls_used == 0

    logger.info(
        "agentic_loop_started",
        remaining_iterations=data.remaining_iterations,
        model_calls_used=data.model_calls_used,
        max_model_calls=data.max_model_calls,
        tool_count=len(data.tools) if data.tools else 0,
        tools_pending_discovery=data.tools is None,
        has_input=bool(data.input),
        handback_tool_count=len(data.handback_tools or []),
    )

    evict_stale_artifact_view_sidecars(
        data.conversation_history,
        current_iteration=current_iteration,
    )
    if is_first_model_call and conversation_has_attachments(data.conversation_history):
        data.tool_filter_metadata = ensure_tool_filter_required_tools(
            data.tool_filter_metadata,
            list(ATTACHMENT_TOOL_NAMES),
        )

    # Stream agentic loop start event
    motet.stream_event(
        "agentic_loop_iteration",
        iteration=current_iteration,
        remaining=data.remaining_iterations,
        model_calls_used=data.model_calls_used,
        stream_key=data.stream_key,
    )
    
    # Emit reasoning_step event for UI (ADR-0050: Agentic Loop observability)
    emit_reasoning_event(
        motet,
        strategy="agentic_loop",
        step=current_iteration,
        thought=f"Starting agentic loop iteration {current_iteration}",
        action=None,
        observation=None,
        stream_key=data.stream_key,
    )
    
    # Check Motet-tool iteration limit
    if data.remaining_iterations <= 0:
        logger.warning("agentic_loop_max_iterations_reached")
        motet.stream_event(
            "agentic_loop_stopped",
            reason="max_iterations",
            stream_key=data.stream_key,
        )
        message, finalized = _forced_finalize_message(
            motet,
            data,
            fallback=(
                "Maximum iterations reached. Please continue to keep working on this task."
            ),
            stop_reason="max_iterations",
            accumulated_usage=accumulated_usage,
        )
        return _budget_stop_result(
            motet,
            data,
            message=message,
            stop_reason="max_iterations",
            iterations_used=data.max_iterations,
            accumulated_usage=accumulated_usage,
            accumulated_media=accumulated_media,
            finalized=finalized,
        )

    # Hard safety rail for handback↔model loops (same-iteration handbacks).
    if data.model_calls_used >= data.max_model_calls:
        logger.warning(
            "agentic_loop_max_model_calls_reached",
            model_calls_used=data.model_calls_used,
            max_model_calls=data.max_model_calls,
        )
        motet.stream_event(
            "agentic_loop_stopped",
            reason="max_model_calls",
            model_calls_used=data.model_calls_used,
            stream_key=data.stream_key,
        )
        message, finalized = _forced_finalize_message(
            motet,
            data,
            fallback=(
                "Model-call budget exhausted for this turn. "
                "Please continue to keep working on this task."
            ),
            stop_reason="max_model_calls",
            accumulated_usage=accumulated_usage,
        )
        return _budget_stop_result(
            motet,
            data,
            message=message,
            stop_reason="max_model_calls",
            iterations_used=iterations_used,
            accumulated_usage=accumulated_usage,
            accumulated_media=accumulated_media,
            finalized=finalized,
        )

    spend_stop = _maybe_stop_for_spend(
        motet,
        data,
        iterations_used=iterations_used,
        accumulated_usage=accumulated_usage,
        accumulated_media=accumulated_media,
    )
    if spend_stop is not None:
        return _replace_stop_with_finalize(
            motet,
            data,
            spend_stop,
            iterations_used=iterations_used,
            accumulated_usage=accumulated_usage,
            accumulated_media=accumulated_media,
        )

    # Step 1a: First model-call setup — add system prompt and ensure user message is present.
    if is_first_model_call:
        has_system_prompt = any(msg.role == "system" for msg in data.conversation_history)
        if not has_system_prompt and _is_motet_owned_turn(data):
            system_prompt = _build_agentic_system_prompt(
                # ADR-0125 §5c.1: enumerate externally-owned tools so the model
                # prefers them for client-environment work over Motet lookalikes.
                handback_tool_names=sorted(_handback_tool_names(data)),
            )
            data.conversation_history.insert(0, Message(role="system", content=system_prompt))
            logger.debug("agentic_loop_added_system_prompt")
        if data.input and needs_user_turn(data.conversation_history):
            data.conversation_history.append(Message(role="user", content=data.input))
            logger.debug("agentic_loop_added_user_message")

    # ADR-0111: Prefilled tool call(s) short-circuit the first planning model call.
    # Applies to the first action only; later model calls behave normally. Multiple
    # entries execute together as parallel tool calls in a single assistant turn.
    prefilled = data.prefilled_tool_calls if is_first_model_call else None
    # Trailing wrap-up only when a model will read the messages this iteration.
    if not prefilled:
        _maybe_append_budget_wrap_up(data)
    if prefilled:
        prefill_error = validate_prefilled_tool_calls(motet, data, prefilled)
        if prefill_error:
            logger.error(
                "agentic_loop_prefilled_tool_call_invalid",
                error=prefill_error,
                tool_names=[item.tool_name for item in prefilled],
            )
            motet.stream_event(
                "agentic_loop_error",
                phase="prefilled_tool_call",
                error=prefill_error,
                stream_key=data.stream_key,
            )
            return build_loop_result(
                f"Prefilled tool call rejected: {prefill_error}",
                [], iterations_used, "error", accumulated_usage,
            )

    # Step 1b: Resolve tool schemas for this model call (meta-tool disclosure).
    #
    # Only when tools is None (caller did not specify). When tools is [] (explicit
    # empty) or [schemas], honour the caller's choice — do NOT rebuild. This
    # prevents workflow-nested agents (e.g. expert-panel optimist/skeptic with
    # explicit_tools: []) from receiving unexpected tools.
    #
    # Shortlist is always frozen: sticky + always-sticky meta tools + keyword
    # pins + required_tools. Catalog reachability is tools_search → tool_call
    # (FunctionDiscoveryVectorStore is used on demand by tools_search, not as a
    # per-turn loop prelude).
    t0_embedding = time.perf_counter()
    # ADR-0111: skip shortlist build when the first action is prefilled; no model
    # call will consume the schemas this iteration.
    if not prefilled and data.tools is None:
        from .tool_shortlist import load_tool_shortlist, store_tool_shortlist

        iter_query = build_context_query(data.input, data.conversation_history)
        sticky_names = load_tool_shortlist(
            tenant_id=motet.tenant_id,
            motet_id=motet.motet_id,
            conversation_id=motet.conversation_id,
        )
        data.tools = merge_sticky_tool_schemas(
            sticky_names,
            motet,
            data.max_tools,
            tool_filter_metadata=data.tool_filter_metadata,
            query=iter_query,
        )
        motet.stream_event(
            "agentic_loop_meta_disclosure_shortlist",
            iteration=current_iteration,
            schemas_found=len(data.tools),
            sticky_carryover=len(sticky_names),
            stream_key=data.stream_key,
        )
        # Persist the working set so the next turn starts from it (best-effort).
        store_tool_shortlist(
            tenant_id=motet.tenant_id,
            motet_id=motet.motet_id,
            conversation_id=motet.conversation_id,
            tool_names=[tool_schema_name(t) for t in (data.tools or [])],
        )
    # else: tools was explicitly provided ([] or [schemas]), use as-is
    timings["embedding_ms"] = (time.perf_counter() - t0_embedding) * 1000

    # Inject externally-owned handback tool schemas (ADR-0125 §5c.1) — always,
    # every iteration, after discovery so the shortlist can never crowd them
    # out. Injected after the sticky-shortlist store above so client tool names
    # do not leak into the conversation's persisted Motet tool working set.
    if data.handback_tools:
        data.tools = _ensure_handback_tools_in_schemas(data.tools, data.handback_tools)
        logger.debug(
            "agentic_loop_handback_tools_injected",
            iteration=current_iteration,
            handback_count=len(data.handback_tools),
        )

    # Step 2: Main LLM call — the sole inference step per iteration (ADR-0074).
    # This call receives the embedding-resolved schemas, streams tokens to the client,
    # and returns either tool calls (with parameters) or a direct text response.
    tool_calls: List[Any] = []
    finish_reason = "stop"
    content = ""
    reasoning_content = None
    reasoning_blocks = None

    tool_names_log = [tool_schema_name(t) for t in (data.tools or [])]
    logger.info(
        "agentic_loop_calling_llm",
        iteration=current_iteration,
        message_count=len(data.conversation_history),
        tool_count=len(data.tools) if data.tools else 0,
        tool_names=tool_names_log[:10],
    )
    motet.stream_event("agentic_loop_llm_inference", phase="starting", stream_key=data.stream_key)

    from motet.core.commands.response_models import CommandExecutionError

    try:
        from motet.core.commands.builtin.model import model_stream
        from motet.core.commands.command_data_classes import ModelStreamData

        t0_llm = time.perf_counter()
        # Build request_context from motet so payload (and trace/debug inputs) show
        # tenant_id, principal_id, etc.; model_stream still fills from motet if missing (ADR-0064).
        request_context = _model_request_context(motet, data)
        if prefilled:
            # ADR-0111: synthesize the model output instead of calling the model.
            stream_data = prefilled_stream_data(prefilled)
            tool_names = [str(item.tool_name) for item in prefilled]
            motet.stream_event(
                "agentic_loop_prefilled_tool_call",
                iteration=current_iteration,
                tool_names=tool_names,
                stream_key=data.stream_key,
            )
            logger.info(
                "agentic_loop_prefilled_tool_call",
                iteration=current_iteration,
                tool_names=tool_names,
            )
        else:
            enable_prompt_caching = _resolve_enable_prompt_caching(
                enable_prompt_caching=data.enable_prompt_caching,
                model_provider=data.model_provider,
                model_name=data.model_name,
            )
            # Sort once: the probe must fingerprint the exact list the provider sees.
            sorted_tools = _sort_tool_schemas_for_caching(data.tools)
            from .prompt_cache_probe import record_prompt_fingerprint

            record_prompt_fingerprint(
                tenant_id=motet.tenant_id,
                motet_id=motet.motet_id,
                conversation_id=getattr(motet, "conversation_id", None),
                tools=sorted_tools,
                messages=data.conversation_history,
                iteration=current_iteration,
                model_calls_used=data.model_calls_used,
            )
            stream_data = motet.do(
                model_stream,
                data=ModelStreamData(
                    messages=data.conversation_history,
                    stream_key=data.stream_key,
                    tools=sorted_tools,
                    model_settings={
                        "provider": data.model_provider or DEFAULT_MODEL_PROVIDER,
                        "model_name": data.model_name or DEFAULT_MODEL_NAME,
                        "temperature": data.temperature,
                        # Always pass both keys explicitly so the adapter never silently activates thinking.
                        "enable_thinking": data.enable_thinking,
                        "reasoning_effort": data.reasoning_effort or "medium",
                        # ADR-0124: agentic default on for CAP_PROMPT_CACHING models.
                        "enable_prompt_caching": enable_prompt_caching,
                    },
                    request_context=request_context,
                    skill_refs=data.skill_refs,
                ),
            )
        timings["llm_ms"] = (time.perf_counter() - t0_llm) * 1000

        content = stream_data.get("final_content", "")
        tool_calls = stream_data.get("tool_calls_canonical", []) or []
        finish_reason = stream_data.get("finish_reason", "stop")
        reasoning_content = stream_data.get("reasoning_content")
        reasoning_blocks = stream_data.get("reasoning_blocks")

        # Count real model inferences only (prefilled first action is not a model call).
        if not prefilled:
            data.model_calls_used = int(data.model_calls_used or 0) + 1

        accumulate_usage(accumulated_usage, stream_data)

        _append_assistant_message(
            data.conversation_history,
            content=content,
            tool_calls=tool_calls,
            reasoning_content=reasoning_content,
            reasoning_blocks=reasoning_blocks,
        )

        logger.info(
            "agentic_loop_llm_response",
            iteration=current_iteration,
            finish_reason=finish_reason,
            tool_call_count=len(tool_calls) if tool_calls else 0,
            content_length=len(content),
            tokens_streamed=stream_data.get("tokens_streamed", 0),
        )
        motet.stream_event(
            "agentic_loop_llm_inference",
            phase="completed",
            finish_reason=finish_reason,
            tool_calls_count=len(tool_calls) if tool_calls else 0,
            stream_key=data.stream_key,
        )

        # Emit reasoning event for UI observability.
        if tool_calls:
            from motet.core.models.adapters.tool_call_codec import tool_call_requests_from_unknown

            called_names: List[str] = [
                tc.tool_name or "unknown" for tc in tool_call_requests_from_unknown(tool_calls)
            ]
            emit_reasoning_event(
                motet,
                strategy="agentic_loop",
                step=current_iteration,
                thought=f"LLM decided to use: {', '.join(called_names[:3])}{'...' if len(called_names) > 3 else ''}",
                action=f"Calling {len(tool_calls)} tool(s)",
                observation=None,
                stream_key=data.stream_key,
            )
        elif content:
            emit_reasoning_event(
                motet,
                strategy="agentic_loop",
                step=current_iteration,
                thought="LLM provided final response",
                action=None,
                observation=content[:200],
                stream_key=data.stream_key,
            )

    except CommandExecutionError as e:
        logger.error("agentic_loop_llm_failed", error_type=e.error_type, error_message=e.message)
        motet.stream_event("agentic_loop_error", phase="llm_inference", error=e.message, stream_key=data.stream_key)
        return build_loop_result(
            f"LLM inference failed: {e.message}", [], iterations_used, "error", accumulated_usage,
            media=accumulated_media,
        )

    except Exception as e:
        logger.error("agentic_loop_llm_exception", error=str(e), exc_info=True)
        motet.stream_event("agentic_loop_error", phase="llm_inference", error=str(e), stream_key=data.stream_key)
        raise
    
    # Step 3: Check finish_reason
    # Also check if ALL tool calls are provider-executed (kind="provider")
    # For Anthropic's server tools (e.g., web_search), the response already contains
    # the complete output with citations - no need for another iteration
    all_provider_tools = _all_provider_executed(tool_calls)
    
    if finish_reason == "stop" or not tool_calls or (all_provider_tools and content):
        # Get the last assistant message content as final response
        final_response = ""
        for msg in reversed(data.conversation_history):
            if msg.role == "assistant" and msg.content:
                final_response = msg.content
                break
        
        # Loop complete - return final response
        effective_finish_reason = "stop" if all_provider_tools else finish_reason
        logger.info("agentic_loop_complete", 
                   finish_reason=effective_finish_reason,
                   has_content=bool(final_response),
                   all_provider_tools=all_provider_tools)
        motet.stream_event(
            "agentic_loop_complete",
            reason=effective_finish_reason,
            stream_key=data.stream_key,
        )
        return build_loop_result(
            final_response or "Task completed.", [], iterations_used, effective_finish_reason, accumulated_usage,
            media=accumulated_media,
        )
    
    # Step 3: Build the unique tool calls to run this iteration.
    filter_result = build_unique_tool_calls(tool_calls, data, motet, current_iteration)

    stall_result = _maybe_stop_for_stall(
        motet, data, filter_result, iterations_used, accumulated_usage, accumulated_media,
    )
    if stall_result is not None:
        return _replace_stop_with_finalize(
            motet,
            data,
            stall_result,
            iterations_used=iterations_used,
            accumulated_usage=accumulated_usage,
            accumulated_media=accumulated_media,
        )

    # ADR-0127: externally-owned tool requested — checkpoint state and hand the
    # turn's calls back instead of executing.
    suspend_result = _maybe_suspend_turn(
        motet, data, filter_result.unique_tool_calls, content,
        iterations_used, accumulated_usage, accumulated_media,
    )
    if suspend_result is not None:
        return suspend_result

    exec_result = execute_tools_and_append_results(
        filter_result.unique_tool_calls,
        filter_result.provider_executed_results,
        data, motet,
        current_iteration, iterations_used, accumulated_usage, timings,
    )
    # ADR-0113: fold any artifact-backed media produced this iteration into the
    # accumulator so it survives to the terminal (text-only) return and recursion.
    _merge_tool_result_media(accumulated_media, exec_result.tool_results)

    if exec_result.auth_response is not None:
        if accumulated_media:
            exec_result.auth_response.setdefault("media", accumulated_media)
        return exec_result.auth_response
    if exec_result.early_return is not None:
        if exec_result.early_return.get("nested_workflow_suspend"):
            return _suspend_for_nested_workflow(
                motet,
                data,
                exec_result.early_return,
                content,
                iterations_used,
                accumulated_usage,
                accumulated_media,
            )
        if accumulated_media:
            exec_result.early_return.setdefault("media", accumulated_media)
        return exec_result.early_return

    expose_activated_skill_runner_tools(
        filter_result.unique_tool_calls,
        exec_result.tool_results,
        data,
        motet,
    )

    fast_path_result = maybe_fast_path_return(
        motet, data, filter_result.unique_tool_calls, exec_result.tool_results, iterations_used, accumulated_usage,
    )
    if fast_path_result is not None:
        if accumulated_media:
            fast_path_result.setdefault("media", accumulated_media)
        return fast_path_result

    # Step 6: Ask the in-process driver to run the next iteration
    # (ADR-0132). Track tools called this iteration for observability / resume.
    this_iteration_tool_names: List[str] = [
        str(tc.get("tool_name") or "")
        for tc in (filter_result.unique_tool_calls or [])
        if isinstance(tc, dict) and tc.get("tool_name")
    ]
    next_used_tool_names: List[str] = list(
        set(data.used_tool_names) | set(this_iteration_tool_names)
    )

    logger.info(
        "agentic_loop_calling_next",
        remaining_iterations=data.remaining_iterations - 1,
        used_tool_names=next_used_tool_names,
    )
    motet.stream_event(
        "agentic_loop_recursion",
        remaining_iterations=data.remaining_iterations - 1,
        stream_key=data.stream_key,
    )

    from .loop_driver import agentic_loop_continue
    from .loop_state_snapshot import LoopStateSnapshot

    return agentic_loop_continue(
        LoopStateSnapshot.from_loop_data(
            data,
            remaining_iterations=data.remaining_iterations - 1,
            usage_accumulator=accumulated_usage,
            used_tool_names=next_used_tool_names,
            media_accumulator=accumulated_media,
            agent_id=data.agent_id or _metadata_agent_id(motet),
        ).to_loop_data(
            conversation_history=data.conversation_history,
            stream_key=data.stream_key,
        )
    )


__all__ = ["agentic_loop", "AgenticLoopData"]


