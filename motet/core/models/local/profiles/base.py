"""
Motet - Llama.cpp Model Profile Base

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-04

Description:
    Protocol and shared implementation for llama.cpp model family profiles.
    Profiles isolate GGUF/Jinja chat template behavior from LocalInferenceManager
    so the manager can stay focused on Redis orchestration, batching, model
    execution, and response publication.

Dependencies:
    - typing: Protocol definitions and structured type annotations.
    - json: Tool schema serialization for profile-specific prompt injection.
    - motet.core.models.local.reasoning: Shared text reasoning and tool-call parsers.

Usage:
    from motet.core.models.local.profiles.registry import profile_for_model

    profile = profile_for_model("qwen3-8b-instruct")
    messages = profile.normalize_messages(request["messages"])
    messages = profile.apply_thinking_control(messages, enabled=False)

Notes:
    - Profiles operate on the local-manager request dictionaries, not provider
      wire formats. The LocalAdapter remains responsible for canonical protocol
      mapping at the provider boundary.
    - The default llama.cpp profile preserves current generic behavior and is
      subclassed by family profiles for small, explicit deviations.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional, Protocol, Tuple, cast

from motet.core.models.local.reasoning import extract_tool_calls_from_text


def message_content_text(content: Any) -> str:
    """Best-effort text extraction for fallback prompts and tool responses."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
        if parts:
            return "\n".join(part for part in parts if part)
    return str(content or "")


class LlamaCppModelProfile(Protocol):
    """Family-specific llama.cpp/GGUF protocol behavior."""

    family: str
    chat_format: Optional[str]

    def supports_system_role(self) -> bool:
        """Return whether the model template accepts native system turns."""
        ...

    def stop_sequences(self) -> List[str]:
        """Return model-family end-of-turn stop sequences."""
        ...

    def normalize_messages(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Normalize canonical-ish chat messages before llama.cpp templating."""
        ...

    def apply_thinking_control(
        self, messages: List[Dict[str, Any]], enabled: bool
    ) -> List[Dict[str, Any]]:
        """Apply family-specific thinking controls."""
        ...

    def apply_tool_schemas(
        self, messages: List[Dict[str, Any]], request: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Attach tool schemas where this family template expects them."""
        ...

    def apply_tool_kwargs(self, request: Dict[str, Any], kwargs: Dict[str, Any]) -> None:
        """Forward or suppress llama.cpp top-level tool kwargs."""
        ...

    def extract_tool_calls(
        self, text: str, tool_names: Optional[Iterable[str]] = None
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """Recover text-emitted tool calls from model output."""
        ...

    def fallback_prompt(self, messages: List[Dict[str, Any]]) -> str:
        """Render a raw-completion fallback prompt."""
        ...


class DefaultLlamaCppModelProfile:
    """Default llama.cpp behavior for OpenAI-compatible template paths."""

    family = "default"
    chat_format: Optional[str] = None

    def __init__(
        self,
        *,
        family: Optional[str] = None,
        chat_format: Optional[str] = None,
        stops: Optional[List[str]] = None,
        supports_system: bool = True,
    ) -> None:
        if family is not None:
            self.family = family
        self.chat_format = chat_format
        self._stops = list(stops or [])
        self._supports_system = supports_system

    def supports_system_role(self) -> bool:
        return self._supports_system

    def stop_sequences(self) -> List[str]:
        return list(self._stops)

    def normalize_messages(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        msgs = [dict(m) for m in (messages or [])]
        if self.supports_system_role():
            return msgs
        return self._fold_system_and_collapse_roles(msgs)

    def apply_thinking_control(
        self, messages: List[Dict[str, Any]], enabled: bool
    ) -> List[Dict[str, Any]]:
        return messages

    def apply_tool_schemas(
        self, messages: List[Dict[str, Any]], request: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        return messages

    def apply_tool_kwargs(self, request: Dict[str, Any], kwargs: Dict[str, Any]) -> None:
        tools = request.get("tools")
        if not tools:
            return
        kwargs["tools"] = tools
        tool_choice = request.get("tool_choice")
        if tool_choice:
            kwargs["tool_choice"] = tool_choice

    def extract_tool_calls(
        self, text: str, tool_names: Optional[Iterable[str]] = None
    ) -> Tuple[str, List[Dict[str, Any]]]:
        return cast(
            Tuple[str, List[Dict[str, Any]]],
            extract_tool_calls_from_text(text, tool_names=tool_names),
        )

    def fallback_prompt(self, messages: List[Dict[str, Any]]) -> str:
        prompt_parts = []
        for msg in messages:
            role = msg.get("role", "user")
            content = message_content_text(msg.get("content", ""))
            prompt_parts.append(f"{role.capitalize()}: {content}")
        return "\n".join(prompt_parts) + "\nAssistant:"

    def _fold_system_and_collapse_roles(
        self, messages: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Fold system turns for strict-alternation templates."""
        pending_system: List[str] = []
        folded: List[Dict[str, Any]] = []
        for message in messages:
            role = message.get("role", "user")
            content = message.get("content")
            if not isinstance(content, str):
                folded.append(message)
                continue
            if role == "system":
                if content.strip():
                    pending_system.append(content.strip())
                continue
            if role == "user" and pending_system:
                prefix = "\n\n".join(pending_system)
                message = {
                    **message,
                    "content": f"{prefix}\n\n{content}".strip() if content else prefix,
                }
                pending_system = []
            folded.append(message)
        if pending_system:
            folded.insert(0, {"role": "user", "content": "\n\n".join(pending_system)})

        collapsed: List[Dict[str, Any]] = []
        for message in folded:
            if (
                collapsed
                and collapsed[-1].get("role") == message.get("role")
                and isinstance(collapsed[-1].get("content"), str)
                and isinstance(message.get("content"), str)
            ):
                previous = collapsed[-1]
                previous["content"] = (
                    f"{previous.get('content') or ''}\n\n{message.get('content') or ''}".strip()
                )
            else:
                collapsed.append(dict(message))
        return collapsed


class ChatMLLlamaCppModelProfile(DefaultLlamaCppModelProfile):
    """Fallback prompt behavior for ChatML-style local families."""

    def fallback_prompt(self, messages: List[Dict[str, Any]]) -> str:
        prompt_parts = []
        for msg in messages:
            role = msg.get("role", "user")
            content = message_content_text(msg.get("content", ""))
            prompt_parts.append(f"<|im_start|>{role}\n{content}<|im_end|>")
        prompt_parts.append("<|im_start|>assistant\n")
        return "\n".join(prompt_parts)


def function_definitions_json(tools: Any) -> Optional[str]:
    """Serialize OpenAI-style function tool definitions for prompt injection."""
    if not tools:
        return None
    functions: List[Any] = []
    for tool in tools:
        if isinstance(tool, dict):
            fn = tool.get("function")
            functions.append(fn if isinstance(fn, dict) else tool)
        else:
            functions.append(tool)
    try:
        return json.dumps(functions)
    except (TypeError, ValueError):
        return None
