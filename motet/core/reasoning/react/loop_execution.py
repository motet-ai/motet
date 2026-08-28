"""
Motet - Agentic Loop Execution Helpers

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    Tool execution, dedup, prefilled-tool-call, and fast-path helpers for the
    agentic loop (issue #147). Owns ToolCallBuildResult / ExecuteToolsResult /
    ExecutionCommand, signature derivation for resume_turn, and the phases that
    build tool calls, execute tools/workflows, and optionally return user-facing
    tool output without a final LLM call. Honors tool ``cache_control``:
    a fresh same-signature hit replays a short notice instead of re-running.

Dependencies:
    - structlog: Structured logging for distributed tracing
    - emit_reasoning_event: Thought/action/observation events for UI
    - loop_discovery: normalize_exec_and_catalog_parameters for exec/catalog fixes
    - AgenticLoopData / PrefilledToolCall: command input models
    - tool_execution / workflow_execution: distributed command backends
    - ParameterInjectionService: token replacement before execution
    - tool_call_codec.tool_calls_from_message: reads tool_calls_canonical only
      (issue #225; leftover tool_calls keys are not lifted)

Usage:
    from motet.core.reasoning.react.loop_execution import (
        build_unique_tool_calls,
        execute_tools_and_append_results,
        derive_executed_signatures,
    )

    filter_result = build_unique_tool_calls(
        tool_calls, data, motet, current_iteration,
    )
    exec_result = execute_tools_and_append_results(
        filter_result.unique_tool_calls,
        filter_result.provider_executed_results,
        data, motet, current_iteration, iterations_used, usage, timings,
    )

Notes:
    - Mechanically extracted from agentic_loop.py (issue #147 Priority 1 step 2).
    - Observation clip / MCP / workflow formatters live in loop_observations;
      this module imports them from there and is their only production caller.
      Artifact sidecars live in loop_skills; build_loop_result / accumulate_usage
      live in loop_results. All are module-scope imports: nothing here imports
      from agentic_loop, so react/ is an acyclic graph and this module can be
      imported and tested without pulling in the conductor.
    - Stall control (MAX_STALLED_ITERATIONS / _maybe_stop_for_stall) stays in
      agentic_loop as loop-control, not execution.
    - This module is the home of these symbols: import and patch them here.
    - Naming: phase entry points called by agentic_loop are public; per-phase
      internals keep the leading underscore. A leading underscore on a
      cross-module name makes Pyright report the definition as unaccessed and
      the importer as reportPrivateUsage.
    - The workflow fast path resolves one level of core.tool_call indirection:
      after core.tools_search the model dispatches through the meta-tool, so the
      outer name in the batch is core.tool_call and the presentation opt-in
      belongs to the workflow underneath.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence
from uuid import uuid4

import structlog

from ...models.adapters.tool_call_codec import tool_calls_from_message
from ...tools.cache_control import (
    cached_observation_text,
    inherit_snapshot_cache,
    remember_observation,
    take_fresh_cache_hit,
)
from ...types import Message, ToolCallRequest, tool_schema_name
from ..reasoning_events import emit_reasoning_event
from .agentic_loop_data import AgenticLoopData, PrefilledToolCall
from .loop_discovery import normalize_exec_and_catalog_parameters
from .loop_observations import (
    clip_observation,
    extract_text_from_mcp_result,
    format_workflow_steps,
)
from .loop_results import accumulate_usage, build_loop_result
from .loop_skills import ARTIFACT_VIEW_TOOL_NAMES, build_artifact_view_sidecar

logger = structlog.get_logger(__name__)


@dataclass(frozen=False)
class ToolCallBuildResult:
    """Result of build_unique_tool_calls."""
    unique_tool_calls: List[Dict[str, Any]]
    provider_executed_results: List[Dict[str, Any]]
    # False when every call this iteration repeats one the turn already made —
    # the progress signal behind the stall rail (see MAX_STALLED_ITERATIONS).
    had_novel_tool_call: bool = True


@dataclass(frozen=False)
class ExecuteToolsResult:
    """Result of execute_tools_and_append_results."""
    tool_results: List[Dict[str, Any]]
    auth_response: Optional[Dict[str, Any]]  # If set, caller should return this
    early_return: Optional[Dict[str, Any]]   # If set (e.g. no valid commands), caller should return this


@dataclass(frozen=True)
class ExecutionCommand:
    """(command, data) pair for motet.join(); command is tool_execution or workflow_execution."""
    command: Any
    data: Any


def _get_tool_presentation(motet: Any, tool_name: str) -> Optional[Dict[str, Any]]:
    """Fetch presentation metadata for a tool from the registry (if available)."""
    try:
        registry = getattr(motet, "tools", None)
        if registry and hasattr(registry, "get"):
            tool = registry.get(tool_name)
            if tool and hasattr(tool, "presentation"):
                pres = getattr(tool, "presentation")
                return pres if isinstance(pres, dict) else None
    except Exception:
        return None
    return None


def _get_workflow_presentation(tool_name: str) -> Optional[Dict[str, Any]]:
    """Fetch presentation metadata for a workflow tool from WorkflowRegistry."""
    if not (tool_name or "").startswith("workflow_"):
        return None
    try:
        from ...workflow import WorkflowRegistry

        workflow_id = tool_name[9:]
        workflow = WorkflowRegistry.get(workflow_id)
        if workflow and workflow.presentation:
            return workflow.presentation if isinstance(workflow.presentation, dict) else None
    except Exception:
        return None
    return None


def _get_presentation_for_tool(motet: Any, tool_name: str) -> Dict[str, Any]:
    """Resolve presentation metadata for a tool or workflow tool name."""
    if (tool_name or "").startswith("workflow_"):
        return _get_workflow_presentation(tool_name) or {}
    return _get_tool_presentation(motet, tool_name) or {}


_DISPATCH_TOOL_NAME = "core.tool_call"


def _explicit_catalog_names(data: AgenticLoopData) -> Optional[set]:
    """Names the model may call when the caller supplied ``tools``.

    ``None`` on the field means discovery — no extra allowlist. A list,
    including empty, is the catalog: ``core.spawn_agents`` children pass the
    declared schemas so ``core.tools_search`` cannot reopen the parent's grant.
    """
    if data.tools is None:
        return None
    return {name for schema in data.tools if (name := tool_schema_name(schema))}


def _dispatched_workflow_name(tool_call: Dict[str, Any], result: Any) -> Optional[str]:
    """
    Workflow this call dispatches through ``core.tool_call``, if it does.

    ``core.tools_search`` hands the model a canonical name and it calls
    ``core.tool_call`` with it, so on the discovery path the outer name is the
    dispatcher and the presentation opt-in belongs to the workflow underneath.
    The dispatcher echoes the name it normalized and ran in its result meta;
    the call parameters are the fallback when no result is at hand.

    Non-workflow dispatch is deliberately left alone: ``core.tool_call``'s own
    observation is a status line, so fast-pathing a tool through it would show
    the user the dispatch summary instead of the tool's output.
    """
    if str(tool_call.get("tool_name") or "") != _DISPATCH_TOOL_NAME:
        return None

    target: Optional[str] = None
    if isinstance(result, dict):
        meta = result.get("meta")
        if isinstance(meta, dict) and isinstance(meta.get("tool_name"), str):
            target = meta["tool_name"]
    if target is None:
        params = tool_call.get("parameters")
        if isinstance(params, dict) and isinstance(params.get("tool_name"), str):
            target = params["tool_name"]

    return target if (target or "").startswith("workflow_") else None


def _dispatched_workflow_result(result: Any) -> Any:
    """Strip ``core.tool_call``'s ok() envelope from around the workflow result."""
    if isinstance(result, dict) and isinstance(result.get("result"), dict):
        return result["result"]
    return result


def _results_by_call_id(tool_results: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {
        str(r.get("tool_call_id")): r for r in tool_results if r.get("tool_call_id") is not None
    }


def _extract_passthrough_from_workflow_result(
    workflow_result: Dict[str, Any],
    presentation: Dict[str, Any],
) -> Optional[str]:
    """
    Extract user-facing passthrough content from a completed workflow result.

    Uses presentation.passthrough_field when set, otherwise workflow_result.output_field.
    """
    if not isinstance(workflow_result, dict) or "step_results" not in workflow_result:
        return None

    passthrough_field = (
        presentation.get("passthrough_field")
        or workflow_result.get("output_field")
    )
    if not passthrough_field:
        return None

    step_results = workflow_result.get("step_results") or {}
    if not step_results:
        return None

    last_step_id = list(step_results.keys())[-1]
    last_step = step_results.get(last_step_id) or {}
    if not isinstance(last_step, dict):
        return None

    status = last_step.get("status")
    if status is not None and status not in {"success", "completed"}:
        return None

    # Wrapped command envelope: {status, data: {field: ...}}.
    # Agent-turn steps store the command payload itself: {agent_id, final_response}.
    raw = None
    step_data = last_step.get("data")
    if isinstance(step_data, dict) and passthrough_field in step_data:
        raw = step_data.get(passthrough_field)
    elif passthrough_field in last_step:
        raw = last_step.get(passthrough_field)
    if raw is None:
        return None

    if isinstance(raw, (dict, list)):
        text = json.dumps(raw, indent=2, ensure_ascii=False)
    else:
        text = str(raw).strip()

    if not text:
        return None

    wrap = presentation.get("response_wrap")
    if wrap == "json_fence" and not text.startswith("```"):
        return f"```json\n{text}\n```"
    return text


def _extract_fast_path_tool_texts(
    motet: Any,
    data: AgenticLoopData,
    unique_tool_calls: List[Dict[str, Any]],
    tool_results: List[Dict[str, Any]],
) -> List[str]:
    """Collect user-facing tool output for fast-path return, preferring workflow passthrough fields."""
    results_by_call_id = _results_by_call_id(tool_results)
    tool_texts: List[str] = []

    for tool_call in unique_tool_calls:
        tool_name = str(tool_call.get("tool_name") or "")
        call_id = str(tool_call.get("tool_call_id") or "")
        result_entry = results_by_call_id.get(call_id) or {}
        workflow_result = result_entry.get("result")

        dispatched = _dispatched_workflow_name(tool_call, workflow_result)
        if dispatched:
            tool_name = dispatched
            workflow_result = _dispatched_workflow_result(workflow_result)

        if tool_name.startswith("workflow_") and isinstance(workflow_result, dict):
            presentation = _get_workflow_presentation(tool_name) or {}
            passthrough = _extract_passthrough_from_workflow_result(workflow_result, presentation)
            if passthrough:
                tool_texts.append(passthrough)
                continue
            # Presentation asked for a field we could not find. Do not dump the
            # clipped workflow observation (step JSON, STM rows, artifact pointer)
            # as the user-visible answer — skip so the caller can keep looping.
            if presentation.get("passthrough_field") or (
                isinstance(workflow_result, dict) and workflow_result.get("output_field")
            ):
                continue

        for msg in data.conversation_history:
            if msg.role == "tool" and str(getattr(msg, "tool_call_id", "") or "") == call_id:
                if msg.content:
                    tool_texts.append(str(msg.content).strip())
                break

    return tool_texts


def _all_tools_user_facing(motet: Any, tool_calls: List[Dict], tool_results: List[Dict]) -> bool:
    """
    Check if all tools in this batch are user-facing and don't require LLM post-processing.
    
    Used for fast-path optimization: if all tools in a batch have requires_llm=False and
    user_facing=True, we can skip the final LLM call and return tool outputs directly.
    
    Args:
        motet: MotetContext instance
        tool_calls: List of unique tool calls with tool_name
        tool_results: List of tool execution results with status
        
    Returns:
        True if all tools are user-facing and don't require LLM, False otherwise
    """
    # All results must be successful
    if not tool_results or any(r.get("status") != "success" for r in tool_results):
        return False
    
    # Check each tool's presentation metadata (tools and opt-in workflow tools).
    results_by_call_id = _results_by_call_id(tool_results)
    for tc in tool_calls:
        result = (results_by_call_id.get(str(tc.get("tool_call_id") or "")) or {}).get("result")
        tool_name = _dispatched_workflow_name(tc, result) or tc.get("tool_name", "")
        presentation = _get_presentation_for_tool(motet, tool_name)
        requires_llm = presentation.get("requires_llm", True)
        user_facing = presentation.get("user_facing", False)

        # If any tool requires LLM or isn't user-facing, skip fast-path
        if requires_llm is not False or user_facing is not True:
            return False

    return True


def _synthesize_prefilled_tool_call(prefilled: PrefilledToolCall) -> Dict[str, Any]:
    """Build a canonical tool-call dict (ADR-0064 shape) from a prefilled spec (ADR-0111)."""
    arguments = dict(prefilled.arguments or {})
    try:
        arguments_json = json.dumps(arguments, sort_keys=True)
    except (TypeError, ValueError):
        arguments_json = "{}"
    return {
        "call_id": f"prefilled_{uuid4().hex[:12]}",
        "tool_name": str(prefilled.tool_name or "").strip(),
        "arguments": arguments,
        "arguments_json": arguments_json,
    }


def prefilled_stream_data(prefilled: List[PrefilledToolCall]) -> Dict[str, Any]:
    """Return a model_stream-shaped result for prefilled tool call(s) (ADR-0111).

    This lets the prefilled-tool-call path reuse the exact downstream handling
    (assistant-message append, dedup/filter, parallel execution, fast-path)
    without a model call. Multiple entries become parallel tool calls in a single
    assistant turn. Usage counters are zero because no LLM ran.
    """
    return {
        "final_content": "",
        "tool_calls_canonical": [
            _synthesize_prefilled_tool_call(item) for item in prefilled
        ],
        "finish_reason": "tool_calls",
        "reasoning_content": None,
        "reasoning_blocks": None,
        "tokens_streamed": 0,
    }


def _prefilled_tool_filter_violation(
    metadata: Optional[Dict[str, Any]],
    tool_name: str,
) -> Optional[str]:
    """Return a reason string if the tool is excluded by the agent tool filter (ADR-0093), else None."""
    if not metadata:
        return None
    if tool_name in set(metadata.get("exclude_tools") or []):
        return f"Tool {tool_name!r} is excluded by the agent tool filter"
    if tool_name.startswith("workflow_"):
        if metadata.get("no_workflows"):
            return "Workflows are disabled for this agent; cannot prefill a workflow tool call"
        wf_id = tool_name[len("workflow_"):]
        excluded_workflows = metadata.get("exclude_workflows")
        if isinstance(excluded_workflows, (list, set, tuple)):
            excluded_set = set(excluded_workflows)
            if wf_id in excluded_set or tool_name in excluded_set:
                return f"Workflow {tool_name!r} is excluded by the agent tool filter"
    return None


def validate_prefilled_tool_calls(
    motet: Any,
    data: AgenticLoopData,
    prefilled: List[PrefilledToolCall],
) -> Optional[str]:
    """Validate prefilled tool call(s) (ADR-0111). Return an error string, or None when valid.

    Each entry's tool/workflow must exist and must not be excluded by the agent's
    tool filter. Fails loudly on the first invalid entry rather than silently
    falling back to a model call.
    """
    if not prefilled:
        return "prefilled_tool_calls must contain at least one entry"
    for item in prefilled:
        error = _validate_prefilled_tool_call_item(motet, data, item)
        if error is not None:
            return error
    return None


def _validate_prefilled_tool_call_item(
    motet: Any,
    data: AgenticLoopData,
    prefilled: PrefilledToolCall,
) -> Optional[str]:
    """Validate a single prefilled tool call entry (ADR-0111)."""
    tool_name = str(prefilled.tool_name or "").strip()
    if not tool_name:
        return "prefilled_tool_calls[].tool_name is required"

    if tool_name.startswith("workflow_"):
        try:
            from ...workflow import WorkflowRegistry

            workflow_id = tool_name[len("workflow_"):]
            if WorkflowRegistry.get(workflow_id) is None:
                return f"Unknown workflow for prefilled tool call: {tool_name!r}"
        except Exception as exc:  # registry unavailable — fail loudly
            return f"Could not resolve prefilled workflow {tool_name!r}: {exc}"
    else:
        registry = getattr(motet, "tools", None)
        resolved = None
        if registry is not None and hasattr(registry, "get"):
            resolved = registry.get(tool_name)
        if not resolved:
            return f"Unknown tool for prefilled tool call: {tool_name!r}"

    return _prefilled_tool_filter_violation(data.tool_filter_metadata, tool_name)


def _generate_tool_signature(tool_name: str, parameters: Dict[str, Any]) -> str:
    """
    Generate a unique signature for a tool call for duplicate detection.
    
    Args:
        tool_name: Name of the tool
        parameters: Tool parameters
        
    Returns:
        Signature string: tool_name:params_hash
    """
    # Sort parameters for consistent hashing
    param_str = json.dumps(parameters, sort_keys=True)
    param_hash = hashlib.md5(param_str.encode()).hexdigest()[:8]
    return f"{tool_name}:{param_hash}"


def _message_attr(message: Any, name: str) -> Any:
    """Read a field from a Message model or its dict form."""
    if isinstance(message, dict):
        return message.get(name)
    return getattr(message, name, None)


def _tool_call_name_and_parameters(call: ToolCallRequest) -> Optional[tuple]:
    """Extract (tool_name, parameters) from a canonical ``ToolCallRequest``.

    ``derive_executed_signatures`` only feeds this helper the result of
    ``tool_calls_from_message``, which is ``ToolCallRequest`` (issue #225).
    """
    if call.kind == "provider":
        return None
    if not call.tool_name:
        return None
    parameters = call.arguments if isinstance(call.arguments, dict) else {}
    return call.tool_name, parameters


def derive_executed_signatures(history: Optional[Sequence[Any]]) -> List[str]:
    """Recompute duplicate-detection signatures from a transcript.

    A signature is a claim *about the transcript* ("this call already ran and
    its result is above"), so it is only valid for the transcript it was derived
    from. When a caller owns the wire transcript (ADR-0125 §5c.1 / ADR-0127) and
    prunes it — client-side summarization drops old tool results — a signature
    carried over from Motet's checkpoint outlives its observation. The loop then
    refuses to re-fetch data the model can no longer see and tells it to "adjust
    parameters", which is what drives read-window thrashing.

    Deriving instead means the set shrinks exactly when the evidence shrinks: a
    call counts as executed only while its ``role="tool"`` result is still
    present. Within an uninterrupted turn Motet only appends, so every executed
    call remains derivable and runaway-loop protection is unchanged.

    Assistant calls are read via ``tool_calls_from_message`` (ADR-0137 / #225):
    ``tool_calls_canonical`` only. Leftover ``tool_calls`` keys are ignored.
    """
    if not history:
        return []

    settled_call_ids: set = set()
    for message in history:
        if str(_message_attr(message, "role") or "") != "tool":
            continue
        call_id = str(_message_attr(message, "tool_call_id") or "").strip()
        if call_id:
            settled_call_ids.add(call_id)

    signatures: List[str] = []
    seen: set = set()
    for message in history:
        if str(_message_attr(message, "role") or "") != "assistant":
            continue
        for call in tool_calls_from_message(message):
            call_id = str(call.call_id or "").strip()
            if call_id not in settled_call_ids:
                # No result in this transcript: the model cannot see an answer,
                # so the call must be allowed to run again.
                continue
            extracted = _tool_call_name_and_parameters(call)
            if not extracted:
                continue
            signature = _generate_tool_signature(extracted[0], extracted[1])
            if signature not in seen:
                seen.add(signature)
                signatures.append(signature)
    return signatures


def _normalize_tool_call(
    tc: Dict[str, Any],
    index: Optional[int] = None,
    group_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Normalize a tool call dict to canonical shape (ADR-0064).
    Handles both standardized format (tool_name/parameters) and OpenAI tool_calls format (function.name/arguments).
    """
    if not isinstance(tc, dict):
        return None
    # Canonical or discovery format: tool_name + arguments/parameters
    tool_name = str(tc.get("tool_name") or "").strip()
    if not tool_name and "function" in tc:
        fn = tc.get("function") or {}
        tool_name = str(fn.get("name") or "").strip()
    if not tool_name:
        return None

    args = tc.get("arguments")
    if args is None:
        args = tc.get("parameters")
    if args is None and "function" in tc:
        try:
            args = json.loads((tc.get("function") or {}).get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
    if not isinstance(args, dict):
        args = {}
    args_json = tc.get("arguments_json")
    if not isinstance(args_json, str) or not args_json:
        try:
            args_json = json.dumps(args)
        except Exception:
            args_json = "{}"
    call_id = str(tc.get("call_id") or tc.get("id") or f"call_{uuid4().hex}")
    out = {
        "call_id": call_id,
        "tool_name": tool_name,
        "arguments_json": args_json,
        "arguments": args,
        "tool_call_group_id": group_id or tc.get("tool_call_group_id"),
        "tool_call_index": index if index is not None else tc.get("tool_call_index"),
    }
    if tc.get("kind"):
        out["kind"] = tc["kind"]
    return out


def build_unique_tool_calls(
    tool_calls: List[Dict[str, Any]],
    data: AgenticLoopData,
    motet: Any,
    current_iteration: int,
) -> ToolCallBuildResult:
    """
    Build the tool calls to run this iteration.

    Records call signatures for stall detection and skips provider-executed tools
    (appending placeholder results). Mutates data.executed_signatures and
    data.conversation_history (provider tool messages).

    Fan-out is ``core.spawn_agents``, which runs through the ordinary tool
    path below and returns an observation, so the loop keeps control.

    Repeated calls reach this path, not a veto: repetition is a signal, not an
    error. A tool the turn already ran is often legitimately needed again — the
    file was just edited, the job status changed — and the parameters are
    identical precisely because the target is. Refusing the call cannot
    distinguish those from a stuck model. A fresh ``cache_control`` entry is
    the one cheap skip (replay a notice; do not re-hit Playwright). Cost and
    looping are bounded by max_model_calls and MAX_STALLED_ITERATIONS.
    """
    filtered_tool_calls: List[Dict[str, Any]] = []
    for tool_call in tool_calls:
        normalized = _normalize_tool_call(tool_call) if isinstance(tool_call, dict) else None
        if not normalized:
            continue
        tool_name = normalized["tool_name"]
        args = normalized["arguments"]

        filtered_tool_calls.append(normalized)

    unique_tool_calls: List[Dict[str, Any]] = []
    provider_executed_results: List[Dict[str, Any]] = []
    had_novel_tool_call = False
    if len(filtered_tool_calls) > data.max_tools:
        logger.info(
            "agentic_loop_tool_calls_exceed_max_tools",
            tool_calls_count=len(filtered_tool_calls),
            max_tools=data.max_tools,
            note="Executing all tool calls from the same assistant turn to preserve protocol correctness",
        )

    # IMPORTANT: execute all tool calls emitted in a single assistant turn.
    # Truncating here can leave unmatched tool_call_ids, which providers reject.
    for tool_call in filtered_tool_calls:
        if not isinstance(tool_call, dict):
            continue
        tool_name = str(tool_call.get("tool_name") or "")
        if not tool_name:
            continue
        if tool_call.get("kind") == "provider":
            call_id = str(tool_call.get("call_id") or tool_call.get("id") or "")
            provider = data.model_provider or "openai"
            logger.info("agentic_loop_provider_tool_skipped",
                       tool_name=tool_name, call_id=call_id, provider=provider,
                       reason="Provider already executed this builtin tool")
            motet.stream_event(
                "agentic_loop_provider_tool",
                tool_name=tool_name,
                kind="provider",
                status="already_executed",
                stream_key=data.stream_key,
            )
            from ...models.adapters.provider_builtin_tools import requires_tool_result_for_provider_builtins
            if requires_tool_result_for_provider_builtins(provider):
                provider_result_content = "[Tool completed by provider - results incorporated into response]"
                data.conversation_history.append(Message(
                    role="tool",
                    tool_call_id=call_id,
                    name=tool_name,
                    content=provider_result_content,
                ))
                provider_executed_results.append({
                    "tool_call_id": call_id,
                    "tool_name": tool_name,
                    "result": provider_result_content,
                    "status": "success",
                })
            continue
        call_id = str(tool_call.get("call_id") or tool_call.get("id") or f"call_{len(unique_tool_calls)}")
        catalog = _explicit_catalog_names(data)
        if catalog is not None and tool_name not in catalog:
            reason = (
                f"Tool {tool_name!r} is not in this turn's declared catalog."
            )
            logger.info(
                "agentic_loop_tool_outside_catalog",
                tool_name=tool_name,
                catalog=sorted(catalog),
            )
            data.conversation_history.append(Message(
                role="tool",
                tool_call_id=call_id,
                name=tool_name,
                content=reason,
            ))
            provider_executed_results.append({
                "tool_call_id": call_id,
                "tool_name": tool_name,
                "result": reason,
                "status": "error",
            })
            continue
        parameters = tool_call.get("arguments")
        if parameters is None or (isinstance(parameters, dict) and not parameters):
            parameters = tool_call.get("parameters")
        if parameters is None or (isinstance(parameters, dict) and not parameters):
            arguments_json = tool_call.get("arguments_json")
            if isinstance(arguments_json, str) and arguments_json:
                try:
                    parameters = json.loads(arguments_json)
                except json.JSONDecodeError:
                    logger.warning("agentic_loop_invalid_arguments_json", tool_name=tool_name, arguments_json=arguments_json)
                    continue
        if not isinstance(parameters, dict):
            parameters = {}
        signature = _generate_tool_signature(tool_name, parameters)
        is_repeat = signature in data.executed_signatures
        if is_repeat:
            logger.info(
                "agentic_loop_repeat_tool_call",
                tool_name=tool_name,
                signature=signature,
                stalled_iterations=data.stalled_iterations,
            )
        else:
            had_novel_tool_call = True
            data.executed_signatures.append(signature)
        unique_tool_calls.append({
            "tool_call_id": call_id,
            "tool_name": tool_name,
            "parameters": parameters,
            "signature": signature,
            "is_repeat": is_repeat,
            "tool_call_group_id": tool_call.get("tool_call_group_id"),
            "tool_call_index": tool_call.get("tool_call_index"),
        })

    return ToolCallBuildResult(
        unique_tool_calls=unique_tool_calls,
        provider_executed_results=provider_executed_results,
        # An iteration with nothing executable (all provider-side or filtered out)
        # has no repetition to judge, so it must not count against the rail.
        had_novel_tool_call=had_novel_tool_call or not unique_tool_calls,
    )


def _spawn_meta_from_payload(payload: Any) -> Optional[Dict[str, Any]]:
    """Find spawn snapshot meta on the live envelope or a contextualized alias."""
    if not isinstance(payload, dict):
        return None
    candidates = [
        payload.get("meta"),
        payload.get("spawn_agents.meta"),
    ]
    inner = payload.get("result")
    if isinstance(inner, dict):
        candidates.append(inner.get("meta"))
        candidates.append(inner.get("spawn_agents.meta"))
    for raw in candidates:
        meta = raw
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError):
                continue
            meta = parsed
        if isinstance(meta, dict) and (
            "snapshot_cache" in meta or "snapshot_signatures" in meta
        ):
            return meta
    return None


def _inherit_spawn_snapshot_cache(data: AgenticLoopData, payload: Any) -> None:
    """Merge child snapshot-tool freshness onto the parent after fan-in.

    ``core.spawn_agents`` puts the children's cache keys in ``meta``.
    The parent does not receive the child's page bodies — a later hit
    is a refetch veto that points at the spawn observation. Snapshot tools
    only: ``http_get`` / ``http_get_browser`` / ``web_search``.
    """
    meta = _spawn_meta_from_payload(payload)
    if not isinstance(meta, dict):
        return
    inherited = inherit_snapshot_cache(
        data.observation_cache,
        data.executed_signatures,
        meta.get("snapshot_cache"),
        meta.get("snapshot_signatures"),
    )
    if inherited:
        logger.info(
            "agentic_loop_inherited_spawn_snapshot_cache",
            inherited=inherited,
            stream_key=data.stream_key,
        )


def _append_cached_observation(
    tool_call: Dict[str, Any],
    data: AgenticLoopData,
    motet: Any,
    current_iteration: int,
    tool_results: List[Dict[str, Any]],
    *,
    hit: Any = None,
) -> None:
    """Replay a 304-style notice for a fresh cache-control hit."""
    tool_name = str(tool_call.get("tool_name") or "")
    call_id = str(tool_call.get("tool_call_id") or "")
    inherited_from = getattr(hit, "inherited_from", None) if hit is not None else None
    artifact_id = getattr(hit, "artifact_id", None) if hit is not None else None
    notice = cached_observation_text(
        tool_name,
        inherited_from=inherited_from,
        artifact_id=artifact_id,
    )
    tool_results.append({
        "tool_call_id": call_id,
        "tool_name": tool_name,
        "result": notice,
        "status": "success",
        "cached": True,
    })
    data.conversation_history.append(Message(
        role="tool",
        tool_call_id=call_id,
        name=tool_name,
        content=notice,
    ))
    emit_reasoning_event(
        motet,
        strategy="agentic_loop",
        step=current_iteration,
        thought=f"{tool_name} cached",
        action=tool_name,
        observation=notice,
        stream_key=data.stream_key,
    )
    motet.stream_event(
        "agentic_loop_tool_cache_hit",
        tool_name=tool_name,
        signature=tool_call.get("signature"),
        stream_key=data.stream_key,
    )
    logger.info(
        "agentic_loop_tool_cache_hit",
        tool_name=tool_name,
        signature=tool_call.get("signature"),
    )


def execute_tools_and_append_results(
    unique_tool_calls: List[Dict[str, Any]],
    provider_executed_results: List[Dict[str, Any]],
    data: AgenticLoopData,
    motet: Any,
    current_iteration: int,
    iterations_used: int,
    accumulated_usage: Dict[str, Any],
    timings: Dict[str, float],
) -> ExecuteToolsResult:
    """
    Run parameter injection, execute tools/workflows in parallel, process results, append to history.
    Mutates data.conversation_history, accumulated_usage, timings.
    """
    tool_results: List[Dict[str, Any]] = []
    if provider_executed_results:
        tool_results.extend(provider_executed_results)
        logger.info("agentic_loop_provider_executed_tools", count=len(provider_executed_results))

    if not unique_tool_calls:
        return ExecuteToolsResult(tool_results=tool_results, auth_response=None, early_return=None)

    try:
        from ...tools.parameter_injection import ParameterInjectionService

        injection_service = ParameterInjectionService(registry=motet.tools)
        for tool_call in unique_tool_calls:
            tool_call["parameters"] = injection_service.inject_parameters(
                tool_call["tool_name"],
                tool_call["parameters"],
                principal_id=motet.principal_id,
                tenant_id=motet.tenant_id,
                task_id=motet.task_id,
                conversation_id=motet.conversation_id,
            )
    except Exception as e:
        logger.warning("agentic_loop_parameter_injection_failed", error=str(e))

    normalize_exec_and_catalog_parameters(unique_tool_calls, data)

    from motet.core.commands.response_models import GatherExecutionError

    try:
        logger.info("agentic_loop_executing_tools", count=len(unique_tool_calls))
        motet.stream_event(
            "agentic_loop_tool_execution",
            phase="starting",
            tool_count=len(unique_tool_calls),
            stream_key=data.stream_key,
        )

        from motet.core.commands.builtin.tool import tool_execution
        from motet.core.commands.builtin.workflow import workflow_execution
        from motet.core.commands.command_data_classes import ToolExecutionData

        execution_commands: List[ExecutionCommand] = []
        executed_tool_calls: List[Dict[str, Any]] = []
        now = time.time()
        for tool_call in unique_tool_calls:
            tool_name = tool_call["tool_name"]
            hit = take_fresh_cache_hit(
                data.observation_cache,
                str(tool_call.get("signature") or ""),
                now=now,
                executed_signatures=data.executed_signatures,
            )
            if hit is not None:
                _append_cached_observation(
                    tool_call, data, motet, current_iteration, tool_results,
                    hit=hit,
                )
                continue
            if tool_name.startswith("workflow_"):
                workflow_id = tool_name[9:]
                try:
                    from ...workflow import WorkflowRegistry
                    workflow_data = WorkflowRegistry.prepare_workflow_for_execution(
                        workflow_id=workflow_id,
                        llm_parameters=tool_call["parameters"],
                        motet=motet,
                    )
                    # Propagate agent handback schemas so workflow ownership=handback
                    # steps can pause with OpenAI-shaped pending tool_calls (#149).
                    if getattr(data, "handback_tools", None) and not getattr(
                        workflow_data, "handback_tools", None
                    ):
                        workflow_data.handback_tools = list(data.handback_tools or [])
                    execution_commands.append(ExecutionCommand(workflow_execution, workflow_data))
                    executed_tool_calls.append(tool_call)
                except ValueError as e:
                    logger.error("agentic_loop_workflow_not_found", workflow_id=workflow_id, tool_name=tool_name, error=str(e))
                    tool_results.append({
                        "tool_call_id": tool_call["tool_call_id"],
                        "tool_name": tool_name,
                        "result": f"Error: {str(e)}",
                        "error": True,
                    })
                except Exception as e:
                    logger.error("agentic_loop_workflow_prep_failed", tool_name=tool_name, error=str(e), exc_info=True)
                    tool_results.append({
                        "tool_call_id": tool_call["tool_call_id"],
                        "tool_name": tool_name,
                        "result": f"Error preparing workflow: {str(e)}",
                        "error": True,
                    })
            else:
                execution_commands.append(ExecutionCommand(
                    tool_execution,
                    ToolExecutionData(
                        tool_name=tool_name,
                        parameters=tool_call["parameters"],
                        tool_call_id=tool_call.get("tool_call_id"),
                        tool_call_group_id=tool_call.get("tool_call_group_id"),
                        tool_call_index=tool_call.get("tool_call_index"),
                        conversation_history=data.conversation_history,
                        stream_key=data.stream_key,
                    ),
                ))
                executed_tool_calls.append(tool_call)

        if not execution_commands:
            if tool_results:
                return ExecuteToolsResult(
                    tool_results=tool_results,
                    auth_response=None,
                    early_return=None,
                )
            logger.warning("agentic_loop_no_valid_commands", unique_tool_calls_count=len(unique_tool_calls))
            return ExecuteToolsResult(
                tool_results=tool_results,
                auth_response=None,
                early_return=build_loop_result("No valid tools or workflows to execute.", tool_results, 0, "error", accumulated_usage),
            )

        # Propagate full chat/model + artifact RAG auth metadata to tool
        # executions. Explicit metadata= on join replaces parent metadata, so
        # use DELEGATED_CONTEXT_KEYS (not SCHEDULE alone) or RAG broader-scope
        # authorization is silently dropped (ADR-0122). tool_filter_metadata
        # is also delegated so core.tool_call / core.tools_search enforce the
        # same ToolFilter gates that shaped the shortlist.
        from motet.core.commands.command_data_classes import DELEGATED_CONTEXT_KEYS
        inherited_metadata: Optional[Dict[str, Any]] = {}
        parent_metadata = dict(getattr(motet, "metadata", {}) or {})
        for key in DELEGATED_CONTEXT_KEYS:
            value = parent_metadata.get(key)
            if value is not None and value != "" and value != []:
                inherited_metadata[key] = value

        # Data-level model settings are authoritative for the current loop execution.
        if data.model_profile_name:
            inherited_metadata["model_profile_name"] = data.model_profile_name
        if data.model_provider:
            inherited_metadata["model_provider"] = data.model_provider
        if data.model_name:
            inherited_metadata["model_name"] = data.model_name
        if data.enable_thinking is not None:
            inherited_metadata["enable_thinking"] = bool(data.enable_thinking)
        if data.reasoning_effort:
            inherited_metadata["reasoning_effort"] = data.reasoning_effort
        # Loop data is authoritative for the filter even when parent metadata
        # omitted it (turn puts the filter on AgentData; nested joins need it).
        if data.tool_filter_metadata:
            inherited_metadata["tool_filter_metadata"] = data.tool_filter_metadata

        inherited_metadata = inherited_metadata or None
        t0_tool_exec = time.perf_counter()
        results = motet.join(
            [(e.command, e.data) for e in execution_commands],
            fail_fast=False,
            metadata=inherited_metadata,
        )
        tool_exec_ms = (time.perf_counter() - t0_tool_exec) * 1000
        timings["tool_execution_ms"] = tool_exec_ms
        accumulate_usage(accumulated_usage, {"tool_time_ms": tool_exec_ms})

        for idx, tool_result_data in enumerate(results):
            tool_call = executed_tool_calls[idx] if idx < len(executed_tool_calls) else unique_tool_calls[idx]
            # Detect errors from two sources:
            # 1. motet.join() failure: sets "_error" key when the Celery command itself fails
            # 2. tool_execution internal error: returns {"error": "...", "error_type": "..."} as
            #    a success-wrapped dict when an exception occurs inside tool_execution (e.g. TimeoutError)
            _is_command_error = isinstance(tool_result_data, dict) and tool_result_data.get("_error")
            _is_tool_error = (
                isinstance(tool_result_data, dict)
                and "error_type" in tool_result_data
                and not tool_result_data.get("executed")
            )
            if _is_command_error or _is_tool_error:
                if _is_command_error:
                    error_msg = f"Error: {tool_result_data.get('message', 'Unknown error')}"
                else:
                    error_detail = tool_result_data.get("error", "Unknown error")
                    error_type = tool_result_data.get("error_type", "Error")
                    error_msg = f"Error ({error_type}): {error_detail}"
                tool_results.append({
                    "tool_call_id": tool_call["tool_call_id"],
                    "tool_name": tool_call["tool_name"],
                    "result": error_msg,
                    "status": "error",
                })
                data.conversation_history.append(Message(
                    role="tool",
                    tool_call_id=tool_call["tool_call_id"],
                    name=tool_call["tool_name"],
                    content=error_msg,
                ))
                emit_reasoning_event(
                    motet,
                    strategy="agentic_loop",
                    step=current_iteration,
                    thought=f"{tool_call['tool_name']} failed",
                    action=tool_call["tool_name"],
                    observation=error_msg,
                    stream_key=data.stream_key,
                )
            else:
                tool_name = tool_call["tool_name"]
                from motet.core.commands.builtin.auth_handler import check_auth_required, handle_auth_required
                if check_auth_required(tool_result_data):
                    auth_result = handle_auth_required(
                        tool_result=tool_result_data,
                        tool_name=tool_name,
                        tool_call_id=tool_call["tool_call_id"],
                        motet=motet,
                        iteration=current_iteration,
                        strategy_name="agentic_loop",
                    )
                    tool_results.append({
                        "tool_call_id": tool_call["tool_call_id"],
                        "tool_name": tool_name,
                        "result": auth_result.to_dict(),
                        "status": "auth_required",
                    })
                    data.conversation_history.append(Message(
                        role="tool",
                        tool_call_id=tool_call["tool_call_id"],
                        name=tool_name,
                        content=auth_result.to_user_message(),
                    ))
                    return ExecuteToolsResult(
                        tool_results=tool_results,
                        auth_response=auth_result.to_reasoning_response(iterations_used=iterations_used),
                        early_return=None,
                    )

                # Nested workflow suspend (issue #149): bubble pending client
                # tool_calls up so the agent turn can checkpoint and hand back.
                if (
                    isinstance(tool_result_data, dict)
                    and (
                        tool_result_data.get("suspended")
                        or tool_result_data.get("status") == "suspended"
                    )
                    and tool_name.startswith("workflow_")
                ):
                    logger.info(
                        "agentic_loop_workflow_suspended",
                        tool_name=tool_name,
                        workflow_run_id=tool_result_data.get("workflow_run_id"),
                        suspend_reason=tool_result_data.get("suspend_reason"),
                        pending_count=len(tool_result_data.get("pending_tool_calls") or []),
                    )
                    return ExecuteToolsResult(
                        tool_results=tool_results,
                        auth_response=None,
                        early_return={
                            "nested_workflow_suspend": True,
                            "workflow_run_id": tool_result_data.get("workflow_run_id"),
                            "suspend_reason": tool_result_data.get("suspend_reason"),
                            "pending_tool_calls": list(
                                tool_result_data.get("pending_tool_calls") or []
                            ),
                            "pending_interactions": list(
                                tool_result_data.get("pending_interactions") or []
                            ),
                            "nested_workflow_tool_call_id": tool_call.get("tool_call_id"),
                            "nested_workflow_tool_name": tool_name,
                        },
                    )

                is_workflow = tool_name.startswith("workflow_") or (
                    isinstance(tool_result_data, dict) and "step_results" in tool_result_data
                )
                if is_workflow:
                    result_content = format_workflow_steps(tool_result_data)
                    workflow_result = tool_result_data
                else:
                    if isinstance(tool_result_data, str):
                        raw_result = tool_result_data
                        result_content = tool_result_data
                    else:
                        raw_result = tool_result_data.get("result", {}) if isinstance(tool_result_data, dict) else tool_result_data
                        result_content = extract_text_from_mcp_result(raw_result)
                    workflow_result = raw_result
                tool_results.append({
                    "tool_call_id": tool_call["tool_call_id"],
                    "tool_name": tool_name,
                    "result": workflow_result,
                    "status": "success",
                })
                artifact_id = None
                if isinstance(tool_result_data, dict):
                    raw_aid = tool_result_data.get("artifact_id")
                    if isinstance(raw_aid, str) and raw_aid.strip():
                        artifact_id = raw_aid.strip()
                data.conversation_history.append(Message(
                    role="tool",
                    tool_call_id=tool_call["tool_call_id"],
                    name=tool_name,
                    content=clip_observation(
                        result_content, artifact_id=artifact_id
                    ),
                ))
                signature = str(tool_call.get("signature") or "")
                if signature:
                    remember_observation(
                        data.observation_cache,
                        signature=signature,
                        tool_name=tool_name,
                        payload=tool_result_data,
                    )
                if tool_name == "core.spawn_agents":
                    _inherit_spawn_snapshot_cache(data, tool_result_data)
                if tool_name in ARTIFACT_VIEW_TOOL_NAMES and isinstance(raw_result, dict):
                    sidecar = build_artifact_view_sidecar(
                        tool_call,
                        raw_result,
                        current_iteration=current_iteration,
                    )
                    if sidecar is not None:
                        data.conversation_history.append(sidecar)

        logger.info("agentic_loop_tools_executed",
                   success_count=sum(1 for r in tool_results if r["status"] == "success"),
                   error_count=sum(1 for r in tool_results if r["status"] == "error"))
        logger.info("agentic_loop_timings",
                   iteration=current_iteration,
                   embedding_ms=round(timings["embedding_ms"], 2),
                   llm_ms=round(timings["llm_ms"], 2),
                   tool_execution_ms=round(timings["tool_execution_ms"], 2),
                   total_ms=round(timings["embedding_ms"] + timings["llm_ms"] + timings["tool_execution_ms"], 2))
        motet.stream_event(
            "agentic_loop_tool_execution",
            phase="completed",
            success_count=sum(1 for r in tool_results if r["status"] == "success"),
            error_count=sum(1 for r in tool_results if r["status"] == "error"),
            stream_key=data.stream_key,
        )
        return ExecuteToolsResult(tool_results=tool_results, auth_response=None, early_return=None)

    except GatherExecutionError as e:
        logger.error("agentic_loop_tool_execution_failed",
                    error_type=e.error_type,
                    error_message=e.message,
                    partial_results_count=len(e.partial_results))
        motet.stream_event(
            "agentic_loop_error",
            phase="tool_execution",
            error=e.message,
            stream_key=data.stream_key,
        )
        raise
    except Exception as e:
        logger.error("agentic_loop_tool_execution_failed", error=str(e), exc_info=True)
        motet.stream_event(
            "agentic_loop_error",
            phase="tool_execution",
            error=str(e),
            stream_key=data.stream_key,
        )
        raise


def maybe_fast_path_return(
    motet: Any,
    data: AgenticLoopData,
    unique_tool_calls: List[Dict[str, Any]],
    tool_results: List[Dict[str, Any]],
    iterations_used: int,
    accumulated_usage: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """
    If all tools are user-facing and don't require LLM, return loop result with tool output.
    Otherwise return None (caller continues to recursion).
    """
    try:
        if not _all_tools_user_facing(motet, unique_tool_calls, tool_results):
            return None
        tool_names = [tc["tool_name"] for tc in unique_tool_calls]
        tool_texts = _extract_fast_path_tool_texts(
            motet, data, unique_tool_calls, tool_results,
        )
        if len(tool_texts) == 1:
            final_response = tool_texts[0]
        elif tool_texts:
            final_response = "\n\n".join(tool_texts)
        else:
            final_response = ""
        if not final_response:
            return None
        logger.info("agentic_loop_fast_path_returning_tool_output",
                    tool_count=len(unique_tool_calls), tool_names=tool_names)
        try:
            if motet.redis:
                # One frame, whitespace preserved. Word-splitting flattened
                # markdown headers and lists into a single paragraph.
                motet.stream_token(f"\n\n{final_response}", stream_key=data.stream_key)
        except Exception as e:
            logger.warning("agentic_loop_fast_path_streaming_failed", error=str(e), exc_info=True)
        motet.stream_event("agentic_loop_complete", reason="deterministic_tool_output",
                           tool_count=len(unique_tool_calls), tool_names=tool_names, stream_key=data.stream_key)
        return build_loop_result(final_response, tool_results, iterations_used, "stop", accumulated_usage)
    except Exception as e:
        logger.warning("agentic_loop_fast_path_failed", error=str(e), error_type=type(e).__name__, exc_info=True)
        return None
