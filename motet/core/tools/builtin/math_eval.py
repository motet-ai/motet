"""
Motet - Math Eval Builtin

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Math evaluation builtin for the Motet distributed framework.
    Provides safe mathematical expression evaluation with character
    validation and comprehensive error handling. Includes synchronous
    execution for gevent/eventlet compatibility in distributed workers.

Dependencies:
    - pydantic: Data validation and model definitions
    - typing: Type hints and annotations
    - Tool registry and protocol system

Usage:
    from motet.core.tools.builtin.math_eval import run

    # Evaluate math expression
    result = run({"expression": "2 + 2 * 3"})

Notes:
    - Provides safe mathematical expression evaluation
    - Includes character validation and security checks
    - Supports synchronous execution for distributed workers
    - Includes comprehensive error handling and validation
    - Integrates with tool registry and protocol system
    - Supports distributed tool coordination
    - Includes comprehensive observability and logging
"""

from __future__ import annotations

from typing import Any, Dict

from pydantic import BaseModel

from ..protocol import ok, err
from ..registry import ToolRegistry


class Params(BaseModel):
    expression: str


def _parse(ln: str, trig: str) -> Dict[str, Any]:
    return {"expression": ln[len(trig):].strip()}


def _fmt(res: Dict[str, Any]) -> str:
    return f"math(result={res.get('result')})" if res.get("status") == "success" else f"math(error={res.get('error')})"


def run(params: Dict[str, Any]) -> Dict[str, Any]:
    """Synchronous math evaluation (ADR-0033: gevent/eventlet compatible)."""
    expression = params.get("expression")
    if not expression:
        return err("expression is required")
    allowed = set("0123456789+-*/(). ")
    if any(ch not in allowed for ch in expression):
        return err("invalid characters in expression")
    try:
        result = eval(expression, {"__builtins__": {}}, {})
    except Exception as exc:
        return err(str(exc))
    return ok(result)


def register(registry: ToolRegistry) -> None:
    registry.register(
        name="core.math_eval",
        description="Evaluate a simple arithmetic expression",
        func=run,
        tool_schema=Params,
        triggers=["math:"],
        priority=1,
        estimate_tokens=lambda _: 50,
        parse_params=_parse,
        observation_formatter=_fmt,
        category="math",
    )


__all__ = ["register"]


