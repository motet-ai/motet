"""
Motet - Plaintext Tool Renderer

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Provider-agnostic tool transcript renderer that emits tool call transcripts as
    plain assistant text (no role="tool", no assistant.tool_calls).

    This renderer is used for providers that do not support OpenAI-style tool messages,
    or where emitting role="tool" would violate provider schema constraints.

Dependencies:
    - motet.core.types: Message, Role
    - motet.core.tools.tool_transcripts: ToolInvocation

Usage:
    from motet.core.tools.rendering import get_renderer
    renderer = get_renderer(provider_name="anthropic")  # returns PlaintextToolRenderer

Notes:
    - This renderer intentionally does not use tool_call_id semantics in the message schema.
    - Payload content should be capped upstream to respect model token budgets.
"""

from __future__ import annotations

from typing import Any, Callable, List, Optional

from ...types import Message, Role
from ..tool_transcripts import ToolInvocation, ToolInvocationStatus
from .base import ToolRenderer


class PlaintextToolRenderer(ToolRenderer):
    """
    Render tool transcripts as plain assistant messages.

    The transcript is split into two assistant messages:
    - one describing the tool call(s)
    - one describing the tool result(s)
    """

    def render_assistant_call(self, invocations: List[ToolInvocation]) -> List[Message]:
        if not invocations:
            return []

        lines: List[str] = []
        if len(invocations) == 1:
            inv = invocations[0]
            lines.append(f"Tool call: {inv.tool_name}")
            lines.append(f"Arguments: {inv.arguments_json}")
        else:
            lines.append(f"Tool calls ({len(invocations)}):")
            for inv in invocations:
                lines.append(f"- {inv.tool_name}: {inv.arguments_json}")

        return [Message(role=Role.assistant, content="\n".join(lines))]

    def render_tool_results(
        self,
        invocations: List[ToolInvocation],
        artifact_getter: Callable[[str], Optional[Any]],
    ) -> List[Message]:
        if not invocations:
            return []

        lines: List[str] = []
        if len(invocations) == 1:
            inv = invocations[0]
            lines.append(f"Tool result: {inv.tool_name}")
            lines.append(self._render_one(inv, artifact_getter))
        else:
            lines.append(f"Tool results ({len(invocations)}):")
            for inv in invocations:
                lines.append(f"- {inv.tool_name}: {self._render_one(inv, artifact_getter)}")

        return [Message(role=Role.assistant, content="\n".join(lines))]

    def _render_one(self, inv: ToolInvocation, artifact_getter: Callable[[str], Optional[Any]]) -> str:
        if inv.status == ToolInvocationStatus.STARTED:
            # Upstream should have filtered STARTED-only records; keep a safe fallback.
            return "Tool execution incomplete."

        if inv.status == ToolInvocationStatus.ERROR:
            return f"Error: {inv.error_summary or 'Unknown error'}"

        if inv.status == ToolInvocationStatus.AUTH_REQUIRED:
            return inv.preview_observation or "Authorization required to proceed."

        # SUCCESS
        if inv.artifact_id:
            payload = artifact_getter(inv.artifact_id)
            if payload is not None:
                # Keep it readable; upstream renderer caps should apply elsewhere if needed.
                return str(payload)

        return inv.preview_observation or "Tool executed successfully."


