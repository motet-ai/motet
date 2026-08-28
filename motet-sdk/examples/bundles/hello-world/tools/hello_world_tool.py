"""
Hello World bundle tool (ADR-0071, ADR-0089).

Simple example that validates input and returns a greeting. Registered via
@motet.tool under hello-world.hello_world_tool.
"""

from __future__ import annotations

from typing import Any, Dict

from pydantic import BaseModel, Field


class HelloWorldToolParams(BaseModel):
    """Input for hello_world_tool."""

    name: str = Field(default="World", description="Name to greet")
    shout: bool = Field(default=False, description="Uppercase the greeting if true")


from motet_sdk import get_motet_context, motet


def _fmt(res: Dict[str, Any]) -> str:
    return f"hello_world_tool(message={res.get('message', '?')})"


def _current_task_id() -> Any:
    try:
        ctx = get_motet_context()
        return ctx.task_id if ctx else None
    except Exception:
        return None


@motet.tool(
    description=(
        "Greet someone by name. "
        "Accepts 'name' (string, default 'World') and 'shout' (bool, default false)."
    ),
    name="hello_world_tool",
    schema=HelloWorldToolParams,
    observation_formatter=_fmt,
    category="hello-world",
    cost_class="low",
    keywords=["hello", "greet", "bundle"],
)
def hello_world_tool(params: Dict[str, Any]) -> Dict[str, Any]:
    """Greet by name; optional shout uppercases the message."""
    parsed = HelloWorldToolParams(**(params or {}))
    message = f"Hello, {parsed.name}!"
    if parsed.shout:
        message = message.upper()

    return {"message": message, "bundle": "hello-world", "task_id": _current_task_id()}
