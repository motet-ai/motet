"""
Motet - Agentic loop observation clipping tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-02

Description:
    Unit tests for clip_observation (loop_observations): tool messages must be
    clipped to a bounded size (preferring the output tail) before entering LLM
    conversation history, since tools registered with
    contextualize_observation=False (core.worker_exec / core.edge_exec)
    skip ContextManager summarization entirely (ADR-0122).

Usage:
    pytest tests/unit/core/test_agentic_loop_observation_clip.py -q
"""

from __future__ import annotations

from motet.core.reasoning.react.loop_observations import (
    _TOOL_OBSERVATION_MAX_CHARS,
    clip_observation,
)


def test_short_observation_unchanged() -> None:
    assert clip_observation("all good") == "all good"


def test_clip_prefers_tail_with_summary() -> None:
    head = "x" * 100_000
    summary = "===== 2344 passed, 61 skipped in 89.92s ====="
    clipped = clip_observation(head + "\n" + summary, limit=4096)
    assert len(clipped) <= 4096
    assert "2344 passed" in clipped
    assert "...[observation truncated]..." in clipped


def test_clip_default_limit_bounded() -> None:
    text = "y" * (_TOOL_OBSERVATION_MAX_CHARS * 3)
    clipped = clip_observation(text)
    assert len(clipped) <= _TOOL_OBSERVATION_MAX_CHARS


def test_clip_nonpositive_limit_disables() -> None:
    text = "z" * 100_000
    assert clip_observation(text, limit=-1) == text


def test_clip_appends_artifact_pointer_when_truncated() -> None:
    text = "y" * 20_000
    clipped = clip_observation(text, limit=4096, artifact_id="art-123")
    assert len(clipped) <= 4096
    assert "artifact_id=art-123" in clipped
    assert "...[observation truncated]..." in clipped
