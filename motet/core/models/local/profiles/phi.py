"""
Motet - Phi Llama.cpp Model Profile

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Phi-family local model profile. Phi-4 Mini uses a ChatML-like prompt format
    and an embedded tool template that reads JSON function definitions from a
    ``tools`` field attached to the system message. The system message also
    carries an anti-narration instruction so the model emits tool calls as
    bare JSON instead of announcing them in prose.

Dependencies:
    - ChatMLLlamaCppModelProfile: Shared ChatML fallback prompt behavior.
    - function_definitions_json: Helper for serializing OpenAI-style tool schemas.

Usage:
    from motet.core.models.local.profiles.phi import Phi4Profile

    profile = Phi4Profile()
    messages = profile.apply_tool_schemas(messages, request)

Notes:
    - Phi's template ignores llama.cpp's top-level ``tools=`` channel, so this
      profile suppresses native tool kwargs after injecting schemas into messages.
"""

from __future__ import annotations

from typing import Any, Dict, List

from .base import ChatMLLlamaCppModelProfile, function_definitions_json

# Anti-narration instruction (ADR-0115). phi-4-mini reads the injected schemas
# but then sometimes *announces* the call in prose ("I will use the tool
# core__web_search... Please wait a moment.") instead of emitting it. Prose
# carries no parseable markup, so no recovery path can save the turn. This
# instruction tells the model to emit the call itself, in the bare-JSON-array
# shape phi-4 uses and the reasoning parser already recovers.
_TOOL_EMISSION_INSTRUCTION = (
    "To use a tool, respond with ONLY the tool call as a JSON array, e.g. "
    '[{"name": "tool_name", "arguments": {"param": "value"}}]. '
    "Never announce or describe a tool call in prose (for example \"I will use "
    "the tool...\"), never tell the user to wait, and never promise to call a "
    "tool later - emit the JSON call itself instead. If no tool is needed, "
    "answer normally."
)


class Phi4Profile(ChatMLLlamaCppModelProfile):
    """Phi-4 profile with system-message tool schema injection."""

    family: str = "phi-4"
    chat_format: str | None = "chatml"

    def __init__(self) -> None:
        super().__init__(
            family=self.family,
            chat_format=self.chat_format,
            stops=["<|im_end|>"],
            supports_system=True,
        )

    def apply_tool_schemas(
        self, messages: List[Dict[str, Any]], request: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        payload = function_definitions_json(request.get("tools"))
        if payload is None:
            return messages

        msgs = [dict(m) for m in messages]
        for message in msgs:
            if message.get("role") == "system" and isinstance(message.get("content"), str):
                message["tools"] = payload
                if _TOOL_EMISSION_INSTRUCTION not in message["content"]:
                    message["content"] = (
                        f"{message['content'].strip()}\n\n{_TOOL_EMISSION_INSTRUCTION}".strip()
                    )
                return msgs

        msgs.insert(
            0,
            {
                "role": "system",
                "content": (
                    "You are a helpful assistant with access to tools.\n\n"
                    f"{_TOOL_EMISSION_INSTRUCTION}"
                ),
                "tools": payload,
            },
        )
        return msgs

    def apply_tool_kwargs(self, request: Dict[str, Any], kwargs: Dict[str, Any]) -> None:
        return
