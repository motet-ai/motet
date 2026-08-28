"""
Unit tests for tools API invoker error → HTTP status mapping.
"""

from fastapi import HTTPException

from motet.interfaces.api.v1.tools import (
    _http_exception_for_tool_error,
    _invoker_error_message,
)


def test_invoker_error_message_reads_nested_adr0029_envelope() -> None:
    """Invoker completed + nested ADR-0029 error must surface the message."""
    result = {
        "status": "completed",
        "result": {
            "status": "error",
            "data": None,
            "error": {
                "type": "ValueError",
                "message": "Tool 'nonexistent_tool' not found in registry. Available: []",
            },
        },
    }
    msg = _invoker_error_message(result)
    assert msg is not None
    assert "not found in registry" in msg.lower()


def test_invoker_error_message_reads_outer_error_status() -> None:
    result = {"status": "error", "error": {"message": "tool not found"}}
    assert _invoker_error_message(result) == "tool not found"


def test_invoker_error_message_none_on_success() -> None:
    result = {
        "status": "completed",
        "result": {"status": "completed", "data": {"ok": True}},
    }
    assert _invoker_error_message(result) is None


def test_http_exception_maps_unknown_tool_to_404() -> None:
    exc = _http_exception_for_tool_error(
        "Tool 'nonexistent_tool' not found in registry. Available: ['core.file_read']"
    )
    assert isinstance(exc, HTTPException)
    assert exc.status_code == 404
    assert exc.detail == "tool not found"


def test_http_exception_maps_other_failures_to_500() -> None:
    exc = _http_exception_for_tool_error("worker timed out")
    assert isinstance(exc, HTTPException)
    assert exc.status_code == 500
    assert "timed out" in str(exc.detail)
