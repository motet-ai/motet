"""
Motet - Provider Builtin Tools Policy Tests

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-23

Description:
    Unit tests for _apply_builtin_tools in the model commands module. Covers the
    explicit "no tools" contract: a caller passing tools=[] (as opposed to
    tools=None) must not receive provider built-in tools (server-side
    web_search), because the merged tool definition silently re-arms tools and
    bills ~2.5k prompt tokens per call on Anthropic. This is the contract the
    adaptive no-tools fast path relies on.

Dependencies:
    - pytest: Test framework
    - unittest.mock: Patching of the builtin tools policy resolution

Usage:
    pytest tests/unit/core/test_builtin_tools_policy.py

Notes:
    - _resolve_builtin_tools_policy is patched so tests exercise the merge
      logic without needing adapter capabilities or env-based tool policy.
"""

from unittest.mock import MagicMock, patch

from motet.core.commands.builtin.model import _apply_builtin_tools


def _call(*, request_tools_explicitly_empty: bool, builtin_names):
    with patch(
        "motet.core.commands.builtin.model._resolve_builtin_tools_policy",
        return_value=([], builtin_names, True),
    ):
        return _apply_builtin_tools(
            provider="anthropic",
            model_name="claude-opus-4.8",
            canonical_tools=None,
            cfg=MagicMock(),
            route_override=None,
            adapter=MagicMock(),
            adapter_name="anthropic:messages",
            spec=MagicMock(),
            request_enable_tools=None,
            request_tools_explicitly_empty=request_tools_explicitly_empty,
        )


def test_explicit_empty_tools_suppresses_provider_builtins() -> None:
    """tools=[] means no tools at all — server web_search must not be merged."""
    merged, enabled, names, configured = _call(
        request_tools_explicitly_empty=True,
        builtin_names=["anthropic.web_search"],
    )
    assert merged == []
    assert enabled is False
    assert names == []
    assert configured is False


def test_default_path_still_merges_web_search() -> None:
    """tools=None keeps the ADR-0064 unified web_search merge intact."""
    merged, enabled, names, _configured = _call(
        request_tools_explicitly_empty=False,
        builtin_names=["anthropic.web_search"],
    )
    assert enabled is True
    assert names == ["web_search"]
    assert [getattr(t, "name", None) for t in merged] == ["web_search"]
