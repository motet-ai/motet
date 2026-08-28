"""Unit tests for xAI Responses adapter registration and ModelSpecs (ADR-0122)."""

from __future__ import annotations

from decimal import Decimal

from motet.core.models.adapters import adapter_registry
from motet.core.models.adapters.providers.xai_responses import (
    DEFAULT_XAI_BASE_URL,
    XAIResponsesAdapter,
    _normalize_xai_credentials,
    _resolve_reasoning_effort,
)
from motet.core.models.registry import get_model_spec
from motet.core.models.specs import CAP_TOOL_USE, CAP_VISION
from motet.core.commands.builtin.model import _normalize_adapter_credentials
from motet.core.types import LLMRequest, Message, RequestContext


def test_xai_adapter_registered() -> None:
    adapter = adapter_registry.build(
        "xai",
        "responses",
        credentials={"xai_api_key": "test-key"},
    )
    assert isinstance(adapter, XAIResponsesAdapter)
    assert adapter.provider == "xai"
    assert adapter.credentials is not None
    assert adapter.credentials["api_key"] == "test-key"
    assert adapter.credentials["base_url"] == DEFAULT_XAI_BASE_URL


def test_normalize_xai_credentials_defaults_base_url() -> None:
    creds = _normalize_xai_credentials({"xai_api_key": "k"})
    assert creds["api_key"] == "k"
    assert creds["base_url"] == DEFAULT_XAI_BASE_URL


def test_normalize_credentials_xai_env_base_url(monkeypatch) -> None:
    monkeypatch.setenv("XAI_API_BASE", "https://example.x.ai/v1")
    out = _normalize_adapter_credentials(
        provider="xai",
        credentials={"xai_api_key": "k"},
        spec=None,
    )
    assert out["base_url"] == "https://example.x.ai/v1"
    assert out["api_key"] == "k"


def test_grok_45_model_spec() -> None:
    spec = get_model_spec("xai", "grok-4.5")
    assert spec is not None
    assert spec.provider == "xai"
    assert CAP_TOOL_USE in spec.capabilities
    assert spec.default_adapter == "responses"
    assert spec.supported_adapters == ["responses"]
    assert spec.supported_builtin_tools == ["xai.web_search"]
    assert spec.base_url == "https://api.x.ai/v1"
    assert spec.pricing is not None
    assert spec.pricing.input_per_1k == Decimal("0.002")
    assert spec.pricing.output_per_1k == Decimal("0.006")


def test_grok_46_model_spec() -> None:
    spec = get_model_spec("xai", "grok-4.6")
    assert spec is not None
    assert spec.provider == "xai"
    assert spec.display_name == "Grok 4.6"
    assert CAP_TOOL_USE in spec.capabilities
    assert CAP_VISION in spec.capabilities
    assert spec.default_adapter == "responses"
    assert spec.supported_adapters == ["responses"]
    assert spec.supported_builtin_tools == ["xai.web_search"]
    assert spec.base_url == "https://api.x.ai/v1"
    assert spec.released_at is not None
    assert spec.released_at.isoformat() == "2026-08-12"
    assert spec.pricing is not None
    assert spec.pricing.input_per_1k == Decimal("0.002")
    assert spec.pricing.output_per_1k == Decimal("0.006")
    assert spec.pricing.cache_read_discount_pct == Decimal("75.0")
    assert spec.provenance is not None
    assert spec.provenance.origin == "us"
    assert spec.provenance.vendor == "xAI"


def test_kimi_k27_code_model_spec() -> None:
    spec = get_model_spec("moonshot", "kimi-k2.7-code")
    assert spec is not None
    assert CAP_TOOL_USE in spec.capabilities
    assert CAP_VISION not in spec.capabilities
    assert spec.pricing is not None
    assert spec.pricing.input_per_1k == Decimal("0.00095")
    assert spec.pricing.output_per_1k == Decimal("0.004")


def test_xai_adapter_capabilities() -> None:
    adapter = XAIResponsesAdapter(
        provider="xai",
        adapter_name="responses",
        credentials={"api_key": "k"},
    )
    for model_name in ("grok-4.5", "grok-4.6"):
        caps = adapter.capabilities(model=model_name)
        assert caps.supports_tools is True
        assert caps.supports_parallel_tool_calls is True
        assert caps.supports_reasoning is True
        assert caps.supports_vision is True
        assert caps.supports_builtin_tools is True
        assert caps.provider_metadata.get("adapter") == "xai_responses"


def test_xai_credentials_include_base_url_for_client() -> None:
    adapter = XAIResponsesAdapter(
        provider="xai",
        adapter_name="responses",
        credentials={"xai_api_key": "k"},
    )
    assert adapter.credentials is not None
    assert adapter.credentials["base_url"] == DEFAULT_XAI_BASE_URL
    assert adapter.credentials["api_key"] == "k"
    # Parent _client() reads credentials["base_url"]; avoid constructing a live
    # OpenAI client here (env may have openai/httpx version skew).
    assert hasattr(adapter, "_client")


def test_resolve_reasoning_effort_defaults_medium() -> None:
    assert _resolve_reasoning_effort({}) == "medium"
    assert _resolve_reasoning_effort({"enable_thinking": True}) == "medium"
    assert _resolve_reasoning_effort({"reasoning_effort": "low"}) == "low"
    assert _resolve_reasoning_effort({"reasoning_effort": "HIGH"}) == "high"
    assert _resolve_reasoning_effort({"reasoning_effort": "nope"}) == "medium"


def test_resolve_reasoning_effort_clamps_max_to_xhigh() -> None:
    """xAI 400s on `max`; canonical max must degrade to its top rung, not the default."""
    assert _resolve_reasoning_effort({"reasoning_effort": "xhigh"}) == "xhigh"
    assert _resolve_reasoning_effort({"reasoning_effort": "max"}) == "xhigh"


def test_finalize_responses_params_always_sets_reasoning_and_cache() -> None:
    adapter = XAIResponsesAdapter(
        provider="xai",
        adapter_name="responses",
        credentials={"api_key": "k"},
    )
    request = LLMRequest(
        messages=[Message(role="user", content="hi")],
        model_settings={"model_name": "grok-4.5", "reasoning_effort": "low"},
        request_context=RequestContext(conversation_id="conv-123"),
    )
    params = adapter._finalize_responses_params(
        {
            "model": "grok-4.5",
            "input": [],
            "presence_penalty": 0.5,
            "stop": ["\n"],
        },
        request,
    )
    assert params["reasoning"] == {"effort": "low", "summary": "auto"}
    assert params["prompt_cache_key"] == "conv-123"
    assert "presence_penalty" not in params
    assert "stop" not in params
