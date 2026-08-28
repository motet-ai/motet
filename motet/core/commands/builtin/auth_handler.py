"""
Motet - Auth Required Handler

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Shared utility for handling OAuth auth_required responses from tool execution
    across all reasoning strategies. Provides DRY functions for:
    - Detecting auth_required in tool results
    - Streaming auth events to UI
    - Formatting conversation history updates
    - Creating standardized early return responses

    Shared OAuth auth_required handling for every reasoning strategy after tool
    execution.

Dependencies:
    - structlog: Structured logging
    - MotetContext: For streaming events and accessing context

Usage:
    from motet.core.commands.builtin.auth_handler import (
        check_auth_required, handle_auth_required, AuthRequiredResult
    )
    
    # In any reasoning strategy after tool execution:
    tool_result = motet.do(tool_execution, data=tool_data)
    
    if check_auth_required(tool_result):
        return handle_auth_required(
            tool_result=tool_result,
            tool_name="mcp.google_workspace.gmail_send",
            tool_call_id="call_123",
            motet=motet,
            iteration=current_iteration,
            strategy_name="agentic_loop"
        )

Notes:
    - Used by all reasoning strategies (auto, agentic, no_tools)
    - Integrates with OAuth prompt flow
    - Streams events for real-time UI updates
    - Provides consistent user experience across strategies
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Type-only import to avoid circular import at runtime.
    from motet.core.commands.decorator import MotetContext

import json
import structlog
from dataclasses import dataclass
from typing import Dict, Any, Optional, List

logger = structlog.get_logger(__name__)


@dataclass
class AuthRequiredResult:
    """
    Structured result when OAuth authorization is required.
    
    Contains all information needed by reasoning strategies to:
    - Stop the current chain/iteration
    - Inform the user about required authorization
    - Provide authorization endpoint for UI
    """
    service_id: str
    display_name: str
    message: str
    authorization_endpoint: str
    required_scopes: List[str]
    tool_name: str
    tool_call_id: str
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "service_id": self.service_id,
            "display_name": self.display_name,
            "message": self.message,
            "authorization_endpoint": self.authorization_endpoint,
            "required_scopes": self.required_scopes,
            "tool_name": self.tool_name,
            "tool_call_id": self.tool_call_id
        }
    
    def to_user_message(self) -> str:
        """Format as user-friendly message for conversation history."""
        return f"⚠️ {self.message}\n\nPlease authorize {self.display_name} to continue."
    
    def to_reasoning_response(self, iterations_used: int = 0) -> Dict[str, Any]:
        """
        Format as reasoning strategy response for early return.
        
        Args:
            iterations_used: Number of iterations completed before auth required
            
        Returns:
            Standardized response dict for reasoning strategies
        """
        return {
            "final_response": self.to_user_message(),
            "tool_results": [{
                "tool_call_id": self.tool_call_id,
                "tool_name": self.tool_name,
                "result": self.to_dict(),
                "status": "auth_required"
            }],
            "iterations_used": iterations_used,
            "stop_reason": "auth_required",
            "auth_required": True,
            "service_id": self.service_id,
            "display_name": self.display_name,
            "authorization_endpoint": self.authorization_endpoint,
            "required_scopes": self.required_scopes
        }


def check_auth_required(tool_result: Any) -> bool:
    """
    Check if tool result indicates OAuth authorization is required.
    
    Handles multiple result formats:
    - Top-level auth_required flag (after motet.do() unwrapping)
    - Nested result.auth_required (direct tool_execution calls)
    - Nested data.auth_required (legacy format)
    
    Args:
        tool_result: Result from tool_execution (may be wrapped or unwrapped)
        
    Returns:
        True if authorization is required, False otherwise
    """
    if not isinstance(tool_result, dict):
        return False
    
    # Check top-level (after motet.do()/motet.join() unwrapping)
    if tool_result.get("auth_required") is True:
        return True
    
    # Check nested 'result' field (for backward compatibility)
    nested_result = tool_result.get("result", {})
    if isinstance(nested_result, dict) and nested_result.get("auth_required") is True:
        return True
    
    # Check nested 'data' field (legacy format)
    nested_data = tool_result.get("data", {})
    if isinstance(nested_data, dict) and nested_data.get("auth_required") is True:
        return True
    
    return False


def extract_auth_info(tool_result: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extract auth_required info from tool result.
    
    Handles nested structures and provides defaults for missing fields.
    
    Supported structures:
    - Direct: {"service_id": "...", "display_name": "...", "auth_required": True}
    - Nested auth_info: {"auth_required": True, "auth_info": {"service_id": "...", ...}}
    - Nested result: {"result": {"auth_required": True, "service_id": "...", ...}}
    - Nested data: {"data": {"auth_required": True, "service_id": "...", ...}}
    
    Args:
        tool_result: Tool result containing auth_required info
        
    Returns:
        Dict with service_id, display_name, message, authorization_endpoint, required_scopes
    """
    # Try to find the auth info in various locations
    auth_info = tool_result
    
    # Priority 1: Check for nested "auth_info" field (from tool discovery / execution helpers)
    if isinstance(tool_result.get("auth_info"), dict):
        auth_info = tool_result["auth_info"]
    # Priority 2: Check nested "data.result" field (from tool_execution command response)
    elif isinstance(tool_result.get("data"), dict):
        data = tool_result["data"]
        if isinstance(data.get("result"), dict) and data["result"].get("auth_required"):
            auth_info = data["result"]
        # Priority 2b: Check nested "data" field directly
        elif data.get("auth_required"):
            auth_info = data
    # Priority 3: Check nested "result" field (top-level)
    elif isinstance(tool_result.get("result"), dict) and tool_result["result"].get("auth_required"):
        auth_info = tool_result["result"]
    # Priority 4: Use top-level (direct auth_required response)
    # auth_info already set to tool_result
    
    service_id = auth_info.get("service_id", "unknown")
    display_name = auth_info.get("display_name", service_id)
    
    return {
        "service_id": service_id,
        "display_name": display_name,
        "message": auth_info.get("message", f"{display_name} requires authorization"),
        "authorization_endpoint": auth_info.get("authorization_endpoint", f"/api/v1/oauth/{service_id}/initiate"),
        "required_scopes": auth_info.get("required_scopes", [])
    }


def handle_auth_required(
    tool_result: Dict[str, Any],
    tool_name: str,
    tool_call_id: str,
    motet: "MotetContext",  # Forward reference to avoid circular import
    iteration: int = 0,
    strategy_name: str = "unknown",
    emit_events: bool = True
) -> AuthRequiredResult:
    """
    Handle auth_required result from tool execution (ADR-0057).
    
    Performs all necessary actions when OAuth authorization is needed:
    1. Extracts auth info from tool result
    2. Logs the auth requirement
    3. Streams auth_required event to UI (if emit_events=True)
    4. Emits reasoning_step event (if emit_events=True)
    5. Returns AuthRequiredResult for strategy to use
    
    Args:
        tool_result: Tool execution result containing auth_required info
        tool_name: Name of the tool that requires auth
        tool_call_id: ID of the tool call (for conversation history)
        motet: MotetContext for streaming events and context
        iteration: Current iteration number (for reasoning_step event)
        strategy_name: Name of the reasoning strategy (for logging/events)
        emit_events: Whether to emit events (set False for testing)
        
    Returns:
        AuthRequiredResult with all auth info and helper methods
        
    Example:
        ```python
        if check_auth_required(tool_result):
            auth_result = handle_auth_required(
                tool_result=tool_result,
                tool_name="mcp.google_workspace.gmail_send",
                tool_call_id="call_123",
                motet=motet,
                iteration=3,
                strategy_name="agentic_loop"
            )
            
            # Add to conversation history
            conversation_history.append(Message(
                role="tool",
                tool_call_id=tool_call_id,
                content=auth_result.to_user_message()
            ))
            
            # Return early from reasoning strategy
            return auth_result.to_reasoning_response(iterations_used=3)
        ```
    """
    # Extract auth info from tool result
    auth_info = extract_auth_info(tool_result)
    
    service_id = auth_info["service_id"]
    display_name = auth_info["display_name"]
    message = auth_info["message"]
    authorization_endpoint = auth_info["authorization_endpoint"]
    required_scopes = auth_info["required_scopes"]
    
    # Log the auth requirement
    logger.info(f"{strategy_name}_auth_required",
               tool_name=tool_name,
               service_id=service_id,
               display_name=display_name,
               iteration=iteration)
    
    if emit_events:
        # Stream auth_required event to UI (ADR-0057)
        # Note: required_scopes must be JSON-serialized for Redis
        motet.stream_event("auth_required",
                          service_id=service_id,
                          display_name=display_name,
                          message=message,
                          authorization_endpoint=authorization_endpoint,
                          required_scopes=json.dumps(required_scopes))
        
        # Emit reasoning_step event for observability
        auth_content = f"⚠️ {message}\n\nPlease authorize {display_name} to continue."
        motet.publish_event({
            "kind": "reasoning_step",
            "task_id": motet.task_id,
            "trace_id": motet.task_id,
            "source": "reasoning",
            "strategy": strategy_name,
            "step": iteration,
            "thought": f"{tool_name} requires authorization",
            "action": tool_name,
            "observation": auth_content[:200]
        })
        
        # Stream chain stopped event
        motet.stream_event(f"{strategy_name}_stopped", reason="auth_required")
    
    # Create and return AuthRequiredResult
    return AuthRequiredResult(
        service_id=service_id,
        display_name=display_name,
        message=message,
        authorization_endpoint=authorization_endpoint,
        required_scopes=required_scopes,
        tool_name=tool_name,
        tool_call_id=tool_call_id
    )


def process_tool_results_for_auth(
    tool_results: List[Dict[str, Any]],
    tool_calls: List[Dict[str, Any]],
    motet: "MotetContext",
    iteration: int = 0,
    strategy_name: str = "unknown"
) -> Optional[AuthRequiredResult]:
    """
    Process multiple tool results and check for auth_required.
    
    Convenience function for reasoning strategies that execute tools in parallel.
    Returns the first auth_required result found, or None if all tools succeeded.
    
    Args:
        tool_results: List of tool execution results (from motet.join())
        tool_calls: List of tool call info (tool_name, tool_call_id, parameters)
        motet: MotetContext for streaming events
        iteration: Current iteration number
        strategy_name: Name of the reasoning strategy
        
    Returns:
        AuthRequiredResult if any tool requires auth, None otherwise
        
    Example:
        ```python
        # After parallel tool execution
        results = motet.join(execution_commands, fail_fast=False)
        
        auth_result = process_tool_results_for_auth(
            tool_results=results,
            tool_calls=unique_tool_calls,
            motet=motet,
            iteration=current_iteration,
            strategy_name="agentic_loop"
        )
        
        if auth_result:
            return auth_result.to_reasoning_response(iterations_used)
        ```
    """
    for idx, tool_result in enumerate(tool_results):
        if check_auth_required(tool_result):
            tool_call = tool_calls[idx] if idx < len(tool_calls) else {}
            return handle_auth_required(
                tool_result=tool_result,
                tool_name=tool_call.get("tool_name", "unknown"),
                tool_call_id=tool_call.get("tool_call_id", f"call_{idx}"),
                motet=motet,
                iteration=iteration,
                strategy_name=strategy_name
            )
    
    return None


__all__ = [
    "AuthRequiredResult",
    "check_auth_required",
    "extract_auth_info",
    "handle_auth_required",
    "process_tool_results_for_auth"
]

