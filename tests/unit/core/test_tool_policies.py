"""
Motet - Tool Role Policy Unit Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-07-29

Description:
    Unit coverage for ToolRegistry role allowlists and denylists. Policies are
    enforced in ``_execute_tool_only`` / ``execute`` (local path). The HTTP
    ``/api/v1/tools/execute`` distributed path does not currently forward role
    into ``tool_execution``; these tests pin the registry contract directly.

Dependencies:
    - pytest
    - motet.core.config.Config
    - motet.core.tools.registry.ToolRegistry

Usage:
    pytest tests/unit/core/test_tool_policies.py -q
"""

from __future__ import annotations

from typing import Any, Dict

from motet.core.config import Config
from motet.core.tools.registry import ToolRegistry


def _make_registry_with_policies() -> ToolRegistry:
    reg = ToolRegistry()

    def _math(params: Dict[str, Any], **_kwargs: Any) -> Dict[str, Any]:
        return {"result": 3}

    def _http(params: Dict[str, Any], **_kwargs: Any) -> Dict[str, Any]:
        return {"result": "fetched"}

    reg.register(name="core.math_eval", description="math", func=_math, category="general")
    reg.register(name="core.http_get", description="http", func=_http, category="http")
    reg.set_config(Config(tool_role_policies_json='{"user":["core.math_eval"]}'))
    return reg


def test_role_based_tool_policies_enforced() -> None:
    """Role allowlist permits listed tools; denylist and role deny block others."""
    reg = _make_registry_with_policies()

    allowed = reg.execute("core.math_eval", {"expression": "1+2"}, role="user")
    assert allowed.get("status") == "success"
    assert allowed.get("result") == 3

    denied_role = reg.execute("core.http_get", {"url": "https://example.com"}, role="user")
    assert denied_role.get("status") == "denied_by_role"
    assert denied_role.get("error")

    denied_list = reg.execute(
        "core.http_get",
        {"url": "https://example.com"},
        deny={"core.http_get"},
    )
    assert denied_list.get("status") == "denied_by_denylist"

    # Role key absent from the policy map is unconstrained (only listed roles).
    admin = reg.execute("core.math_eval", {"expression": "1+2"}, role="admin")
    assert admin.get("status") == "success"


def test_role_policy_denies_unlisted_tool_for_role() -> None:
    """A tool not on the role allowlist is denied even when not denylisted."""
    reg = ToolRegistry()

    def _other(params: Dict[str, Any], **_kwargs: Any) -> Dict[str, Any]:
        return {"result": "ok"}

    reg.register(name="core.other_tool", description="other", func=_other)
    reg.set_config(Config(tool_role_policies_json='{"user":["core.math_eval"]}'))

    denied = reg.execute("core.other_tool", {}, role="user")
    assert denied.get("status") == "denied_by_role"
    assert "role" in (denied.get("error") or "").lower()
