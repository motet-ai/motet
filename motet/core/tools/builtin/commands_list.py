"""
Motet - Commands List Tool

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Commands list tool for the Motet distributed framework.
    Provides the ability to list and query available command types with
    descriptions, metadata, and example data structures. Enables LLMs to
    discover what commands can be scheduled or executed.

Dependencies:
    - pydantic: Data validation and model definitions
    - typing: Type hints and annotations
    - Tool registry and runtime stack
    - Command type registry
    - Command data registry

Usage:
    from motet.core.tools.builtin.commands_list import run

    # List all commands
    result = run({})
    
    # List commands with filtering
    result = run({
        "name_contains": "reasoning",
        "include_schema": True,
        "limit": 20
    })

Notes:
    - Lists available command types from command_type_registry
    - Bundle commands are registered in the worker's in-memory registry at startup
      via load_bundles_on_startup, so they appear here automatically
    - Includes command metadata (capabilities, timeout, etc.)
    - Shows data class schema information
    - Supports filtering by name, implementation type, bundle
    - Returns example command_data structures
    - Integrates with command type and data registries
    - Includes comprehensive error handling
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

from ..registry import ToolRegistry, get_runtime_stack
from ..protocol import ok, err


class CommandsListParams(BaseModel):
    """Parameters for listing commands."""
    name_contains: Optional[str] = Field(default=None, description="Filter commands by name containing this string")
    implementation_type: Optional[str] = Field(default=None, description="Filter by implementation type: 'class' or 'decorator'")
    bundle_id: Optional[str] = Field(default=None, description="Filter by bundle ID (manifest name)")
    include_schema: bool = Field(default=False, description="Include JSON schema for command data classes")
    include_metadata: bool = Field(default=True, description="Include command metadata (capabilities, timeout, etc.)")
    include_example: bool = Field(default=True, description="Include example command_data structure")
    limit: Optional[int] = Field(default=100, ge=1, le=500, description="Maximum number of commands to return")
    offset: int = Field(default=0, ge=0, description="Number of commands to skip")


def _extract_example_data(data_class: Any) -> Dict[str, Any]:
    """Extract example data structure from Pydantic model."""
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
            # Check for PydanticUndefined - it's a sentinel value, not a real default
            try:
                from pydantic import PydanticUndefined
                if hasattr(field_info, 'default'):
                    default_value = field_info.default
                    # Check if it's actually PydanticUndefined (sentinel value) - skip it
                    if default_value is PydanticUndefined or default_value is ...:
                        # No real default - will infer from type below
                        pass
                    else:
                        # Field has a real default value
                        example[field_name] = default_value
                        continue
            except (ImportError, AttributeError):
                # PydanticUndefined not available or field_info doesn't have default
                if hasattr(field_info, 'default') and field_info.default is not ...:
                    example[field_name] = field_info.default
                    continue
            
            # Check for default_factory
            if hasattr(field_info, 'default_factory') and field_info.default_factory:
                # Handle default_factory (e.g., list, dict)
                if field_info.default_factory == list:
                    # Special handling for messages field - provide example structure
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
                # Handle List types (including List[Any], List[Message], etc.)
                if hasattr(annotation, '__origin__'):
                    origin = annotation.__origin__
                    if origin is list:
                        # Check if it's List[Message] or similar
                        args = getattr(annotation, '__args__', ())
                        if args and len(args) > 0:
                            arg_type = args[0]
                            # Special handling for Message types
                            if 'Message' in str(arg_type) or 'message' in field_name.lower():
                                example[field_name] = [
                                    {"role": "user", "content": "example message"}
                                ]
                            else:
                                example[field_name] = []
                        else:
                            # List[Any] or just list - check field name for hints
                            if 'message' in field_name.lower():
                                example[field_name] = [
                                    {"role": "user", "content": "example message"}
                                ]
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
                    # Special handling for messages field
                    if 'message' in field_name.lower():
                        example[field_name] = [
                            {"role": "user", "content": "example message"}
                        ]
                    else:
                        example[field_name] = []
                elif annotation == dict:
                    example[field_name] = {}
                else:
                    # Try to get string representation to check for Message
                    annotation_str = str(annotation)
                    if 'Message' in annotation_str or 'message' in field_name.lower():
                        example[field_name] = [
                            {"role": "user", "content": "example message"}
                        ]
                    else:
                        example[field_name] = None
            else:
                # No annotation - check field name for hints
                if 'message' in field_name.lower():
                    example[field_name] = [
                        {"role": "user", "content": "example message"}
                    ]
                else:
                    example[field_name] = None
        
        return example
    except Exception:
        return {}


def run(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    List available command types (synchronous for Celery workers - ADR-0033).

    Queries command_type_registry to retrieve all registered command types
    with their metadata, schemas, and example data structures.
    """
    try:
        # Parse parameters
        parsed = CommandsListParams(**(params or {}))
    except Exception as exc:
        return err(f"validation error: {exc}")
    
    try:
        # Import here to avoid circular dependencies at module level
        from motet.core.commands.command_type_registry import (
            command_type_registry, CommandImplementationType
        )
        from motet.core.commands.command_data_registry import command_data_registry
        
        # Get all command registrations
        all_registrations = command_type_registry.get_all_registrations()
        
        # Filter commands
        filtered_commands = []
        name_filter = (parsed.name_contains or "").lower().strip()
        
        for command_type, registration in all_registrations.items():
            # Filter by name
            if name_filter and name_filter not in command_type.lower():
                continue
            
            # Filter by implementation type
            if parsed.implementation_type:
                impl_type = parsed.implementation_type.lower()
                if impl_type == "class" and registration.implementation_type != CommandImplementationType.CLASS_BASED:
                    continue
                elif impl_type == "decorator" and registration.implementation_type != CommandImplementationType.DECORATOR_BASED:
                    continue
            
            # Filter by bundle_id
            if parsed.bundle_id and registration.bundle_id != parsed.bundle_id:
                continue
            
            # Build command info
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
            
            # Get data class if available
            data_class = registration.data_class
            if not data_class:
                # Try to get from data registry
                data_class = command_data_registry.get(command_type)
            
            if data_class:
                # Get description from docstring
                if hasattr(data_class, '__doc__') and data_class.__doc__:
                    command_info["description"] = data_class.__doc__.strip().split('\n')[0]
                
                # Include schema if requested
                if parsed.include_schema:
                    try:
                        if hasattr(data_class, 'model_json_schema'):
                            command_info["schema"] = data_class.model_json_schema()
                        elif hasattr(data_class, 'schema'):
                            command_info["schema"] = data_class.schema()
                    except Exception:
                        pass  # Schema extraction failed, skip it
                
                # Include example if requested
                if parsed.include_example:
                    example = _extract_example_data(data_class)
                    if example:
                        # Clean up any PydanticUndefined values that might have been serialized as strings
                        cleaned_example = {}
                        for key, value in example.items():
                            # Skip PydanticUndefined sentinel values (they might be serialized as strings)
                            if value == "PydanticUndefined" or (isinstance(value, str) and "PydanticUndefined" in str(value)):
                                # Replace with proper example based on field name
                                if 'message' in key.lower():
                                    cleaned_example[key] = [{"role": "user", "content": "example message"}]
                                else:
                                    continue  # Skip undefined fields
                            else:
                                cleaned_example[key] = value
                        if cleaned_example:
                            command_info["example_data"] = cleaned_example
            
            filtered_commands.append(command_info)

        # Sort by command_type
        filtered_commands.sort(key=lambda x: x["command_type"])
        
        # Apply limit and offset
        total = len(filtered_commands)
        start = parsed.offset
        end = start + parsed.limit if parsed.limit else total
        paginated_commands = filtered_commands[start:end]
        
        result = {
            "total": total,
            "commands": paginated_commands,
            "limit": parsed.limit,
            "offset": parsed.offset,
            "filters": {
                "name_contains": parsed.name_contains,
                "implementation_type": parsed.implementation_type,
                "bundle_id": parsed.bundle_id,
            }
        }
        
        # Add pagination warning if not all results are returned
        if parsed.limit and len(paginated_commands) < total:
            result["pagination_note"] = (
                f"Showing {len(paginated_commands)} of {total} commands. "
                f"To get all commands, call again with limit={total} or limit=500 (max). "
                f"DO NOT invent or hallucinate command names - only use commands from the actual response."
            )
        
        return ok(result)
        
    except Exception as exc:
        return err(f"failed to list commands: {str(exc)}")


def _parse(ln: str, trig: str) -> Dict[str, Any]:
    """Parse natural language input into structured parameters."""
    text = ln[len(trig):].strip()
    params: Dict[str, Any] = {}
    
    # Simple parsing: extract key=value pairs
    if "=" in text:
        parts = text.split()
        for part in parts:
            if "=" in part:
                k, v = part.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k == "name" or k == "name_contains":
                    params["name_contains"] = v
                elif k == "type" or k == "implementation_type":
                    params["implementation_type"] = v
                elif k == "bundle":
                    params["bundle_id"] = v
                elif k == "limit":
                    try:
                        params["limit"] = int(v)
                    except ValueError:
                        pass
                elif k == "schema":
                    params["include_schema"] = (v.lower() == "true")
    elif text:
        # If just text provided, treat as name filter
        params["name_contains"] = text
    
    return params


def _fmt(res: Dict[str, Any]) -> str:
    """Format result for observation."""
    if "error" in res:
        return f"commands_list(error={res['error']})"
    
    total = res.get("total", 0)
    return f"commands_list(count={total})"


def register(registry: ToolRegistry) -> None:
    registry.register(
        name="core.commands_list",
        description="List available command types with descriptions, schemas, and examples. Use this to discover what commands can be scheduled or executed. Similar to tools_list but for distributed commands. "
        "IMPORTANT: When user asks for 'all commands' or 'list all commands', you MUST call this tool with limit=500 (or omit limit to get all). "
        "The default limit is 100, but if the response shows total > limit, you need to call again with a higher limit to get the complete list. "
        "NEVER hallucinate or invent command names - only return commands that are actually in the response.",
        func=run,
        tool_schema=CommandsListParams,
        triggers=["commands:", "commands_list:", "list_commands:", "command_types:"],
        priority=5,
        parse_params=_parse,
        observation_formatter=_fmt,
        category="system",
        default_timeout_seconds=5.0,
        suggested_max_calls=5,
        cost_class="low",
    )


__all__ = ["register"]
