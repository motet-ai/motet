"""
Motet - Adapter Capability Contract Tests (ADR-0064)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-07-23

Description:
    Parametrized registry-wide contract: every ModelSpec's CAP_* flags must match
    the CapabilityDescriptor returned by its default (and supported) adapters.
    Also verifies adapters are registered and buildable without network calls.

Dependencies:
    - pytest: parametrize / assertions
    - motet.core.models.adapters: adapter_registry
    - tests.unit.core.providers.canonical_contract: shared helpers

Usage:
    pytest tests/unit/core/providers/test_adapter_capability_contract.py
"""

from __future__ import annotations

import pytest

from motet.core.models.adapters import adapter_registry
from motet.core.models.specs import ModelSpec

from tests.fixtures.canonical_adapter_contract import (
    _dummy_credentials,
    assert_caps_match_spec,
    iter_registry_cases,
)


_CASES = iter_registry_cases()


def _case_id(case: tuple[str, str, ModelSpec]) -> str:
    provider, model_name, _spec = case
    return f"{provider}/{model_name}"


@pytest.mark.parametrize("case", _CASES, ids=[_case_id(c) for c in _CASES])
def test_default_adapter_registered_and_buildable(case: tuple[str, str, ModelSpec]) -> None:
    provider, model_name, spec = case
    assert spec.default_adapter, f"{provider}/{model_name} missing default_adapter"
    assert adapter_registry.supports(provider, spec.default_adapter), (
        f"No adapter registered for {provider}/{spec.default_adapter} "
        f"(model={model_name})"
    )
    adapter = adapter_registry.build(
        provider,
        spec.default_adapter,
        credentials=_dummy_credentials(provider),
    )
    assert adapter.provider == provider
    assert adapter.adapter_name == spec.default_adapter


@pytest.mark.parametrize("case", _CASES, ids=[_case_id(c) for c in _CASES])
def test_supported_adapters_are_registered(case: tuple[str, str, ModelSpec]) -> None:
    provider, model_name, spec = case
    supported = list(spec.supported_adapters or [])
    assert supported, f"{provider}/{model_name} has empty supported_adapters"
    assert spec.default_adapter in supported
    for adapter_name in supported:
        assert adapter_registry.supports(provider, adapter_name), (
            f"{provider}/{model_name} lists unsupported adapter {adapter_name!r}"
        )


@pytest.mark.parametrize("case", _CASES, ids=[_case_id(c) for c in _CASES])
def test_cap_flags_match_capability_descriptor(case: tuple[str, str, ModelSpec]) -> None:
    provider, model_name, spec = case
    adapter = adapter_registry.build(
        provider,
        spec.default_adapter,
        credentials=_dummy_credentials(provider),
    )
    caps = adapter.capabilities(model=model_name)
    assert_caps_match_spec(spec, caps, model_id=model_name)


@pytest.mark.parametrize("case", _CASES, ids=[_case_id(c) for c in _CASES])
def test_all_supported_adapters_agree_on_caps(case: tuple[str, str, ModelSpec]) -> None:
    """Every supported adapter for a model must report the same CAP-derived flags."""
    provider, model_name, spec = case
    supported = list(spec.supported_adapters or [])
    if len(supported) < 2:
        pytest.skip("single adapter")

    descriptors = []
    for adapter_name in supported:
        adapter = adapter_registry.build(
            provider,
            adapter_name,
            credentials=_dummy_credentials(provider),
        )
        caps = adapter.capabilities(model=model_name)
        assert_caps_match_spec(spec, caps, model_id=model_name)
        descriptors.append(
            (
                caps.supports_streaming,
                caps.supports_tools,
                caps.supports_vision,
                caps.supports_reasoning,
                caps.supports_json_mode,
                caps.supports_system_prompt,
                caps.supports_image_generation,
            )
        )
    assert len(set(descriptors)) == 1, (
        f"{provider}/{model_name} adapters disagree on CAP-derived flags: "
        f"{list(zip(supported, descriptors))}"
    )
