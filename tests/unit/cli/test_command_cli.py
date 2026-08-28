"""
Tests for command development CLI tools.

Tests:
- Command validation and mock motet
- Command listing and info (via API)
"""

import pytest
from unittest.mock import Mock, patch, MagicMock
from motet.cli.testing import create_mock_motet, run_command_test, validate_command_structure


def test_create_mock_motet():
    """Test creating mock MotetContext."""
    mock_motet = create_mock_motet(
        tools_result={"result": "test"},
        agent_result="AI response"
    )
    
    assert mock_motet.tools is not None
    assert mock_motet.do is not None  # model inference via motet.do(model_inference, ...)
    assert mock_motet.memory is not None
    assert mock_motet.stream_event is not None
    assert mock_motet.publish_event is not None


def test_validate_command_structure():
    """Test command structure validation."""
    from motet.core.commands.decorator import distributed_command
    from motet.core.commands.decorator import MotetContext
    from pydantic import BaseModel
    from typing import Dict, Any
    
    class TestData(BaseModel):
        value: str
    
    @distributed_command()
    def test_command(data: TestData, motet: MotetContext) -> Dict[str, Any]:
        return {"result": "test"}
    
    result = validate_command_structure(test_command)
    
    assert result["valid"] is True
    assert "data_class" in result["metadata"]

