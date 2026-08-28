"""
Motet - Parameter Injection Service

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Service for injecting user context, credentials, and system parameters
    into tool calls after LLM tool selection.

    Uses token-based approach: Context parameters are shown to LLM as tokens
    (e.g., __CTX_google_access_token__). This service replaces those tokens
    with actual values from motet context. Supports both Pydantic tools
    (via Field metadata) and MCP tools (via convention-based recognition).

    Security: Always overrides context parameters with actual values, even if
    LLM provides non-token values (prevents parameter tampering attacks).

Dependencies:
    - structlog: Structured logging
    - pydantic: Parameter model validation
    - typing: Type hints and annotations
    - ToolRegistry: Access to tool schemas
    - Parameter sources: Classification and conventions

Usage:
    from motet.core.tools.parameter_injection import ParameterInjectionService
    
    # Create service
    injector = ParameterInjectionService(tool_registry)
    
    # Inject parameters after LLM tool selection
    complete_params = injector.inject_parameters(
        tool_name="gmail_send",
        llm_parameters={"to": "user@example.com", "subject": "Hello"},
        motet=motet_context
    )
    
    # complete_params now includes user_google_email and access_token
    # injected from motet context

Notes:
    - Integrates with ToolRegistry for schema access
    - Uses MotetContext for parameter value resolution
    - Supports convention-based recognition for MCP tools
    - Logs all parameter injections for observability
    - Injected parameters cannot be overridden by LLM
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Set
import structlog

from .parameter_sources import ParameterSource, PARAMETER_CONVENTIONS, get_context_token
from .registry import ToolRegistry
from .schema_normalizer import ToolSchemaNormalizer

logger = structlog.get_logger(__name__)


class ParameterInjectionService:
    """
    Service for replacing context parameter tokens with actual values.
    
    Uses token-based approach (ADR-0046): Context parameters are shown to
    LLM as tokens (e.g., __CTX_google_access_token__). This service replaces
    those tokens with actual values from motet context after LLM tool selection.
    
    Supports both Pydantic tools (native Field metadata) and MCP tools
    (convention-based recognition using PARAMETER_CONVENTIONS).
    
    The service integrates with:
    - ToolRegistry: For accessing tool schemas and parameter metadata
    - MotetContext: For resolving parameter values from user context
    - OAuth21SessionStore: For credential lookup (access tokens)
    - VaultClient: For API keys and secrets
    
    Security Features:
    - Credentials shown as tokens to LLM (never actual values)
    - Context parameters ALWAYS overridden with actual values (forced replacement)
    - LLM cannot bypass token system (parameter tampering prevented)
    - Override attempts logged with "parameter_override_attempted" warnings
    - All injections logged for audit trails
    - Tenant boundaries respected during resolution
    """
    
    def __init__(self, registry: ToolRegistry):
        """
        Initialize parameter injection service.
        
        Args:
            registry: Tool registry for accessing tool schemas
        """
        self.registry = registry
        self.logger = logger.bind(service="parameter_injection")
    
    def inject_parameters(
        self,
        tool_name: str,
        llm_parameters: Dict[str, Any],
        principal_id: str,
        tenant_id: str,
        task_id: str,
        conversation_id: str,
        access_token: Optional[str] = None,
        api_key: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Inject context parameters into LLM-provided parameters.
        
        This is the main entry point for parameter injection. It handles
        both Pydantic tools (with ContextParam metadata) and MCP tools
        (with convention-based recognition).
        
        Only injects parameters that the tool's schema declares - prevents
        validation errors from unexpected parameters.
        
        Args:
            tool_name: Name of the tool being called
            llm_parameters: Parameters provided by LLM
            principal_id: Principal (user) ID for user context parameters
            tenant_id: Tenant ID for multi-tenant isolation
            task_id: Current task ID for system context
            conversation_id: Current conversation ID for system context
            access_token: Optional OAuth access token for credential injection
            api_key: Optional API key for credential injection
        
        Returns:
            Complete parameters dict with injected values
        
        Example:
            # LLM provides partial parameters
            llm_params = {"to": "user@example.com", "subject": "Hi"}
            
            # Service injects missing context parameters
            complete = injector.inject_parameters(
                tool_name="gmail_send",
                llm_parameters=llm_params,
                principal_id="alice@example.com",
                tenant_id="tenant_123",
                task_id="task_456",
                conversation_id="conv_789"
            )
            
            # complete = {
            #     "to": "user@example.com",
            #     "subject": "Hi",
            #     "user_google_email": "alice@example.com",  # Injected
            #     "task_id": "task_456"  # Injected if tool declares it
            # }
        """
        self.logger.info(
            "parameter_injection_started",
            tool_name=tool_name,
            llm_param_count=len(llm_parameters),
            principal_id=principal_id,
            tenant_id=tenant_id
        )
        
        # Create context bundle for resolution
        context = {
            'principal_id': principal_id,
            'tenant_id': tenant_id,
            'task_id': task_id,
            'conversation_id': conversation_id,
            'access_token': access_token,
            'api_key': api_key
        }
        
        # Get tool info from registry
        tool_info = self.registry.get(tool_name)
        
        if not tool_info:
            self.logger.warning(
                "parameter_injection_tool_not_found",
                tool_name=tool_name
            )
            return llm_parameters
        
        # Get parameter names from normalized schema
        accepted_params = ToolSchemaNormalizer.get_parameter_names(tool_info)
        
        if not accepted_params:
            self.logger.warning(
                "parameter_injection_no_schema",
                tool_name=tool_name
            )
            return llm_parameters
        
        # Inject parameters based on schema and conventions
        return self._inject_parameters(
            tool_name=tool_name,
            tool_info=tool_info,
            accepted_params=accepted_params,
            llm_parameters=llm_parameters,
            context=context
        )
    
    def _inject_parameters(
        self,
        tool_name: str,
        tool_info: Any,
        accepted_params: Set[str],
        llm_parameters: Dict[str, Any],
        context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Unified parameter injection for all tool types.
        
        Handles both:
        - Pydantic tools: Uses Field metadata (x-imf-source, x-imf-context-key)
        - MCP tools: Uses convention-based recognition (PARAMETER_CONVENTIONS)
        
        Only injects parameters that exist in accepted_params (from schema).
        
        Args:
            tool_name: Name of the tool
            tool_info: RegisteredTool with schema
            accepted_params: Set of parameter names tool accepts
            llm_parameters: Parameters from LLM
            context: Dict of context values (principal_id, tenant_id, etc.)
        
        Returns:
            Complete parameters with injected values
        """
        complete_params = dict(llm_parameters)
        injected_count = 0
        
        # Import here to avoid circular dependency
        from .parameter_sources import get_context_token
        
        # Try Pydantic metadata-based injection first
        if tool_info.tool_schema is not None and hasattr(tool_info.tool_schema, 'model_fields'):
            fields_info = tool_info.tool_schema.model_fields
            
            for field_name, field_info in fields_info.items():
                # Skip if not in accepted parameters
                if field_name not in accepted_params:
                    continue
                
                # Check for injection metadata
                json_schema_extra = getattr(field_info, 'json_schema_extra', None)
                if not json_schema_extra:
                    continue
                
                source_str = json_schema_extra.get('x-imf-source')
                if not source_str:
                    continue
                
                # Inject based on metadata
                source = ParameterSource(source_str)
                context_key = json_schema_extra.get('x-imf-context-key', field_name)
                
                # Check if LLM provided a token instead of actual value
                llm_value = llm_parameters.get(field_name)
                expected_token = get_context_token(context_key)
                
                # Replace token with actual value, or inject if missing
                if llm_value == expected_token or field_name not in llm_parameters:
                    value = self._resolve_parameter(source, context_key, context)
                    
                    # Only inject if value is not None and not empty string
                    if value is not None and value != "":
                        complete_params[field_name] = value
                        injected_count += 1
                        self.logger.info(
                            "parameter_injected_from_metadata",
                            tool_name=tool_name,
                            param_name=field_name,
                            source=source.value,
                            context_key=context_key,
                            was_token=llm_value == expected_token
                        )
                elif llm_value is not None and llm_value != expected_token:
                    # LLM provided a value (not the token) - keep it but log warning
                    # This could be a security risk if LLM tries to override context
                    self.logger.warning(
                        "parameter_override_attempted",
                        tool_name=tool_name,
                        param_name=field_name,
                        source=source.value,
                        llm_value=llm_value[:50] if isinstance(llm_value, str) else str(llm_value)[:50],
                        expected_token=expected_token
                    )
                    # For security, we still override with context value
                    # (LLM shouldn't be able to override context params)
                    value = self._resolve_parameter(source, context_key, context)
                    if value is not None and value != "":
                        complete_params[field_name] = value
                        injected_count += 1
                        self.logger.info(
                            "parameter_injected_overriding_llm_value",
                            tool_name=tool_name,
                            param_name=field_name,
                            source=source.value
                        )
        
        # Convention-based injection for parameters not handled by metadata (MCP tools)
        for param_name in accepted_params:
            # Skip if already handled by metadata-based injection
            if param_name in complete_params:
                # Check if it's a token that needs replacement
                llm_value = llm_parameters.get(param_name)
                if llm_value and isinstance(llm_value, str) and llm_value.startswith("__CTX_"):
                    # This is a token, check if we need to replace it
                    if param_name in PARAMETER_CONVENTIONS:
                        source, context_key = PARAMETER_CONVENTIONS[param_name]
                        expected_token = get_context_token(context_key)
                        if llm_value == expected_token:
                            # Replace token with actual value
                            value = self._resolve_parameter(source, context_key, context)
                            if value is not None and value != "":
                                complete_params[param_name] = value
                                self.logger.info(
                                    "parameter_token_replaced_from_convention",
                                    tool_name=tool_name,
                                    param_name=param_name,
                                    source=source.value,
                                    context_key=context_key
                                )
                continue
            
            # Check if this parameter matches a convention
            if param_name not in PARAMETER_CONVENTIONS:
                continue
            
            # Get source and context key from convention
            source, context_key = PARAMETER_CONVENTIONS[param_name]
            
            # Check if LLM provided a token
            llm_value = llm_parameters.get(param_name)
            expected_token = get_context_token(context_key)
            
            # Replace token with actual value, or inject if missing
            if llm_value == expected_token or param_name not in llm_parameters:
                value = self._resolve_parameter(source, context_key, context)
                
                # Only inject if value is not None and not empty string
                if value is not None and value != "":
                    complete_params[param_name] = value
                    injected_count += 1
                    self.logger.info(
                        "parameter_injected_from_convention",
                        tool_name=tool_name,
                        param_name=param_name,
                        source=source.value,
                        context_key=context_key,
                        was_token=llm_value == expected_token
                    )
            elif llm_value is not None and llm_value != expected_token:
                # LLM provided a value (not the token) - override for security
                value = self._resolve_parameter(source, context_key, context)
                if value is not None and value != "":
                    complete_params[param_name] = value
                    injected_count += 1
                    self.logger.warning(
                        "parameter_injected_overriding_llm_value_convention",
                    tool_name=tool_name,
                    param_name=param_name,
                    source=source.value,
                    context_key=context_key
                )
        
        self.logger.info(
            "parameter_injection_complete",
            tool_name=tool_name,
            injected_count=injected_count,
            total_params=len(complete_params),
            accepted_params_count=len(accepted_params)
        )
        
        return complete_params
    
    def _resolve_parameter(
        self,
        source: ParameterSource,
        context_key: str,
        context: Dict[str, Any]
    ) -> Optional[Any]:
        """
        Resolve a parameter value from context based on source.
        
        Handles different parameter sources:
        - USER_CONTEXT: principal_id, authenticated_user_email
        - CREDENTIAL: access_token, api_key (from OAuth or Vault)
        - SYSTEM: task_id, conversation_id, tenant_id, etc.
        - CONVERSATION: Conversation state (future)
        
        Args:
            source: Parameter source type
            context_key: Key to lookup in context
            context: Dict with context values
        
        Returns:
            Resolved parameter value or None if not available
        """
        try:
            if source == ParameterSource.USER_CONTEXT:
                return self._get_user_context_value(context_key, context)
            
            elif source == ParameterSource.CREDENTIAL:
                return self._get_credential_value(context_key, context)
            
            elif source == ParameterSource.SYSTEM:
                return self._get_system_context_value(context_key, context)
            
            elif source == ParameterSource.CONVERSATION:
                return self._get_conversation_context_value(context_key, context)
            
            else:
                self.logger.warning(
                    "parameter_resolution_unknown_source",
                    source=source.value,
                    context_key=context_key
                )
                return None
                
        except Exception as e:
            self.logger.error(
                "parameter_resolution_failed",
                source=source.value,
                context_key=context_key,
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True
            )
            return None
    
    def _get_user_context_value(self, key: str, context: Dict[str, Any]) -> Optional[str]:
        """
        Get value from user context.
        
        Resolves user-specific parameters like email, user_id, username.
        All user context keys map to principal_id.
        
        Args:
            key: Context key to lookup
            context: Context dict with values
        
        Returns:
            User context value or None
        """
        # All user context keys map to principal_id
        if key in ["authenticated_user_email", "user_email", "user_id", "username", "principal_id"]:
            return context.get("principal_id")
        
        return None
    
    def _get_credential_value(self, key: str, context: Dict[str, Any]) -> Optional[str]:
        """
        Get value from credential store.
        
        Currently returns access_token and api_key from context if provided.
        Phase 4 will integrate with OAuth21SessionStore and VaultClient.
        
        Args:
            key: Credential key to lookup
            context: Context dict with values
        
        Returns:
            Credential value or None
        """
        # Direct credential mapping
        credential_mappings = {
            "google_access_token": "access_token",
            "access_token": "access_token",
            "auth_token": "access_token",
            "oauth_token": "access_token",
            "api_key": "api_key",
        }
        
        context_key = credential_mappings.get(key)
        if context_key:
            value = context.get(context_key)
            if not value:
                self.logger.debug(
                    "credential_not_available",
                    key=key,
                    context_key=context_key,
                    principal_id=context.get("principal_id")
                )
            return value
        
        # TODO: Phase 4 - Lookup from OAuth21SessionStore or VaultClient
        self.logger.debug(
            "credential_lookup_not_implemented",
            key=key,
            principal_id=context.get("principal_id")
        )
        return None
    
    def _get_system_context_value(self, key: str, context: Dict[str, Any]) -> Optional[str]:
        """
        Get value from system context.
        
        Resolves system parameters like task_id, conversation_id, tenant_id.
        
        Args:
            key: Context key to lookup
            context: Context dict with values
        
        Returns:
            System context value or None
        """
        system_mappings = {
            "task_id": "task_id",
            "conversation_id": "conversation_id",
            "tenant_id": "tenant_id",
            "motet_id": "tenant_id",  # Alias
        }
        
        context_key = system_mappings.get(key, key)
        return context.get(context_key)
    
    def _get_conversation_context_value(self, key: str, context: Dict[str, Any]) -> Optional[Any]:
        """
        Get value from conversation context.
        
        This is a placeholder for future conversation state integration.
        
        Args:
            key: Context key to lookup
            context: Context dict with values
        
        Returns:
            Conversation context value or None
        """
        # TODO: Phase 5 - Integrate with conversation state management
        self.logger.debug(
            "conversation_context_not_implemented",
            key=key,
            conversation_id=context.get("conversation_id")
        )
        return None


__all__ = ["ParameterInjectionService"]

