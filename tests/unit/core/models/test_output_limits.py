"""
Motet - Unit tests for ModelSpec-backed max output token resolution

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-07-29

Description:
    Verifies that unset max_tokens falls back to ModelSpec.max_output_tokens in
    model-command settings merge and adapter resolve helpers.
"""

from __future__ import annotations

from typing import Any, Dict

from motet.core.models.output_limits import (
    apply_max_tokens_from_spec,
    resolve_max_output_tokens,
)
from motet.core.models.registry import get_model_spec
from motet.core.commands.builtin.model import (
    _select_adapter_and_effective_model_settings,
)


def test_apply_max_tokens_from_spec_fills_when_unset() -> None:
    spec = get_model_spec("deepseek", "deepseek-v4-pro")
    assert spec is not None
    settings: Dict[str, Any] = {"provider": "deepseek", "model_name": "deepseek-v4-pro"}
    apply_max_tokens_from_spec(settings, spec)
    assert settings["max_tokens"] == spec.max_output_tokens == 384000


def test_apply_max_tokens_from_spec_preserves_explicit_request() -> None:
    spec = get_model_spec("deepseek", "deepseek-v4-pro")
    settings: Dict[str, Any] = {"max_tokens": 1200}
    apply_max_tokens_from_spec(settings, spec)
    assert settings["max_tokens"] == 1200


def test_apply_max_tokens_from_spec_preserves_max_completion_tokens() -> None:
    spec = get_model_spec("openai", "gpt-4o-mini")
    settings: Dict[str, Any] = {"max_completion_tokens": 500}
    apply_max_tokens_from_spec(settings, spec)
    assert "max_tokens" not in settings
    assert settings["max_completion_tokens"] == 500


def test_resolve_max_output_tokens_uses_model_spec() -> None:
    resolved = resolve_max_output_tokens(
        {},
        provider="deepseek",
        model_name="deepseek-v4-pro",
        fallback=None,
    )
    assert resolved == 384000


def test_resolve_max_output_tokens_request_wins() -> None:
    resolved = resolve_max_output_tokens(
        {"max_tokens": 256},
        provider="deepseek",
        model_name="deepseek-v4-pro",
        fallback=None,
    )
    assert resolved == 256


def test_select_adapter_fills_max_tokens_from_deepseek_spec() -> None:
    class _Cfg:
        pass

    _selection, effective, _route, spec = _select_adapter_and_effective_model_settings(
        provider="deepseek",
        model_name="deepseek-v4-pro",
        model_settings={"provider": "deepseek", "model_name": "deepseek-v4-pro"},
        request_context=None,
        cfg=_Cfg(),
    )
    assert spec is not None
    assert effective["max_tokens"] == spec.max_output_tokens == 384000


def test_resolve_max_output_tokens_none_fallback_omits_when_unknown() -> None:
    resolved = resolve_max_output_tokens(
        {},
        provider="unknown-provider",
        model_name="not-a-real-model",
        fallback=None,
    )
    assert resolved is None


def test_resolve_max_output_tokens_openai_spec_without_fallback() -> None:
    resolved = resolve_max_output_tokens(
        {},
        provider="openai",
        model_name="gpt-4o-mini",
        fallback=None,
    )
    assert resolved == 16384
