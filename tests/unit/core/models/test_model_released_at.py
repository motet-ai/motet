"""
Motet - ModelSpec released_at registry tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-18

Description:
    Verifies ModelSpec.released_at backfill and live-matrix canary selection
    that prefers newest release month then lowest input price.

Dependencies:
    - pytest
    - motet.core.models.specs
    - tests.fixtures.live_adapter_matrix

Usage:
    pytest tests/unit/core/models/test_model_released_at.py -q
"""

from __future__ import annotations

from datetime import date

from motet.core.models.specs import MODEL_REGISTRY
from tests.fixtures.live_adapter_matrix import default_live_cases


def test_hosted_live_providers_have_released_at_on_chat_models() -> None:
    """Every chat model for live providers should have a recorded launch date."""
    live_providers = ("openai", "anthropic", "gemini", "moonshot", "deepseek", "xai", "meta")
    missing: list[str] = []
    for provider in live_providers:
        for key, spec in (MODEL_REGISTRY.get(provider) or {}).items():
            if set(spec.capabilities or set()) == {"image_generation"}:
                continue
            if spec.released_at is None:
                missing.append(f"{provider}/{key}")
    assert not missing, f"missing released_at: {missing}"


def test_openai_and_anthropic_flagship_released_at() -> None:
    assert MODEL_REGISTRY["openai"]["gpt-5.5"].released_at == date(2026, 4, 23)
    assert MODEL_REGISTRY["anthropic"]["claude-opus-5"].released_at == date(2026, 7, 24)
    assert MODEL_REGISTRY["xai"]["grok-4.6"].released_at == date(2026, 8, 12)


def test_default_live_cases_prefer_newest_month_then_cheapest() -> None:
    """
    OpenAI newest month is July 2026 (gpt-5.6 tiers) → cheapest luna.
    Anthropic newest month is July 2026 (opus-5) → opus-5.
    Gemini newest month includes flash-lite → cheapest flash-lite preview.
    """
    cases = dict(default_live_cases())
    assert cases["openai"] == "gpt-5.6-luna"  # same month as sol/terra, cheapest
    assert cases["anthropic"] == "claude-opus-5"  # newest month (July 2026)
    assert cases["gemini"] == "gemini-3.1-flash-lite-preview"
    assert cases["moonshot"] == "kimi-k3"
    assert cases["deepseek"] == "deepseek-v4-flash"  # same month as pro, cheaper
    assert cases["xai"] == "grok-4.6"
    assert cases["meta"] == "muse-spark-1.2"


def test_alias_keys_not_selected_as_live_canary() -> None:
    cases = dict(default_live_cases())
    assert cases["anthropic"] != "claude-opus-4.8"
    assert not cases["openai"].endswith("-chat")
