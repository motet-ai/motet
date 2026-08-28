"""
Calculator bundle command — smoke-test for bundle deployment (ADR-0071, ADR-0089).
"""
from __future__ import annotations

from typing import Any, Dict, Literal

from pydantic import Field

from motet_sdk import BaseCommandData, MotetContext, motet


class CalculateData(BaseCommandData):
    """Input data for the calculator.calculate command."""

    a: float = Field(..., description="First operand")
    b: float = Field(..., description="Second operand")
    operation: Literal["add", "subtract", "multiply", "divide"] = Field(
        default="add",
        description="Arithmetic operation to perform",
    )


@motet.command(timeout_seconds=30)
def calculate(data: CalculateData, motet: MotetContext) -> Dict[str, Any]:
    """
    Perform a simple arithmetic operation.

    Smoke-test command for the calculator bundle.  Validates that:
      - Bundle commands are namespaced and routable (calculator.calculate)
      - MotetContext is accessible during execution
      - Pydantic validation and Literal types work end-to-end
    """
    ops: Dict[str, Any] = {
        "add": data.a + data.b,
        "subtract": data.a - data.b,
        "multiply": data.a * data.b,
        "divide": data.a / data.b if data.b != 0 else None,
    }
    result = ops[data.operation]

    if data.operation == "divide" and data.b == 0:
        return {
            "error": "division by zero",
            "a": data.a,
            "b": data.b,
            "operation": data.operation,
            "result": None,
        }

    return {
        "a": data.a,
        "b": data.b,
        "operation": data.operation,
        "result": result,
        "bundle": "calculator",
        "task_id": motet.task_id,
    }
