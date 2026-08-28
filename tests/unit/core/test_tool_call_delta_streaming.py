"""
Motet - Tool Call Delta Streaming Tests

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-17

Description:
    Covers the path that carries tool-call argument fragments from a provider
    adapter to a streaming client (ADR-0064 canonical events, ADR-0125 §5f SSE
    bodies). Generating the arguments for one tool call can take minutes when
    the arguments are a whole file, and before this path existed a client saw
    nothing at all for that window and timed the request out.

    Four claims are checked here:
    - the ChatCompletions adapters emit ToolCallDeltaEvent (inline or via
      ``chat_completions_deltas.apply_chat_completions_tool_call_delta``)
    - the Responses adapter recovers a fragment's call id and tool name from the
      output item, since the argument events themselves carry neither
    - model_stream republishes those fragments onto the task stream
    - fragments never replace the completed call as the source of truth

Dependencies:
    - motet.core.types: canonical stream event models
    - motet.core.commands.builtin.model: task-stream publication

Usage:
    pytest tests/unit/core/test_tool_call_delta_streaming.py

Notes:
    - ChatCompletions adapters share ``chat_completions_deltas`` (ADR-0137);
      the source assertion accepts that helper or an inline yield.
"""

from pathlib import Path
from unittest.mock import Mock

import pytest

from motet.core.commands.builtin.model import StreamEventResult, _handle_stream_event
from motet.core.types import ToolCallCompleteEvent, ToolCallDeltaEvent

ADAPTER_DIR = Path(__file__).resolve().parents[3] / "motet/core/models/adapters/providers"
CHAT_COMPLETIONS_ADAPTERS = (
    "moonshot_chat_completions.py",
    "openai_chat_completions.py",
    "deepseek_chat_completions.py",
)


@pytest.mark.parametrize("filename", CHAT_COMPLETIONS_ADAPTERS)
def test_chat_completions_adapters_emit_argument_fragments(filename: str) -> None:
    """Accumulating a fragment silently leaves a long tool call invisible."""
    source = (ADAPTER_DIR / filename).read_text()
    uses_shared = (
        "apply_chat_completions_tool_call_delta" in source
        or "ChatCompletionsToolCallAssembler" in source
    )
    emits_inline = "yield ToolCallDeltaEvent(" in source
    assert uses_shared or emits_inline, (
        f"{filename} accumulates tool call arguments without emitting them"
    )


class TestResponsesFragmentIdentity:
    """The Responses wire splits a call's identity from its argument fragments.

    `response.function_call_arguments.delta` carries only `item_id` and the text —
    `call_id` is absent and `name` is null (verified against the live API). The
    identity arrives once, on the `output_item` events, so a fragment is only
    usable if that mapping is kept.
    """

    def test_identity_comes_from_the_item_map(self) -> None:
        from motet.core.models.adapters.providers.openai_responses import (
            _resolve_tool_call_identity,
        )

        call_by_item = {"fc_abc": ("call_xyz", "Write")}
        raw = {"item_id": "fc_abc", "delta": '{"path":', "name": None}

        call_id, name = _resolve_tool_call_identity(Mock(spec=[]), raw, call_by_item)

        assert call_id == "call_xyz"
        assert name == "Write"

    def test_unknown_item_falls_back_to_the_item_id(self) -> None:
        """A fragment is still attributable when the item event was missed."""
        from motet.core.models.adapters.providers.openai_responses import (
            _resolve_tool_call_identity,
        )

        call_id, name = _resolve_tool_call_identity(
            Mock(spec=[]), {"item_id": "fc_abc"}, {}
        )

        assert call_id == "fc_abc"
        assert name == ""

    def test_event_fields_win_when_the_sdk_supplies_them(self) -> None:
        from motet.core.models.adapters.providers.openai_responses import (
            _resolve_tool_call_identity,
        )

        call_id, name = _resolve_tool_call_identity(
            Mock(spec=[]),
            {"item_id": "fc_abc", "call_id": "call_direct", "name": "Read"},
            {"fc_abc": ("call_xyz", "Write")},
        )

        assert (call_id, name) == ("call_direct", "Read")

    def test_adapter_records_the_item_mapping(self) -> None:
        """Guard the recording site: without it every fragment is unusable."""
        source = (ADAPTER_DIR / "openai_responses.py").read_text()
        assert "response.output_item.added" in source
        assert "call_by_item[item_id]" in source


def test_fragment_is_published_to_the_task_stream() -> None:
    motet = Mock()
    state = _handle_stream_event(
        ev=ToolCallDeltaEvent(
            call_id="Write_1", tool_name="Write", arguments_delta='{"path":'
        ),
        motet=motet,
        usage_data={},
        state=StreamEventResult(),
        allow_citations=True,
        error_label="Moonshot",
        stream_key="task:t1:response",
    )

    motet.stream_event.assert_called_once_with(
        "tool_call_delta",
        call_id="Write_1",
        tool_name="Write",
        arguments_delta='{"path":',
        stream_key="task:t1:response",
    )
    # Progress only: fragments must not become the call the loop executes.
    assert state.tool_calls_canonical is None


def test_completed_call_remains_the_source_of_truth() -> None:
    motet = Mock()
    state = StreamEventResult()
    for fragment in ('{"path":', '"a.py"}'):
        state = _handle_stream_event(
            ev=ToolCallDeltaEvent(
                call_id="Write_1", tool_name="Write", arguments_delta=fragment
            ),
            motet=motet,
            usage_data={},
            state=state,
            allow_citations=True,
            error_label="Moonshot",
            stream_key=None,
        )
    state = _handle_stream_event(
        ev=ToolCallCompleteEvent(
            call_id="Write_1", tool_name="Write", arguments_json='{"path": "a.py"}'
        ),
        motet=motet,
        usage_data={},
        state=state,
        allow_citations=True,
        error_label="Moonshot",
        stream_key=None,
    )

    assert state.tool_calls_canonical is not None
    assert len(state.tool_calls_canonical) == 1
    call = state.tool_calls_canonical[0]
    assert call["arguments"] == {"path": "a.py"}
    assert call["arguments_json"] == '{"path": "a.py"}'
