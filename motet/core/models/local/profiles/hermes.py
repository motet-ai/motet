"""
Motet - Hermes Llama.cpp Model Profile

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-04

Description:
    Hermes-family local model profile for Nous Research Hermes 4 GGUFs. Hermes
    uses ChatML-style turns, explicit ``<think>`` reasoning blocks, and
    ``<tool_call>`` / ``<tool_response>`` tool markup with tool definitions
    supplied in a system-prompt ``<tools>`` block.

Dependencies:
    - json: Serializes OpenAI-style tool schemas for the Hermes prompt contract.
    - ChatMLLlamaCppModelProfile: Shared ChatML fallback prompt behavior.

Usage:
    from motet.core.models.local.profiles.hermes import HermesProfile

    profile = HermesProfile()
    messages = profile.apply_tool_schemas(messages, {"tools": tools})
    messages = profile.apply_thinking_control(messages, enabled=True)

Notes:
    - Tool-call extraction for ``<tool_call>{...}</tool_call>`` blocks is shared
      in ``motet.core.models.local.reasoning``.
    - Thinking control uses the documented system-prompt instruction path rather
      than a tokenizer ``thinking=True`` flag so the llama.cpp local path can
      control reasoning consistently.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from .base import ChatMLLlamaCppModelProfile, message_content_text

_THINKING_PROMPT = (
    "You are a deep thinking AI. When a problem benefits from deliberate "
    "reasoning, enclose your internal reasoning inside <think> </think> tags, "
    "then provide the final answer after the closing tag."
)
_TOOLS_PROMPT = (
    "You are a function-calling AI. Tools are provided inside <tools>...</tools>. "
    "When appropriate, call a tool by emitting a <tool_call>{...}</tool_call> "
    "JSON object with name and arguments. After a tool responds as "
    "<tool_response>, continue and produce the final answer."
)


class HermesProfile(ChatMLLlamaCppModelProfile):
    """Hermes profile with ChatML fallback, thinking, and tool prompt support."""

    family: str = "hermes"
    chat_format: str | None = "chatml"

    def __init__(self) -> None:
        super().__init__(
            family=self.family,
            chat_format=self.chat_format,
            stops=["<|im_end|>"],
            supports_system=True,
        )

    def apply_thinking_control(
        self, messages: List[Dict[str, Any]], enabled: bool
    ) -> List[Dict[str, Any]]:
        if enabled:
            return self._ensure_system_section(messages, _THINKING_PROMPT)
        return self._remove_system_section(messages, _THINKING_PROMPT)

    def apply_tool_schemas(
        self, messages: List[Dict[str, Any]], request: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        payload = self._tool_definitions_json(request.get("tools"))
        if payload is None:
            return messages

        section = f"{_TOOLS_PROMPT}\n<tools>\n{payload}\n</tools>"
        return self._ensure_system_section(messages, section)

    def apply_tool_kwargs(self, request: Dict[str, Any], kwargs: Dict[str, Any]) -> None:
        return

    def _ensure_system_section(
        self, messages: List[Dict[str, Any]], section: str
    ) -> List[Dict[str, Any]]:
        msgs = [dict(m) for m in messages]
        for idx, message in enumerate(msgs):
            if message.get("role") != "system":
                continue
            content = message_content_text(message.get("content"))
            if section in content:
                return msgs
            joined = "\n\n".join(part for part in [content.strip(), section] if part)
            msgs[idx] = {**message, "content": joined}
            return msgs

        return [{"role": "system", "content": section}, *msgs]

    def _remove_system_section(
        self, messages: List[Dict[str, Any]], section: str
    ) -> List[Dict[str, Any]]:
        msgs = [dict(m) for m in messages]
        for idx, message in enumerate(msgs):
            if message.get("role") != "system":
                continue
            content = message_content_text(message.get("content"))
            if section not in content:
                return msgs
            content = content.replace(f"\n\n{section}", "").replace(section, "").strip()
            msgs[idx] = {**message, "content": content}
            return msgs
        return msgs

    def _tool_definitions_json(self, tools: Any) -> Optional[str]:
        if not tools:
            return None
        try:
            return "\n".join(json.dumps(tool) for tool in tools)
        except (TypeError, ValueError):
            return None
