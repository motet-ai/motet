"""
Motet - Meta Muse Spark Adapter Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    Unit tests for Meta Model API Responses adapter registration, Muse Spark
    specs, credential normalization, and always-on reasoning policy.

Usage:
    pytest tests/unit/core/providers/test_meta_adapter.py
"""

from __future__ import annotations

from decimal import Decimal

from motet.core.commands.builtin.model import _normalize_adapter_credentials
from motet.core.models.adapters import adapter_registry
from motet.core.models.adapters.providers.meta_responses import (
    DEFAULT_META_BASE_URL,
    MetaResponsesAdapter,
    _normalize_meta_credentials,
    _resolve_reasoning_effort,
)
from motet.core.models.registry import get_model_spec
from motet.core.models.specs import CAP_TOOL_USE, CAP_VISION
from motet.core.types import LLMRequest, Message, RequestContext


def test_meta_adapter_registered() -> None:
    adapter = adapter_registry.build(
        "meta",
        "responses",
        credentials={"meta_api_key": "test-key"},
    )
    assert isinstance(adapter, MetaResponsesAdapter)
    assert adapter.provider == "meta"
    assert adapter.credentials is not None
    assert adapter.credentials["api_key"] == "test-key"
    assert adapter.credentials["base_url"] == DEFAULT_META_BASE_URL


def test_normalize_meta_credentials_defaults_base_url() -> None:
    creds = _normalize_meta_credentials({"meta_api_key": "k"})
    assert creds["api_key"] == "k"
    assert creds["base_url"] == DEFAULT_META_BASE_URL


def test_normalize_credentials_meta_env_base_url(monkeypatch) -> None:
    monkeypatch.setenv("META_API_BASE", "https://example.meta.ai/v1")
    out = _normalize_adapter_credentials(
        provider="meta",
        credentials={"meta_api_key": "k"},
        spec=None,
    )
    assert out["base_url"] == "https://example.meta.ai/v1"
    assert out["api_key"] == "k"


def test_muse_spark_12_model_spec() -> None:
    spec = get_model_spec("meta", "muse-spark-1.2")
    assert spec is not None
    assert spec.provider == "meta"
    assert spec.display_name == "Muse Spark 1.2"
    assert CAP_TOOL_USE in spec.capabilities
    assert CAP_VISION in spec.capabilities
    assert spec.default_adapter == "responses"
    assert spec.supported_adapters == ["responses"]
    assert spec.supported_builtin_tools == ["meta.web_search"]
    assert spec.base_url == "https://api.meta.ai/v1"
    assert spec.released_at is not None
    assert spec.released_at.isoformat() == "2026-08-05"
    assert spec.pricing is not None
    assert spec.pricing.input_per_1k == Decimal("0.00125")
    assert spec.pricing.output_per_1k == Decimal("0.00425")
    assert spec.pricing.cache_read_discount_pct == Decimal("88.0")
    assert spec.provenance is not None
    assert spec.provenance.origin == "us"
    assert spec.provenance.vendor == "Meta"


def test_muse_spark_11_model_spec() -> None:
    spec = get_model_spec("meta", "muse-spark-1.1")
    assert spec is not None
    assert spec.supported_builtin_tools == ["meta.web_search"]
    assert spec.default_adapter == "responses"


def test_contributor_tier_not_registered() -> None:
    assert get_model_spec("meta", "muse-spark-1.2-contributor") is None


def test_meta_adapter_capabilities() -> None:
    adapter = MetaResponsesAdapter(
        provider="meta",
        adapter_name="responses",
        credentials={"api_key": "k"},
    )
    for model_name in ("muse-spark-1.1", "muse-spark-1.2"):
        caps = adapter.capabilities(model=model_name)
        assert caps.supports_tools is True
        assert caps.supports_reasoning is True
        assert caps.supports_vision is True
        assert caps.supports_builtin_tools is True
        assert caps.provider_metadata.get("adapter") == "meta_responses"


def test_resolve_reasoning_effort_thinking_off_is_minimal() -> None:
    assert _resolve_reasoning_effort({}) == "minimal"
    assert _resolve_reasoning_effort({"enable_thinking": False}) == "minimal"


def test_resolve_reasoning_effort_clamps_max_to_xhigh() -> None:
    assert _resolve_reasoning_effort({"enable_thinking": True, "reasoning_effort": "max"}) == "xhigh"
    assert _resolve_reasoning_effort({"enable_thinking": True, "reasoning_effort": "high"}) == "high"
    assert _resolve_reasoning_effort({"enable_thinking": True}) == "medium"


def test_finalize_responses_params_always_sets_reasoning_and_store_false() -> None:
    adapter = MetaResponsesAdapter(
        provider="meta",
        adapter_name="responses",
        credentials={"api_key": "k"},
    )
    request = LLMRequest(
        messages=[Message(role="user", content="hi")],
        model_settings={
            "model_name": "muse-spark-1.2",
            "enable_thinking": True,
            "reasoning_effort": "low",
            "enable_prompt_caching": True,
        },
        request_context=RequestContext(conversation_id="conv-123"),
    )
    params = adapter._finalize_responses_params(
        {"model": "muse-spark-1.2", "input": []},
        request,
    )
    assert params["reasoning"] == {"effort": "low", "summary": "auto"}
    assert params["store"] is False
    assert params["include"] == ["reasoning.encrypted_content"]
    assert params["prompt_cache_key"] == "conv-123"
