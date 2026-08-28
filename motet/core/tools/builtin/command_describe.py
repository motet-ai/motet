"""
Motet - Command Describe Tool

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-26

Description:
    Command describe tool for the Motet distributed framework.
    Provides comprehensive command description and schema information for
    a specific command type. Includes full data class schema, metadata,
    and example usage. Enables LLMs to understand how to construct
    command_data for scheduling or execution.

Dependencies:
    - pydantic: Data validation and model definitions
    - typing: Type hints and annotations
    - Tool registry and runtime stack
    - Command type registry
    - Command data registry

Usage:
    from motet.core.tools.builtin.command_describe import run

    # Describe a command
    result = run({
        "command_type": "core.agent_turn",
        "include_schema": True,
        "include_example": True
    })

Notes:
    - Provides comprehensive command description and schema information
    - Includes full Pydantic model schema with field descriptions
    - Shows command metadata (capabilities, timeout, priority, etc.)
    - Returns example command_data structure
    - Integrates with command type and data registries
    - Includes comprehensive error handling and validation
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

from ..registry import ToolRegistry, get_runtime_stack
from ..protocol import ok, err


class CommandDescribeParams(BaseModel):
    """Parameters for describing a command."""
    command_type: str = Field(..., description="Command type identifier (e.g., 'core.agent_turn', 'core.tool_execution')")
    include_schema: bool = Field(default=True, description="Include full JSON schema for command data class")
    include_metadata: bool = Field(default=True, description="Include command metadata (capabilities, timeout, etc.)")
    include_example: bool = Field(default=True, description="Include example command_data structure")


def _extract_example_data(data_class: Any) -> Dict[str, Any]:
    """Extract example data structure from Pydantic model with concrete examples."""
    if not data_class:
        return {}
    
    try:
        # Get model fields
        if hasattr(data_class, 'model_fields'):
            fields = data_class.model_fields
        elif hasattr(data_class, '__fields__'):
            fields = data_class.__fields__
        else:
            return {}
        
        example = {}
        for field_name, field_info in fields.items():
            # Skip PydanticUndefined
            try:
                from pydantic import PydanticUndefined
                if hasattr(field_info, 'default') and field_info.default is PydanticUndefined:
                    # No default - infer from type
                    pass
                elif hasattr(field_info, 'default') and field_info.default is not ...:
                    if callable(field_info.default):
                        example[field_name] = None
                    else:
                        example[field_name] = field_info.default
                    continue
            except (ImportError, AttributeError):
                if hasattr(field_info, 'default') and field_info.default is not ...:
                    if callable(field_info.default):
                        example[field_name] = None
                    else:
                        example[field_name] = field_info.default
                    continue
            
            # Check for default_factory
            if hasattr(field_info, 'default_factory') and field_info.default_factory:
                if field_info.default_factory == list:
                    # Special handling for messages field
                    if 'message' in field_name.lower():
                        example[field_name] = [{"role": "user", "content": "example message"}]
                    else:
                        example[field_name] = []
                elif field_info.default_factory == dict:
                    example[field_name] = {}
                else:
                    try:
                        example[field_name] = field_info.default_factory()
                    except Exception:
                        example[field_name] = None
                continue
            
            # Infer type from annotation
            annotation = getattr(field_info, 'annotation', None) if hasattr(field_info, 'annotation') else None
            if annotation:
                # Handle List types
                if hasattr(annotation, '__origin__'):
                    origin = annotation.__origin__
                    if origin is list:
                        args = getattr(annotation, '__args__', ())
                        if 'message' in field_name.lower():
                            example[field_name] = [{"role": "user", "content": "example message"}]
                        else:
                            example[field_name] = []
                    elif origin is dict:
                        example[field_name] = {}
                    elif origin is str:
                        example[field_name] = "string_value"
                    elif origin is int:
                        example[field_name] = 0
                    elif origin is bool:
                        example[field_name] = False
                    else:
                        example[field_name] = None
                elif annotation == str:
                    example[field_name] = "string_value"
                elif annotation == int:
                    example[field_name] = 0
                elif annotation == bool:
                    example[field_name] = False
                elif annotation == list:
                    if 'message' in field_name.lower():
                        example[field_name] = [{"role": "user", "content": "example message"}]
                    else:
                        example[field_name] = []
                elif annotation == dict:
                    example[field_name] = {}
                else:
                    example[field_name] = None
            else:
                example[field_name] = None
        
        return example
    except Exception:
        return {}


def _extract_field_info(data_class: Any) -> Dict[str, Dict[str, Any]]:
    """Extract detailed field information including descriptions, types, defaults, and validation hints."""
    if not data_class:
        return {}
    
    try:
        fields_info = {}
        
        if hasattr(data_class, 'model_fields'):
            fields = data_class.model_fields
        elif hasattr(data_class, '__fields__'):
            fields = data_class.__fields__
        else:
            return {}
        
        for field_name, field_info in fields.items():
            field_desc: Dict[str, Any] = {}
            
            # Get field description from Pydantic Field
            # In Pydantic v2, description can be in multiple places
            description = None
            if hasattr(field_info, 'description'):
                description = field_info.description
            elif hasattr(field_info, 'field_info'):
                # Try nested field_info
                if hasattr(field_info.field_info, 'description'):
                    description = field_info.field_info.description
            # Also try getting from JSON schema if available
            if not description:
                try:
                    if hasattr(data_class, 'model_json_schema'):
                        schema = data_class.model_json_schema()
                        properties = schema.get('properties', {})
                        if field_name in properties:
                            description = properties[field_name].get('description')
                except Exception:
                    pass  # optional: schema extraction for field description
            
            if description:
                field_desc["description"] = description
            
            # Get field type with full annotation
            annotation = getattr(field_info, 'annotation', None)
            if annotation:
                # Format type nicely
                if hasattr(annotation, '__origin__'):
                    origin = annotation.__origin__
                    args = getattr(annotation, '__args__', ())
                    if origin is list:
                        if args:
                            field_desc["type"] = f"List[{args[0]}]"
                        else:
                            field_desc["type"] = "List[Any]"
                    elif origin is dict:
                        if args and len(args) >= 2:
                            field_desc["type"] = f"Dict[{args[0]}, {args[1]}]"
                        else:
                            field_desc["type"] = "Dict[str, Any]"
                    elif origin is Optional or (hasattr(annotation, '__origin__') and str(annotation.__origin__) == 'typing.Union'):
                        # Handle Optional types
                        non_none_args = [arg for arg in args if arg is not type(None)]
                        if non_none_args:
                            field_desc["type"] = f"Optional[{non_none_args[0]}]"
                        else:
                            field_desc["type"] = "Optional[Any]"
                    else:
                        field_desc["type"] = str(annotation)
                else:
                    field_desc["type"] = str(annotation)
            
            # Get default value
            try:
                from pydantic import PydanticUndefined
                if hasattr(field_info, 'default'):
                    default = field_info.default
                    if default is not PydanticUndefined and default is not ...:
                        field_desc["default"] = default
            except (ImportError, AttributeError):
                if hasattr(field_info, 'default') and field_info.default is not ...:
                    field_desc["default"] = field_info.default
            
            # Check for default_factory
            if hasattr(field_info, 'default_factory') and field_info.default_factory:
                if field_info.default_factory == list:
                    field_desc["default"] = "[] (empty list)"
                elif field_info.default_factory == dict:
                    field_desc["default"] = "{} (empty dict)"
                else:
                    field_desc["default"] = f"default_factory: {field_info.default_factory}"
            
            # Determine if required
            try:
                from pydantic import PydanticUndefined
                has_default = (
                    (hasattr(field_info, 'default') and field_info.default is not PydanticUndefined and field_info.default is not ...) or
                    (hasattr(field_info, 'default_factory') and field_info.default_factory)
                )
                field_desc["required"] = not has_default
            except (ImportError, AttributeError):
                has_default = (
                    (hasattr(field_info, 'default') and field_info.default is not ...) or
                    (hasattr(field_info, 'default_factory') and field_info.default_factory)
                )
                field_desc["required"] = not has_default
            
            # Add validation hints and examples for common patterns
            if 'message' in field_name.lower():
                if not field_desc.get("description"):
                    field_desc["description"] = "Array of message objects representing conversation messages"
                field_desc["validation_hints"] = "Each message object must have 'role' (e.g., 'user', 'assistant', 'system') and 'content' (string). Example: [{\"role\": \"user\", \"content\": \"Hello\"}]"
                field_desc["example"] = [{"role": "user", "content": "Hello, how are you?"}]
            elif field_desc.get("type", "").startswith("List["):
                if not field_desc.get("description"):
                    field_desc["description"] = f"Array/list of items of type {field_desc.get('type', 'Any')}"
                field_desc["validation_hints"] = "Array/list of items matching the specified type"
            elif field_desc.get("type", "").startswith("Dict["):
                if not field_desc.get("description"):
                    field_desc["description"] = f"Object/dictionary with key-value pairs of type {field_desc.get('type', 'Any')}"
                field_desc["validation_hints"] = "Object/dictionary with key-value pairs"
            
            # Add default description if missing
            if not field_desc.get("description"):
                field_desc["description"] = f"Field of type {field_desc.get('type', 'Any')}"
            
            fields_info[field_name] = field_desc
        
        return fields_info
    except Exception:
        return {}


def run(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Describe a specific command type (synchronous for Celery workers - ADR-0033).
    
    Retrieves comprehensive information about a command type including
    its registration, data class schema, metadata, and example usage.
    """
    try:
        # Parse parameters
        parsed = CommandDescribeParams(**(params or {}))
    except Exception as exc:
        return err(f"validation error: {exc}")
    
    if not parsed.command_type:
        return err("command_type is required - use 'command:command_type' or 'command_describe:command_type'")
    
    # Normalize command type - strip common prefixes that LLMs sometimes add
    # OpenAI models sometimes prefix names with "functions." or "commands." in their internal namespace
    command_type = parsed.command_type
    for prefix in ("functions.", "function.", "commands.", "command."):
        if command_type.startswith(prefix):
            command_type = command_type[len(prefix):]
            break
    
    try:
        # Import here to avoid circular dependencies at module level
        from motet.core.commands.command_type_registry import (
            command_type_registry, CommandImplementationType
        )
        from motet.core.commands.command_data_registry import command_data_registry
        
        # Get command registration
        registration = command_type_registry.get(command_type)
        if not registration:
            # Get available command types for helpful error message
            available_types = command_type_registry.get_command_types()
            return err(
                f"command type '{command_type}' not found. "
                f"Available types: {', '.join(available_types[:10])}"
                + (f" (and {len(available_types) - 10} more)" if len(available_types) > 10 else "")
            )
        
        # Build command description
        command_info: Dict[str, Any] = {
            "command_type": command_type,
            "implementation_type": registration.implementation_type.value,
            "version": registration.version,
        }
        
        # Add metadata if requested
        if parsed.include_metadata:
            command_info["metadata"] = registration.metadata or {}
            if registration.bundle_id:
                command_info["bundle_id"] = registration.bundle_id
            if registration.hot_loadable:
                command_info["hot_loadable"] = True
        
        # Get data class
        data_class = registration.data_class
        if not data_class:
            # Try to get from data registry
            data_class = command_data_registry.get(command_type)
        
        if data_class:
            # Get description from docstring
            if hasattr(data_class, '__doc__') and data_class.__doc__:
                docstring = data_class.__doc__.strip()
                # Extract first paragraph as description
                description = docstring.split('\n\n')[0] if '\n\n' in docstring else docstring.split('\n')[0]
                command_info["description"] = description
            
            # Extract detailed field information
            fields_info = _extract_field_info(data_class)
            
            # Separate required and optional fields
            required_fields = {}
            optional_fields = {}
            for field_name, field_data in fields_info.items():
                if field_data.get("required", False):
                    required_fields[field_name] = field_data
                else:
                    optional_fields[field_name] = field_data
            
            # Include full schema if requested
            if parsed.include_schema:
                try:
                    if hasattr(data_class, 'model_json_schema'):
                        command_info["schema"] = data_class.model_json_schema()
                    elif hasattr(data_class, 'schema'):
                        command_info["schema"] = data_class.schema()
                except Exception as e:
                    command_info["schema_error"] = str(e)
            
            # Include structured field information
            command_info["fields"] = {
                "required": required_fields,
                "optional": optional_fields,
                "all": fields_info
            }
            
            # Include example if requested
            if parsed.include_example:
                example = _extract_example_data(data_class)
                if example:
                    # Clean up example - remove None values and PydanticUndefined
                    cleaned_example = {}
                    for key, value in example.items():
                        if value is not None and value != "PydanticUndefined" and not (isinstance(value, str) and "PydanticUndefined" in value):
                            cleaned_example[key] = value
                    
                    if cleaned_example:
                        command_info["example_data"] = cleaned_example
                        command_info["example_usage"] = {
                            "command_type": command_type,
                            "command_data": cleaned_example
                        }
                        command_info["complete_example"] = {
                            "command_type": command_type,
                            "command_data": cleaned_example,
                            "note": "This is a complete example. Use 'command_describe' with include_schema=true for full JSON schema with all validation rules."
                        }
            
            # Add helpful summary for LLMs
            if fields_info:
                summary_parts = []
                if required_fields:
                    summary_parts.append(f"Required fields ({len(required_fields)}): {', '.join(required_fields.keys())}")
                if optional_fields:
                    summary_parts.append(f"Optional fields ({len(optional_fields)}): {', '.join(optional_fields.keys())}")
                command_info["field_summary"] = ". ".join(summary_parts) + "."
        else:
            command_info["note"] = "No data class registered for this command type"
        
        return ok(command_info)
        
    except Exception as exc:
        return err(f"failed to describe command: {str(exc)}")


def _parse(line: str, trig: str) -> Dict[str, Any]:
    """Parse natural language input into structured parameters."""
    rest = line[len(trig):].strip()
    if not rest:
        return {}
    
    if rest.startswith("command_type="):
        return {"command_type": rest.split("=", 1)[1].strip()}
    
    # Treat as command_type directly
    return {"command_type": rest}


def register(registry: ToolRegistry) -> None:
    registry.register(
        name="core.command_describe",
        description="Describe a single command type including full schema, metadata, and example usage. Use this to understand how to construct command_data for scheduling or execution. Similar to tool_describe but for distributed commands.",
        func=run,
        tool_schema=CommandDescribeParams,
        triggers=["command:", "command_describe:", "describe_command:"],
        parse_params=_parse,
        category="system",
        contextualize_observation=False,  # Don't truncate - user wants full command details
        default_timeout_seconds=3.0,
        suggested_max_calls=1,
        cost_class="low",
    )


__all__ = ["register"]
