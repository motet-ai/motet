"""
Motet - Transform Command

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Transform command for result transformation.
    Provides declarative way to transform tool outputs for use in subsequent workflow steps.

    Supported transforms:
    - Phase 2: regex_extract, json_parse, json_path, default, first/last
    - Phase 3: substring, trim, upper, lower, split, join, replace
    - MCP unwrap: mcp_text (envelope → text), playwright_result (### Result body)

Dependencies:
    - pydantic: Data validation
    - structlog: Structured logging
    - Decorator pattern
    - motet.core.tools.result_formatting: Shared MCP text unwrap for mcp_text

Usage:
    from motet.core.commands.builtin.transform import TransformData, TransformOperation
    
    data = TransformData(
        input="{create_sheet.result}",
        operations=[
            TransformOperation(type="mcp_text", output_key="raw"),
            TransformOperation(
                type="regex_extract",
                pattern=r"ID: ([\\w]+)",
                group=1,
                output_key="spreadsheet_id"
            )
        ]
    )
    
    result = motet.do(transform, data=data)
    # Returns: {"raw": "...", "spreadsheet_id": "1a2b3c4d5e"}

Notes:
    - All operations are applied in sequence
    - Each operation stores its result with a custom output_key
    - Designed for inspectability in Task visualizer
    - json_parse expects a JSON string, not a dict (use mcp_text first for MCP tools)
    - Playwright browser_evaluate reports are markdown; use playwright_result
      after mcp_text before json_parse
"""

import re
import json
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field
import structlog

from motet import motet
from motet.core.commands.decorator import MotetContext
from motet.core.commands.base_command_data import BaseCommandData
from motet.core.tools.result_formatting import extract_text_from_mcp_result


class TransformOperation(BaseModel):
    """
    Single transformation operation.
    
    Supported operation types:
    - regex_extract: Extract text using regex pattern
    - json_parse: Parse a JSON string into an object (not a dict — use mcp_text first)
    - json_path: Extract value using JSONPath expression ($.path.to.field)
    - mcp_text: Unwrap MCP CallToolResult / tool_execution payload to text
    - playwright_result: Take the ### Result body from a Playwright MCP markdown report
    - default: Return default value if input is None/empty
    - first: Get first element of array
    - last: Get last element of array
    - substring: Extract substring by start/end index
    - trim: Remove leading/trailing whitespace
    - upper: Convert to uppercase
    - lower: Convert to lowercase
    - split: Split string into array
    - join: Join array elements into string
    - replace: Replace substring with another string
    """
    type: str = Field(
        ...,
        description=(
            "Transform type. MCP tool steps: mcp_text unwraps result.content[] / "
            "structuredContent (and tool_execution wrappers). Playwright "
            "browser_evaluate: add playwright_result after mcp_text, then json_parse. "
            "json_parse expects a JSON string, not a dict. Also: regex_extract, "
            "json_path, default, first, last, substring, trim, upper, lower, split, "
            "join, replace, to_rows."
        ),
    )
    output_key: Optional[str] = Field(None, description="Key to store result (default: 'result')")
    
    # Regex operations
    pattern: Optional[str] = Field(None, description="Regex pattern for regex_extract or split delimiter")
    group: Optional[int] = Field(1, description="Capture group number for regex_extract (default: 1)")
    
    # JSONPath operations
    path: Optional[str] = Field(None, description="JSONPath expression (e.g., '$.data.items[0].name')")
    
    # Substring operations
    start: Optional[int] = Field(None, description="Start index for substring")
    end: Optional[int] = Field(None, description="End index for substring")
    
    # Replace operations
    old: Optional[str] = Field(None, description="String to replace")
    new: Optional[str] = Field(None, description="Replacement string")
    
    # Join operations
    separator: Optional[str] = Field(" ", description="Separator for join operation")
    
    # Default operations
    default_value: Optional[Any] = Field(None, description="Default value if input is None/empty")


class TransformData(BaseCommandData):
    """
    Data for transform command.
    
    Applies a sequence of transformations to input data.
    Each operation can store its result with a custom key for later reference.
    
    Extends BaseCommandData to provide:
    - metadata: Optional metadata for the transform operation
    - execution_hints: Optional hints for execution optimization
    - Serialization methods: to_serializable_dict(), from_serializable_dict()
    - Monitoring methods: get_data_size_estimate(), is_large_data()
    - Context methods: get_context_summary()
    
    Example:
        TransformData(
            input="{create_sheet.result}",
            operations=[
                TransformOperation(type="mcp_text", output_key="raw"),
                TransformOperation(
                    type="regex_extract",
                    pattern=r"ID: ([\\w]+)",
                    group=1,
                    output_key="spreadsheet_id"
                )
            ],
            metadata={"workflow_id": "research_to_sheets", "step_id": "extract_sheet_id"}
        )
    """
    input: Any = Field(..., description="Input value to transform (can use template variables)")
    operations: List[TransformOperation] = Field(
        ...,
        description=(
            "List of transformations to apply in sequence. For MCP tool output "
            "bound as {{step.result}}, start with mcp_text; for Playwright "
            "markdown reports add playwright_result before json_parse."
        ),
    )


@motet.command(
    description="Apply declarative transforms to step or command output data (map fields, reshape payloads) inside workflows.",
    timeout_seconds=5)
def transform(data: TransformData, motet: MotetContext) -> Dict[str, Any]:
    """
    Transform - apply transformations to input data (ADR-0049 Fix #4).
    
    Provides a declarative way to transform tool outputs for use in subsequent steps.
    Each operation is applied in sequence, with the output of one operation
    becoming the input to the next.
    
    Args:
        data: Transform configuration with input and operations
        motet: Motet context (not used, but required by decorator)
    
    Returns:
        Dict with all transformation results, keyed by output_key
    
    Example:
        # Extract spreadsheet ID from text response
        TransformData(
            input="Created sheet 'My Sheet'. ID: 1a2b3c4d5e | URL: https://...",
            operations=[
                TransformOperation(
                    type="regex_extract",
                    pattern=r"ID: ([\\w]+)",
                    group=1,
                    output_key="spreadsheet_id"
                )
            ]
        )
        # Returns: {"spreadsheet_id": "1a2b3c4d5e"}
    """
    logger = structlog.get_logger(__name__)
    
    result = data.input
    outputs = {}
    
    logger.info(
        "Starting transform operations",
        input_type=type(result).__name__,
        operation_count=len(data.operations)
    )
    
    for i, operation in enumerate(data.operations):
        op_type = operation.type
        output_key = operation.output_key or f"result_{i}"
        
        logger.debug(
            "Applying transform",
            operation_index=i,
            operation_type=op_type,
            output_key=output_key
        )
        
        try:
            # Phase 2: Essential Transforms
            if op_type == "regex_extract":
                if not operation.pattern:
                    raise ValueError("regex_extract requires 'pattern' parameter")
                match = re.search(operation.pattern, str(result))
                if match:
                    # Try to get the specified group, fall back to group 0 (entire match)
                    grp = operation.group
                    if grp is None:
                        result = match.group(0)
                    else:
                        try:
                            result = match.group(grp)
                        except IndexError:
                            result = match.group(0)
                else:
                    result = ""
                    logger.warning("Regex pattern did not match", pattern=operation.pattern)
            
            elif op_type == "json_parse":
                if isinstance(result, (dict, list)):
                    raise TypeError(
                        "json_parse expected a JSON string, got "
                        f"{type(result).__name__}. For MCP tool output use mcp_text "
                        "first (and playwright_result if the text is a Playwright "
                        "markdown report)."
                    )
                if not isinstance(result, str):
                    raise TypeError(
                        f"json_parse expected a JSON string, got {type(result).__name__}"
                    )
                result = json.loads(result)

            elif op_type == "mcp_text":
                result = _mcp_text(result)

            elif op_type == "playwright_result":
                result = _extract_playwright_result(result)
            
            elif op_type == "json_path":
                if not operation.path:
                    raise ValueError("json_path requires 'path' parameter")
                result = _extract_json_path(result, operation.path)
            
            elif op_type == "default":
                # Return default value if result is None or empty
                if result is None:
                    result = operation.default_value
                elif isinstance(result, (str, list, dict)) and len(result) == 0:
                    result = operation.default_value
            
            elif op_type == "first":
                if isinstance(result, list) and len(result) > 0:
                    result = result[0]
                else:
                    result = None
                    logger.warning("first operation on non-list or empty list")
            
            elif op_type == "last":
                if isinstance(result, list) and len(result) > 0:
                    result = result[-1]
                else:
                    result = None
                    logger.warning("last operation on non-list or empty list")
            
            # Phase 3: String Transforms
            elif op_type == "substring":
                if operation.start is None:
                    raise ValueError("substring requires 'start' parameter")
                result = str(result)[operation.start:operation.end]
            
            elif op_type == "trim":
                result = str(result).strip()
            
            elif op_type == "upper":
                result = str(result).upper()
            
            elif op_type == "lower":
                result = str(result).lower()
            
            elif op_type == "split":
                delimiter = operation.pattern or operation.separator or " "
                result = str(result).split(delimiter)
            
            elif op_type == "join":
                if isinstance(result, list):
                    separator = operation.separator or " "
                    result = separator.join(str(item) for item in result)
                else:
                    logger.warning("join operation on non-list", type=type(result).__name__)
            
            elif op_type == "replace":
                if operation.old is None:
                    raise ValueError("replace requires 'old' parameter")
                result = str(result).replace(operation.old, operation.new or "")
            
            elif op_type == "to_rows":
                # Convert list of strings to 2D array (list of rows) for Google Sheets
                # Input: ["result1", "result2"] -> Output: [["result1"], ["result2"]]
                if isinstance(result, list):
                    result = [[str(item)] for item in result if item]  # Filter out empty items
                else:
                    # If not a list, wrap it in a list
                    result = [[str(result)]]
            
            else:
                raise ValueError(f"Unknown transform operation type: {op_type}")
            
            # Store result with the specified key
            outputs[output_key] = result
            
            logger.debug(
                "Transform applied successfully",
                operation_type=op_type,
                output_key=output_key,
                result_type=type(result).__name__
            )
        
        except Exception as e:
            logger.error(
                "Transform operation failed",
                operation_index=i,
                operation_type=op_type,
                error=str(e),
                exc_info=True
            )
            raise RuntimeError(f"Transform operation '{op_type}' failed: {e}") from e
    
    logger.info(
        "Transform operations completed",
        output_keys=list(outputs.keys()),
        final_result_type=type(result).__name__
    )
    
    return outputs


def _mcp_text(value: Any) -> str:
    """Unwrap MCP envelopes and tool_execution payloads to text."""
    if isinstance(value, dict):
        inner = value.get("result")
        if isinstance(inner, dict) and (
            "content" in inner or "structuredContent" in inner
        ):
            value = inner
    return extract_text_from_mcp_result(value)


_PLAYWRIGHT_RESULT_SECTION = re.compile(
    r"### Result\s*\n(.*?)(?=\n### |\Z)",
    re.DOTALL,
)


def _extract_playwright_result(value: Any) -> str:
    """Take the ### Result body from a Playwright MCP markdown report.

    If the heading is absent, return the stripped input (no-op for GitHub-style
    JSON text). When Playwright stringified a JS string, the body is a JSON
    string literal — unwrap one layer so json_parse sees the inner payload.
    """
    if not isinstance(value, str):
        raise TypeError(
            "playwright_result expected a markdown string, got "
            f"{type(value).__name__}. Use mcp_text first to unwrap the MCP envelope."
        )
    match = _PLAYWRIGHT_RESULT_SECTION.search(value)
    body = match.group(1).strip() if match else value.strip()
    if len(body) >= 2 and body[0] == '"' and body[-1] == '"':
        try:
            decoded = json.loads(body)
        except json.JSONDecodeError:
            return body
        if isinstance(decoded, str):
            return decoded
    return body


def _extract_json_path(value: Any, path: str) -> Any:
    """
    Extract value using simplified JSONPath expression.
    
    Supports:
    - $.field - Top-level field
    - $.field.subfield - Nested fields
    - $.field[0] - Array indexing
    - $.field[0].subfield - Array element fields
    
    Args:
        value: Object to extract from (dict or list)
        path: JSONPath expression (e.g., '$.data.items[0].name')
    
    Returns:
        Extracted value or None if path not found
    """
    if not path:
        return value
    
    # Remove leading $.
    if path.startswith('$.'):
        path = path[2:]
    elif path.startswith('$'):
        path = path[1:]
    
    if not path:
        return value
    
    # Split path into parts, preserving array indices
    # Example: "data.items[0].name" -> ["data", "items[0]", "name"]
    parts = re.split(r'\.(?![^\[]*\])', path)
    
    result = value
    for part in parts:
        if result is None:
            return None
        
        # Check for array index: "items[0]"
        array_match = re.match(r'^(\w+)\[(\d+)\]$', part)
        if array_match:
            field_name = array_match.group(1)
            index = int(array_match.group(2))
            
            # First access the field
            if isinstance(result, dict):
                result = result.get(field_name)
            else:
                return None
            
            # Then access the array index
            if isinstance(result, list):
                if 0 <= index < len(result):
                    result = result[index]
                else:
                    return None
            else:
                return None
        
        # Check if entire part is just an array index: "[0]"
        elif (bracket_m := re.match(r'^\[(\d+)\]$', part)):
            index = int(bracket_m.group(1))
            if isinstance(result, list):
                if 0 <= index < len(result):
                    result = result[index]
                else:
                    return None
            else:
                return None
        
        # Regular field access
        else:
            if isinstance(result, dict):
                result = result.get(part)
            else:
                return None
    
    return result

