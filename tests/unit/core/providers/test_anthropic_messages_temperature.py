"""
Motet - Anthropic Messages Adapter Temperature Gating Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-03

Description:
    Unit tests for the Anthropic adapter's temperature parameter gating.

    Newer Claude families (adaptive-thinking models: opus/sonnet 4.6+, mythos)
    reject any request containing `temperature` with a 400
    ("`temperature` is deprecated for this model.") — even when thinking is
    disabled. The adapter must omit `temperature` from requests to those models
    while continuing to send it for older models.

    Validates:
    - `_anthropic_model_supports_temperature` classification per model family
    - `complete()` omits/includes `temperature` in `client.messages.create(**params)`
    - `stream()` omits/includes `temperature` in `client.messages.stream(**params)`

Dependencies:
    - pytest
    - motet.core.models.adapters.providers.anthropic_messages: implementation under test

Usage:
    pytest tests/unit/core/providers/test_anthropic_messages_temperature.py

Notes:
    - No network calls: a stub ``anthropic`` module is injected into ``sys.modules``
      so these tests run in the lightweight Docker test-runner (which does not
      install provider SDKs) as well as local environments with the real SDK.
"""

from __future__ import annotations

import sys
import types
from typing import Any, Dict, List

import pytest


class _FakeStream:
    """Minimal stand-in for the Anthropic SDK streaming context manager."""

    def __enter__(self) -> "_FakeStream":
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None

    @property
    def text_stream(self) -> List[str]:
        return ["Hello"]

    def get_final_message(self) -> Dict[str, Any]:
        return {
            "content": [{"type": "text", "text": "Hello"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }


class _FakeMessages:
    def __init__(self, captured: Dict[str, Any]):
        self._captured = captured

    def create(self, **params: Any) -> Dict[str, Any]:
        self._captured["create"] = params
        return {
            "content": [{"type": "text", "text": "Hello"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    def stream(self, **params: Any) -> _FakeStream:
        self._captured["stream"] = params
        return _FakeStream()


class _FakeAnthropic:
    """Captures request params instead of calling the Anthropic API."""

    captured: Dict[str, Any] = {}

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.messages = _FakeMessages(self.__class__.captured)


def _install_fake_anthropic() -> None:
    """
    Inject a stub ``anthropic`` module so adapter lazy imports resolve without
    the real SDK (absent from the lightweight Docker test-runner image).
    """
    mod = types.ModuleType("anthropic")
    mod.Anthropic = _FakeAnthropic  # type: ignore[attr-defined]
    sys.modules["anthropic"] = mod


def _run_complete(model_name: str, model_settings: Dict[str, Any]) -> Dict[str, Any]:
    """Run adapter.complete() against the fake SDK; return captured create params."""
    from motet.core.models.adapters.providers.anthropic_messages import AnthropicMessagesAdapter
    from motet.core.types import LLMRequest, Message

    _FakeAnthropic.captured = {}
    _install_fake_anthropic()
    adapter = AnthropicMessagesAdapter(
        provider="anthropic",
        adapter_name="messages",
        credentials={"anthropic_api_key": "test-key"},
    )
    # Anthropic Messages requires max_tokens; supply a fixture default so these
    # temperature-gating tests do not depend on ModelSpec registry contents.
    settings = {"max_tokens": 256, **model_settings}
    request = LLMRequest(
        messages=[Message(role="user", content="hi")],
        model_settings={"model_name": model_name, **settings},
    )
    adapter.complete(request)
    return _FakeAnthropic.captured["create"]


def _run_stream(model_name: str, model_settings: Dict[str, Any]) -> Dict[str, Any]:
    """Run adapter.stream() against the fake SDK; return captured stream params."""
    from motet.core.models.adapters.providers.anthropic_messages import AnthropicMessagesAdapter
    from motet.core.types import LLMRequest, Message

    _FakeAnthropic.captured = {}
    _install_fake_anthropic()
    adapter = AnthropicMessagesAdapter(
        provider="anthropic",
        adapter_name="messages",
        credentials={"anthropic_api_key": "test-key"},
    )
    settings = {"max_tokens": 256, **model_settings}
    request = LLMRequest(
        messages=[Message(role="user", content="hi")],
        model_settings={"model_name": model_name, **settings},
    )
    list(adapter.stream(request))
    return _FakeAnthropic.captured["stream"]


@pytest.mark.parametrize(
    "model_name,expected",
    [
        ("claude-3-5-sonnet-latest", True),
        ("claude-sonnet-4-5-20250929", True),
        ("claude-haiku-4-5-20251001", True),  # 4.5 snapshot: date suffix not a version
        ("claude-opus-4-6", False),
        ("claude-opus-4-7", False),
        ("claude-opus-4-8", False),  # version-aware: new releases classify without markers
        ("claude-opus-5", False),  # 5-series adaptive; temperature deprecated
        ("claude-sonnet-4-6", False),
        ("claude-sonnet-4.7", False),  # dot-normalized
        ("claude-sonnet-5", False),  # 5-series adaptive
        ("claude-fable-5", False),
        ("claude-mythos-1", False),
    ],
)
def test_anthropic_model_supports_temperature(model_name: str, expected: bool) -> None:
    from motet.core.models.adapters.providers.anthropic_messages import (
        _anthropic_model_supports_temperature,
    )

    assert _anthropic_model_supports_temperature(model_name) is expected


def test_complete_omits_temperature_for_deprecated_model() -> None:
    """
    Goal: complete() must not send `temperature` to models that deprecated it
    Boundary: param construction only; SDK faked
    Success Criteria: no `temperature` key even when the caller sets one
    """
    params = _run_complete("claude-opus-4-7", {"temperature": 0.2})
    assert "temperature" not in params
    assert params["model"] == "claude-opus-4-7"


def test_complete_sends_temperature_for_supported_model() -> None:
    params = _run_complete("claude-3-5-sonnet-latest", {"temperature": 0.7})
    assert params["temperature"] == 0.7


def test_complete_sends_default_temperature_when_unset_for_supported_model() -> None:
    params = _run_complete("claude-3-5-sonnet-latest", {})
    assert params["temperature"] == 0.2


def test_stream_omits_temperature_for_deprecated_model() -> None:
    """
    Goal: stream() must not send `temperature` to models that deprecated it
    Boundary: param construction only; SDK faked (regression for the no-tools
    path that first exercised a non-thinking request against claude-opus-4-7)
    Success Criteria: no `temperature` key even when the caller sets one
    """
    params = _run_stream("claude-opus-4-7", {"temperature": 0.2})
    assert "temperature" not in params
    assert params["model"] == "claude-opus-4-7"


def test_stream_sends_temperature_for_supported_model() -> None:
    params = _run_stream("claude-3-5-sonnet-latest", {"temperature": 0.0})
    # temperature=0.0 is a valid setting and must not be treated as unset
    assert params["temperature"] == 0.0
