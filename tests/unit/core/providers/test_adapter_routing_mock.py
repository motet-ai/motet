"""
Motet - Adapter routing for mock provider

Ensures MOTET_MODEL_PROVIDER=mock works even when model_name is still an
OpenAI leftover default (common in Docker test stacks).
"""

from motet.core.commands.builtin.model import _resolve_provider_and_model
from motet.core.config import Config
from motet.core.models.adapters.routing import select_adapter_name
from motet.core.orchestration.turn.prepare import resolve_turn_model_policy


def test_select_adapter_name_mock_with_openai_leftover_model() -> None:
    selection = select_adapter_name(
        provider="mock",
        model_name="gpt-4o-mini",
        model_settings={},
    )
    assert selection.adapter_name == "mock"
    assert selection.source == "env_default"


def test_select_adapter_name_mock_small_uses_model_spec() -> None:
    selection = select_adapter_name(
        provider="mock",
        model_name="mock-small",
        model_settings={},
    )
    assert selection.adapter_name == "mock"
    assert selection.source == "model_spec"


def test_config_coerces_mock_provider_openai_model_name(monkeypatch) -> None:
    monkeypatch.setenv("MOTET_MODEL_PROVIDER", "mock")
    monkeypatch.setenv("MOTET_MODEL_NAME", "gpt-4o-mini")
    cfg = Config()
    assert cfg.model_provider == "mock"
    assert cfg.model_name == "mock-small"


def test_resolve_provider_and_model_mock_defaults() -> None:
    cfg = Config(model_provider="mock", model_name="gpt-4o-mini")
    provider, model_name = _resolve_provider_and_model(cfg, "mock", "gpt-4o-mini")
    assert provider == "mock"
    assert model_name == "mock-small"


def test_resolve_turn_model_policy_coerces_mock_openai_leftover() -> None:
    from types import SimpleNamespace

    # Use a plain namespace so Config's own validator is not what we exercise.
    stack_cfg = SimpleNamespace(
        model_provider="mock",
        model_name="gpt-4o-mini",
        model_profile_name=None,
    )
    policy = resolve_turn_model_policy({}, agent_config=None, stack_cfg=stack_cfg)
    assert policy.provider == "mock"
    assert policy.model_name == "mock-small"
