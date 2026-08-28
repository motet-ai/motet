"""
Motet - Transcript Replay Merge Unit Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-07-28

Description:
    Unit tests for merge_conversation_history / _make_message_key (issue #138).
    Covers client-echoed assistant dedupe when inbound agent_id is missing,
    multi-agent distinction when both sides carry agent_id, and unchanged
    user/tool dedupe behavior. Also covers the OpenAI-compatible facade echo
    path (ADR-0125 §5d), where the client resends prior assistant turns without
    agent_id and must not duplicate against the stored transcript.

Dependencies:
    - pytest: test framework
    - motet.core.conversations.transcript_replay: merge under test
    - motet.core.types.Message: canonical message model
    - motet.interfaces.api.openai_compat: facade translation for end-to-end echo shape

Usage:
    pytest tests/unit/core/test_transcript_replay_merge.py -q

Notes:
    - Missing inbound agent_id is a wildcard against stored provenance.
    - Each inbound key is consumed once so identical-content multi-agent
      turns are not over-collapsed when the client echoes only one copy.
    - Pure function tests; no Redis, memory backend, or worker required.
"""

from __future__ import annotations

from motet.core.conversations.transcript_replay import merge_conversation_history
from motet.core.types import Message


def test_user_turns_dedupe_without_agent_id() -> None:
    stored = [Message(role="user", content="hi")]
    client = [
        Message(role="user", content="hi"),
        Message(role="user", content="follow up"),
    ]
    merged = merge_conversation_history(client, stored)
    assert [m.content for m in merged if m.role == "user"] == ["hi", "follow up"]


def test_tool_turns_dedupe_by_tool_call_id() -> None:
    stored = [
        Message(role="tool", content="ok", tool_call_id="call_1", name="search"),
    ]
    client = [
        Message(role="tool", content="ok", tool_call_id="call_1", name="search"),
        Message(role="user", content="thanks"),
    ]
    merged = merge_conversation_history(client, stored)
    tool_msgs = [m for m in merged if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].tool_call_id == "call_1"


def test_assistant_dedupes_when_agent_id_matches() -> None:
    stored = [Message(role="assistant", content="hello", agent_id="core.default")]
    client = [
        Message(role="assistant", content="hello", agent_id="core.default"),
        Message(role="user", content="next"),
    ]
    merged = merge_conversation_history(client, stored)
    assistants = [m for m in merged if m.role == "assistant" and m.content == "hello"]
    assert len(assistants) == 1
    assert assistants[0].agent_id == "core.default"


def test_client_echo_without_agent_id_dedupes_against_stored() -> None:
    """Issue #138: stored has agent_id, client echo does not — must collapse to one."""
    stored = [
        Message(role="user", content="hi"),
        Message(role="assistant", content="hello", agent_id="core.default"),
    ]
    client = [
        Message(role="user", content="hi"),
        Message(role="assistant", content="hello"),  # no agent_id
        Message(role="user", content="how are you?"),
    ]
    merged = merge_conversation_history(client, stored)
    assert [m.content for m in merged if m.role == "assistant"] == ["hello"]
    assert [m.content for m in merged if m.role == "user"] == ["hi", "how are you?"]


def test_multi_agent_assistants_with_distinct_agent_ids_both_kept() -> None:
    """When both sides carry agent_id, distinct sub-agents remain distinct."""
    stored = [
        Message(role="user", content="q"),
        Message(role="assistant", content="take A", agent_id="expert-panel.a"),
        Message(role="assistant", content="take B", agent_id="expert-panel.b"),
        Message(role="assistant", content="synthesis", agent_id="core.default"),
    ]
    client = [
        Message(role="user", content="q"),
        Message(role="assistant", content="take A", agent_id="expert-panel.a"),
        Message(role="assistant", content="take B", agent_id="expert-panel.b"),
        Message(role="assistant", content="synthesis", agent_id="core.default"),
        Message(role="user", content="next"),
    ]
    merged = merge_conversation_history(client, stored)
    assistants = [(m.content, m.agent_id) for m in merged if m.role == "assistant"]
    assert assistants == [
        ("take A", "expert-panel.a"),
        ("take B", "expert-panel.b"),
        ("synthesis", "core.default"),
    ]


def test_same_content_multi_agent_not_over_collapsed_by_one_wildcard() -> None:
    """One client echo without agent_id absorbs only one stored same-content turn."""
    stored = [
        Message(role="assistant", content="same text", agent_id="expert-panel.a"),
        Message(role="assistant", content="same text", agent_id="expert-panel.b"),
    ]
    client = [
        Message(role="assistant", content="same text"),  # no agent_id
        Message(role="user", content="next"),
    ]
    merged = merge_conversation_history(client, stored)
    same = [m for m in merged if m.content == "same text"]
    # First stored copy matches the echo; the second is prepended (not over-collapsed).
    assert len(same) == 2
    assert [m.agent_id for m in same] == ["expert-panel.b", None]


def test_empty_string_agent_id_treated_as_missing() -> None:
    stored = [Message(role="assistant", content="hello", agent_id="core.default")]
    client = [Message(role="assistant", content="hello", agent_id="")]
    merged = merge_conversation_history(client, stored)
    assert len([m for m in merged if m.role == "assistant"]) == 1


def test_new_user_only_caller_keeps_full_history_prepended() -> None:
    """Callers that send only the new user turn still get full transcript prepended."""
    stored = [
        Message(role="user", content="hi"),
        Message(role="assistant", content="hello", agent_id="core.default"),
    ]
    client = [Message(role="user", content="how are you?")]
    merged = merge_conversation_history(client, stored)
    assert [(m.role, m.content) for m in merged] == [
        ("user", "hi"),
        ("assistant", "hello"),
        ("user", "how are you?"),
    ]


def test_openai_facade_echo_dedupes_assistant_turn() -> None:
    """End-to-end: facade translation + merge collapses echoed assistant (issue #138).

    Mirrors agent mode over the OpenAI facade with a resolved conversation_id
    (ADR-0125 §5d): the client resends the full conversation without agent_id;
    prepare_context replays the stored transcript and must not duplicate.
    """
    from motet.interfaces.api.openai_compat.translation import messages_to_canonical
    from motet.interfaces.api.openai_compat.wire import ChatCompletionRequest

    request = ChatCompletionRequest(
        model="openai/gpt-4o-mini",
        messages=[
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello back"},
            {"role": "user", "content": "and now?"},
        ],
    )
    caller = messages_to_canonical(request)

    echoed_assistant = [m for m in caller if m.role == "assistant"]
    assert echoed_assistant and echoed_assistant[0].agent_id is None

    stored = [
        Message(role="user", content="hi"),
        Message(role="assistant", content="hello back", agent_id="core.default"),
    ]

    merged = merge_conversation_history(caller, stored)

    assistant_turns = [m for m in merged if m.role == "assistant"]
    assert len(assistant_turns) == 1
    assert len([m for m in merged if m.role == "user" and m.content == "hi"]) == 1
