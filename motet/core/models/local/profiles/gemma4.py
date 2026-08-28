"""
Motet - Gemma 4 Llama.cpp Model Profile

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-04

Description:
    Gemma 4 local model profile for Google Gemma 4 instruction GGUFs. It handles
    the family-specific tool-call handshake, including a compatibility conversion
    for Motet's multi-call tool loop and Gemma 4's system-prompt ``<|think|>``
    control token.

Dependencies:
    - json: Decodes canonical tool-call argument strings and tool output payloads.
    - DefaultLlamaCppModelProfile: Shared llama.cpp profile behavior.

Usage:
    from motet.core.models.local.profiles.gemma4 import Gemma4Profile

    profile = Gemma4Profile()
    messages = profile.normalize_messages(messages)

Notes:
    - Gemma 4's embedded template can render ``tool_responses`` inside the same
      model turn. Motet's orchestration resumes generation in a second inference
      call, so completed tool outputs are converted into a follow-up user turn to
      force a normal model-generation prompt and avoid empty continuations.
    - Gemma 4 enables thinking by placing ``<|think|>`` at the beginning of the
      system prompt. The shared reasoning parser separates the emitted
      ``<|channel>thought`` block from user-facing text.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from .base import DefaultLlamaCppModelProfile, message_content_text

_THINK_TOKEN = "<|think|>"


class Gemma4Profile(DefaultLlamaCppModelProfile):
    """Gemma 4 profile with native tool calls and second-call tool result bridging."""

    family: str = "gemma-4"
    chat_format: str | None = "gemma"

    def __init__(self) -> None:
        super().__init__(
            family=self.family,
            chat_format=self.chat_format,
            stops=["<|turn|>", "<|tool_response|>"],
            supports_system=True,
        )

    def normalize_messages(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return self._normalize_tool_result_turns(messages)

    def apply_thinking_control(
        self, messages: List[Dict[str, Any]], enabled: bool
    ) -> List[Dict[str, Any]]:
        msgs = [dict(m) for m in messages]
        system_idx = next(
            (i for i, message in enumerate(msgs) if message.get("role") == "system"),
            None,
        )

        if enabled:
            if system_idx is None:
                return [{"role": "system", "content": _THINK_TOKEN}, *msgs]

            content = message_content_text(msgs[system_idx].get("content"))
            stripped = content.lstrip()
            if not stripped.startswith(_THINK_TOKEN):
                prefix_gap = content[: len(content) - len(stripped)]
                new_content = f"{prefix_gap}{_THINK_TOKEN}\n{stripped or content}".strip()
                msgs[system_idx] = {**msgs[system_idx], "content": new_content}
            return msgs

        if system_idx is None:
            return msgs

        content = message_content_text(msgs[system_idx].get("content"))
        stripped = content.lstrip()
        if stripped.startswith(_THINK_TOKEN):
            leading = content[: len(content) - len(stripped)]
            stripped = stripped[len(_THINK_TOKEN) :].lstrip("\n ")
            msgs[system_idx] = {**msgs[system_idx], "content": f"{leading}{stripped}".strip()}
        return msgs

    def _normalize_tool_result_turns(
        self, messages: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        normalized: List[Dict[str, Any]] = []
        call_id_to_name: Dict[str, str] = {}

        for message in [dict(m) for m in (messages or [])]:
            role = message.get("role")
            if role == "assistant":
                raw_calls = message.get("tool_calls") or []
                if raw_calls:
                    converted_calls: List[Dict[str, Any]] = []
                    for raw_call in raw_calls:
                        if not isinstance(raw_call, dict):
                            continue
                        converted = self._tool_call_dict(raw_call)
                        converted_calls.append(converted)
                        call_id = converted.get("id")
                        name = converted.get("function", {}).get("name")
                        if call_id and name:
                            call_id_to_name[str(call_id)] = str(name)
                    message["tool_calls"] = converted_calls
                normalized.append(message)
                continue

            if role == "tool":
                if not normalized or normalized[-1].get("role") != "assistant":
                    normalized.append(message)
                    continue

                call_id = message.get("tool_call_id") or message.get("call_id")
                tool_name = (
                    message.get("name")
                    or message.get("tool_name")
                    or (call_id_to_name.get(str(call_id)) if call_id else None)
                    or "unknown"
                )
                normalized.append(
                    {
                        "role": "user",
                        "content": self._tool_result_user_content(
                            str(tool_name),
                            self._tool_response_payload(message.get("content")),
                        ),
                    }
                )
                continue

            normalized.append(message)

        return normalized

    def _tool_call_dict(self, tool_call: Dict[str, Any]) -> Dict[str, Any]:
        fn = tool_call.get("function")
        if isinstance(fn, dict):
            name = fn.get("name") or tool_call.get("name")
            arguments = fn.get("arguments", {})
        else:
            name = tool_call.get("tool_name") or tool_call.get("name")
            arguments = tool_call.get("arguments", tool_call.get("arguments_json", {}))

        if isinstance(arguments, str):
            try:
                parsed = json.loads(arguments)
                arguments = parsed if isinstance(parsed, dict) else arguments
            except (TypeError, ValueError):
                pass

        out: Dict[str, Any] = {
            "type": "function",
            "function": {
                "name": str(name or ""),
                "arguments": arguments if arguments is not None else {},
            },
        }
        call_id = tool_call.get("id") or tool_call.get("call_id") or tool_call.get("tool_call_id")
        if call_id:
            out["id"] = str(call_id)
        return out

    def _tool_response_payload(self, content: Any) -> Any:
        text = message_content_text(content)
        try:
            return json.loads(text)
        except (TypeError, ValueError):
            return {"result": text}

    def _tool_result_user_content(self, tool_name: str, payload: Any) -> str:
        return (
            f"Tool {tool_name} returned: {json.dumps(payload, sort_keys=True)}. "
            "Use this tool result to answer the original user request. "
            "Do not call the same tool again unless more information is required."
        )
