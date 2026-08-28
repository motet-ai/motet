"""
Motet - Facade Policy Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-07-29

Description:
    Unit tests for per-credential OpenAI facade policy (ADR-0125 §4, §5c, §11a):
    deny-by-default model access, allowlist matching, mode ceilings,
    force_thinking resolution, and the precedence between service account
    claims and configuration defaults.

Dependencies:
    - pytest: test runner
    - motet.core.security.facade_policy: system under test

Usage:
    pytest tests/unit/core/security/test_facade_policy.py

Notes:
    - An empty allowlist must grant nothing, so enabling the facade without
      policy cannot expose every vault-backed provider
"""

from types import SimpleNamespace

import pytest

from motet.core.security.facade_policy import (
    FacadeMode,
    FacadePolicy,
    parse_facade_mode,
    resolve_facade_policy,
)


def make_cfg(**overrides):
    """Minimal config stub with facade defaults."""
    base = {
        "openai_compat_default_mode": "passthrough",
        "openai_compat_default_allowed_models": "",
        "openai_compat_force_thinking": False,
        "openai_compat_force_thinking_effort": "medium",
        "openai_compat_default_agent_id": "",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def make_principal(**claims):
    return SimpleNamespace(id="service-account:test", tenant_id="t1", claims=claims)


class TestAllowlist:
    """Model access is deny-by-default and pattern based."""

    def test_empty_allowlist_denies_everything(self):
        policy = FacadePolicy(mode=FacadeMode.PASSTHROUGH, allowed_models=[])
        assert policy.allows_model("openai", "gpt-4o-mini") is False

    def test_exact_match(self):
        policy = FacadePolicy(allowed_models=["openai/gpt-4o-mini"])
        assert policy.allows_model("openai", "gpt-4o-mini") is True
        assert policy.allows_model("openai", "gpt-4o") is False

    def test_provider_wildcard(self):
        policy = FacadePolicy(allowed_models=["anthropic/*"])
        assert policy.allows_model("anthropic", "claude-sonnet-4") is True
        assert policy.allows_model("openai", "gpt-4o-mini") is False

    def test_global_wildcard(self):
        policy = FacadePolicy(allowed_models=["*"])
        assert policy.allows_model("openai", "gpt-4o-mini") is True
        assert policy.allows_model("local", "anything") is True

    def test_matching_is_case_insensitive(self):
        policy = FacadePolicy(allowed_models=["OpenAI/GPT-4o-Mini"])
        assert policy.allows_model("openai", "gpt-4o-mini") is True


class TestModeCeiling:
    """A request may weaken its mode but never escalate past the credential."""

    def test_requested_mode_is_clamped_to_bound_mode(self):
        policy = FacadePolicy(mode=FacadeMode.HOSTED_TOOLS)
        assert policy.resolve_mode(FacadeMode.AGENT) is FacadeMode.HOSTED_TOOLS

    def test_weaker_mode_is_allowed(self):
        policy = FacadePolicy(mode=FacadeMode.AGENT)
        assert policy.resolve_mode(FacadeMode.PASSTHROUGH) is FacadeMode.PASSTHROUGH

    def test_absent_request_uses_bound_mode(self):
        policy = FacadePolicy(mode=FacadeMode.AGENT)
        assert policy.resolve_mode(None) is FacadeMode.AGENT

    def test_permits_mode_reports_ceiling(self):
        policy = FacadePolicy(mode=FacadeMode.PASSTHROUGH)
        assert policy.permits_mode(FacadeMode.PASSTHROUGH) is True
        assert policy.permits_mode(FacadeMode.HOSTED_TOOLS) is False


class TestResolution:
    """Service account claims win over configuration defaults."""

    def test_service_account_claims_take_precedence(self):
        principal = make_principal(
            facade_mode="agent", allowed_models=["openai/gpt-4o-mini"]
        )
        cfg = make_cfg(
            openai_compat_default_mode="passthrough",
            openai_compat_default_allowed_models="anthropic/*",
        )

        policy = resolve_facade_policy(principal, cfg)

        assert policy.mode is FacadeMode.AGENT
        assert policy.mode_source == "service_account"
        assert policy.allowed_models == ["openai/gpt-4o-mini"]
        assert policy.allowlist_source == "service_account"

    def test_falls_back_to_config_defaults(self):
        principal = make_principal()
        cfg = make_cfg(
            openai_compat_default_mode="hosted_tools",
            openai_compat_default_allowed_models="openai/gpt-4o-mini, anthropic/*",
        )

        policy = resolve_facade_policy(principal, cfg)

        assert policy.mode is FacadeMode.HOSTED_TOOLS
        assert policy.mode_source == "config_default"
        assert policy.allowed_models == ["openai/gpt-4o-mini", "anthropic/*"]
        assert policy.allowlist_source == "config_default"

    def test_unknown_mode_falls_back_to_passthrough(self):
        principal = make_principal(facade_mode="superuser")
        cfg = make_cfg(openai_compat_default_mode="nonsense")

        policy = resolve_facade_policy(principal, cfg)

        assert policy.mode is FacadeMode.PASSTHROUGH

    def test_principal_without_claims_is_denied_models(self):
        policy = resolve_facade_policy(SimpleNamespace(id="u", claims=None), make_cfg())
        assert policy.allowed_models == []
        assert policy.allows_model("openai", "gpt-4o-mini") is False

    def test_force_thinking_from_service_account(self):
        principal = make_principal(force_thinking=True, force_thinking_effort="high")
        cfg = make_cfg(openai_compat_force_thinking=False)

        policy = resolve_facade_policy(principal, cfg)

        assert policy.force_thinking is True
        assert policy.force_thinking_effort == "high"
        assert policy.force_thinking_source == "service_account"

    def test_force_thinking_falls_back_to_config(self):
        principal = make_principal()
        cfg = make_cfg(
            openai_compat_force_thinking=True,
            openai_compat_force_thinking_effort="low",
        )

        policy = resolve_facade_policy(principal, cfg)

        assert policy.force_thinking is True
        assert policy.force_thinking_effort == "low"
        assert policy.force_thinking_source == "config_default"

    def test_force_thinking_false_on_sa_overrides_config_true(self):
        principal = make_principal(force_thinking=False)
        cfg = make_cfg(openai_compat_force_thinking=True)

        policy = resolve_facade_policy(principal, cfg)

        assert policy.force_thinking is False
        assert policy.force_thinking_source == "service_account"

    def test_agent_id_from_service_account(self):
        principal = make_principal(agent_id="cursor.backend")
        cfg = make_cfg(openai_compat_default_agent_id="core.default")

        policy = resolve_facade_policy(principal, cfg)

        assert policy.agent_id == "cursor.backend"
        assert policy.agent_id_source == "service_account"

    def test_agent_id_falls_back_to_config(self):
        principal = make_principal()
        cfg = make_cfg(openai_compat_default_agent_id="cursor.backend")

        policy = resolve_facade_policy(principal, cfg)

        assert policy.agent_id == "cursor.backend"
        assert policy.agent_id_source == "config_default"


@pytest.mark.parametrize(
    "value,expected",
    [
        ("agent", FacadeMode.AGENT),
        ("  Hosted_Tools ", FacadeMode.HOSTED_TOOLS),
        ("passthrough", FacadeMode.PASSTHROUGH),
        ("", None),
        (None, None),
        ("bogus", None),
    ],
)
def test_parse_facade_mode(value, expected):
    assert parse_facade_mode(value) is expected
