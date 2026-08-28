"""
Motet - Unit tests for provider API key presence

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    Verifies which providers need a cloud API key and whether config or vault
    presence is reported without exposing the secret.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Optional

from motet.core.models.provider_credentials import (
    provider_has_api_key,
    provider_requires_api_key,
)


def test_local_and_mock_do_not_require_api_key() -> None:
    assert provider_requires_api_key("local") is False
    assert provider_requires_api_key("mock") is False
    assert provider_requires_api_key("LOCAL") is False


def test_cloud_providers_require_api_key() -> None:
    assert provider_requires_api_key("openai") is True
    assert provider_requires_api_key("anthropic") is True
    assert provider_requires_api_key("") is True


def test_has_api_key_from_config() -> None:
    cfg = SimpleNamespace(openai_api_key="sk-test")
    assert provider_has_api_key("openai", cfg=cfg) is True
    assert provider_has_api_key("anthropic", cfg=cfg) is False


def test_has_api_key_ignores_blank_config() -> None:
    cfg = SimpleNamespace(openai_api_key="   ")
    assert provider_has_api_key("openai", cfg=cfg) is False


def test_has_api_key_from_vault(monkeypatch: Any) -> None:
    cfg = SimpleNamespace(openai_api_key=None)

    class FakeVault:
        def get_api_key(self, service_name: str, context: Any) -> Optional[str]:
            assert service_name == "openai"
            assert context is not None
            return "sk-vault"

    monkeypatch.setattr(
        "motet.core.security.vault_client.get_vault_client",
        lambda: FakeVault(),
    )
    assert provider_has_api_key("openai", command_context=object(), cfg=cfg) is True


def test_has_api_key_vault_failure_is_false(monkeypatch: Any) -> None:
    cfg = SimpleNamespace(openai_api_key=None)

    def _boom() -> Any:
        raise RuntimeError("vault unavailable")

    monkeypatch.setattr(
        "motet.core.security.vault_client.get_vault_client",
        _boom,
    )
    assert provider_has_api_key("openai", command_context=object(), cfg=cfg) is False
