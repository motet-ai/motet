"""
Motet - Tool Parameter Boolean-to-Null Coercion Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-07-13

Description:
Unit tests for the pre-validation parameter coercion in the tool registry
(ADR-0115). Small local models occasionally emit booleans for optional string
parameters (e.g. ``execute_js: false`` where the schema is ``string | null``),
which previously failed Pydantic validation and burned a reasoning iteration.
These tests cover the ``_coerce_boolean_null_params`` helper directly and the
end-to-end ``ToolRegistry.execute`` path.

Dependencies:
- pytest: Test framework
- pydantic: Schema models used to exercise the coercion rules
- motet.core.tools.registry: ToolRegistry and _coerce_boolean_null_params under test

Usage:
pytest tests/unit/core/test_tool_param_coercion.py

Notes:
- Pure unit tests; no Redis/Celery/Docker stack required.
- Coercion only applies to fields typed ``Optional[str]`` (accepts None and str
  but no boolean/numeric type); boolean and numeric fields must be untouched.
"""

from typing import Any, Dict, Optional

from pydantic import BaseModel

from motet.core.tools.registry import ToolRegistry, _coerce_boolean_null_params


class _BrowserLikeParams(BaseModel):
    """Mirrors the http_get_browser shape that produced the production failure."""

    url: str
    execute_js: Optional[str] = None
    wait_seconds: Optional[int] = None
    full_page: Optional[bool] = None
    label: str = "default"


def test_false_coerced_to_none_for_optional_str() -> None:
    params, changed = _coerce_boolean_null_params(
        _BrowserLikeParams, {"url": "https://cnn.com", "execute_js": False}
    )
    assert params["execute_js"] is None
    assert changed == ["execute_js"]
    # Original url untouched; validation now passes.
    validated = _BrowserLikeParams(**params)
    assert validated.url == "https://cnn.com"
    assert validated.execute_js is None


def test_true_coerced_to_none_for_optional_str() -> None:
    params, changed = _coerce_boolean_null_params(
        _BrowserLikeParams, {"url": "https://cnn.com", "execute_js": True}
    )
    assert params["execute_js"] is None
    assert changed == ["execute_js"]


def test_optional_bool_field_untouched() -> None:
    params, changed = _coerce_boolean_null_params(
        _BrowserLikeParams, {"url": "https://cnn.com", "full_page": True}
    )
    assert params["full_page"] is True
    assert changed == []


def test_optional_int_field_untouched() -> None:
    # bool is an int subclass in Python; an Optional[int] field must keep the
    # boolean and let Pydantic's own lax coercion decide.
    params, changed = _coerce_boolean_null_params(
        _BrowserLikeParams, {"url": "https://cnn.com", "wait_seconds": True}
    )
    assert params["wait_seconds"] is True
    assert changed == []


def test_required_and_defaulted_str_fields_untouched() -> None:
    raw = {"url": False, "label": True}
    params, changed = _coerce_boolean_null_params(_BrowserLikeParams, raw)
    # Neither field accepts None, so both booleans are left for validation to reject.
    assert params["url"] is False
    assert params["label"] is True
    assert changed == []


def test_string_value_untouched() -> None:
    params, changed = _coerce_boolean_null_params(
        _BrowserLikeParams, {"url": "https://cnn.com", "execute_js": "window.scrollTo(0, 9999)"}
    )
    assert params["execute_js"] == "window.scrollTo(0, 9999)"
    assert changed == []


def test_registry_execute_succeeds_with_boolean_for_optional_str() -> None:
    """End-to-end: the exact production failure (execute_js=false) now validates."""
    reg = ToolRegistry()
    seen: Dict[str, Any] = {}

    def _fake_browser(params: Dict[str, Any]) -> Dict[str, Any]:
        seen.update(params)
        return {"status": "success", "text": "page content"}

    reg.register(
        "fake_browser",
        description="fake browser tool",
        func=_fake_browser,
        tool_schema=_BrowserLikeParams,
    )

    result = reg.execute("fake_browser", {"url": "https://cnn.com", "execute_js": False})

    assert result["status"] == "success"
    assert seen["url"] == "https://cnn.com"
    assert seen["execute_js"] is None


def test_registry_execute_still_rejects_boolean_for_required_str() -> None:
    reg = ToolRegistry()

    reg.register(
        "fake_browser2",
        description="fake browser tool",
        func=lambda params: {"status": "success"},
        tool_schema=_BrowserLikeParams,
    )

    result = reg.execute("fake_browser2", {"url": False})

    assert result["status"] == "validation_error"
