"""
Tests for CommandExecutor

Tests the command execution lifecycle and integration with routing.
"""

import pytest
from unittest.mock import Mock, patch
from typing import Dict, Any

from motet.core.workers.command_executor import CommandExecutor
from motet.core.workers.routing.worker_router import RoutingDecision
from motet.core.commands.distributed import DistributedCommand
from motet.core.commands.base import CommandStatus


class MockWorkerRouter:
    """Mock worker router for testing (sync - CommandExecutor calls route_command synchronously)."""

    def __init__(self):
        self.route_calls = []
        self.mock_decision = RoutingDecision(
            selected_worker={
                'worker_id': 'test-worker-1',
                'state': 'READY',
                'current_load': 0.2,
                'capabilities': ['model_inference']
            },
            strategy_used='least_loaded',
            decision_time_ms=5.0,
            available_workers=2,
            filtered_workers=2,
            selection_reason='Test selection',
            fallback_used=False,
            error=None,
            metadata={}
        )

    def route_command(self, command, target_worker_id=None, strategy_override=None):
        self.route_calls.append({
            'command': command,
            'target_worker_id': target_worker_id,
            'strategy_override': strategy_override
        })
        return self.mock_decision

    def get_routing_stats(self):
        return {
            'total_requests': 1,
            'successful_routes': 1,
            'strategy_usage': {'least_loaded': 1}
        }


class MockWorkerCommunicator:
    """Mock worker communicator for testing (sync)."""

    def __init__(self):
        self.send_calls = []
        self.mock_result = {
            'status': 'completed',
            'result': {'content': 'Test response', 'model': 'gpt-4o-mini'},
            'worker_id': 'test-worker-1',
            'response_time_ms': 1500,
            'task_id': 'test-task-123'
        }

    def send_command(self, worker, command):
        self.send_calls.append({'worker': worker, 'command': command})
        return self.mock_result

    def get_communication_stats(self):
        return {
            'total_commands_sent': 1,
            'successful_commands': 1,
            'avg_response_time_ms': 1500.0
        }


class MockDistributedCommand(DistributedCommand):
    """Mock distributed command for testing (implements abstract _do_execute)."""

    def __init__(self, command_id="test-cmd-123"):
        super().__init__(
            task_id="test-task-123",
            data=object(),
            command_id=command_id,
            conversation_id="test-conv-123",
            tenant_id="test-tenant",
            principal_id="test-principal"
        )

    def get_command_type(self):
        return "MockDistributedCommand"

    def _do_execute(self, worker_context: Dict[str, Any]) -> Any:
        return {"result": "mock execution"}

    def can_undo(self) -> bool:
        return False

    def undo(self, stack=None):
        return {"result": "mock undo"}
    
    def serialize_for_transport(self):
        return {
            "command_id": self.command_id,
            "command_type": self.get_command_type(),
            "data": {"mock": "data"}
        }


@pytest.fixture
def mock_worker_router():
    return MockWorkerRouter()


@pytest.fixture
def mock_worker_communicator():
    return MockWorkerCommunicator()


@pytest.fixture
def command_executor(mock_worker_router, mock_worker_communicator):
    return CommandExecutor(
        worker_router=mock_worker_router,
        worker_communicator=mock_worker_communicator,
        enable_circuit_breaker=True,
        enable_metrics=True
    )


@pytest.fixture
def mock_command():
    return MockDistributedCommand()


class TestCommandExecutor:
    """Test CommandExecutor core functionality (execute_command and execute_batch are sync)."""

    def test_successful_command_execution(self, command_executor, mock_command):
        """Test successful command execution flow"""
        result = command_executor.execute_command(mock_command)

        assert result['status'] == 'completed'
        assert result['command_id'] == 'test-cmd-123'
        assert result['command_type'] == 'MockDistributedCommand'
        assert 'execution_time_ms' in result
        assert 'routing_info' in result
        assert len(command_executor.worker_router.route_calls) == 1
        assert len(command_executor.worker_communicator.send_calls) == 1
        assert mock_command.status == CommandStatus.COMPLETED

    def test_command_execution_with_target_worker(self, command_executor, mock_command):
        """Test command execution with specific target worker"""
        result = command_executor.execute_command(
            mock_command,
            target_worker_id="specific-worker-123"
        )
        assert result['status'] == 'completed'
        route_call = command_executor.worker_router.route_calls[0]
        assert route_call['target_worker_id'] == "specific-worker-123"

    def test_command_execution_with_strategy_override(self, command_executor, mock_command):
        """Test command execution with strategy override"""
        result = command_executor.execute_command(
            mock_command,
            strategy_override="tenant_affinity"
        )
        assert result['status'] == 'completed'
        route_call = command_executor.worker_router.route_calls[0]
        assert route_call['strategy_override'] == "tenant_affinity"

    def test_routing_failure(self, command_executor, mock_command):
        """Test handling of routing failure"""
        command_executor.worker_router.mock_decision.selected_worker = None
        command_executor.worker_router.mock_decision.error = "No suitable workers"

        result = command_executor.execute_command(mock_command)

        assert result['status'] == 'error'
        assert 'No suitable workers' in result['error']
        assert mock_command.status == CommandStatus.FAILED

    def test_communication_failure(self, command_executor, mock_command):
        """Test handling of communication failure"""
        command_executor.worker_communicator.mock_result = {
            'status': 'error',
            'error': 'Worker communication failed',
            'worker_id': 'test-worker-1',
            'response_time_ms': 100
        }

        result = command_executor.execute_command(mock_command)

        assert result['status'] == 'error'
        assert 'Worker communication failed' in result['error']
        assert mock_command.status == CommandStatus.FAILED

    def test_execute_on_specific_worker(self, command_executor, mock_command):
        """Test execute_on_specific_worker method"""
        result = command_executor.execute_on_specific_worker(
            mock_command,
            "target-worker-456"
        )
        assert result['status'] == 'completed'
        route_call = command_executor.worker_router.route_calls[0]
        assert route_call['target_worker_id'] == "target-worker-456"

    def test_batch_execution(self, command_executor):
        """Test batch command execution"""
        commands = [MockDistributedCommand(f"cmd-{i}") for i in range(3)]

        results = command_executor.execute_batch(commands, max_concurrent=2)

        assert len(results) == 3
        for result in results:
            assert result['status'] == 'completed'
        assert len(command_executor.worker_router.route_calls) == 3

    def test_batch_execution_with_exception(self, command_executor):
        """Test batch execution with some commands failing"""
        commands = [MockDistributedCommand(f"cmd-{i}") for i in range(3)]

        call_count = 0
        original_send = command_executor.worker_communicator.send_command

        def mock_send(worker, command):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise Exception("Communication error")
            return original_send(worker, command)

        command_executor.worker_communicator.send_command = mock_send

        results = command_executor.execute_batch(commands)

        assert len(results) == 3
        success_count = sum(1 for r in results if r['status'] == 'completed')
        error_count = sum(1 for r in results if r['status'] == 'error')
        assert success_count == 2
        assert error_count == 1
    
    def test_get_execution_stats(self, command_executor):
        """Test getting execution statistics"""
        stats = command_executor.get_execution_stats()
        
        assert 'total_executions' in stats
        assert 'successful_executions' in stats
        assert 'failed_executions' in stats
        assert 'circuit_breaker' in stats
        assert 'command_history_size' in stats
    
    def test_get_command_history(self, command_executor):
        """Test getting command history"""
        history = command_executor.get_command_history()
        
        assert isinstance(history, list)
        # Initially empty
        assert len(history) == 0
    
    def test_reset_stats(self, command_executor):
        """Test resetting execution statistics"""
        # Set some stats
        command_executor.execution_stats['total_executions'] = 10
        command_executor.command_history.append({'test': 'data'})
        
        command_executor.reset_stats()
        
        assert command_executor.execution_stats['total_executions'] == 0
        assert len(command_executor.command_history) == 0


class TestCircuitBreaker:
    """Test circuit breaker functionality (sync)."""

    def test_circuit_breaker_open(self, command_executor, mock_command):
        """Test circuit breaker opens after failures"""
        command_executor.worker_communicator.mock_result = {
            'status': 'error',
            'error': 'Simulated failure'
        }
        threshold = command_executor.circuit_breaker["failure_threshold"]
        for _ in range(threshold):
            command_executor.execute_command(mock_command)
        assert command_executor.circuit_breaker['state'] == 'open'
        result = command_executor.execute_command(mock_command)
        assert result['status'] == 'error'
        assert 'Circuit breaker is open' in result['error']

    def test_circuit_breaker_ignores_cooperative_cancel(
        self, command_executor, mock_command
    ):
        """task_cancelled and workflow_cancelled must not trip the breaker."""
        threshold = command_executor.circuit_breaker["failure_threshold"]
        command_executor.worker_communicator.mock_result = {
            "status": "error",
            "error": "Workflow cancelled (wait)",
            "error_code": "workflow_cancelled",
        }
        for _ in range(threshold + 2):
            result = command_executor.execute_command(mock_command)
            assert result.get("error_code") == "workflow_cancelled"
        assert command_executor.circuit_breaker["state"] == "closed"
        assert mock_command.status == CommandStatus.CANCELLED

    def test_circuit_breaker_recovery(self, command_executor, mock_command):
        """Test circuit breaker recovery"""
        command_executor.circuit_breaker['state'] = 'open'
        command_executor.circuit_breaker['last_failure_time'] = 0
        command_executor.worker_communicator.mock_result = {
            'status': 'completed',
            'result': {'content': 'Success after recovery'}
        }
        result = command_executor.execute_command(mock_command)
        assert result['status'] == 'completed'
        assert command_executor.circuit_breaker['state'] == 'closed'


class TestMetrics:
    """Test metrics collection (sync)."""

    def test_metrics_collection(self, command_executor, mock_command):
        """Test that metrics are collected during execution"""
        mock_command.tenant_id = "test-tenant"
        mock_command.session_id = "test-session"

        command_executor.execute_command(mock_command)

        stats = command_executor.execution_stats
        assert stats['total_executions'] == 1
        assert stats['successful_executions'] == 1
        assert stats['total_execution_time_ms'] >= 0
        assert 'MockDistributedCommand' in stats['command_type_stats']
        cmd_stats = stats['command_type_stats']['MockDistributedCommand']
        assert cmd_stats['count'] == 1
        assert cmd_stats['success_count'] == 1
        assert 'test-tenant' in stats['tenant_stats']
        tenant_stats = stats['tenant_stats']['test-tenant']
        assert tenant_stats['count'] == 1
        assert tenant_stats['success_count'] == 1
        assert 'test-worker-1' in stats['worker_stats']
        worker_stats = stats['worker_stats']['test-worker-1']
        assert worker_stats['count'] == 1
        assert worker_stats['success_count'] == 1

    def test_error_metrics(self, command_executor, mock_command):
        """Test error metrics collection"""
        command_executor.worker_communicator.mock_result = {
            'status': 'error',
            'error': 'Test error message'
        }
        command_executor.execute_command(mock_command)
        stats = command_executor.execution_stats
        assert stats['failed_executions'] == 1
        assert 'Test error message' in stats['error_stats']
        assert stats['error_stats']['Test error message'] == 1


class TestIntegration:
    """Integration tests with real components (sync)."""

    def test_end_to_end_execution(self, command_executor, mock_command):
        """Test complete end-to-end execution flow"""
        result = command_executor.execute_command(mock_command)
        assert result['status'] == 'completed'
        assert result['execution_id'] is not None
        assert result['routing_info']['selected_worker_id'] == 'test-worker-1'
        assert result['routing_info']['strategy_used'] == 'least_loaded'
        assert result['metadata']['tenant_id'] is None
        stats = command_executor.get_execution_stats()
        assert stats['total_executions'] == 1
        assert stats['successful_executions'] == 1
        
        # Verify history was recorded
        history = command_executor.get_command_history()
        assert len(history) == 1
        assert history[0]['command_id'] == 'test-cmd-123'


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
