"""
Calculator bundle math tool (ADR-0071, ADR-0089).

Simple example registered via @motet.tool under calculator.math_tool.
"""
from __future__ import annotations

from typing import Any, Dict, Literal

from pydantic import BaseModel, Field


class MathToolParams(BaseModel):
    """Input for math_tool."""

    a: float = Field(..., description="First operand")
    b: float = Field(..., description="Second operand")
    operation: Literal["add", "subtract", "multiply", "divide"] = Field(
        default="add",
        description="Arithmetic operation: add, subtract, multiply, divide",
    )


from motet_sdk import get_motet_context, motet


def _fmt(res: Dict[str, Any]) -> str:
    return f"math_tool({res.get('a')} {res.get('operation')} {res.get('b')} = {res.get('result')})"


def _current_task_id() -> Any:
    try:
        ctx = get_motet_context()
        return ctx.task_id if ctx else None
    except Exception:
        return None


@motet.tool(
    description=(
        "Perform arithmetic on two numbers. "
        "Accepts 'a', 'b', and 'operation' (add/subtract/multiply/divide)."
    ),
    name="math_tool",
    schema=MathToolParams,
    observation_formatter=_fmt,
    category="calculator",
    cost_class="low",
    keywords=["math", "arithmetic", "calculate", "bundle"],
)
def math_tool(params: Dict[str, Any]) -> Dict[str, Any]:
    parsed = MathToolParams(**(params or {}))
    if parsed.operation == "divide" and parsed.b == 0:
        return {
            "a": parsed.a,
            "b": parsed.b,
            "operation": parsed.operation,
            "result": None,
            "error": "division by zero",
            "task_id": _current_task_id(),
        }

    operations = {
        "add": parsed.a + parsed.b,
        "subtract": parsed.a - parsed.b,
        "multiply": parsed.a * parsed.b,
        "divide": parsed.a / parsed.b,
    }
    return {
        "a": parsed.a,
        "b": parsed.b,
        "operation": parsed.operation,
        "result": operations[parsed.operation],
        "task_id": _current_task_id(),
    }
