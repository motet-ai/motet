"""
Motet - Agents CLI Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
Unit tests for `motet-cli agents` discovery commands.

Dependencies:
- pytest: Test framework
- click.testing: CliRunner
- motet.cli.agents: agents_group
"""

from __future__ import annotations

from typing import Any, Dict

import pytest
from click.testing import CliRunner

from motet.cli.agents import agents_group


class _Resp:
    """Simple response stub with JSON payload."""

    def __init__(self, payload: Dict[str, Any]) -> None:
        self._payload = payload

    def json(self) -> Dict[str, Any]:
        return self._payload


def test_agents_list_plain_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """agents list prints readable agent summary lines."""
    payload = {
        "agents": [
            {
                "qualified_id": "core.default",
                "display_name": "Motet Agent",
                "bundle_id": None,
                "allowed_roles": ["*"],
            },
            {
                "qualified_id": "sales.assistant",
                "display_name": "Sales Assistant",
                "bundle_id": "sales",
                "allowed_roles": ["sales_user", "admin"],
            },
        ],
        "total": 2,
    }

    monkeypatch.setattr("motet_sdk.cli.agents.get_api_headers", lambda: {"Authorization": "Bearer t"})
    monkeypatch.setattr("motet_sdk.cli.agents.api_request", lambda *_args, **_kwargs: _Resp(payload))

    runner = CliRunner()
    result = runner.invoke(agents_group, ["list", "--api-url", "http://localhost:8000"])

    assert result.exit_code == 0, result.output
    assert "Found 2 agent(s)" in result.output
    assert "core.default" in result.output
    assert "sales.assistant" in result.output
    assert "Source: core" in result.output
    assert "Source: sales" in result.output


def test_agents_list_json_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """agents list --json-output returns raw JSON payload."""
    payload = {
        "agents": [{"qualified_id": "core.default", "display_name": "Motet Agent"}],
        "total": 1,
    }

    monkeypatch.setattr("motet_sdk.cli.agents.get_api_headers", lambda: {"Authorization": "Bearer t"})
    monkeypatch.setattr("motet_sdk.cli.agents.api_request", lambda *_args, **_kwargs: _Resp(payload))

    runner = CliRunner()
    result = runner.invoke(
        agents_group,
        ["list", "--json-output", "--api-url", "http://localhost:8000"],
    )

    assert result.exit_code == 0, result.output
    assert '"qualified_id": "core.default"' in result.output
    assert '"total": 1' in result.output

