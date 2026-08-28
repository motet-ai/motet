"""
Motet - Llama 3 Llama.cpp Model Profile

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-04

Description:
    Llama 3-family local model profile. It captures Llama 3 chat-template
    boundaries, stop guards, and raw-completion fallback rendering so local GGUF
    inference keeps using the same turn delimiters even when llama.cpp cannot
    apply an embedded chat template.

Dependencies:
    - DefaultLlamaCppModelProfile: Shared llama.cpp profile behavior.
    - message_content_text: Helper for flattening canonical-ish message content.

Usage:
    from motet.core.models.local.profiles.llama3 import Llama3Profile

    profile = Llama3Profile()
    prompt = profile.fallback_prompt([{"role": "user", "content": "hi"}])

Notes:
    - The primary llama.cpp path still prefers the GGUF's embedded Jinja template.
      This profile provides the family-specific safety rails around that path:
      explicit stop sequences and an aligned raw fallback prompt.
"""

from __future__ import annotations

from typing import Any, Dict, List

from .base import DefaultLlamaCppModelProfile, message_content_text

_BEGIN = "<|begin_of_text|>"
_START_HEADER = "<|start_header_id|>"
_END_HEADER = "<|end_header_id|>"
_EOT = "<|eot_id|>"


class Llama3Profile(DefaultLlamaCppModelProfile):
    """Llama 3 profile with native header fallback and stop guards."""

    family: str = "llama-3"
    chat_format: str | None = "llama-3"

    def __init__(self) -> None:
        super().__init__(
            family=self.family,
            chat_format=self.chat_format,
            stops=[_EOT, "<|end_of_text|>"],
            supports_system=True,
        )

    def fallback_prompt(self, messages: List[Dict[str, Any]]) -> str:
        """Render raw completion fallback using Llama 3 header delimiters."""
        prompt_parts = [_BEGIN]
        for msg in messages:
            role = str(msg.get("role") or "user")
            content = message_content_text(msg.get("content"))
            prompt_parts.append(
                f"{_START_HEADER}{role}{_END_HEADER}\n\n{content}{_EOT}"
            )
        prompt_parts.append(f"{_START_HEADER}assistant{_END_HEADER}\n\n")
        return "".join(prompt_parts)
