"""
Motet - Tool Rendering Package

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Provides renderers for converting ToolInvocations into provider-specific messages.
"""

from .base import ToolRenderer
from .openai import OpenAIToolRenderer
from .plaintext import PlaintextToolRenderer

def get_renderer(provider_name: str = "openai") -> ToolRenderer:
    provider = (provider_name or "openai").lower().strip()
    if provider in {"openai"}:
        return OpenAIToolRenderer()
    # Default for providers without OpenAI-style tool message schema
    return PlaintextToolRenderer()

__all__ = ["ToolRenderer", "OpenAIToolRenderer", "PlaintextToolRenderer", "get_renderer"]

