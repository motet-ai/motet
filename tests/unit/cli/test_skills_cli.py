"""
Motet - Skills CLI Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
Unit tests for `motet-cli skills` operator commands.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest
from click.testing import CliRunner

from motet.cli.skills import skills_group


class _Resp:
    """Simple response stub with JSON payload."""

    def __init__(self, payload: Dict[str, Any]) -> None:
        self._payload = payload

    def json(self) -> Dict[str, Any]:
        return self._payload


def test_skills_list_calls_skills_api(monkeypatch: pytest.MonkeyPatch) -> None:
    """skills list prints installed Agent Skills from /api/v1/skills."""
    captured: Dict[str, Any] = {}
    payload = {
        "total": 1,
        "skills": [
            {
                "skill_id": "skills-vendor-demo.pdf",
                "bundle_id": "skills-vendor-demo",
                "description": "Work with PDF files.",
                "base_image_stack": "python-office",
                "runtime_capabilities": ["python", "pdf"],
            }
        ],
    }

    def _fake_api_request(*args: Any, **kwargs: Any) -> _Resp:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _Resp(payload)

    monkeypatch.setattr("motet_sdk.cli.skills.get_api_headers", lambda: {"Authorization": "Bearer t"})
    monkeypatch.setattr("motet_sdk.cli.skills.api_request", _fake_api_request)

    runner = CliRunner()
    result = runner.invoke(
        skills_group,
        [
            "list",
            "--tenant-id",
            "default",
            "--bundle-id",
            "skills-vendor-demo",
            "--api-url",
            "http://localhost:8000",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["args"][:2] == ("GET", "http://localhost:8000/api/v1/skills")
    assert captured["kwargs"]["params"] == {
        "bundle_id": "skills-vendor-demo",
        "tenant_id": "default",
    }
    assert "skills-vendor-demo.pdf" in result.output
    assert "python-office" in result.output
