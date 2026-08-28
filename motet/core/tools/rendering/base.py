"""
Motet - Base Tool Renderer

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Base interface for Tool Renderers.
    Renderers convert ToolInvocation records (from memory) into
    provider-specific Message objects for the LLM context.
"""

from typing import List, Protocol
from ...types import Message
from ..tool_transcripts import ToolInvocation

class ToolRenderer(Protocol):
    def render_assistant_call(self, invocations: List[ToolInvocation]) -> List[Message]:
        """
        Render the assistant message that requested the tools.
        For OpenAI, this is one message with tool_calls=[...].
        """
        ...
        
    def render_tool_results(self, invocations: List[ToolInvocation], artifact_getter) -> List[Message]:
        """
        Render the tool output messages.
        For OpenAI, this is N messages with role='tool'.
        """
        ...


