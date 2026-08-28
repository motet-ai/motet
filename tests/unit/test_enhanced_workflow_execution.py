#!/usr/bin/env python3
"""
Unit Tests for Enhanced Workflow Execution

This module contains unit tests for the enhanced workflow execution system.
"""

import pytest
import asyncio
from unittest.mock import Mock, AsyncMock, patch
from typing import Dict, Any, List

from motet.core.commands.builtin.workflow import (
    workflow_execution
)
# WorkflowDependencyGraph no longer exists - workflow execution uses WorkflowExecutor internally
from motet.core.workers.observers import EventPriority


# Deprecated test classes removed:
# - TestWorkflowDependencyGraph: WorkflowDependencyGraph class no longer exists
# - TestWorkflowExecutionCommand: workflow_execution is the decorator-based command (ADR-0030)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
