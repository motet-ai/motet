"""
Motet - Canonical Tool-Call Codec

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Single owner for assistant tool-call shape. Lifts Chat Completions,
    Responses, and canonical dicts into ``ToolCallRequest``, and renders
    ``ToolCallRequest`` to provider transcript items.

    Name convert (``mcp.`` ↔ ``mcp__``) is NOT this module. Outbound wire names
    are applied in ``model.py``; inbound wire names are applied by adapters via
    ``tool_wire_to_canonical`` when parsing provider events.

Dependencies:
    - json: arguments_json round-trip
    - motet.core.types: ToolCallRequest

Usage:
    from motet.core.models.adapters.tool_call_codec import (
        tool_call_request_from_unknown,
        tool_calls_from_message,
        tool_call_requests_to_openai_chat,
    )
    calls = tool_calls_from_message(msg)
    wire = tool_call_requests_to_openai_chat(calls)

Notes:
    - Assistant tool calls live on ``tool_calls_canonical``; leftover ``tool_calls`` keys are discarded, not lifted (issue #225).
    - Provider-executed calls (kind="provider") are skipped when rendering
      function_call / tool_use items that the local runtime cannot replay.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence

from motet.core.types import ToolCallRequest


def _arguments_json_from_unknown(tc: Dict[str, Any], *, fn: Optional[Dict[str, Any]] = None) -> str:
    """Best-effort JSON string from any of the historical argument keys."""
    args_json = tc.get("arguments_json")
    if isinstance(args_json, str) and args_json:
        return args_json
    if fn is not None:
        args = fn.get("arguments")
        if isinstance(args, str) and args:
            return args
        if isinstance(args, dict):
            try:
                return json.dumps(args)
            except (TypeError, ValueError):
                return "{}"
    for key in ("arguments", "parameters", "input"):
        val = tc.get(key)
        if isinstance(val, str) and val:
            return val
        if isinstance(val, dict):
            try:
                return json.dumps(val)
            except (TypeError, ValueError):
                return "{}"
    return "{}"


def tool_call_request_from_unknown(tc: Any) -> Optional[ToolCallRequest]:
    """Lift one tool-call payload into ``ToolCallRequest``.

    Accepts ``ToolCallRequest``, canonical dicts, ChatCompletions
    ``{function: {name, arguments}}``, and Responses ``{name, call_id, arguments}``.
    Returns None when name and call_id cannot both be recovered (call_id may be
    synthesized as ``call_unknown`` when a name exists).
    """
    if isinstance(tc, ToolCallRequest):
        return tc
    if not isinstance(tc, dict):
        return None

    fn = tc.get("function") if isinstance(tc.get("function"), dict) else None
    tool_name = str(
        tc.get("tool_name")
        or (fn.get("name") if fn else None)
        or tc.get("name")
        or ""
    ).strip()
    if not tool_name:
        return None

    call_id = str(tc.get("call_id") or tc.get("id") or tc.get("tool_call_id") or "").strip()
    if not call_id:
        call_id = "call_unknown"

    args_json = _arguments_json_from_unknown(tc, fn=fn)
    try:
        args_obj = json.loads(args_json) if args_json else None
        if not isinstance(args_obj, dict):
            args_obj = None
    except (json.JSONDecodeError, TypeError):
        args_obj = None

    kind = tc.get("kind")
    kind_str = str(kind) if isinstance(kind, str) and kind else None
    thought = tc.get("thought_signature")
    thought_str = str(thought) if isinstance(thought, str) and thought else None

    return ToolCallRequest(
        call_id=call_id,
        tool_name=tool_name,
        arguments_json=args_json,
        arguments=args_obj,
        kind=kind_str,
        thought_signature=thought_str,
        tool_call_group_id=tc.get("tool_call_group_id"),
        tool_call_index=tc.get("tool_call_index") if isinstance(tc.get("tool_call_index"), int) else None,
        arguments_artifact_id=tc.get("arguments_artifact_id"),
    )


def tool_call_requests_from_unknown(items: Any) -> List[ToolCallRequest]:
    """Lift a list of mixed tool-call payloads. Drops unparseable entries."""
    if not items:
        return []
    out: List[ToolCallRequest] = []
    for tc in items:
        parsed = tool_call_request_from_unknown(tc)
        if parsed is not None:
            out.append(parsed)
    return out


def tool_calls_from_message(msg: Any) -> List[ToolCallRequest]:
    """Read canonical tool calls off a Message, dict, or similar.

    Reads ``tool_calls_canonical`` only. Leftover ``tool_calls`` keys are ignored
    (issue #225). The OpenAI HTTP facade converts client wire before persistence.
    """
    if msg is None:
        return []
    if isinstance(msg, dict):
        canonical = msg.get("tool_calls_canonical")
    else:
        canonical = getattr(msg, "tool_calls_canonical", None)
    if canonical:
        return tool_call_requests_from_unknown(canonical)
    return []


def message_has_tool_calls(msg: Any) -> bool:
    """True when an assistant message declares one or more tool calls."""
    return bool(tool_calls_from_message(msg))


def tool_call_requests_to_openai_chat(calls: Sequence[ToolCallRequest]) -> List[Dict[str, Any]]:
    """Render ToolCallRequest list as Chat Completions ``message.tool_calls``.

    Names are passed through; ``model.py`` already applied wire format.
    """
    out: List[Dict[str, Any]] = []
    for tc in calls:
        if tc.kind == "provider":
            continue
        out.append(
            {
                "id": tc.call_id,
                "type": "function",
                "function": {
                    "name": tc.tool_name,
                    "arguments": tc.arguments_json or "{}",
                },
            }
        )
    return out


def tool_call_requests_to_responses_items(calls: Sequence[ToolCallRequest]) -> List[Dict[str, Any]]:
    """Render ToolCallRequest list as Responses ``function_call`` input items."""
    out: List[Dict[str, Any]] = []
    for tc in calls:
        if tc.kind == "provider":
            continue
        out.append(
            {
                "type": "function_call",
                "call_id": tc.call_id,
                "name": tc.tool_name,
                "arguments": tc.arguments_json or "{}",
            }
        )
    return out


def tool_call_requests_to_anthropic_blocks(calls: Sequence[ToolCallRequest]) -> List[Dict[str, Any]]:
    """Render ToolCallRequest list as Anthropic ``tool_use`` / ``server_tool_use`` blocks."""
    out: List[Dict[str, Any]] = []
    for tc in calls:
        try:
            parsed = json.loads(tc.arguments_json) if tc.arguments_json else {}
            input_obj = parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, TypeError):
            input_obj = tc.arguments if isinstance(tc.arguments, dict) else {}
        block_type = "server_tool_use" if tc.kind == "provider" else "tool_use"
        out.append(
            {
                "type": block_type,
                "id": tc.call_id,
                "name": tc.tool_name,
                "input": input_obj,
            }
        )
    return out


def inbound_tool_call_request(
    *,
    call_id: str,
    tool_name: str,
    arguments_json: str,
    kind: Optional[str] = None,
    thought_signature: Optional[str] = None,
    tool_call_index: Optional[int] = None,
) -> ToolCallRequest:
    """Build a ToolCallRequest from a provider event, mapping wire names to canonical.

    This is the shared inbound helper (ADR-0137 Decision §1).
    """
    from motet.core.models.adapters.provider_builtin_tools import tool_wire_to_canonical

    name = tool_wire_to_canonical(tool_name or "")
    args = arguments_json if isinstance(arguments_json, str) else "{}"
    try:
        args_obj = json.loads(args) if args else None
        if not isinstance(args_obj, dict):
            args_obj = None
    except (json.JSONDecodeError, TypeError):
        args_obj = None
    return ToolCallRequest(
        call_id=str(call_id or "call_unknown"),
        tool_name=name,
        arguments_json=args,
        arguments=args_obj,
        kind=kind,
        thought_signature=thought_signature,
        tool_call_index=tool_call_index,
    )
