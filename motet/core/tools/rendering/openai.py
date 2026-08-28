"""
Motet - OpenAI Tool Renderer

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-13

Description:
    Renders ToolInvocations into OpenAI-compatible tool messages.
    Supports batched tool calls (multi-tool assistant messages).
"""

import json
from typing import List, Callable, Optional, Any
from ...types import Message, Role
from ..tool_transcripts import ToolInvocation, ToolInvocationStatus
from .base import ToolRenderer

class OpenAIToolRenderer(ToolRenderer):
    """
    Renders transcripts for OpenAI (and compatible) providers.
    Uses 'tool_calls' in assistant message and role='tool' for results.
    """
    
    def render_assistant_call(self, invocations: List[ToolInvocation]) -> List[Message]:
        if not invocations:
            return []
            
        from motet.core.types import ToolCallRequest

        calls = [
            ToolCallRequest(
                call_id=inv.tool_call_id,
                tool_name=inv.tool_name,
                arguments_json=inv.arguments_json or "{}",
            )
            for inv in invocations
        ]
        return [Message(
            role=Role.assistant,
            content="",  # Often empty for tool calls, but could be thought trace
            tool_calls_canonical=calls,
        )]

    def render_tool_results(
        self, 
        invocations: List[ToolInvocation], 
        artifact_getter: Callable[[str], Optional[Any]]
    ) -> List[Message]:
        messages = []
        max_output_chars = 8000  # hard cap to avoid context blowups (ADR-0061 token budgeting guidance)
        
        for inv in invocations:
            # Skip STARTED status - only render final statuses (SUCCESS, ERROR, AUTH_REQUIRED)
            # STARTED status is intermediate and should not appear in conversation history
            if inv.status == ToolInvocationStatus.STARTED:
                continue
            
            content = ""
            
            # Determine content
            if inv.status == ToolInvocationStatus.SUCCESS:
                # Try to get full artifact, fall back to preview
                if inv.artifact_id:
                    payload = artifact_getter(inv.artifact_id)
                    if payload is not None:
                        content = self._format_payload(payload)
                    else:
                        content = inv.preview_observation or "Result available but not loaded."
                else:
                    content = inv.preview_observation or "Tool executed successfully."
            elif inv.status == ToolInvocationStatus.ERROR:
                content = f"Error: {inv.error_summary or 'Unknown error'}"
            elif inv.status == ToolInvocationStatus.AUTH_REQUIRED:
                content = "Authorization required to proceed."
            else:
                # Fallback for unknown statuses (shouldn't happen after filtering)
                content = f"Tool status: {inv.status}"

            # ADR-0061: Hard cap rendered content to avoid injecting huge artifacts into model context.
            if isinstance(content, str) and len(content) > max_output_chars:
                suffix = ""
                if inv.artifact_id:
                    suffix = f"\n...[truncated, full result in artifact_id={inv.artifact_id}]"
                else:
                    suffix = "\n...[truncated]"
                budget = max(0, max_output_chars - len(suffix))
                content = content[:budget] + suffix

            messages.append(Message(
                role=Role.tool,
                content=content,
                tool_call_id=inv.tool_call_id,
                name=inv.tool_name
            ))
            
        return messages

    def _format_payload(self, payload: Any) -> str:
        """
        Format tool payload for LLM consumption.
        
        Handles MCP tool results with content arrays, nested structures, and plain text.
        Returns properly formatted JSON or text that the LLM can parse.
        """
        if payload is None:
            return ""
        
        # Handle string results (already formatted)
        if isinstance(payload, str):
            # Check if it's a JSON string that needs parsing
            try:
                parsed = json.loads(payload)
                return self._format_payload(parsed)  # Recursively format parsed JSON
            except (json.JSONDecodeError, ValueError):
                # Plain string, return as-is
                return payload
        
        # Handle dict results (MCP format)
        if isinstance(payload, dict):
            # Priority 1: MCP content array format
            if "content" in payload:
                content_items = payload["content"]
                if isinstance(content_items, list):
                    # Extract text from all content items
                    text_parts = []
                    for item in content_items:
                        if isinstance(item, dict):
                            if item.get("type") == "text" and "text" in item:
                                text_parts.append(str(item["text"]))
                            elif "text" in item:
                                text_parts.append(str(item["text"]))
                        elif isinstance(item, str):
                            text_parts.append(item)
                    
                    if text_parts:
                        return "\n".join(text_parts)
            
            # Priority 2: structuredContent.result (MCP normalized format)
            if "structuredContent" in payload:
                structured = payload["structuredContent"]
                if isinstance(structured, dict) and "result" in structured:
                    result_value = structured["result"]
                    if isinstance(result_value, str):
                        return result_value
                    elif isinstance(result_value, dict):
                        if "text" in result_value:
                            return str(result_value["text"])
                        # Fall through to JSON stringification
            
            # Priority 3: Direct text/result fields
            if "text" in payload:
                text_value = payload["text"]
                if isinstance(text_value, str):
                    return text_value
                # If text is a dict, try to extract meaningful content
                if isinstance(text_value, dict):
                    return json.dumps(text_value, indent=2, ensure_ascii=False)
            
            if "result" in payload:
                result_value = payload["result"]
                if isinstance(result_value, str):
                    return result_value
                # If result is a dict, format it properly
                if isinstance(result_value, dict):
                    return json.dumps(result_value, indent=2, ensure_ascii=False)
            
            # Fallback: JSON stringify the entire dict (properly formatted)
            return json.dumps(payload, indent=2, ensure_ascii=False, default=str)
        
        # Handle list results
        if isinstance(payload, list):
            # If it's a list of content items (MCP format)
            if payload and isinstance(payload[0], dict) and "type" in payload[0]:
                text_parts = []
                for item in payload:
                    if isinstance(item, dict) and item.get("type") == "text" and "text" in item:
                        text_parts.append(str(item["text"]))
                if text_parts:
                    return "\n".join(text_parts)
            # Otherwise, JSON stringify
            return json.dumps(payload, indent=2, ensure_ascii=False, default=str)
        
        # Fallback: Convert to string
        return str(payload)

