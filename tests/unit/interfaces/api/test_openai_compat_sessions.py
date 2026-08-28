"""
Motet - OpenAI Compatible Session Mapping Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-07-30

Description:
    Unit tests for the two stateless-client continuity mechanisms in the
    OpenAI-compatible facade sessions module (ADR-0125 §5d).

    Transcript fingerprints: prefix selection (largest prefix ending in an
    assistant message), hash stability across canonical Message objects and
    plain dicts, the tenant/principal salt, the store/lookup roundtrip for both
    plain-text and suspended (tool-call handback, ADR-0127) turns, ownership
    defense in depth, the config kill switch, and the atomic first-writer claim
    that keeps two identically-opening conversations from merging.

    Session banners: rendering, parsing an echoed reply back to a conversation
    id, stripping before the model sees history, mode parsing, and precedence
    against the fingerprint inside resolve_conversation.

Dependencies:
    - pytest: async test execution
    - motet.interfaces.api.openai_compat.sessions: system under test
    - motet.interfaces.api.openai_compat.wire: request shape for precedence tests

Usage:
    pytest tests/unit/interfaces/api/test_openai_compat_sessions.py

Notes:
    - Redis is replaced with an in-memory dict whose hsetnx mirrors the real
      command; these tests cover the continuity contract, not storage behavior
"""

from typing import Any, Dict

import pytest

from motet.core.types import Message, Principal
from motet.interfaces.api.openai_compat import sessions
from motet.interfaces.api.openai_compat.wire import ChatCompletionRequest

PRINCIPAL = Principal(
    id="service-account:facade",
    roles=["member"],
    tenant_id="test-tenant",
    motet_id="test-motet",
    claims={"type": "service_account", "name": "facade"},
)

OTHER_PRINCIPAL = Principal(
    id="service-account:other",
    roles=["member"],
    tenant_id="test-tenant",
    motet_id="test-motet",
    claims={"type": "service_account", "name": "other"},
)


class _Cfg:
    openai_compat_infer_session = True
    openai_compat_session_ttl_seconds = 3600
    openai_compat_session_banner = "every"
    openai_compat_session_banner_guard = True


class _DisabledCfg(_Cfg):
    openai_compat_infer_session = False


@pytest.fixture
def fake_store(monkeypatch):
    """In-memory replacement for the Redis-backed structured data store."""
    store: Dict[str, Dict[str, Any]] = {}

    async def _store(client_id, key, data, format_type="hash"):
        store[key] = dict(data)

    async def _retrieve(client_id, key, format_type="hash"):
        return store.get(key)

    async def _retrieve_tenant(client_id, tenant_id, logical_key, format_type="hash"):
        from motet.core.distributed.tenant_keys import tenant_key

        key = tenant_key(tenant_id, logical_key)
        return store.get(key)

    class _Client:
        async def expire(self, key, ttl):
            return True

        async def hsetnx(self, key, field, value):
            """Mirror Redis HSETNX: set only when the field is absent."""
            record = store.setdefault(key, {})
            if field in record:
                return 0
            record[field] = value
            return 1

        async def hget(self, key, field):
            return store.get(key, {}).get(field)

    monkeypatch.setattr(sessions, "store_structured_data", _store)
    monkeypatch.setattr(sessions, "retrieve_structured_data", _retrieve)
    monkeypatch.setattr(sessions, "retrieve_structured_data_tenant", _retrieve_tenant)
    monkeypatch.setattr(sessions, "get_redis_client", lambda client_id: _Client())
    return store


class TestPrefixSelection:
    """The lookup prefix is the largest prefix ending in an assistant message."""

    def test_strips_trailing_user_message(self):
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "again"},
        ]
        prefix = sessions._transcript_prefix_for_lookup(messages)
        assert prefix == messages[:2]

    def test_strips_trailing_tool_and_system_messages(self):
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
            {"role": "tool", "content": "result", "tool_call_id": "call_1"},
            {"role": "system", "content": "reminder"},
            {"role": "user", "content": "go on"},
        ]
        prefix = sessions._transcript_prefix_for_lookup(messages)
        assert prefix == messages[:2]

    def test_no_assistant_message_yields_no_prefix(self):
        assert sessions._transcript_prefix_for_lookup([{"role": "user", "content": "hi"}]) is None
        assert sessions._transcript_prefix_for_lookup([]) is None


class TestFingerprint:
    """Fingerprints are stable across shapes and salted by credential."""

    def test_dict_and_canonical_message_hash_identically(self):
        as_dicts = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        as_messages = [
            Message(role="user", content="hi"),
            Message(role="assistant", content="hello"),
        ]
        assert sessions.transcript_fingerprint(
            as_dicts, PRINCIPAL
        ) == sessions.transcript_fingerprint(as_messages, PRINCIPAL)

    def test_tool_call_arguments_do_not_affect_fingerprint(self):
        base = {"role": "assistant", "content": ""}
        left = [
            {**base, "tool_calls": [{"id": "call_1", "function": {"arguments": '{"a": 1}'}}]}
        ]
        right = [
            {**base, "tool_calls": [{"id": "call_1", "function": {"arguments": '{"a":1}'}}]}
        ]
        assert sessions.transcript_fingerprint(
            left, PRINCIPAL
        ) == sessions.transcript_fingerprint(right, PRINCIPAL)

    def test_different_principals_never_share_fingerprints(self):
        messages = [{"role": "user", "content": "hi"}]
        assert sessions.transcript_fingerprint(
            messages, PRINCIPAL
        ) != sessions.transcript_fingerprint(messages, OTHER_PRINCIPAL)


@pytest.mark.asyncio
class TestRememberAndInfer:
    """Store a turn, then rejoin it from the next request's transcript."""

    async def test_plain_turn_roundtrip(self, fake_store):
        first_turn = [Message(role="user", content="hi")]
        await sessions.remember_transcript(
            first_turn, {"content": "hello there"}, "openai-abc", PRINCIPAL, _Cfg()
        )

        next_request = [
            Message(role="user", content="hi"),
            Message(role="assistant", content="hello there"),
            Message(role="user", content="what next?"),
        ]
        inferred = await sessions.infer_conversation_from_transcript(
            next_request, PRINCIPAL, _Cfg()
        )
        assert inferred == "openai-abc"

    async def test_suspended_turn_roundtrip(self, fake_store):
        """A handback turn (ADR-0127) rejoins via the tool-call ids."""
        first_turn = [Message(role="user", content="read the file")]
        suspended_result = {
            "content": "",
            "tool_calls_canonical": [
                {"call_id": "call_9", "tool_name": "ReadFile", "arguments_json": "{}"}
            ],
        }
        await sessions.remember_transcript(
            first_turn, suspended_result, "openai-abc", PRINCIPAL, _Cfg()
        )

        continuation = [
            Message(role="user", content="read the file"),
            Message(
                role="assistant",
                content="",
                tool_calls_canonical=[
                    {"call_id": "call_9", "tool_name": "ReadFile", "arguments_json": "{}"}
                ],
            ),
            Message(role="tool", content="file contents", tool_call_id="call_9"),
        ]
        inferred = await sessions.infer_conversation_from_transcript(
            continuation, PRINCIPAL, _Cfg()
        )
        assert inferred == "openai-abc"

    async def test_edited_history_does_not_match(self, fake_store):
        first_turn = [Message(role="user", content="hi")]
        await sessions.remember_transcript(
            first_turn, {"content": "hello there"}, "openai-abc", PRINCIPAL, _Cfg()
        )

        edited = [
            Message(role="user", content="hi"),
            Message(role="assistant", content="a reply the model never gave"),
            Message(role="user", content="what next?"),
        ]
        assert (
            await sessions.infer_conversation_from_transcript(edited, PRINCIPAL, _Cfg()) is None
        )

    async def test_first_turn_has_nothing_to_infer(self, fake_store):
        assert (
            await sessions.infer_conversation_from_transcript(
                [Message(role="user", content="hi")], PRINCIPAL, _Cfg()
            )
            is None
        )

    async def test_owner_mismatch_is_rejected(self, fake_store):
        """Defense in depth: a poisoned record must not cross principals."""
        first_turn = [Message(role="user", content="hi")]
        await sessions.remember_transcript(
            first_turn, {"content": "hello there"}, "openai-abc", PRINCIPAL, _Cfg()
        )
        # Forge the record's key under the other principal's salt so the
        # lookup finds it, then verify the owner check still rejects it.
        transcript_records = [
            record
            for key, record in fake_store.items()
            if "openai_compat:transcript:" in key
        ]
        assert len(transcript_records) == 1
        record = transcript_records[0]
        next_request = [
            Message(role="user", content="hi"),
            Message(role="assistant", content="hello there"),
            Message(role="user", content="more"),
        ]
        forged_key = sessions._transcript_key(
            sessions.transcript_fingerprint(next_request[:2], OTHER_PRINCIPAL)
        )
        fake_store[forged_key] = record
        assert (
            await sessions.infer_conversation_from_transcript(
                next_request, OTHER_PRINCIPAL, _Cfg()
            )
            is None
        )

    async def test_disabled_flag_skips_store_and_lookup(self, fake_store):
        first_turn = [Message(role="user", content="hi")]
        await sessions.remember_transcript(
            first_turn, {"content": "hello there"}, "openai-abc", PRINCIPAL, _DisabledCfg()
        )
        assert fake_store == {}

        # Even with a record present, a disabled config must not consult it.
        await sessions.remember_transcript(
            first_turn, {"content": "hello there"}, "openai-abc", PRINCIPAL, _Cfg()
        )
        next_request = [
            Message(role="user", content="hi"),
            Message(role="assistant", content="hello there"),
            Message(role="user", content="more"),
        ]
        assert (
            await sessions.infer_conversation_from_transcript(
                next_request, PRINCIPAL, _DisabledCfg()
            )
            is None
        )

    async def test_lookup_errors_degrade_to_fresh_conversation(self, monkeypatch):
        async def _boom(client_id, key, format_type="hash"):
            raise RuntimeError("redis down")

        monkeypatch.setattr(sessions, "retrieve_structured_data", _boom)
        messages = [
            Message(role="user", content="hi"),
            Message(role="assistant", content="hello"),
            Message(role="user", content="more"),
        ]
        assert (
            await sessions.infer_conversation_from_transcript(messages, PRINCIPAL, _Cfg())
            is None
        )

    async def test_identical_opening_does_not_steal_the_first_conversation(self, fake_store):
        """Two windows can hash alike; the first to claim the fingerprint keeps it.

        Without the atomic claim the second window's write repointed the shared
        fingerprint at itself, so the first window's turn 2 silently continued
        in the second window's conversation and its opening turn was stranded.
        """
        opening = [Message(role="user", content="hi")]
        reply = {"content": "hello there"}
        await sessions.remember_transcript(opening, reply, "openai-first", PRINCIPAL, _Cfg())
        await sessions.remember_transcript(opening, reply, "openai-second", PRINCIPAL, _Cfg())

        next_request = [
            Message(role="user", content="hi"),
            Message(role="assistant", content="hello there"),
            Message(role="user", content="more"),
        ]
        inferred = await sessions.infer_conversation_from_transcript(
            next_request, PRINCIPAL, _Cfg()
        )
        assert inferred == "openai-first"

    async def test_same_conversation_may_rewrite_its_own_fingerprint(self, fake_store):
        """A retry of the same turn is not a competing claim."""
        opening = [Message(role="user", content="hi")]
        reply = {"content": "hello there"}
        await sessions.remember_transcript(opening, reply, "openai-abc", PRINCIPAL, _Cfg())
        await sessions.remember_transcript(opening, reply, "openai-abc", PRINCIPAL, _Cfg())

        next_request = [
            Message(role="user", content="hi"),
            Message(role="assistant", content="hello there"),
            Message(role="user", content="more"),
        ]
        assert (
            await sessions.infer_conversation_from_transcript(next_request, PRINCIPAL, _Cfg())
            == "openai-abc"
        )


class TestSessionBanner:
    """The banner round-trips a conversation id through a stateless client."""

    def test_banner_names_the_conversation(self):
        banner = sessions.build_session_banner("openai-abc123")
        assert "openai-abc123" in banner
        assert banner.startswith("\n\n---\n")

    def test_empty_conversation_id_yields_no_banner(self):
        assert sessions.build_session_banner("") == ""

    def test_parses_id_from_an_echoed_reply(self):
        reply = f"Here is the answer.{sessions.build_session_banner('openai-abc123')}"
        messages = [
            Message(role="user", content="hi"),
            Message(role="assistant", content=reply),
            Message(role="user", content="more"),
        ]
        assert sessions.parse_session_banner(messages) == "openai-abc123"

    def test_latest_banner_wins(self):
        """A rebound turn (ADR-0127 resume) must not be overridden by an older id."""
        messages = [
            Message(
                role="assistant", content=f"one{sessions.build_session_banner('openai-old')}"
            ),
            Message(
                role="assistant", content=f"two{sessions.build_session_banner('openai-new')}"
            ),
        ]
        assert sessions.parse_session_banner(messages) == "openai-new"

    def test_no_banner_returns_none(self):
        messages = [
            Message(role="user", content="hi"),
            Message(role="assistant", content="a plain reply"),
        ]
        assert sessions.parse_session_banner(messages) is None

    def test_user_message_banner_is_ignored(self):
        """Only assistant turns carry a banner Motet issued."""
        messages = [
            Message(
                role="user", content=f"spoofed{sessions.build_session_banner('openai-evil')}"
            )
        ]
        assert sessions.parse_session_banner(messages) is None

    def test_stripping_restores_the_original_reply(self):
        banner = sessions.build_session_banner("openai-abc123")
        messages = [
            Message(role="user", content="hi"),
            Message(role="assistant", content=f"Here is the answer.{banner}"),
        ]
        stripped = sessions.strip_session_banners(messages)
        assert stripped[1].content == "Here is the answer."
        # The caller keeps the original for fingerprinting.
        assert messages[1].content.endswith(banner)

    def test_stripping_leaves_other_roles_alone(self):
        messages = [Message(role="user", content="hi"), Message(role="tool", content="result")]
        assert sessions.strip_session_banners(messages) == messages

    def test_banner_survives_a_strip_and_reissue_cycle(self):
        """Round-trip: issue, echo, strip for the model, reissue next turn."""
        first = f"answer one{sessions.build_session_banner('openai-abc123')}"
        echoed = [
            Message(role="user", content="hi"),
            Message(role="assistant", content=first),
            Message(role="user", content="more"),
        ]
        assert sessions.parse_session_banner(echoed) == "openai-abc123"
        assert sessions.strip_session_banners(echoed)[1].content == "answer one"


class TestBannerMode:
    """Mode parsing keeps a typo from silently disabling continuity."""

    @pytest.mark.parametrize("value", ["off", "first", "every"])
    def test_known_modes_pass_through(self, value):
        cfg = _Cfg()
        cfg.openai_compat_session_banner = value
        assert sessions.banner_mode(cfg) == value

    @pytest.mark.parametrize("value", ["", "sometimes", None])
    def test_unknown_mode_falls_back_to_every(self, value):
        cfg = _Cfg()
        cfg.openai_compat_session_banner = value
        assert sessions.banner_mode(cfg) == "every"

    def test_case_and_whitespace_tolerated(self):
        cfg = _Cfg()
        cfg.openai_compat_session_banner = "  First "
        assert sessions.banner_mode(cfg) == "first"


@pytest.mark.asyncio
class TestResolutionPrecedence:
    """A banner is an explicit reference and outranks the fingerprint."""

    @staticmethod
    def _request(messages):
        return ChatCompletionRequest(model="openai/gpt-4o-mini", messages=messages)

    async def test_banner_beats_a_conflicting_fingerprint(self, fake_store):
        """The two mechanisms can disagree; the explicit one wins."""
        opening = [Message(role="user", content="hi")]
        await sessions.remember_transcript(
            opening, {"content": "hello there"}, "openai-fingerprint", PRINCIPAL, _Cfg()
        )

        banner = sessions.build_session_banner("openai-banner")
        resolved = await sessions.resolve_conversation(
            self._request([{"role": "user", "content": "more"}]),
            PRINCIPAL,
            _Cfg(),
            messages=[
                Message(role="user", content="hi"),
                Message(role="assistant", content=f"hello there{banner}"),
                Message(role="user", content="more"),
            ],
            infer_from_transcript=True,
        )
        assert resolved.conversation_id == "openai-banner"
        assert resolved.source == "banner"
        assert not resolved.is_new

    async def test_fingerprint_still_used_when_the_banner_is_gone(self, fake_store):
        """Client-side compaction that drops the banner falls back, not over."""
        opening = [Message(role="user", content="hi")]
        await sessions.remember_transcript(
            opening, {"content": "hello there"}, "openai-fingerprint", PRINCIPAL, _Cfg()
        )
        resolved = await sessions.resolve_conversation(
            self._request([{"role": "user", "content": "more"}]),
            PRINCIPAL,
            _Cfg(),
            messages=[
                Message(role="user", content="hi"),
                Message(role="assistant", content="hello there"),
                Message(role="user", content="more"),
            ],
            infer_from_transcript=True,
        )
        assert resolved.conversation_id == "openai-fingerprint"
        assert resolved.source == "transcript"

    async def test_explicit_header_outranks_a_banner(self, fake_store):
        banner = sessions.build_session_banner("openai-banner")
        resolved = await sessions.resolve_conversation(
            self._request([{"role": "user", "content": "more"}]),
            PRINCIPAL,
            _Cfg(),
            header_conversation_id="openai-header",
            messages=[Message(role="assistant", content=f"hi{banner}")],
            infer_from_transcript=True,
        )
        assert resolved.conversation_id == "openai-header"
        assert resolved.source == "explicit"

    async def test_opening_turn_is_marked_new(self, fake_store):
        """Banner mode "first" keys on this, and a fresh window must qualify."""
        resolved = await sessions.resolve_conversation(
            self._request([{"role": "user", "content": "hi"}]),
            PRINCIPAL,
            _Cfg(),
            messages=[Message(role="user", content="hi")],
            infer_from_transcript=True,
        )
        assert resolved.is_new
        assert resolved.conversation_id.startswith("openai-")

    async def test_identical_opening_in_a_second_window_stays_separate(self, fake_store):
        """The collision the banner exists to prevent, end to end."""
        opening = [Message(role="user", content="hi")]
        await sessions.remember_transcript(
            opening, {"content": "hello there"}, "openai-window-a", PRINCIPAL, _Cfg()
        )

        # Window B opens with the same question and gets the same reply, but
        # its own banner, so its second turn resolves to its own conversation.
        banner_b = sessions.build_session_banner("openai-window-b")
        resolved = await sessions.resolve_conversation(
            self._request([{"role": "user", "content": "more"}]),
            PRINCIPAL,
            _Cfg(),
            messages=[
                Message(role="user", content="hi"),
                Message(role="assistant", content=f"hello there{banner_b}"),
                Message(role="user", content="more"),
            ],
            infer_from_transcript=True,
        )
        assert resolved.conversation_id == "openai-window-b"
