"""
Motet - No-Tools Turn Reply

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-26

Description:
    One model call with an empty tool list. Used when the turn gate marks a
    greeting or acknowledgement, or when the caller forces ``no_tools``.
    Not a command and not a second executor — just the reply path next to
    ``run_agent``.

Dependencies:
    - motet.core.commands.builtin.model.model_stream: the streamed model call
    - motet.core.types.Message: system brief plus the turn history
    - motet.core.reasoning.react.agent_data: shared model fallback constants

Usage:
    from motet.core.orchestration.turn.no_tools import answer_without_tools

    result = answer_without_tools(
        motet,
        messages=history,
        reason="trivial",
        provider=provider,
        model_name=model_name,
    )

Notes:
    - ``reason="trivial"`` is a greeting/ack from the turn gate. Unset reason
      is an explicit caller force (safety / tests).
    - Honors the turn's provider and model. A hardcoded fallback would
      silently switch vendors on local-model greetings.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from motet.core.commands.builtin.model import model_stream
from motet.core.commands.command_data_classes import ModelStreamData
from motet.core.reasoning.react.agent_data import (
    DEFAULT_MODEL_NAME,
    DEFAULT_MODEL_PROVIDER,
)
from motet.core.types import Message, OutputContract


def no_tools_system_prompt(reason: Optional[str]) -> str:
    """System brief for the no_tools path."""
    if reason == "trivial":
        return (
            "Reply briefly. Do not use tools. Do not start a plan or a "
            "longer explanation."
        )
    return "You are Motet's assistant. Reply without using any tools."


def answer_without_tools(
    motet: Any,
    *,
    messages: List[Any],
    reason: Optional[str] = None,
    provider: str = DEFAULT_MODEL_PROVIDER,
    model_name: str = DEFAULT_MODEL_NAME,
    output_contract: Optional[OutputContract] = None,
) -> Dict[str, Any]:
    """Stream one reply with no tools. Returns content, usage, and finish_reason."""
    result = motet.do(
        model_stream,
        data=ModelStreamData(
            messages=[
                Message(
                    role="system",
                    content=no_tools_system_prompt(reason),
                    metadata={"source": "no_tools", "cache_volatile": True},
                ),
                *messages,
            ],
            tools=[],
            stream_key=motet.stream_key,
            output_contract=output_contract,
            model_settings={
                "provider": provider,
                "model_name": model_name,
            },
        ),
    )

    usage = {
        "prompt_tokens": int(result.get("prompt_tokens") or 0),
        "completion_tokens": int(result.get("completion_tokens") or 0),
        "total_tokens": int(result.get("total_tokens") or 0),
        "cache_read_tokens": int(result.get("cache_read_tokens") or 0),
        "cache_creation_tokens": int(result.get("cache_creation_tokens") or 0),
        "reasoning_tokens": int(result.get("reasoning_tokens") or 0),
    }
    if not usage["total_tokens"]:
        usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
    return {
        "content": result.get("final_content", ""),
        "finish_reason": result.get("finish_reason", "stop"),
        "usage": usage,
    }
