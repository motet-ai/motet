"""
Motet - Distributed Commands Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Unit tests for distributed command classes, serialization, and execution.

Dependencies:
    - pytest: test framework
    - motet.core.commands.distributed: core distributed types
    - motet.core.types: Message

Usage:
    pytest tests/integration/test_distributed_commands.py

Notes:
    - ReasoningTask/ReasoningService legacy APIs were removed in ADR-0059.
"""

import pytest
import uuid
from datetime import datetime, timedelta
from typing import Optional
from unittest.mock import Mock, AsyncMock

from motet.core.commands.distributed import (
    DistributedCommand, DistributedCommandContext, WorkerCapability, 
    DistributionStrategy, WorkerAssignment
)
# CommandSerializer removed - now using self-serializing commands
from motet.core.commands.builtin.model import (
    model_inference, embedding_generation
)
from motet.core.types import Message


class MockDistributedCommand(DistributedCommand):
    """Mock distributed command for testing. Supports legacy test signature (command_id, task_id, test_data, context) and (command_id, context)."""

    def __init__(
        self,
        command_id: str,
        task_id=None,
        test_data: str = "test",
        context: Optional[DistributedCommandContext] = None,
    ):
        # Support legacy (command_id, context) where second arg is DistributedCommandContext
        if isinstance(task_id, DistributedCommandContext):
            context = task_id
            task_id = None
        # Support legacy (command_id, task_id, test_data, context) and (command_id, task_id, context)
        if context is not None and task_id is None:
            task_id = getattr(context, "task_id", "test-task-123")
        task_id = task_id or "test-task-123"
        from types import SimpleNamespace

        data = SimpleNamespace(test_data=test_data)
        kwargs = {}
        if context is not None:
            kwargs = dict(
                conversation_id=getattr(context, "conversation_id", "") or "",
                tenant_id=getattr(context, "tenant_id", "") or "default",
                principal_id=getattr(context, "principal_id", "") or "",
                motet_id=getattr(context, "motet_id", "") or "default",
            )
        super().__init__(task_id, data, command_id=command_id, **kwargs)
        self.test_data = test_data
        if context is not None:
            self.distributed_context.required_capabilities = (
                getattr(context, "required_capabilities", None)
                or {WorkerCapability.REASONING, WorkerCapability.MODEL_INFERENCE}
            )
            self.distributed_context.result_aggregation_strategy = getattr(
                context, "result_aggregation_strategy", "first_success"
            )
        elif not self.distributed_context.required_capabilities:
            self.distributed_context.required_capabilities = {
                WorkerCapability.REASONING,
                WorkerCapability.MODEL_INFERENCE,
            }

    def _do_execute(self, worker_context):
        return {"result": f"mock_execution_{self.test_data}"}

    def get_command_type(self) -> str:
        return "mock_command"

    def can_undo(self) -> bool:
        return False

    def undo(self, stack):
        return None


@pytest.fixture
def sample_context():
    """Create a sample distributed command context"""
    return DistributedCommandContext(
        task_id="test-task-123",
        conversation_id="conv-456",
        tenant_id="tenant-789",
        principal_id="user-abc",
        required_capabilities={WorkerCapability.REASONING, WorkerCapability.MODEL_INFERENCE},
        distribution_strategy=DistributionStrategy.SINGLE_WORKER,
        max_workers=1,
        timeout_seconds=60,
        priority=5,
        trace_id="trace-xyz"
    )


@pytest.fixture
def sample_messages():
    """Create sample messages for testing"""
    return [
        Message(role="user", content="What is machine learning?"),
        Message(role="assistant", content="Machine learning is..."),
        Message(role="user", content="Can you explain neural networks?")
    ]


class TestDistributedCommandContext:
    """Test cases for DistributedCommandContext"""
    
    def test_context_creation_with_defaults(self):
        """Test context creation with default values"""
        context = DistributedCommandContext(task_id="test-123")
        
        assert context.task_id == "test-123"
        assert context.conversation_id == ""
        assert context.tenant_id == ""
        assert context.principal_id == ""
        assert len(context.required_capabilities) == 0
        assert context.distribution_strategy == DistributionStrategy.SINGLE_WORKER
        assert context.max_workers == 1
        assert context.timeout_seconds == 60
        assert context.priority == 5
        assert context.max_retries == 3
        assert context.circuit_breaker_enabled is True
        assert context.tenant_isolation_required is True
        assert context.result_aggregation_strategy == "first_success"
    
    def test_context_creation_with_custom_values(self):
        """Test context creation with custom values"""
        capabilities = {WorkerCapability.REASONING, WorkerCapability.TOOL_EXECUTION}
        
        context = DistributedCommandContext(
            task_id="custom-task",
            conversation_id="conv-123",
            tenant_id="tenant-456",
            principal_id="user-789",
            required_capabilities=capabilities,
            distribution_strategy=DistributionStrategy.PARALLEL_FANOUT,
            max_workers=3,
            timeout_seconds=120,
            priority=8,
            max_retries=5,
            circuit_breaker_enabled=False,
            tenant_isolation_required=False,
            result_aggregation_strategy="all_results"
        )
        
        assert context.task_id == "custom-task"
        assert context.conversation_id == "conv-123"
        assert context.tenant_id == "tenant-456"
        assert context.principal_id == "user-789"
        assert context.required_capabilities == capabilities
        assert context.distribution_strategy == DistributionStrategy.PARALLEL_FANOUT
        assert context.max_workers == 3
        assert context.timeout_seconds == 120
        assert context.priority == 8
        assert context.max_retries == 5
        assert context.circuit_breaker_enabled is False
        assert context.tenant_isolation_required is False
        assert context.result_aggregation_strategy == "all_results"


class TestDistributedCommand:
    """Test cases for DistributedCommand base class"""
    
    def test_command_creation(self, sample_context):
        """Test distributed command creation"""
        command = MockDistributedCommand("cmd-123", "test-task-123", "test_data", context=sample_context)
        
        assert command.command_id == "cmd-123"
        assert command.distributed_context.task_id == "test-task-123"
        assert command.test_data == "test_data"
        assert len(command.worker_assignments) == 0
        assert len(command.worker_results) == 0
        assert command.retry_count == 0
        assert command.get_command_type() == "mock_command"
    
    def test_get_required_capabilities(self, sample_context):
        """Test getting required capabilities"""
        command = MockDistributedCommand("cmd-123", "test-task-123", context=sample_context)
        
        capabilities = command.get_required_capabilities()
        assert WorkerCapability.REASONING in capabilities
        assert WorkerCapability.MODEL_INFERENCE in capabilities
    
    def test_can_execute_on_worker(self, sample_context):
        """Test worker capability checking"""
        command = MockDistributedCommand("cmd-123", "test-task-123", context=sample_context)
        
        # Worker with all required capabilities
        full_capabilities = {WorkerCapability.REASONING, WorkerCapability.MODEL_INFERENCE, WorkerCapability.TOOL_EXECUTION}
        assert command.can_execute_on_worker(full_capabilities) is True
        
        # Worker with only some capabilities
        partial_capabilities = {WorkerCapability.REASONING}
        assert command.can_execute_on_worker(partial_capabilities) is False
        
        # Worker with no matching capabilities
        no_capabilities = {WorkerCapability.EMBEDDINGS}
        assert command.can_execute_on_worker(no_capabilities) is False
    
    def test_worker_assignment(self, sample_context):
        """Test worker assignment functionality"""
        command = MockDistributedCommand("cmd-123", "test-task-123", context=sample_context)
        
        # Assign worker
        capabilities = {WorkerCapability.REASONING, WorkerCapability.MODEL_INFERENCE}
        command.assign_to_worker("worker-1", "reasoning", capabilities)
        
        assert len(command.worker_assignments) == 1
        assignment = command.worker_assignments[0]
        assert assignment.worker_id == "worker-1"
        assert assignment.worker_type == "reasoning"
        assert assignment.capabilities == capabilities
        assert assignment.assigned_at is not None
        assert assignment.started_at is None
        assert assignment.completed_at is None
    
    def test_worker_lifecycle_tracking(self, sample_context):
        """Test worker lifecycle tracking"""
        command = MockDistributedCommand("cmd-123", sample_context)
        
        # Assign and start worker
        capabilities = {WorkerCapability.REASONING}
        command.assign_to_worker("worker-1", "reasoning", capabilities)
        command.mark_worker_started("worker-1")
        
        assignment = command.get_worker_assignment("worker-1")
        assert assignment.started_at is not None
        assert assignment.completed_at is None
        
        # Complete worker
        result = {"output": "test result"}
        command.mark_worker_completed("worker-1", result)
        
        assignment = command.get_worker_assignment("worker-1")
        assert assignment.completed_at is not None
        assert assignment.result == result
        assert command.worker_results["worker-1"] == result
    
    def test_worker_failure_tracking(self, sample_context):
        """Test worker failure tracking"""
        command = MockDistributedCommand("cmd-123", sample_context)
        
        # Assign and fail worker
        capabilities = {WorkerCapability.REASONING}
        command.assign_to_worker("worker-1", "reasoning", capabilities)
        command.mark_worker_started("worker-1")
        
        error = Exception("Worker failed")
        command.mark_worker_failed("worker-1", error)
        
        assignment = command.get_worker_assignment("worker-1")
        assert assignment.completed_at is not None
        assert assignment.error == str(error)
    
    def test_result_aggregation_first_success(self, sample_context):
        """Test first success result aggregation"""
        command = MockDistributedCommand("cmd-123", sample_context)
        command.distributed_context.result_aggregation_strategy = "first_success"
        
        # Add multiple results
        command.worker_results = {
            "worker-1": "result-1",
            "worker-2": "result-2",
            "worker-3": None
        }
        
        aggregated = command.aggregate_results()
        assert aggregated == "result-1"  # First non-None result
    
    def test_result_aggregation_all_results(self, sample_context):
        """Test all results aggregation"""
        command = MockDistributedCommand("cmd-123", sample_context)
        command.distributed_context.result_aggregation_strategy = "all_results"
        
        # Add multiple results
        command.worker_results = {
            "worker-1": "result-1",
            "worker-2": "result-2"
        }
        
        aggregated = command.aggregate_results()
        assert aggregated == ["result-1", "result-2"]
    
    def test_result_aggregation_majority_vote(self, sample_context):
        """Test majority vote result aggregation"""
        command = MockDistributedCommand("cmd-123", sample_context)
        command.distributed_context.result_aggregation_strategy = "majority_vote"
        
        # Add multiple results with duplicates
        command.worker_results = {
            "worker-1": "result-A",
            "worker-2": "result-A",
            "worker-3": "result-B"
        }
        
        aggregated = command.aggregate_results()
        assert aggregated == "result-A"  # Majority result
    
    def test_distribution_completion_checking(self, sample_context):
        """Test distribution completion checking"""
        command = MockDistributedCommand("cmd-123", sample_context)
        
        # No assignments
        assert command.is_distribution_complete() is False
        
        # Incomplete assignment
        capabilities = {WorkerCapability.REASONING}
        command.assign_to_worker("worker-1", "reasoning", capabilities)
        assert command.is_distribution_complete() is False
        
        # Complete assignment
        command.mark_worker_started("worker-1")
        command.mark_worker_completed("worker-1", "result")
        assert command.is_distribution_complete() is True
    
    def test_successful_results_checking(self, sample_context):
        """Test successful results checking"""
        command = MockDistributedCommand("cmd-123", sample_context)
        
        # No assignments
        assert command.has_successful_results() is False
        
        # Failed assignment
        capabilities = {WorkerCapability.REASONING}
        command.assign_to_worker("worker-1", "reasoning", capabilities)
        command.mark_worker_started("worker-1")
        command.mark_worker_failed("worker-1", Exception("Failed"))
        assert command.has_successful_results() is False
        
        # Successful assignment
        command.assign_to_worker("worker-2", "reasoning", capabilities)
        command.mark_worker_started("worker-2")
        command.mark_worker_completed("worker-2", "success")
        assert command.has_successful_results() is True
    
    def test_retry_logic(self, sample_context):
        """Test retry logic"""
        command = MockDistributedCommand("cmd-123", sample_context)
        command.distributed_context.max_retries = 2
        
        # Should retry when no successful results and under retry limit
        assert command.should_retry() is True
        
        # Increment retry count
        command.retry_count = 1
        assert command.should_retry() is True
        
        # Exceed retry limit
        command.retry_count = 2
        assert command.should_retry() is False
        
        # Should not retry when has successful results
        command.retry_count = 0
        capabilities = {WorkerCapability.REASONING}
        command.assign_to_worker("worker-1", "reasoning", capabilities)
        command.mark_worker_completed("worker-1", "success")
        assert command.should_retry() is False
    
    def test_prepare_for_retry(self, sample_context):
        """Test retry preparation"""
        command = MockDistributedCommand("cmd-123", sample_context)
        
        # Add some state
        capabilities = {WorkerCapability.REASONING}
        command.assign_to_worker("worker-1", "reasoning", capabilities)
        command.worker_results["worker-1"] = "failed_result"
        
        # Prepare for retry
        command.prepare_for_retry()
        
        assert command.retry_count == 1
        assert command.last_retry_at is not None
        assert len(command.worker_assignments) == 0
        assert len(command.worker_results) == 0
        assert command.status.value == "pending"
        assert command.error is None


class TestWorkerCapability:
    """Test cases for WorkerCapability enum"""
    
    def test_capability_values(self):
        """Test worker capability enum values"""
        assert WorkerCapability.REASONING.value == "reasoning"
        assert WorkerCapability.MODEL_INFERENCE.value == "model_inference"
        assert WorkerCapability.MODEL_STREAMING.value == "model_streaming"
        assert WorkerCapability.TOOL_EXECUTION.value == "tool_execution"
        assert WorkerCapability.MEMORY_STORAGE.value == "memory_storage"
        assert WorkerCapability.EMBEDDINGS.value == "embeddings"
    
    def test_capability_set_operations(self):
        """Test set operations with capabilities"""
        reasoning_caps = {WorkerCapability.REASONING, WorkerCapability.MODEL_INFERENCE}
        model_caps = {WorkerCapability.MODEL_INFERENCE, WorkerCapability.EMBEDDINGS}
        
        # Intersection
        common = reasoning_caps & model_caps
        assert common == {WorkerCapability.MODEL_INFERENCE}
        
        # Union
        all_caps = reasoning_caps | model_caps
        assert WorkerCapability.REASONING in all_caps
        assert WorkerCapability.EMBEDDINGS in all_caps
        
        # Subset checking
        assert {WorkerCapability.REASONING}.issubset(reasoning_caps)
        assert not {WorkerCapability.EMBEDDINGS}.issubset(reasoning_caps)


class TestDistributionStrategy:
    """Test cases for DistributionStrategy enum"""
    
    def test_strategy_values(self):
        """Test distribution strategy enum values"""
        assert DistributionStrategy.SINGLE_WORKER.value == "single_worker"
        assert DistributionStrategy.PARALLEL_FANOUT.value == "parallel_fanout"
        assert DistributionStrategy.SEQUENTIAL_CHAIN.value == "sequential_chain"
        assert DistributionStrategy.MAP_REDUCE.value == "map_reduce"
        assert DistributionStrategy.BROADCAST.value == "broadcast"


class TestWorkerAssignment:
    """Test cases for WorkerAssignment"""
    
    def test_worker_assignment_creation(self):
        """Test worker assignment creation"""
        capabilities = {WorkerCapability.REASONING, WorkerCapability.MODEL_INFERENCE}
        
        assignment = WorkerAssignment(
            worker_id="worker-123",
            worker_type="reasoning",
            capabilities=capabilities
        )
        
        assert assignment.worker_id == "worker-123"
        assert assignment.worker_type == "reasoning"
        assert assignment.capabilities == capabilities
        assert assignment.assigned_at is not None
        assert assignment.started_at is None
        assert assignment.completed_at is None
        assert assignment.result is None
        assert assignment.error is None
