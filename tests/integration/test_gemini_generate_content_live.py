"""
Motet - Gemini generateContent Integration Test

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Description:
    Live integration test for `GeminiGenerateContentAdapter` (native Gemini API).

    Skips unless `MOTET_GEMINI_API_KEY`, `GEMINI_API_KEY`, or `GOOGLE_API_KEY` is set.

Usage:
    MOTET_GEMINI_API_KEY=... pytest tests/integration/test_gemini_generate_content_live.py -v
"""

from __future__ import annotations

import os
import importlib.util

import pytest


def _get_gemini_api_key() -> str | None:
    return (
        os.getenv("MOTET_GEMINI_API_KEY")
        or os.getenv("GEMINI_API_KEY")
        or os.getenv("GOOGLE_API_KEY")
    )


@pytest.mark.integration
def test_gemini_generate_content_adapter_complete_live() -> None:
    api_key = _get_gemini_api_key()
    if not api_key:
        pytest.skip(
            "MOTET_GEMINI_API_KEY / GEMINI_API_KEY / GOOGLE_API_KEY not set; skipping live Gemini test."
        )
    if importlib.util.find_spec("google.genai") is None:
        pytest.skip("google-genai package is not installed; skipping live Gemini test.")

    from motet.core.models.adapters.providers.gemini_generate_content import GeminiGenerateContentAdapter
    from motet.core.types import LLMRequest, Message

    model_name = os.getenv("MOTET_GEMINI_MODEL_NAME", "gemini-2.5-flash")

    adapter = GeminiGenerateContentAdapter(
        provider="gemini",
        adapter_name="generate_content",
        credentials={"gemini_api_key": api_key},
    )

    req = LLMRequest(
        messages=[
            Message(
                role="user",
                content="Reply with exactly the single word: pong",
            )
        ],
        tools=None,
        output_contract=None,
        model_settings={
            "provider": "gemini",
            "model_name": model_name,
            "temperature": 0.0,
            "max_tokens": 32,
        },
        request_context=None,
    )

    resp = adapter.complete(req)
    assert resp.output_text is not None
    assert "pong" in resp.output_text.strip().lower()
