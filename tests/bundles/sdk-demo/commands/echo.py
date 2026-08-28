"""
Echo command using motet_sdk (ADR-0080, ADR-0089).
"""
from __future__ import annotations

from typing import Any, Dict

from pydantic import BaseModel, Field

from motet_sdk import MotetContext, WorkerCapability, motet


class EchoData(BaseModel):
    """Input for sdk-demo.echo."""

    message: str = Field(..., description="Message to echo back")


@motet.command(
    timeout_seconds=30,
    required_capabilities=[WorkerCapability.TOOL_EXECUTION],
)
def echo(data: EchoData, motet: MotetContext) -> Dict[str, Any]:
    """Echo the message and include task/conversation context from motet."""
    return {
        "echo": data.message,
        "task_id": motet.task_id,
        "conversation_id": motet.conversation_id,
        "command_id": motet.command_id,
        "bundle": "sdk-demo",
    }
