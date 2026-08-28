# Testing Strategies

Writing effective tests for distributed systems requires understanding how to test commands, workflows, and distributed operations. This section covers testing strategies, patterns, and best practices.

## Testing Philosophy

### Principles

1. **Test in Isolation**: Test commands independently when possible
2. **Mock External Dependencies**: Mock MotetContext, tools, memory
3. **Test Distributed Behavior**: Test actual distributed execution when needed
4. **Test Error Scenarios**: Don't just test happy paths
5. **Test Performance**: Verify performance characteristics

## Unit Testing

### Testing Decorated Commands

Test the underlying function directly:

```python
"""Tests for text analysis command."""

import pytest
from unittest.mock import Mock
from motet.core.commands.builtin.text_analysis import (
    text_analysis,
    TextAnalysisData
)
from motet.core.commands.decorator import MotetContext

@pytest.fixture
def mock_motet():
    """Create mock motet context."""
    motet = Mock(spec=MotetContext)
    motet.memory = Mock()
    motet.memory.store = Mock()
    motet.memory.recall = Mock(return_value=[])
    motet.command_id = "test-command-id"
    motet.task_id = "test-task-id"
    return motet

def test_text_analysis_basic(mock_motet):
    """Test basic text analysis."""
    data = TextAnalysisData(text="This is a test sentence.")
    
    # Call the underlying function (bypass decorator)
    result = text_analysis.__wrapped__(data, mock_motet)
    
    assert result["word_count"] == 5
    assert result["char_count"] == 25
    assert "sentiment" in result

def test_text_analysis_with_memory(mock_motet):
    """Test text analysis with memory storage."""
    data = TextAnalysisData(
        text="This is a test.",
        store_in_memory=True,
        tags=["test"]
    )
    
    result = text_analysis.__wrapped__(data, mock_motet)
    
    # Verify memory.store was called
    mock_motet.memory.store.assert_called_once()
    assert result["word_count"] == 4

def test_text_analysis_empty_text(mock_motet):
    """Test text analysis with empty text."""
    data = TextAnalysisData(text="")
    
    with pytest.raises(ValueError, match="Text cannot be empty"):
        text_analysis.__wrapped__(data, mock_motet)
```

### Testing Command Composition

Test command composition with mocked motet.do():

```python
def test_command_composition(mock_motet):
    """Test command composition."""
    # Mock motet.do() to return expected results
    mock_motet.do = Mock(side_effect=[
        {"result": "extracted"},
        {"result": "processed"},
        {"result": "stored"}
    ])
    
    result = my_composed_command.__wrapped__(data, mock_motet)
    
    # Verify composition
    assert mock_motet.do.call_count == 3
    assert result["status"] == "complete"
```

## Integration Testing

### Testing with Real Workers

Test actual distributed execution:

```python
"""Integration tests for distributed commands."""

import pytest
from motet_sdk.testing import MockMotetContext

@pytest.mark.integration
def test_distributed_execution():
    """Test the command body against a mock context."""
    motet = MockMotetContext()
    
    result = text_analysis.__wrapped__(
        data=TextAnalysisData(text="Test text"),
        motet=motet,
    )
    
    assert result["word_count"] == 2
```

`motet.do()` is synchronous, so these tests are plain `def`, not `async def`.
Call `.__wrapped__` to run the undecorated function body directly; the
decorator's response envelope is not applied, so assert on your returned
data rather than on a `status` field.

### Testing Workflows

Test workflow execution:

```python
@pytest.mark.integration
def test_workflow_execution():
    """Test workflow execution."""
    from motet.core.workflow import WorkflowRegistry, WorkflowExecutor
    from motet_sdk.testing import MockMotetContext
    
    workflow = WorkflowRegistry.get("document_analysis")
    executor = WorkflowExecutor()
    motet = MockMotetContext()
    
    workflow.context = {
        "document_path": "/test/path",
        "document_type": "test"
    }
    
    result = executor.execute_workflow(workflow, motet)
    
    assert result["status"] == "completed"
    assert "extract" in result["steps"]
    assert "analyze" in result["steps"]
```

## E2E Testing

### Full System Testing

Test complete workflows end-to-end:

```python
"""E2E tests for complete workflows."""

import pytest
from motet.core import MotetStack, Message

@pytest.mark.e2e
async def test_document_analysis_e2e():
    """Test document analysis end-to-end."""
    stack = MotetStack()
    
    # Execute complete workflow
    response = await stack.chat([
        Message(
            role="user",
            content="Analyze the document at /test/document.pdf"
        )
    ])
    
    assert response.content is not None
    assert "analysis" in response.content.lower()
```

## Testing Error Scenarios

### Test Error Handling

```python
def test_error_handling(mock_motet):
    """Test error handling."""
    # Simulate error
    mock_motet.tools.execute = Mock(side_effect=Exception("Tool error"))
    
    with pytest.raises(Exception):
        my_command.__wrapped__(data, mock_motet)
    
    # Verify error was logged
    # (check logs or mock logger)
```

### Test Retry Logic

```python
@pytest.mark.integration
def test_retry_logic():
    """Test retry logic."""
    from motet_sdk.testing import MockMotetContext
    motet = MockMotetContext()
    
    # max_retries is a per-call argument, not command data
    result = motet.do(
        retry_command,
        data=RetryData(),
        max_retries=3
    )
    
    # Verify retries occurred if needed
    assert result["status"] in ["success", "failed"]
```

## Performance Testing

### Test Execution Time

```python
import time

def test_performance(mock_motet):
    """Test command performance."""
    start_time = time.time()
    result = my_command.__wrapped__(data, mock_motet)
    execution_time = time.time() - start_time
    
    assert execution_time < 1.0  # Should complete in < 1 second
    assert result["status"] == "success"
```

### Test Concurrent Execution

```python
@pytest.mark.integration
def test_concurrent_execution():
    """Test concurrent command execution."""
    from motet_sdk.testing import MockMotetContext
    motet = MockMotetContext()
    
    # motet.apply fans out one command over many inputs, in parallel.
    # Do not gather motet.do() calls: motet.do is synchronous and returns
    # a result, not an awaitable.
    results = motet.apply(
        text_analysis,
        inputs=[{"text": f"Text {i}"} for i in range(10)]
    )
    
    assert len(results) == 10
```

## Test Fixtures and Utilities

### Common Fixtures

```python
"""Common test fixtures."""

import pytest
from unittest.mock import Mock
from motet.core.commands.decorator import MotetContext

@pytest.fixture
def mock_motet():
    """Create mock motet context."""
    motet = Mock(spec=MotetContext)
    motet.memory = Mock()
    motet.tools = Mock()
    motet.vault = Mock()
    motet.do = Mock(return_value={"content": "mock response"})  # model calls via motet.do(model_inference, ...)
    motet.command_id = "test-command-id"
    motet.task_id = "test-task-id"
    motet.conversation_id = "test-conversation-id"
    motet.tenant_id = "test-tenant-id"
    motet.principal_id = "test-principal-id"
    return motet

@pytest.fixture
def test_motet():
    """Create a context stub backed by the SDK's mock."""
    from motet_sdk.testing import MockMotetContext
    return MockMotetContext()
```

### Test Utilities

```python
"""Test utilities."""

def create_mock_command_result(data: dict, status: str = "success"):
    """Create mock command result."""
    return {
        "status": status,
        "data": data,
        "command_id": "test-command-id",
        "execution_time_ms": 100
    }

def assert_command_success(result: dict):
    """Assert command succeeded."""
    assert result["status"] == "success"
    assert "data" in result

def assert_command_error(result: dict, error_type: str = None):
    """Assert command failed."""
    assert result["status"] == "error"
    assert "error" in result
    if error_type:
        assert result["error"]["error_type"] == error_type
```

## Best Practices

### 1. Test in Isolation

```python
# ✅ CORRECT: Test command independently
def test_command(mock_motet):
    result = my_command.__wrapped__(data, mock_motet)
    assert result["status"] == "success"
```

### 2. Mock External Dependencies

```python
# ✅ CORRECT: Mock external dependencies
mock_motet.tools.execute = Mock(return_value={"result": "success"})
```

### 3. Test Error Scenarios

```python
# ✅ CORRECT: Test error scenarios
def test_error_handling(mock_motet):
    mock_motet.tools.execute = Mock(side_effect=Exception("Error"))
    with pytest.raises(Exception):
        my_command.__wrapped__(data, mock_motet)
```

### 4. Use Appropriate Test Types

```python
# ✅ CORRECT: Use unit tests for logic
@pytest.mark.unit
def test_logic():
    pass

# ✅ CORRECT: Use integration tests for distributed behavior
@pytest.mark.integration
async def test_distributed():
    pass
```

## Next Steps

Now that you understand testing strategies:

- **[Concurrency Primitives](./19-concurrency-primitives.md)** - Thread-safe code
- **[Best Practices](./27-best-practices.md)** - Learn from experience
- **[Common Patterns](./25-common-patterns.md)** - Learn reusable patterns

## Navigation

- **[← Back to Documentation Home](./00-landing-page.md)** - Main documentation hub

---

**Last Updated**: 2026-02-13

**Ready for advanced topics?** Continue to [Concurrency Primitives](./19-concurrency-primitives.md).
