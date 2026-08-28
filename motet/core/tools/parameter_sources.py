"""
Motet - Parameter Sources and Classification

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Parameter source classification system for tool calling.
    Provides enum for classifying parameter origins and helper function for
    declarative parameter definitions using Pydantic Field metadata.

    Enables automatic parameter injection for user context, credentials,
    and system parameters. Uses token-based approach for USER_CONTEXT and
    SYSTEM parameters (shows tokens instead of hiding completely).

Dependencies:
    - enum: Parameter source classification
    - pydantic: Field metadata for parameter definitions
    - typing: Type hints and annotations

Usage:
    from motet.core.tools.parameter_sources import (
        ParameterSource, ContextParam, get_context_token
    )
    from pydantic import BaseModel, Field
    
    # Define tool parameters with source classification
    class GmailSendParams(BaseModel):
        # LLM-provided parameters
        to: str = Field(..., description="Recipient email")
        subject: str = Field(..., description="Email subject")
        body: str = Field(..., description="Email body")
        
        # Context-injected parameters (shown as tokens in LLM schema)
        user_google_email: str = ContextParam(
            description="Sender's authenticated Google email",
            source=ParameterSource.USER_CONTEXT,
            context_key="authenticated_user_email"
        )
        access_token: str = ContextParam(
            description="OAuth access token",
            source=ParameterSource.CREDENTIAL,
            context_key="google_access_token"
        )

Notes:
    - All context params (USER_CONTEXT, SYSTEM, CREDENTIAL): Shown as tokens (e.g., __CTX_authenticated_user_email__)
    - Tokens provide security by not exposing actual values to LLM
    - Schema token substitution happens in ToolSchemaExporter
    - Parameter injection happens in ParameterInjectionService (replaces tokens with values)
    - Supports both Pydantic tools and MCP tools (via conventions)
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional
from pydantic import Field


class ParameterSource(Enum):
    """
    Classification of where a parameter value originates.
    
    Used to determine how parameters should be handled:
    - LLM_PROVIDED: Extracted from user query by LLM
    - USER_CONTEXT: Injected from authenticated user session
    - CREDENTIAL: Injected from credential store (OAuth, API keys)
    - SYSTEM: Injected from system context (task_id, conversation_id)
    - CONVERSATION: Injected from conversation state/history
    
    Parameters not marked as LLM_PROVIDED are hidden from LLM schemas
    and injected automatically at execution time.
    """
    LLM_PROVIDED = "llm"
    USER_CONTEXT = "user_context"
    CREDENTIAL = "credential"
    SYSTEM = "system"
    CONVERSATION = "conversation"


def get_context_token(context_key: str) -> str:
    """
    Generate a context token for a given context key.
    
    Token format: __CTX_{context_key}__
    Used to represent context parameters in LLM-visible schemas
    without exposing actual values.
    
    Args:
        context_key: The context key (e.g., "authenticated_user_email")
    
    Returns:
        Token string (e.g., "__CTX_authenticated_user_email__")
    
    Example:
        token = get_context_token("authenticated_user_email")
        # Returns: "__CTX_authenticated_user_email__"
    """
    return f"__CTX_{context_key}__"


def ContextParam(
    description: str,
    source: ParameterSource = ParameterSource.USER_CONTEXT,
    context_key: Optional[str] = None,
    default: Any = ...,
    **kwargs
) -> Any:
    """
    Helper to define a context-injected parameter in Pydantic models.
    
    Creates a Pydantic Field with metadata that marks it for automatic
    injection from motet context. Parameters defined with ContextParam
    are automatically:
    - Shown as tokens in LLM-visible schemas (all context params)
    - Injected at execution time (by ParameterInjectionService)
    - Tokens replaced with actual values at execution
    - Validated by Pydantic after injection
    
    Args:
        description: Human-readable description of the parameter
        source: Where the parameter value comes from (default: USER_CONTEXT)
        context_key: Key to lookup in context (defaults to parameter name)
        default: Default value if not available (use ... for required)
        **kwargs: Additional Pydantic Field kwargs (e.g., alias, examples)
    
    Returns:
        Pydantic Field with parameter source metadata
    
    Example:
        class MyToolParams(BaseModel):
            # LLM provides this
            query: str = Field(..., description="Search query")
            
            # System injects this from user context (shown as token)
            user_email: str = ContextParam(
                description="User's email address",
                source=ParameterSource.USER_CONTEXT,
                context_key="authenticated_user_email"
            )
            # LLM sees: default="__CTX_authenticated_user_email__"
            
            # System injects this from credential store (shown as token)
            api_key: str = ContextParam(
                description="API key for external service",
                source=ParameterSource.CREDENTIAL,
                context_key="service_api_key"
            )
            # LLM sees: default="__CTX_service_api_key__"
    
    Security Notes:
        - All context params (USER_CONTEXT, SYSTEM, CREDENTIAL) shown as tokens
        - Tokens (e.g., __CTX_google_access_token__) don't expose actual values
        - Tokens are replaced with actual values at execution time
        - Injected parameters cannot be overridden by LLM (security)
        - All parameter injection is logged for audit trails
    
    See Also:
        - ParameterInjectionService: Handles actual parameter injection (replaces tokens)
        - ToolSchemaExporter: Shows tokens in LLM schemas for USER_CONTEXT/SYSTEM
        - get_context_token: Generate token string for a context key
        - ADR-0046: Complete design and security considerations
    """
    return Field(
        default=default,
        description=description,
        json_schema_extra={
            "x-imf-source": source.value,
            "x-imf-context-key": context_key,
            "x-imf-hide-from-llm": False,  # Show all params as tokens (security via token replacement)
            "x-imf-use-token": source != ParameterSource.LLM_PROVIDED  # Use tokens for all context params
        },
        **kwargs
    )


# Convention-based parameter recognition for MCP tools (ADR-0046 Appendix E)
# Maps common parameter names to (source, context_key) tuples
PARAMETER_CONVENTIONS = {
    # User Context
    "user_email": (ParameterSource.USER_CONTEXT, "authenticated_user_email"),
    "user_google_email": (ParameterSource.USER_CONTEXT, "authenticated_user_email"),
    "user_id": (ParameterSource.USER_CONTEXT, "principal_id"),
    "username": (ParameterSource.USER_CONTEXT, "principal_id"),
    "principal_id": (ParameterSource.USER_CONTEXT, "principal_id"),
    "authenticated_user_email": (ParameterSource.USER_CONTEXT, "authenticated_user_email"),
    
    # Credentials
    "access_token": (ParameterSource.CREDENTIAL, "access_token"),
    "auth_token": (ParameterSource.CREDENTIAL, "access_token"),
    "api_key": (ParameterSource.CREDENTIAL, "api_key"),
    "oauth_token": (ParameterSource.CREDENTIAL, "oauth_token"),
    "bearer_token": (ParameterSource.CREDENTIAL, "bearer_token"),
    "google_access_token": (ParameterSource.CREDENTIAL, "google_access_token"),
    
    # System Context
    "task_id": (ParameterSource.SYSTEM, "task_id"),
    "conversation_id": (ParameterSource.SYSTEM, "conversation_id"),
    "tenant_id": (ParameterSource.SYSTEM, "tenant_id"),
    "motet_id": (ParameterSource.SYSTEM, "motet_id"),
    "trace_id": (ParameterSource.SYSTEM, "trace_id"),
    "command_id": (ParameterSource.SYSTEM, "command_id"),
    "parent_command_id": (ParameterSource.SYSTEM, "parent_command_id"),
}


__all__ = [
    "ParameterSource",
    "ContextParam",
    "get_context_token",
    "PARAMETER_CONVENTIONS",
]

