"""
Hello World bundle command — smoke test for bundle deployment (ADR-0071, ADR-0089).
"""
from typing import Any, Dict

from pydantic import Field

from motet_sdk import BaseCommandData, MotetContext, motet


class HelloWorldData(BaseCommandData):
    """Input data for the hello-world.hello_world command."""

    name: str = Field(default="World", description="Name to greet")
    shout: bool = Field(default=False, description="Uppercase the greeting if true")


@motet.command(timeout_seconds=30)
def hello_world(data: HelloWorldData, motet: MotetContext) -> Dict[str, Any]:
    """
    Return a greeting message.

    A minimal smoke-test command that verifies bundle fetch, lint, publish,
    and worker reload all completed successfully end-to-end.
    """
    message = f"Hello, {data.name}!"
    if data.shout:
        message = message.upper()
    return {
        "message": message,
        "bundle": "hello-world",
        "version": "0.1.0",
        "task_id": motet.task_id,
    }
