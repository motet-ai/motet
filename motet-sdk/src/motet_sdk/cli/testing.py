"""
Motet - Command Testing Utilities

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2025-11-14

Description:
    Automated testing and validation utilities for command development.
    Provides test runners, validators, and mock motet context.

Dependencies:
    - unittest.mock: Mock objects
    - pytest: Testing framework (optional)
    - typing: Type hints

Usage:
    from motet_sdk.cli.testing import create_mock_motet, test_command
    
    # Create mock motet
    mock_motet = create_mock_motet()
    
    # Test command
    result = test_command(my_command, MyCommandData(...), mock_motet)

Notes:
    - Provides testing utilities for command development
    - Includes mock MotetContext for unit testing
    - Supports both decorator-based and class-based commands
"""

from typing import Dict, Any, Optional, Type
from unittest.mock import Mock, MagicMock
from pydantic import BaseModel


def create_mock_motet(
    tools_result: Optional[Any] = None,
    agent_result: Optional[Any] = None,
    memory_result: Optional[Any] = None
) -> Mock:
    """
    Create a mock MotetContext for testing.
    
    Args:
        tools_result: Mock result for motet.tools.execute()
        agent_result: Optional; if set, motet.do() is mocked to return {"content": agent_result}
            for model inference (use model_inference command, not motet.llm).
        memory_result: Mock result for motet.memory operations
    
    Returns:
        Mock MotetContext object
    """
    from motet.core.commands.decorator import MotetContext
    
    mock_motet = Mock(spec=MotetContext)
    
    # Mock tools
    mock_motet.tools = Mock()
    mock_motet.tools.execute = Mock(return_value=tools_result or {"result": "success"})
    mock_motet.tools.list = Mock(return_value=[])
    
    # Mock motet.do() for model inference (canonical path); no motet.llm
    if agent_result is not None:
        content = getattr(agent_result, "content", agent_result) if not isinstance(agent_result, str) else agent_result
        mock_motet.do = Mock(return_value={"content": content})
    else:
        mock_motet.do = Mock(return_value={"content": "Mock AI response"})
    
    # Mock memory
    mock_motet.memory = Mock()
    mock_motet.memory.store = Mock(return_value="memory_id")
    mock_motet.memory.recall = Mock(return_value=memory_result or [])
    
    # Mock vault
    mock_motet.vault = Mock()
    mock_motet.vault.get_credential = Mock(return_value=None)
    
    # Mock streaming
    mock_motet.stream_event = Mock()
    mock_motet.ensure_stream = Mock()
    mock_motet.reset_stream = Mock()
    
    # Mock event publishing
    mock_motet.publish_event = Mock()
    mock_motet.observe_events = Mock()
    
    # Mock command composition
    mock_motet.do = Mock()
    mock_motet.join = Mock()
    mock_motet.apply = Mock()
    mock_motet.maybe = Mock()
    
    # Mock context
    mock_motet.command_id = "test_command_id"
    mock_motet.task_id = "test_task_id"
    mock_motet.conversation_id = "test_conversation_id"
    mock_motet.tenant_id = "test_tenant_id"
    mock_motet.principal_id = "test_principal_id"
    
    return mock_motet


def run_command_test(
    command_func,
    data: BaseModel,
    mock_motet: Optional[Mock] = None,
    expected_result: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    Test a decorated command function.
    
    Args:
        command_func: Command function (decorated)
        data: Command input data (Pydantic model)
        mock_motet: Optional mock MotetContext (creates default if not provided)
        expected_result: Optional expected result for validation
    
    Returns:
        Command result dict
    
    Example:
        result = run_command_test(
            my_command,
            MyCommandData(input_value="test"),
            create_mock_motet()
        )
    """
    if mock_motet is None:
        mock_motet = create_mock_motet()
    
    # Call the wrapped function (bypasses decorator for testing)
    if hasattr(command_func, '__wrapped__'):
        result = command_func.__wrapped__(data=data, motet=mock_motet)
    else:
        result = command_func(data=data, motet=mock_motet)
    
    # Validate result structure
    assert isinstance(result, dict), "Command must return a dict"
    assert "status" in result or "result" in result, "Command result must include status or result"
    
    # Validate expected result if provided
    if expected_result:
        for key, value in expected_result.items():
            assert key in result, f"Result missing key: {key}"
            assert result[key] == value, f"Result[{key}] = {result[key]}, expected {value}"
    
    return result


def validate_command_structure(command_func) -> Dict[str, Any]:
    """
    Validate command structure and return metadata.
    
    Args:
        command_func: Command function to validate
    
    Returns:
        Dict with validation results and metadata
    """
    import inspect
    
    result = {
        "valid": True,
        "errors": [],
        "warnings": [],
        "metadata": {}
    }
    
    # Check if function is decorated
    if not hasattr(command_func, '__wrapped__'):
        result["warnings"].append("Function may not be decorated with @distributed_command")
    
    # Get function signature
    sig = inspect.signature(command_func.__wrapped__ if hasattr(command_func, '__wrapped__') else command_func)
    
    # Check parameters
    params = list(sig.parameters.values())
    if len(params) < 2:
        result["errors"].append("Command must have at least 2 parameters: data and motet")
        result["valid"] = False
    
    # Check data parameter
    if len(params) > 0:
        data_param = params[0]
        if data_param.annotation == inspect.Parameter.empty:
            result["errors"].append("Data parameter must have type hint")
            result["valid"] = False
        else:
            result["metadata"]["data_class"] = data_param.annotation.__name__
    
    # Check motet parameter
    if len(params) > 1:
        motet_param = params[1]
        if "MotetContext" not in str(motet_param.annotation):
            result["warnings"].append("Motet parameter should be MotetContext")
    
    # Check return type
    if sig.return_annotation == inspect.Signature.empty:
        result["warnings"].append("Command should have return type hint: Dict[str, Any]")
    elif "Dict" not in str(sig.return_annotation):
        result["warnings"].append("Command should return Dict[str, Any]")
    
    return result

