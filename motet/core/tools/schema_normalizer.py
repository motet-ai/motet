"""
Motet - Tool Schema Normalizer

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-07-13

Description:
    Normalize tool schemas from different sources (Pydantic, MCP, OpenAPI) to
    a common format for consistent parameter extraction and validation.
    
    Provides a single interface for:
    - Extracting parameter names from any tool type
    - Getting full JSON Schema from any tool type
    - Checking parameter metadata (required, types, etc.)
    
    Supports multiple schema formats:
    - Pydantic models (native tools with model_fields)
    - MCP JSON Schema (direct properties or inputSchema)
    - Future: OpenAPI/Swagger specifications

Dependencies:
    - structlog: Structured logging
    - typing: Type hints and annotations
    - pydantic: For Pydantic model detection

Usage:
    from motet.core.tools.schema_normalizer import ToolSchemaNormalizer
    
    # Get parameter names from any tool
    param_names = ToolSchemaNormalizer.get_parameter_names(tool_info)
    
    # Get full normalized schema
    schema = ToolSchemaNormalizer.get_full_schema(tool_info)
    
    # Check if parameter is required
    is_required = ToolSchemaNormalizer.is_parameter_required(tool_info, "user_email")

Notes:
    - Handles Pydantic v1 and v2 schemas
    - Supports both direct and nested MCP schemas
    - Returns empty sets/dicts for unrecognized formats
    - Thread-safe (stateless static methods)
    - Used by ParameterInjectionService and ToolSchemaExporter
"""

from __future__ import annotations

from typing import Any, Dict, Set, Optional
import structlog

logger = structlog.get_logger(__name__)


class ToolSchemaNormalizer:
    """
    Normalize tool schemas from different sources to a common format.
    
    Provides a unified interface for extracting parameter information
    from Pydantic models, MCP JSON Schemas, and other tool formats.
    
    All methods are static and stateless for thread-safety and reusability.
    """
    
    @staticmethod
    def get_parameter_names(tool_info: Any) -> Set[str]:
        """
        Extract parameter names from any tool schema format.
        
        Supports:
        - Pydantic models: Extracts from model_fields
        - MCP JSON Schema: Extracts from properties or inputSchema.properties
        - Unknown formats: Returns empty set
        
        Args:
            tool_info: RegisteredTool with schema attribute
        
        Returns:
            Set of parameter names the tool accepts
        
        Example:
            tool_info = registry.get("gmail_send")
            params = ToolSchemaNormalizer.get_parameter_names(tool_info)
            # params = {"to", "subject", "body", "user_google_email"}
        """
        if not tool_info or not hasattr(tool_info, 'tool_schema'):
            logger.warning(
                "schema_normalizer_no_schema",
                tool_info=str(tool_info)
            )
            return set()
        
        schema = tool_info.tool_schema
        
        # Handle Pydantic model (native tools)
        if hasattr(schema, 'model_fields'):
            param_names = set(schema.model_fields.keys())
            logger.debug(
                "schema_normalizer_pydantic_params",
                param_count=len(param_names),
                params=list(param_names)
            )
            return param_names
        
        # Handle MCP JSON Schema
        if isinstance(schema, dict):
            # Try direct properties first
            if 'properties' in schema:
                param_names = set(schema['properties'].keys())
                logger.debug(
                    "schema_normalizer_mcp_direct_params",
                    param_count=len(param_names),
                    params=list(param_names)
                )
                return param_names
            
            # Try nested inputSchema (MCP server format)
            if 'inputSchema' in schema:
                input_schema = schema['inputSchema']
                if isinstance(input_schema, dict) and 'properties' in input_schema:
                    param_names = set(input_schema['properties'].keys())
                    logger.debug(
                        "schema_normalizer_mcp_nested_params",
                        param_count=len(param_names),
                        params=list(param_names)
                    )
                    return param_names
        
        # Unknown schema format
        logger.warning(
            "schema_normalizer_unknown_format",
            schema_type=type(schema).__name__,
            has_model_fields=hasattr(schema, 'model_fields'),
            is_dict=isinstance(schema, dict),
            dict_keys=list(schema.keys()) if isinstance(schema, dict) else None
        )
        return set()
    
    @staticmethod
    def get_full_schema(tool_info: Any) -> Dict[str, Any]:
        """
        Get normalized JSON Schema for any tool type.
        
        Returns a standard JSON Schema dict with:
        - type: "object"
        - properties: Dict of parameter schemas
        - required: List of required parameter names
        
        Args:
            tool_info: RegisteredTool with schema attribute
        
        Returns:
            Normalized JSON Schema dict
        
        Example:
            schema = ToolSchemaNormalizer.get_full_schema(tool_info)
            # {
            #   "type": "object",
            #   "properties": {"to": {...}, "subject": {...}},
            #   "required": ["to", "subject"]
            # }
        """
        if not tool_info or not hasattr(tool_info, 'schema'):
            return {
                "type": "object",
                "properties": {},
                "required": []
            }
        
        schema = tool_info.tool_schema
        
        # Handle Pydantic model
        if hasattr(schema, 'model_json_schema'):
            try:
                # Pydantic v2
                json_schema = schema.model_json_schema()
                out = {
                    "type": json_schema.get("type", "object"),
                    "properties": json_schema.get("properties", {}),
                    "required": json_schema.get("required", [])
                }
                for key in ("$defs", "definitions"):
                    defs = json_schema.get(key)
                    if isinstance(defs, dict) and defs:
                        out[key] = defs
                return out
            except Exception as e:
                logger.error(
                    "schema_normalizer_pydantic_v2_failed",
                    error=str(e),
                    exc_info=True
                )
        
        elif hasattr(schema, 'schema'):
            try:
                # Pydantic v1
                json_schema = schema.schema()
                out = {
                    "type": json_schema.get("type", "object"),
                    "properties": json_schema.get("properties", {}),
                    "required": json_schema.get("required", [])
                }
                for key in ("$defs", "definitions"):
                    defs = json_schema.get(key)
                    if isinstance(defs, dict) and defs:
                        out[key] = defs
                return out
            except Exception as e:
                logger.error(
                    "schema_normalizer_pydantic_v1_failed",
                    error=str(e),
                    exc_info=True
                )
        
        # Handle MCP JSON Schema
        if isinstance(schema, dict):
            # Try direct schema
            if 'properties' in schema:
                return {
                    "type": schema.get("type", "object"),
                    "properties": schema.get("properties", {}),
                    "required": schema.get("required", [])
                }
            
            # Try nested inputSchema
            if 'inputSchema' in schema:
                input_schema = schema['inputSchema']
                if isinstance(input_schema, dict):
                    return {
                        "type": input_schema.get("type", "object"),
                        "properties": input_schema.get("properties", {}),
                        "required": input_schema.get("required", [])
                    }
        
        # Unknown format - return empty schema
        logger.warning(
            "schema_normalizer_full_schema_unknown_format",
            schema_type=type(schema).__name__
        )
        return {
            "type": "object",
            "properties": {},
            "required": []
        }
    
    @staticmethod
    def is_parameter_required(tool_info: Any, param_name: str) -> bool:
        """
        Check if a parameter is required by the tool.
        
        Args:
            tool_info: RegisteredTool with schema attribute
            param_name: Name of parameter to check
        
        Returns:
            True if parameter is required, False otherwise
        
        Example:
            is_required = ToolSchemaNormalizer.is_parameter_required(
                tool_info, "user_google_email"
            )
        """
        full_schema = ToolSchemaNormalizer.get_full_schema(tool_info)
        required_params = full_schema.get("required", [])
        return param_name in required_params
    
    @staticmethod
    def get_parameter_schema(tool_info: Any, param_name: str) -> Optional[Dict[str, Any]]:
        """
        Get the schema for a specific parameter.
        
        Args:
            tool_info: RegisteredTool with schema attribute
            param_name: Name of parameter to get schema for
        
        Returns:
            Parameter schema dict or None if not found
        
        Example:
            param_schema = ToolSchemaNormalizer.get_parameter_schema(
                tool_info, "user_google_email"
            )
            # {"type": "string", "description": "User's Google email"}
        """
        full_schema = ToolSchemaNormalizer.get_full_schema(tool_info)
        properties = full_schema.get("properties", {})
        return properties.get(param_name)
    
    @staticmethod
    def has_parameter(tool_info: Any, param_name: str) -> bool:
        """
        Check if tool declares a specific parameter.
        
        Args:
            tool_info: RegisteredTool with schema attribute
            param_name: Name of parameter to check
        
        Returns:
            True if parameter exists in schema, False otherwise
        
        Example:
            has_email = ToolSchemaNormalizer.has_parameter(
                tool_info, "user_google_email"
            )
        """
        param_names = ToolSchemaNormalizer.get_parameter_names(tool_info)
        return param_name in param_names


__all__ = ["ToolSchemaNormalizer"]

