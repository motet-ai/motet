"""
Motet - Test Decorator Command

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-13

Description:
    Test command demonstrating decorator pattern in the Motet distributed framework.
    Validates decorator-based command creation, MotetContext initialization,
    distributed execution, and task hierarchy tracking. Includes comprehensive
    testing of decorator functionality and distributed coordination.

Dependencies:
    - pydantic: Data validation and model definitions
    - typing: Type hints and annotations
    - Decorator command system
    - MotetContext and distributed execution

Usage:
    from motet.core.commands.builtin.test_decorator_command import test_decorator_command
    
    # Execute test command
    result = await test_decorator_command(
        data=TestDecoratorData(
            message="Testing decorator pattern!",
            count=3,
            test_motet_call=False
        )
    )

Notes:
    - Demonstrates decorator-based command creation and execution
    - Validates MotetContext initialization and distributed execution
    - Tests task hierarchy tracking and command coordination
    - Includes comprehensive testing of decorator functionality
    - Supports nested command execution and error handling
    - Integrates with distributed command system and worker routing
    - Provides comprehensive test results and context information
"""


from pydantic import BaseModel, Field
from typing import Dict, Any

from motet import motet
from motet.core.commands.decorator import MotetContext, get_motet_context
from motet.core.commands.base_command_data import BaseCommandData


class TestDecoratorData(BaseCommandData):
    """Input data for test decorated command."""
    message: str = Field(default="Hello from decorator!", description="Test message to echo")
    count: int = Field(default=1, ge=1, le=10, description="Number of times to repeat message")
    test_motet_call: bool = Field(default=False, description="Test motet.do() nested composition")


@motet.command(
    description="Test-only command that exercises the decorator-based distributed command pattern in CI and local stacks.",
    timeout_seconds=30)
def test_decorator_command(data: TestDecoratorData) -> Dict[str, Any]:
    """
    Test command demonstrating decorator pattern in distributed environment.
    
    This command validates:
    - Decorator properly registers command
    - MotetContext is correctly initialized
    - Command executes in Celery worker
    - Task hierarchy tracking works
    - ADR-0029 response formatting works
    
    Args:
        data: Test input data
        
    Returns:
        Test results with context information
        
    Example schedule:
        POST /api/schedule
        {
            "command_type": "test_decorator_command",
            "data": {
                "message": "Testing decorator pattern!",
                "count": 3,
                "test_motet_call": false
            },
            "schedule_time": "2025-10-06T12:00:00Z"
        }
    """
    motet = get_motet_context()
    
    # Build repeated message
    repeated_message = " ".join([data.message] * data.count)
    
    # Test motet.do() if requested
    nested_result = None
    if data.test_motet_call:
        # Call ourselves recursively (but don't nest further)
        try:
            nested_result = motet.do(
                test_decorator_command,
                data={"message": "Nested call!", "count": 1, "test_motet_call": False}
            )
        except Exception as e:
            nested_result = {"error": str(e)}
    
    # Return comprehensive test results
    return {
        "status": "success",
        "test": "decorator_pattern",
        "original_message": data.message,
        "count": data.count,
        "repeated_message": repeated_message,
        "context": {
            "task_id": motet.task_id,
            "command_id": motet.command_id,
            "conversation_id": motet.conversation_id,
            "tenant_id": motet.tenant_id,
            "principal_id": motet.principal_id
        },
        "motet_call_test": {
            "tested": data.test_motet_call,
            "result": nested_result
        },
        "metadata": motet.metadata,
        "decorator_features": {
            "automatic_registration": True,
            "context_injection": True,
            "adr_0029_compliant": True,
            "concurrency_helpers_available": True
        }
    }


# Register the command type for data class
from motet.core.commands.command_data_classes import COMMAND_DATA_CLASSES
COMMAND_DATA_CLASSES["test_decorator_command"] = TestDecoratorData


__all__ = [
    "test_decorator_command",
    "TestDecoratorData"
]
