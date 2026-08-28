"""
Motet - Qwen Llama.cpp Model Profile

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Qwen-family local model profile. It captures ChatML fallback prompt rendering,
    Qwen3 end-of-turn stop handling, and the ``/no_think`` prompt switch used to
    suppress native reasoning for simple turns.

Dependencies:
    - ChatMLLlamaCppModelProfile: Shared ChatML fallback prompt behavior.

Usage:
    from motet.core.models.local.profiles.qwen import QwenProfile

    profile = QwenProfile()
    messages = profile.apply_thinking_control(messages, enabled=False)

Notes:
    - Reasoning separation still happens in shared parsing helpers; this profile
      only controls the model-family prompt switch that asks Qwen not to reason.
"""

from __future__ import annotations

from typing import Any, Dict, List

from .base import ChatMLLlamaCppModelProfile


class QwenProfile(ChatMLLlamaCppModelProfile):
    """Qwen ChatML profile with optional thinking suppression."""

    family: str = "qwen"
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
            return messages
        msgs = [dict(m) for m in messages]
        for i in range(len(msgs) - 1, -1, -1):
            if msgs[i].get("role") == "user":
                content = str(msgs[i].get("content") or "")
                if "/no_think" not in content:
                    msgs[i] = {**msgs[i], "content": f"{content} /no_think".strip()}
                break
        return msgs
