"""
Motet - Live-Provider Reasoning Checks

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-22

Description:
    In-process MotetStack.chat checks that need a real LLM (arithmetic,
    multi-tool observations). These are not lane C-workers: compose uses
    mock-small. Worker-backed mock coverage is ``test_reasoning_integration.py``.

Dependencies:
    - motet.core: MotetStack, Message, Config
    - A non-mock MOTET_MODEL_PROVIDER

Usage:
    MOTET_MODEL_PROVIDER=openai pytest tests/integration/reasoning/test_reasoning_live.py -q

Notes:
    - Skips when MOTET_MODEL_PROVIDER is mock or unset-as-mock
    - Not marked distributed; lane B collects them and they skip on mock
"""

from __future__ import annotations

import os

import pytest

from motet.core import Config, Message, MotetStack

pytestmark = [pytest.mark.integration, pytest.mark.requires_external]


def _skip_if_mock() -> None:
    if os.getenv("MOTET_MODEL_PROVIDER", "mock").lower() == "mock":
        pytest.skip("Requires a real LLM provider, not mock")


@pytest.mark.asyncio
async def test_orchestrator_act_math() -> None:
    _skip_if_mock()
    stack = MotetStack(Config())
    resp = await stack.chat([Message(role="user", content="math: 2+3*4")])
    assert "14" in resp.content


@pytest.mark.asyncio
async def test_orchestrator_multi_act_and_read(tmp_path) -> None:
    _skip_if_mock()
    os.environ["MOTET_FILE_READ_ALLOWLIST"] = str(tmp_path)
    fp = tmp_path / "sample.txt"
    fp.write_text("hello world from reactor")

    stack = MotetStack(Config())
    user = """
        math: 10/2
        http_get: https://example.com
        read: {path}
        """.strip().format(path=str(fp))
    resp = await stack.chat([Message(role="user", content=user)])
    assert "math(result=5.0)" in resp.content
    assert "http_get(status=" in resp.content
    assert "read(ok," in resp.content
