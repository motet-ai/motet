import asyncio

from motet.core.tools import registry


def test_math_eval():
    # execute() is sync; tool may be registered as core.math_eval
    out = registry.execute("core.math_eval", {"expression": "2*(3+4)"})
    if out.get("status") == "not_found":
        out = registry.execute("math_eval", {"expression": "2*(3+4)"})
    assert out.get("status") != "not_found", out.get("error", "tool not found")
    assert out.get("result") == 14


def test_execute_and_execute_tool_only_same_shape():
    """ADR-0084: execute() delegates to _execute_tool_only(); both return same contract."""
    params = {"expression": "1+1"}
    out_execute = registry.execute("core.math_eval", params)
    out_inner = registry._execute_tool_only("core.math_eval", params)
    if out_execute.get("status") == "not_found":
        out_execute = registry.execute("math_eval", params)
        out_inner = registry._execute_tool_only("math_eval", params)
    assert out_execute.get("status") == out_inner.get("status")
    assert out_execute.get("result") == out_inner.get("result")


