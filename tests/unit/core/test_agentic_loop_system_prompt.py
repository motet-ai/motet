"""
Motet - Agentic Loop System Prompt Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-04

Description:
    Unit tests for issue #131: keep the agentic system prompt cache-stable by
    omitting daily-changing date strings, and instruct the model to use
    ``core.current_time`` / ``delay_seconds`` for temporal needs.
    Also covers Motet-only fallback identity (no provider/model personas) and
    the client-provided (handback) tool preference directive (ADR-0125 §5c.1):
    when a turn carries handback tools, the prompt enumerates them and
    instructs the model to prefer them for client-environment work.

Usage:
    pytest tests/unit/core/test_agentic_loop_system_prompt.py
"""

from __future__ import annotations

from motet.core.reasoning.react.agentic_loop import (
    _build_agentic_system_prompt,
)


def test_system_prompt_has_no_current_date() -> None:
    prompt = _build_agentic_system_prompt()
    assert "Current date:" not in prompt
    assert "Current UTC datetime:" not in prompt
    assert "You are Motet's assistant" in prompt


def test_system_prompt_uses_motet_identity_only() -> None:
    """Fallback identity is Motet-only; no Claude/Kimi/provider personas."""
    prompt = _build_agentic_system_prompt()
    assert "You are Motet's assistant" in prompt
    assert "Claude" not in prompt
    assert "Kimi" not in prompt
    assert "OpenAI assistant" not in prompt
    assert "DeepSeek" not in prompt
    assert "selected model" not in prompt


def test_system_prompt_points_at_time_tools() -> None:
    prompt = _build_agentic_system_prompt()
    assert "core.current_time" in prompt
    assert "delay_seconds" in prompt
    assert "Do NOT guess the current date" in prompt


def test_system_prompt_omits_client_tools_section_by_default() -> None:
    prompt = _build_agentic_system_prompt()
    assert "Client-provided tools" not in prompt

    prompt = _build_agentic_system_prompt(handback_tool_names=[])
    assert "Client-provided tools" not in prompt


def test_system_prompt_enumerates_handback_tools() -> None:
    prompt = _build_agentic_system_prompt(
        handback_tool_names=["Shell", "ReadFile"],
    )
    assert "Client-provided tools" in prompt
    # Names are enumerated verbatim (sorted) so they match the model's tool list.
    assert "ReadFile, Shell" in prompt
    assert "PREFER these client tools" in prompt
    # Motet lookalikes are called out so the model knows what to deprioritize.
    assert "core.file_search" in prompt
