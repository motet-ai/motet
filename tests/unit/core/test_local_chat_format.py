"""
Motet - Local Chat-Format & Stop-Token Tests (ADR-0114)

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-10

Description:
    Regression tests for the local-inference runaway-generation fix. A live Gemma 3
    turn ran ~100s and emitted ~4000 tokens of a fabricated "System:/User:/Assistant:"
    transcript because:

      1. The GGUF was loaded without a family-correct ``chat_format``, so llama.cpp
         fell back to a Llama-2 template and the model never emitted its end-of-turn
         stop token.
      2. No explicit ``stop`` sequences were passed as a safety net.
      3. ``system`` turns were injected mid-conversation for Gemma (which has no
         system role), corrupting the prompt structure.

    These tests pin the resolution: per-family chat_format + stop sequences, and
    message normalization that folds system content into a user turn (and collapses
    consecutive same-role turns) for system-less families.

Dependencies:
    - pytest: test runner
    - model_cache / profiles: units under test (pure helpers, no GGUF/Redis)

Usage:
    pytest tests/unit/core/test_local_chat_format.py

Notes:
    - All helpers under test are pure functions, so no llama.cpp, GGUF, or Redis is
      required (unit-test safe).
"""

import pytest

from motet.core.models.local.model_cache import (
    chat_format_for_model,
    resolve_local_model_family,
)
from motet.core.models.local.profiles import profile_for_model


# Profile seams (production code calls these via profile_for_model).
def _normalize_chat_messages(messages, model_id):
    return profile_for_model(model_id).normalize_messages(messages)


def stop_sequences_for_model(model_id):
    return profile_for_model(model_id).stop_sequences()


def model_supports_system_role(model_id):
    return profile_for_model(model_id).supports_system_role()


@pytest.mark.parametrize(
    "model_id,family,chat_format,stop",
    [
        # Refreshed local tier (ADR-0117)
        ("gemma-3-4b", "gemma", "gemma", ["<end_of_turn>"]),
        ("gemma-4-e4b", "gemma-4", "gemma", ["<|turn|>", "<|tool_response|>"]),
        ("gemma-4-26b-a4b", "gemma-4", "gemma", ["<|turn|>", "<|tool_response|>"]),
        ("phi-4-mini", "phi-4", "chatml", ["<|im_end|>"]),
        ("llama-3.1-8b-instruct", "llama-3", "llama-3", ["<|eot_id|>", "<|end_of_text|>"]),
        # "ministral" must win over the "mistral" substring (longest-match)
        ("ministral-3-8b-instruct", "ministral", "mistral-instruct", ["</s>"]),
        ("qwen3-8b-instruct", "qwen", "chatml", ["<|im_end|>"]),
        # Legacy families retained as fallback handlers for templateless GGUFs
        ("mistral-7b-instruct-v0.2", "mistral", "mistral-instruct", ["</s>"]),
        ("phi-3-mini", "phi-3", "phi-3", ["<|end|>"]),
    ],
)
def test_family_resolution_and_stop(model_id, family, chat_format, stop):
    """Known families resolve to the right chat_format and end-of-turn stop tokens."""
    assert resolve_local_model_family(model_id) == family
    assert chat_format_for_model(model_id) == chat_format
    assert stop_sequences_for_model(model_id) == stop


def test_unknown_model_defaults_to_autodetect():
    """Unknown models leave chat_format/stop unset so llama.cpp can auto-detect."""
    assert resolve_local_model_family("some-random-model") is None
    assert chat_format_for_model("some-random-model") is None
    assert stop_sequences_for_model("some-random-model") == []
    # Unknown models keep native system handling (no destructive folding).
    assert model_supports_system_role("some-random-model") is True


def test_none_model_id_is_safe():
    assert resolve_local_model_family(None) is None
    assert chat_format_for_model(None) is None
    assert stop_sequences_for_model(None) == []


def test_gemma_has_no_system_role_but_phi_does():
    assert model_supports_system_role("gemma-3-4b") is False
    assert model_supports_system_role("phi-4-mini") is True


def test_gemma_folds_system_into_user_and_alternates():
    """The exact failing trace shape must normalize to clean user/assistant alternation."""
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "Hi there! How can I help you today?"},
        {"role": "system", "content": "You are a helpful AI assistant."},
        {"role": "system", "content": "Relevant context from memory:"},
        {"role": "user", "content": "my name is matt"},
    ]
    out = _normalize_chat_messages(msgs, "gemma-3-4b")

    assert [m["role"] for m in out] == ["user", "assistant", "user"]
    # No system role survives for a system-less family.
    assert all(m["role"] != "system" for m in out)
    # System content is folded into the trailing user turn, ahead of the user text.
    last = out[-1]["content"]
    assert "You are a helpful AI assistant." in last
    assert "Relevant context from memory:" in last
    assert last.endswith("my name is matt")


def test_gemma_collapses_consecutive_same_role():
    msgs = [
        {"role": "user", "content": "part one"},
        {"role": "user", "content": "part two"},
    ]
    out = _normalize_chat_messages(msgs, "gemma-3-4b")
    assert len(out) == 1
    assert out[0]["role"] == "user"
    assert "part one" in out[0]["content"] and "part two" in out[0]["content"]


def test_gemma_trailing_system_becomes_leading_user():
    """System with no following user turn still survives as a user turn."""
    msgs = [
        {"role": "user", "content": "earlier question"},
        {"role": "assistant", "content": "an answer"},
        {"role": "system", "content": "remember this instruction"},
    ]
    out = _normalize_chat_messages(msgs, "gemma-3-4b")
    assert all(m["role"] != "system" for m in out)
    assert any("remember this instruction" in m["content"] for m in out)


def test_system_capable_model_passthrough_unchanged():
    """Phi keeps its native system turns (no folding/collapsing)."""
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "u"},
    ]
    out = _normalize_chat_messages(msgs, "phi-4-mini")
    assert out == msgs


def test_llama3_fallback_prompt_uses_native_headers():
    from motet.core.models.local.profiles import profile_for_model

    profile = profile_for_model("llama-3.1-8b-instruct")
    prompt = profile.fallback_prompt(
        [
            {"role": "system", "content": "You are concise."},
            {"role": "user", "content": "hi"},
        ]
    )

    assert prompt.startswith("<|begin_of_text|>")
    assert "<|start_header_id|>system<|end_header_id|>\n\nYou are concise.<|eot_id|>" in prompt
    assert "<|start_header_id|>user<|end_header_id|>\n\nhi<|eot_id|>" in prompt
    assert prompt.endswith("<|start_header_id|>assistant<|end_header_id|>\n\n")
