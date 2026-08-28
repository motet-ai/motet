"""
Motet - Chat Completions Tool-Call Delta Assembler

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Shared assembly of OpenAI-shaped ``delta.tool_calls`` fragments into
    canonical ``ToolCallDeltaEvent`` / ``ToolCallCompleteEvent``. Used by the
    OpenAI, DeepSeek, and Moonshot Chat Completions adapters so a buffering
    bug cannot silently drop progress on one family member.

Dependencies:
    - motet.core.types: ToolCallDeltaEvent, ToolCallCompleteEvent
    - motet.core.models.adapters.tool_call_codec: inbound_tool_call_request

Usage:
    assembler = ChatCompletionsToolCallAssembler()
    for delta_tc in delta.tool_calls:
        ev = assembler.apply_delta(delta_tc)
        if ev is not None:
            yield ev
    for complete in assembler.complete():
        yield complete
    # Non-stream complete():
    requests = assembler.ingest_complete_calls(message.tool_calls)

Notes:
    - ``tool_call_complete`` remains the only input to the executed call.
    - ``tool_call_delta`` is progress: a whole-file argument body can take
      minutes.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional, Sequence

from motet.core.models.adapters.tool_call_codec import inbound_tool_call_request
from motet.core.types import ToolCallCompleteEvent, ToolCallDeltaEvent, ToolCallRequest


def _kind_from_slot(slot: Dict[str, str]) -> Optional[str]:
    return "provider" if str(slot.get("type") or "") == "builtin_function" else None


def _normalize_complete_tool_call(tc: Any, index: int) -> Any:
    """Give a finished Chat Completions tool_call a stable index for the assembler."""
    if isinstance(tc, dict):
        fn_raw = tc.get("function")
        fn = fn_raw if isinstance(fn_raw, dict) else {}
        raw_index = tc.get("index")
        return SimpleNamespace(
            index=int(raw_index if raw_index is not None else index),
            id=tc.get("id"),
            type=tc.get("type") or "function",
            function=SimpleNamespace(name=fn.get("name"), arguments=fn.get("arguments")),
        )
    raw_index = getattr(tc, "index", None)
    return SimpleNamespace(
        index=int(raw_index if raw_index is not None else index),
        id=getattr(tc, "id", None),
        type=getattr(tc, "type", None) or "function",
        function=getattr(tc, "function", None),
    )


def apply_chat_completions_tool_call_delta(
    accumulator: Dict[int, Dict[str, str]],
    tool_call_delta: Any,
) -> Optional[ToolCallDeltaEvent]:
    """Accumulate one Chat Completions ``delta.tool_calls`` fragment.

    Returns a ``ToolCallDeltaEvent`` when the fragment has a usable call_id
    (after this or a prior fragment supplied ``id``). Fragments that only
    extend arguments before ``id`` arrives still accumulate; they emit once
    ``id`` is known.
    """
    idx = int(getattr(tool_call_delta, "index", 0) or 0)
    slot = accumulator.setdefault(
        idx,
        {"id": "", "name": "", "arguments": "", "type": "function"},
    )
    delta_id = getattr(tool_call_delta, "id", None)
    if delta_id:
        slot["id"] = str(delta_id)
    delta_type = getattr(tool_call_delta, "type", None)
    if delta_type:
        slot["type"] = str(delta_type)
    func = getattr(tool_call_delta, "function", None)
    arguments_delta: Optional[str] = None
    if func is not None:
        name = getattr(func, "name", None)
        if name:
            slot["name"] = slot["name"] + str(name)
        arguments = getattr(func, "arguments", None)
        if arguments:
            arguments_delta = str(arguments)
            slot["arguments"] = slot["arguments"] + arguments_delta
    call_id = slot["id"]
    if not call_id:
        return None
    canonical_name = None
    if slot["name"]:
        canonical_name = inbound_tool_call_request(
            call_id=call_id,
            tool_name=slot["name"],
            arguments_json="{}",
        ).tool_name
    return ToolCallDeltaEvent(
        call_id=call_id,
        tool_name=canonical_name,
        arguments_delta=arguments_delta,
    )


class ChatCompletionsToolCallAssembler:
    """Accumulator used by OpenAI / DeepSeek / Moonshot Chat Completions adapters.

    Adapters stay separate classes (ADR-0137: do not merge the nine adapters).
    This object is the shared ``delta.tool_calls`` buffer.
    """

    def __init__(self) -> None:
        self.accumulator: Dict[int, Dict[str, str]] = {}

    def apply_delta(self, tool_call_delta: Any) -> Optional[ToolCallDeltaEvent]:
        return apply_chat_completions_tool_call_delta(self.accumulator, tool_call_delta)

    def ingest_complete_calls(self, tool_calls: Sequence[Any]) -> List[ToolCallRequest]:
        """Fold finished ``message.tool_calls`` into the same buffer as stream deltas."""
        for i, tc in enumerate(tool_calls or []):
            self.apply_delta(_normalize_complete_tool_call(tc, i))
        return self.requests()

    def complete(self) -> Iterator[ToolCallCompleteEvent]:
        yield from complete_chat_completions_tool_calls(self.accumulator)

    def requests(self) -> List[ToolCallRequest]:
        return completed_tool_call_requests(self.accumulator)


def complete_chat_completions_tool_calls(
    accumulator: Dict[int, Dict[str, str]],
) -> Iterator[ToolCallCompleteEvent]:
    """Yield canonical complete events for every accumulated Chat Completions call."""
    for idx in sorted(accumulator.keys()):
        slot = accumulator[idx]
        call_id = str(slot.get("id") or "")
        name = str(slot.get("name") or "")
        args = str(slot.get("arguments") or "")
        if not call_id or not name:
            continue
        req = inbound_tool_call_request(
            call_id=call_id,
            tool_name=name,
            arguments_json=args,
            kind=_kind_from_slot(slot),
        )
        yield ToolCallCompleteEvent(
            call_id=req.call_id,
            tool_name=req.tool_name,
            arguments_json=req.arguments_json,
            kind=req.kind,
        )


def completed_tool_call_requests(
    accumulator: Dict[int, Dict[str, str]],
) -> List[ToolCallRequest]:
    """Materialize completed calls as ToolCallRequest (non-stream complete path)."""
    out: List[ToolCallRequest] = []
    for idx in sorted(accumulator.keys()):
        slot = accumulator[idx]
        call_id = str(slot.get("id") or "")
        name = str(slot.get("name") or "")
        args = str(slot.get("arguments") or "")
        if call_id and name:
            out.append(
                inbound_tool_call_request(
                    call_id=call_id,
                    tool_name=name,
                    arguments_json=args,
                    kind=_kind_from_slot(slot),
                )
            )
    return out
