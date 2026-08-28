"""
Motet - Anthropic Messages API Integration Test

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Live integration tests that exercise the Anthropic **Messages API** via the
    `AnthropicMessagesAdapter` (ADR-0064).

    These tests are skipped automatically unless an Anthropic API key is provided
    (via `ANTHROPIC_API_KEY` or `MOTET_ANTHROPIC_API_KEY`).

Dependencies:
    - pytest
    - anthropic (Python SDK)
    - motet.core.models.adapters.providers.anthropic_messages.AnthropicMessagesAdapter

Usage:
    # In Docker (REQUIRED for integration tests per AGENTS.md):
    docker-compose -f tests/docker-compose.test.yml run --rm test-runner \
      python -m pytest -q tests/integration/test_anthropic_messages_live.py -v
"""

from __future__ import annotations

import os

import pytest


def _get_anthropic_api_key() -> str | None:
    return os.getenv("MOTET_ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_API_KEY")


def _discover_sonnet_3_5_model_id(*, api_key: str) -> str | None:
    """
    Discover an available Claude Sonnet 3.5 model ID for this API key.

    Anthropic model availability varies by account, and aliases like
    `claude-3-5-sonnet-latest` may not be enabled everywhere. This helper queries
    the Models API and selects the first match.
    """
    try:
        import httpx
    except Exception:
        return None

    headers = {
        "X-Api-Key": api_key,
        "anthropic-version": "2023-06-01",
    }

    try:
        resp = httpx.get("https://api.anthropic.com/v1/models", headers=headers, timeout=20)
        resp.raise_for_status()
        payload = resp.json()
    except Exception:
        return None

    data = payload.get("data")
    if not isinstance(data, list):
        return None

    for item in data:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id")
        display_name = item.get("display_name")
        if not isinstance(model_id, str):
            continue
        if isinstance(display_name, str) and "3.5" in display_name and "Sonnet" in display_name:
            return model_id
        if "3-5" in model_id and "sonnet" in model_id:
            return model_id

    return None


@pytest.mark.integration
def test_anthropic_messages_adapter_complete_live() -> None:
    api_key = _get_anthropic_api_key()
    if not api_key:
        pytest.skip("ANTHROPIC_API_KEY/MOTET_ANTHROPIC_API_KEY not set; skipping live Anthropic integration test.")

    from motet.core.models.adapters.providers.anthropic_messages import AnthropicMessagesAdapter
    from motet.core.types import LLMRequest, Message

    # Prefer an explicit override, otherwise try to find an enabled Sonnet 3.5 model.
    model_name = os.getenv("MOTET_ANTHROPIC_MODEL_NAME")
    if not model_name:
        model_name = _discover_sonnet_3_5_model_id(api_key=api_key)

    if not model_name:
        pytest.skip(
            "No Claude Sonnet 3.5 model was discoverable for this Anthropic API key. "
            "Set MOTET_ANTHROPIC_MODEL_NAME to a model ID returned by GET /v1/models."
        )

    adapter = AnthropicMessagesAdapter(
        provider="anthropic",
        adapter_name="messages",
        credentials={"anthropic_api_key": api_key},
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
            "provider": "anthropic",
            "model_name": model_name,
            "temperature": 0.0,
            "max_tokens": 16,
        },
        request_context=None,
    )

    resp = adapter.complete(req)
    assert resp.output_text is not None
    assert "pong" in resp.output_text.strip().lower()

