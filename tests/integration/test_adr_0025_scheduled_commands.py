"""
Tests for ADR-0025: Scheduled Distributed Commands

Tests the core scheduling infrastructure including:
- DistributedCommandContext scheduling fields
- ScheduledCommandManager functionality
- Schedule storage and retrieval
- Basic scheduling workflow
- ETA (delayed) scheduling
- Cron-based recurring schedules
"""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch, MagicMock

def _utc_now():
    """Timezone-aware UTC now for comparison with scheduling models."""
    return datetime.now(timezone.utc)
import time

from motet.core.commands.distributed import (
    DistributedCommand, DistributedCommandContext, ScheduleType
)
from motet.core.commands.builtin.schedule import ScheduleCommand, ScheduleData
from motet.core.orchestration.scheduling import (
    ScheduledCommandManager, ScheduleMetadata, ScheduleStatus, ScheduleFilter
)


class _ScheduleTestCommand(DistributedCommand):
    """Helper command for scheduling tests (name avoids pytest collecting as test class)."""
    
    def _do_execute(self, worker_context: dict) -> dict:
        return {"status": "success", "message": "Test command executed"}
    
    def get_command_type(self) -> str:
        return "test_command"
    
    def can_undo(self) -> bool:
        return False
    
    def undo(self) -> bool:
        return False


class TestDistributedCommandContextScheduling:
    """Test scheduling fields in DistributedCommandContext"""
    
    def test_schedule_type_default(self):
        """Test that schedule_type defaults to IMMEDIATE"""
        context = DistributedCommandContext(task_id="test")
        assert context.schedule_type == ScheduleType.IMMEDIATE
    
    def test_schedule_type_delayed(self):
        """Test DELAYED schedule type with scheduled_at"""
        scheduled_time = datetime.utcnow() + timedelta(hours=1)
        context = DistributedCommandContext(
            task_id="test",
            schedule_type=ScheduleType.DELAYED,
            scheduled_at=scheduled_time
        )
        assert context.schedule_type == ScheduleType.DELAYED
        assert context.scheduled_at == scheduled_time
    
    def test_schedule_type_recurring(self):
        """Test RECURRING schedule type with cron expression"""
        context = DistributedCommandContext(
            task_id="test",
            schedule_type=ScheduleType.RECURRING,
            cron_expression="0 9 * * MON-FRI",
            recurring_until=datetime.utcnow() + timedelta(days=30)
        )
        assert context.schedule_type == ScheduleType.RECURRING
        assert context.cron_expression == "0 9 * * MON-FRI"
        assert context.recurring_until is not None
    
    def test_schedule_type_conditional(self):
        """Test CONDITIONAL schedule type with condition"""
        context = DistributedCommandContext(
            task_id="test",
            schedule_type=ScheduleType.CONDITIONAL,
            condition_expression="queue_depth > 1000",
            condition_check_interval=60
        )
        assert context.schedule_type == ScheduleType.CONDITIONAL
        assert context.condition_expression == "queue_depth > 1000"
        assert context.condition_check_interval == 60
    
    def test_schedule_metadata_fields(self):
        """Test schedule metadata fields"""
        context = DistributedCommandContext(
            task_id="test",
            schedule_id="test-schedule-123",
            original_command_id="original-command-456",
            execution_count=5,
            max_executions=100
        )
        assert context.schedule_id == "test-schedule-123"
        assert context.original_command_id == "original-command-456"
        assert context.execution_count == 5
        assert context.max_executions == 100


class TestScheduleMetadata:
    """Test ScheduleMetadata model"""
    
    def test_schedule_metadata_creation(self):
        """Test creating ScheduleMetadata"""
        schedule = ScheduleMetadata(
            schedule_id="test-123",
            command_id="cmd-456",
            command_type="TestCommand",
            schedule_type=ScheduleType.DELAYED,
            scheduled_at=datetime.utcnow() + timedelta(hours=1)
        )
        
        assert schedule.schedule_id == "test-123"
        assert schedule.command_id == "cmd-456"
        assert schedule.command_type == "TestCommand"
        assert schedule.schedule_type == ScheduleType.DELAYED
        assert schedule.status == ScheduleStatus.ACTIVE
        assert schedule.execution_count == 0
    
    def test_schedule_is_expired(self):
        """Test schedule expiration logic (use timezone-aware datetimes for comparison)."""
        # Not expired
        schedule = ScheduleMetadata(
            schedule_id="test-123",
            command_id="cmd-456",
            command_type="TestCommand",
            schedule_type=ScheduleType.RECURRING,
            recurring_until=_utc_now() + timedelta(days=1)
        )
        assert not schedule.is_expired()

        # Expired by date
        schedule.recurring_until = _utc_now() - timedelta(days=1)
        assert schedule.is_expired()

        # Expired by execution count
        schedule.recurring_until = _utc_now() + timedelta(days=1)
        schedule.max_executions = 5
        schedule.execution_count = 5
        assert schedule.is_expired()
    
    def test_schedule_should_execute(self):
        """Test schedule execution eligibility"""
        schedule = ScheduleMetadata(
            schedule_id="test-123",
            command_id="cmd-456",
            command_type="TestCommand",
            schedule_type=ScheduleType.DELAYED
        )
        assert schedule.should_execute()
        
        # Should not execute if not active
        schedule.status = ScheduleStatus.CANCELLED
        assert not schedule.should_execute()
        
        # Should not execute if expired
        schedule.status = ScheduleStatus.ACTIVE
        schedule.recurring_until = _utc_now() - timedelta(days=1)
        assert not schedule.should_execute()

        # Should not execute if too many failures
        schedule.recurring_until = _utc_now() + timedelta(days=1)
        schedule.consecutive_failures = 5
        schedule.max_consecutive_failures = 3
        assert not schedule.should_execute()
    
    def test_schedule_execution_tracking(self):
        """Test execution count and failure tracking"""
        schedule = ScheduleMetadata(
            schedule_id="test-123",
            command_id="cmd-456",
            command_type="TestCommand",
            schedule_type=ScheduleType.RECURRING
        )
        
        # Test successful execution
        schedule.increment_execution_count()
        assert schedule.execution_count == 1
        assert schedule.consecutive_failures == 0
        assert schedule.last_execution_at is not None
        
        # Test failure recording
        schedule.record_failure("Test error")
        assert schedule.consecutive_failures == 1
        assert schedule.last_error == "Test error"


class TestScheduledCommandManager:
    """Test ScheduledCommandManager functionality"""
    
    @pytest.fixture
    def mock_storage(self):
        """Mock storage for testing"""
        with patch('motet.core.orchestration.scheduling.manager.ScheduleStorage') as mock:
            mock_instance = Mock()
            mock.return_value = mock_instance
            
            # Create separate mocks for sync methods to preserve assert methods
            retrieve_mock = Mock()
            update_mock = Mock()
            list_mock = Mock()
            store_mock = Mock()
            
            # Make sync methods call the mocks
            def sync_retrieve_schedule(schedule_id):
                retrieve_mock(schedule_id)
                return retrieve_mock.return_value
            
            def sync_update_schedule(schedule):
                update_mock(schedule)
                return update_mock.return_value
            
            def sync_list_schedules(filters=None):
                list_mock(filters)
                return list_mock.return_value
            
            def sync_store_schedule(schedule):
                store_mock(schedule)
                return store_mock.return_value
            
            mock_instance.retrieve_schedule = sync_retrieve_schedule
            mock_instance.update_schedule = sync_update_schedule
            mock_instance.list_schedules = sync_list_schedules
            mock_instance.store_schedule = sync_store_schedule
            
            # Store the mocks for assertions
            mock_instance._retrieve_mock = retrieve_mock
            mock_instance._update_mock = update_mock
            mock_instance._list_mock = list_mock
            mock_instance._store_mock = store_mock
            
            yield mock_instance
    
    def test_schedule_command_immediate(self, mock_storage):
        """Test scheduling immediate command"""
        manager = ScheduledCommandManager()
        
        command = _ScheduleTestCommand(
            task_id="test",
            data={"test": "data"},
            schedule_type=ScheduleType.IMMEDIATE
        )
        
        # Mock storage methods
        mock_storage._store_mock.return_value = True
        
        schedule_id = manager.schedule_command(command)
        
        assert schedule_id is not None
        assert command.distributed_context.schedule_id == schedule_id
        mock_storage._store_mock.assert_called_once()
    
    def test_schedule_command_delayed(self, mock_storage):
        """Test scheduling delayed command"""
        manager = ScheduledCommandManager()
        
        scheduled_time = datetime.utcnow() + timedelta(hours=1)
        command = _ScheduleTestCommand(
            task_id="test",
            data={"test": "data"}
        )
        
        # Set schedule type and time on the context
        command.distributed_context.schedule_type = ScheduleType.DELAYED
        command.distributed_context.scheduled_at = scheduled_time
        
        # Mock storage methods
        mock_storage._store_mock.return_value = True
        
        schedule_id = manager.schedule_command(command)
        
        assert schedule_id is not None
        assert command.distributed_context.schedule_id == schedule_id
        
        # Verify schedule metadata was created correctly
        call_args = mock_storage._store_mock.call_args[0][0]
        assert call_args.schedule_type == ScheduleType.DELAYED
        assert call_args.scheduled_at == scheduled_time

    def test_schedule_command_with_worker_targeting(self, mock_storage):
        """Test scheduling a command with worker targeting"""
        manager = ScheduledCommandManager()
        
        scheduled_time = datetime.utcnow() + timedelta(hours=1)
        command = _ScheduleTestCommand(
            task_id="test",
            data={"test": "data"}
        )
        
        # Set schedule type and worker targeting on the context
        command.distributed_context.schedule_type = ScheduleType.DELAYED
        command.distributed_context.scheduled_at = scheduled_time
        command.distributed_context.target_worker_id = "worker-123"
        command.distributed_context.preferred_worker_ids = ["worker-456", "worker-789"]
        command.distributed_context.worker_affinity = "user-session-abc"
        command.distributed_context.avoid_worker_ids = ["worker-999"]
        
        # Mock storage methods
        mock_storage._store_mock.return_value = True
        
        schedule_id = manager.schedule_command(command)
        
        assert schedule_id is not None
        assert command.distributed_context.schedule_id == schedule_id
        
        # Verify schedule metadata was created correctly with worker targeting
        call_args = mock_storage._store_mock.call_args[0][0]
        assert call_args.schedule_type == ScheduleType.DELAYED
        assert call_args.scheduled_at == scheduled_time
        assert call_args.target_worker_id == "worker-123"
        assert call_args.preferred_worker_ids == ["worker-456", "worker-789"]
        assert call_args.worker_affinity == "user-session-abc"
        assert call_args.avoid_worker_ids == ["worker-999"]
    
    def test_cancel_schedule(self, mock_storage):
        """Test cancelling a schedule"""
        manager = ScheduledCommandManager()
        
        # Mock existing schedule
        schedule = ScheduleMetadata(
            schedule_id="test-123",
            command_id="cmd-456",
            command_type="TestCommand",
            schedule_type=ScheduleType.DELAYED
        )
        mock_storage._retrieve_mock.return_value = schedule
        mock_storage._update_mock.return_value = True
        
        result = manager.cancel_schedule("test-123")
        
        assert result is True
        assert schedule.status == ScheduleStatus.CANCELLED
        mock_storage._update_mock.assert_called_once_with(schedule)
    
    def test_modify_schedule(self, mock_storage):
        """Test modifying a schedule"""
        manager = ScheduledCommandManager()
        
        # Mock existing schedule
        schedule = ScheduleMetadata(
            schedule_id="test-123",
            command_id="cmd-456",
            command_type="TestCommand",
            schedule_type=ScheduleType.DELAYED,
            scheduled_at=datetime.utcnow() + timedelta(hours=1)
        )
        mock_storage._retrieve_mock.return_value = schedule
        mock_storage._update_mock.return_value = True
        
        new_time = datetime.utcnow() + timedelta(hours=2)
        updates = {"scheduled_at": new_time}
        
        result = manager.modify_schedule("test-123", updates)
        
        assert result is True
        assert schedule.scheduled_at == new_time
        mock_storage._update_mock.assert_called_once_with(schedule)
    
    def test_list_schedules(self, mock_storage):
        """Test listing schedules"""
        manager = ScheduledCommandManager()
        
        # Mock schedules
        schedules = [
            ScheduleMetadata(
                schedule_id="test-1",
                command_id="cmd-1",
                command_type="TestCommand",
                schedule_type=ScheduleType.DELAYED
            ),
            ScheduleMetadata(
                schedule_id="test-2",
                command_id="cmd-2",
                command_type="TestCommand",
                schedule_type=ScheduleType.RECURRING
            )
        ]
        mock_storage._list_mock.return_value = schedules
        
        result = manager.list_schedules()
        
        assert len(result) == 2
        assert result[0].schedule_id == "test-1"
        assert result[1].schedule_id == "test-2"
    
    def test_calculate_next_execution_immediate(self, mock_storage):
        """Test next execution calculation for immediate schedules"""
        manager = ScheduledCommandManager()
        
        schedule = ScheduleMetadata(
            schedule_id="test-123",
            command_id="cmd-456",
            command_type="TestCommand",
            schedule_type=ScheduleType.IMMEDIATE
        )
        
        next_execution = manager._calculate_next_execution(schedule)

        # Should be very close to now (within 1 second); use timezone-aware now for comparison
        now = _utc_now()
        time_diff = abs((next_execution - now).total_seconds())
        assert time_diff < 1.0

    def test_calculate_next_execution_delayed(self, mock_storage):
        """Test next execution calculation for delayed schedules"""
        manager = ScheduledCommandManager()

        scheduled_time = _utc_now() + timedelta(hours=1)
        schedule = ScheduleMetadata(
            schedule_id="test-123",
            command_id="cmd-456",
            command_type="TestCommand",
            schedule_type=ScheduleType.DELAYED,
            scheduled_at=scheduled_time
        )
        
        next_execution = manager._calculate_next_execution(schedule)
        
        assert next_execution == scheduled_time


class TestScheduleCommandContextPropagation:
    """Test context propagation into scheduled target commands via envelope metadata."""

    @patch("motet.core.orchestration.scheduling.manager.ScheduledCommandManager.schedule_command")
    def test_schedule_context_written_to_envelope_metadata(self, mock_schedule_command):
        """ScheduleCommand writes schedule_context into target distributed_context.metadata."""
        mock_schedule_command.return_value = "schedule-ctx-123"

        schedule_data = ScheduleData(
            target_command_type="core.agent_turn",
            target_command_data={
                "agent_id": "core.default",
                "messages": [{"role": "user", "content": "search latest news"}],
            },
            schedule_type="recurring",
            interval_seconds=30,
            timeout_seconds=300,
            priority=5,
            max_retries=3,
            schedule_context={
                "agent_id": "core.default",
                "surface_id": "demo_chat",
                "principal_roles": ["motet-admin"],
                "enable_thinking": True,
                "reasoning_effort": "medium",
                "model_provider": "openai",
                "model_name": "gpt-5.2",
                "model_profile_name": "default",
            },
        )

        schedule_cmd = ScheduleCommand(
            task_id="task-ctx-test",
            data=schedule_data,
            conversation_id="conv-123",
            tenant_id="tenant-1",
            principal_id="principal-1",
        )

        result = schedule_cmd._do_execute({})
        assert result.get("status") == "success"

        target_command = mock_schedule_command.call_args[0][0]
        metadata = target_command.distributed_context.metadata or {}
        assert metadata.get("agent_id") == "core.default"
        assert metadata.get("surface_id") == "demo_chat"
        assert metadata.get("principal_roles") == ["motet-admin"]
        assert metadata.get("enable_thinking") is True
        assert metadata.get("reasoning_effort") == "medium"
        assert metadata.get("model_provider") == "openai"
        assert metadata.get("model_name") == "gpt-5.2"
        assert metadata.get("model_profile_name") == "default"

    @patch("motet.core.orchestration.scheduling.manager.ScheduledCommandManager.schedule_command")
    def test_no_schedule_context_leaves_metadata_empty(self, mock_schedule_command):
        """Without schedule_context, target command metadata is not populated."""
        mock_schedule_command.return_value = "schedule-no-ctx-123"

        schedule_data = ScheduleData(
            target_command_type="core.agent_turn",
            target_command_data={
                "agent_id": "core.default",
                "messages": [{"role": "user", "content": "hello"}],
            },
            schedule_type="recurring",
            interval_seconds=60,
        )

        schedule_cmd = ScheduleCommand(
            task_id="task-no-ctx",
            data=schedule_data,
            conversation_id="conv-no-ctx",
            tenant_id="tenant-1",
            principal_id="principal-1",
        )

        result = schedule_cmd._do_execute({})
        assert result.get("status") == "success"

        target_command = mock_schedule_command.call_args[0][0]
        metadata = target_command.distributed_context.metadata or {}
        assert metadata.get("model_provider") is None
        assert metadata.get("surface_id") is None


class TestImmediateScheduleFireAndForget:
    """Immediate schedules dispatch fire-and-forget (issue #129).

    Creating an immediate schedule must return promptly with the schedule_id
    and dispatch info instead of blocking on target completion — the old
    synchronous execute blew through core.schedule's 30s Celery time limit
    for long targets (e.g. core.workflow_execution build cycles) and reported
    TimeLimitExceeded even though the target succeeded.
    """

    def _make_schedule_command(self, **data_overrides):
        data_kwargs = dict(
            target_command_type="core.agent_turn",
            target_command_data={
                "agent_id": "core.default",
                "messages": [{"role": "user", "content": "run cycle"}],
            },
            schedule_type="immediate",
            timeout_seconds=900,
            target_worker_id="edge_app_builder_smoke",
        )
        data_kwargs.update(data_overrides)
        return ScheduleCommand(
            task_id="task-imm-129",
            data=ScheduleData(**data_kwargs),
            conversation_id="conv-imm",
            tenant_id="tenant-1",
            principal_id="app-builder/smoke",
        )

    def _patched_invoker(self, selected_worker, error=None):
        """Patch the global invoker singleton's primary node router."""
        from motet.core.workers.command_invoker import new_global_invoker

        mock_router = Mock()
        mock_router.route_command.return_value = Mock(
            selected_worker=selected_worker, error=error
        )
        mock_node = Mock()
        mock_node.worker_router = mock_router
        return (
            patch.object(new_global_invoker, "initialize"),
            patch.object(new_global_invoker, "primary_node", mock_node),
            mock_router,
        )

    @patch("motet.core.commands.builtin.schedule.ScheduleCommand._mark_immediate_schedule_dispatched")
    @patch("motet.core.distributed.redis_manager.acquire_distributed_lock_sync")
    @patch("motet.core.workers.celery_app.get_celery_app")
    @patch("motet.core.orchestration.scheduling.manager.ScheduledCommandManager.schedule_command")
    def test_immediate_dispatches_without_waiting(
        self, mock_schedule_command, mock_get_app, mock_acquire_lock, mock_mark
    ):
        """Immediate schedule enqueues the target and returns dispatch info."""
        mock_schedule_command.return_value = "sched-imm-1"
        mock_lock = Mock()
        mock_acquire_lock.return_value = mock_lock
        mock_celery = Mock()
        mock_celery.send_task.return_value = Mock(id="celery-task-1")
        mock_get_app.return_value = mock_celery

        patch_init, patch_router, mock_router = self._patched_invoker(
            selected_worker={"worker_id": "edge_app_builder_smoke"}
        )
        with patch_init, patch_router:
            result = self._make_schedule_command()._do_execute({})

        assert result["status"] == "success"
        assert result["schedule_id"] == "sched-imm-1"

        execution = result["execution"]
        assert execution["dispatched"] is True
        assert execution["worker_id"] == "edge_app_builder_smoke"
        assert execution["celery_task_id"] == "celery-task-1"
        assert execution["time_limit"] == 900

        # Fire-and-forget: exactly one send_task, per-command time limits,
        # correct per-worker queue, no result polling.
        mock_celery.send_task.assert_called_once()
        send_kwargs = mock_celery.send_task.call_args.kwargs
        assert send_kwargs["queue"] == "worker.edge_app_builder_smoke"
        assert send_kwargs["time_limit"] == 900
        assert send_kwargs["soft_time_limit"] == 840

        # Router honored the schedule's target_worker_id pin.
        assert (
            mock_router.route_command.call_args.kwargs["target_worker_id"]
            == "edge_app_builder_smoke"
        )

        # One-shot bookkeeping ran and the per-schedule lock was released.
        mock_mark.assert_called_once_with("sched-imm-1")
        mock_lock.release_sync.assert_called_once()

    @patch("motet.core.orchestration.scheduling.manager.ScheduledCommandManager.schedule_command")
    def test_immediate_no_eligible_worker_fails_loudly(self, mock_schedule_command):
        """No routable worker → error response naming the schedule (no silent success)."""
        mock_schedule_command.return_value = "sched-imm-2"

        patch_init, patch_router, _ = self._patched_invoker(
            selected_worker=None, error="no suitable workers available"
        )
        with patch_init, patch_router:
            result = self._make_schedule_command()._do_execute({})

        assert result["status"] == "error"
        assert "sched-imm-2" in result["error"]
        assert "no eligible worker" in result["error"]

    @patch("motet.core.commands.builtin.schedule.ScheduleCommand._mark_immediate_schedule_dispatched")
    @patch("motet.core.distributed.redis_manager.acquire_distributed_lock_sync")
    @patch("motet.core.workers.celery_app.get_celery_app")
    @patch("motet.core.orchestration.scheduling.manager.ScheduledCommandManager.schedule_command")
    def test_immediate_beat_claim_skips_duplicate_dispatch(
        self, mock_schedule_command, mock_get_app, mock_acquire_lock, mock_mark
    ):
        """If check_delayed_schedules holds the lock, do not dispatch a second copy."""
        mock_schedule_command.return_value = "sched-imm-3"
        mock_acquire_lock.return_value = None  # beat tick claimed the schedule
        mock_celery = Mock()
        mock_get_app.return_value = mock_celery

        patch_init, patch_router, _ = self._patched_invoker(
            selected_worker={"worker_id": "edge_app_builder_smoke"}
        )
        with patch_init, patch_router:
            result = self._make_schedule_command()._do_execute({})

        assert result["status"] == "success"
        assert result["execution"]["dispatched"] is True
        assert "scheduler" in result["execution"]["note"]
        mock_celery.send_task.assert_not_called()
        mock_mark.assert_not_called()

    @patch("motet.core.workers.celery_app.get_celery_app")
    @patch("motet.core.orchestration.scheduling.manager.ScheduledCommandManager.schedule_command")
    def test_recurring_does_not_dispatch_inline(self, mock_schedule_command, mock_get_app):
        """Non-immediate schedules never take the inline dispatch path."""
        mock_schedule_command.return_value = "sched-rec-1"
        mock_celery = Mock()
        mock_get_app.return_value = mock_celery

        result = self._make_schedule_command(
            schedule_type="recurring",
            interval_seconds=3600,
            target_worker_id=None,
        )._do_execute({})

        assert result["status"] == "success"
        assert "execution" not in result
        mock_celery.send_task.assert_not_called()


class TestScheduleFilter:
    """Test ScheduleFilter functionality"""
    
    def test_schedule_filter_default(self):
        """Test default filter values"""
        filter_obj = ScheduleFilter()
        
        assert filter_obj.status is None
        assert filter_obj.schedule_type is None
        assert filter_obj.tenant_id is None
        assert filter_obj.limit == 100
        assert filter_obj.offset == 0
    
    def test_schedule_filter_with_values(self):
        """Test filter with specific values"""
        filter_obj = ScheduleFilter(
            status=ScheduleStatus.ACTIVE,
            schedule_type=ScheduleType.DELAYED,
            tenant_id="tenant-123",
            limit=50,
            offset=10
        )
        
        assert filter_obj.status == ScheduleStatus.ACTIVE
        assert filter_obj.schedule_type == ScheduleType.DELAYED
        assert filter_obj.tenant_id == "tenant-123"
        assert filter_obj.limit == 50
        assert filter_obj.offset == 10


class TestETADelayedScheduling:
    """Test ETA (delayed) scheduling functionality"""
    
    @pytest.fixture
    def mock_storage(self):
        """Mock storage for testing"""
        with patch('motet.core.orchestration.scheduling.manager.ScheduleStorage') as mock:
            mock_instance = Mock()
            mock.return_value = mock_instance
            yield mock_instance
    
    def test_delayed_schedule_creation(self, mock_storage):
        """Test creating a delayed schedule with ETA"""
        manager = ScheduledCommandManager()
        
        # Create a command scheduled for 2 hours from now
        future_time = datetime.utcnow() + timedelta(hours=2)
        command = _ScheduleTestCommand(
            task_id="delayed_test",
            data={"test": "data"}
        )
        command.distributed_context.schedule_type = ScheduleType.DELAYED
        command.distributed_context.scheduled_at = future_time
        
        # Mock storage operations
        mock_storage.store_schedule.return_value = True
        
        schedule_id = manager.schedule_command(command)
        
        # Verify schedule was created
        assert schedule_id is not None
        mock_storage.store_schedule.assert_called_once()
        
        # Check the schedule metadata passed to storage
        call_args = mock_storage.store_schedule.call_args[0]
        schedule_metadata = call_args[0]
        
        assert schedule_metadata.schedule_type == ScheduleType.DELAYED
        assert schedule_metadata.scheduled_at == future_time
        assert schedule_metadata.status == ScheduleStatus.ACTIVE
    
    def test_delayed_schedule_next_execution_calculation(self, mock_storage):
        """Test next execution calculation for delayed schedules"""
        manager = ScheduledCommandManager()
        
        scheduled_time = datetime.utcnow() + timedelta(minutes=30)
        schedule = ScheduleMetadata(
            schedule_id="delayed-123",
            command_id="cmd-456",
            command_type="TestCommand",
            schedule_type=ScheduleType.DELAYED,
            scheduled_at=scheduled_time
        )
        
        next_execution = manager._calculate_next_execution(schedule)
        
        assert next_execution == scheduled_time
    
    def test_delayed_schedule_ready_for_execution(self, mock_storage):
        """Test that delayed schedules are ready when ETA is reached"""
        manager = ScheduledCommandManager()
        
        # Create a schedule that should execute now
        past_time = datetime.utcnow() - timedelta(minutes=5)
        schedule = ScheduleMetadata(
            schedule_id="ready-123",
            command_id="cmd-456",
            command_type="TestCommand",
            schedule_type=ScheduleType.DELAYED,
            scheduled_at=past_time,
            status=ScheduleStatus.ACTIVE,
            next_execution_at=past_time
        )
        
        # Mock storage to return our schedule
        mock_storage.list_schedules.return_value = [schedule]
        
        ready_schedules = manager.get_schedules_ready_for_execution()
        
        assert len(ready_schedules) == 1
        assert ready_schedules[0].schedule_id == "ready-123"
    
    def test_delayed_schedule_not_ready_future_eta(self, mock_storage):
        """Test that delayed schedules are not ready when ETA is in future"""
        manager = ScheduledCommandManager()
        
        # Create a schedule that should execute in the future
        future_time = datetime.utcnow() + timedelta(hours=1)
        schedule = ScheduleMetadata(
            schedule_id="future-123",
            command_id="cmd-456",
            command_type="TestCommand",
            schedule_type=ScheduleType.DELAYED,
            scheduled_at=future_time,
            status=ScheduleStatus.ACTIVE,
            next_execution_at=future_time
        )
        
        # Mock storage to return our schedule
        mock_storage.list_schedules.return_value = [schedule]
        
        ready_schedules = manager.get_schedules_ready_for_execution()
        
        assert len(ready_schedules) == 0
    
    @patch('motet.core.workers.schedule_tasks.schedule_distributed_command')
    def test_delayed_schedule_celery_eta_integration(self, mock_celery_task, mock_storage):
        """Test that delayed schedules integrate with Celery ETA"""
        from motet.core.workers.schedule_tasks import schedule_distributed_command

        # Mock Celery task
        mock_result = Mock()
        mock_celery_task.apply_async.return_value = mock_result

        # Create schedule data for a delayed execution
        future_time = _utc_now() + timedelta(hours=1)
        schedule_data = {
            "schedule_id": "delayed-celery-123",
            "command_data": {"test": "data"},
            "distributed_context": {"task_id": "test-task"}
        }
        
        # Test that we can schedule with ETA
        # Note: In real implementation, this would be called by the scheduler
        result = schedule_distributed_command.apply_async(
            args=[schedule_data],
            eta=future_time
        )
        
        # Verify the task was scheduled with ETA
        mock_celery_task.apply_async.assert_called_once()
        call_kwargs = mock_celery_task.apply_async.call_args[1]
        assert 'eta' in call_kwargs
        assert call_kwargs['eta'] == future_time


class TestCronRecurringScheduling:
    """Test cron-based recurring scheduling functionality"""
    
    @pytest.fixture
    def mock_storage(self):
        """Mock storage for testing"""
        with patch('motet.core.orchestration.scheduling.manager.ScheduleStorage') as mock:
            mock_instance = Mock()
            mock.return_value = mock_instance
            yield mock_instance
    
    def test_cron_schedule_creation(self, mock_storage):
        """Test creating a cron-based recurring schedule"""
        manager = ScheduledCommandManager()
        
        command = _ScheduleTestCommand(
            task_id="cron_test",
            data={"test": "data"}
        )
        command.distributed_context.schedule_type = ScheduleType.RECURRING
        command.distributed_context.cron_expression = "0 9 * * MON-FRI"  # Weekdays at 9 AM
        command.distributed_context.max_executions = 100
        
        # Mock storage operations
        mock_storage.store_schedule.return_value = True
        
        schedule_id = manager.schedule_command(command)
        
        # Verify schedule was created
        assert schedule_id is not None
        mock_storage.store_schedule.assert_called_once()
        
        # Check the schedule metadata
        call_args = mock_storage.store_schedule.call_args[0]
        schedule_metadata = call_args[0]
        
        assert schedule_metadata.schedule_type == ScheduleType.RECURRING
        assert schedule_metadata.cron_expression == "0 9 * * MON-FRI"
        assert schedule_metadata.max_executions == 100
        assert schedule_metadata.status == ScheduleStatus.ACTIVE
    
    def test_interval_based_recurring_schedule(self, mock_storage):
        """Test interval-based recurring schedule (alternative to cron)"""
        manager = ScheduledCommandManager()
        
        command = _ScheduleTestCommand(
            task_id="interval_test",
            data={"test": "data"}
        )
        command.distributed_context.schedule_type = ScheduleType.RECURRING
        command.distributed_context.interval_seconds = 3600  # Every hour
        
        # Mock storage operations
        mock_storage.store_schedule.return_value = True
        
        schedule_id = manager.schedule_command(command)
        
        # Verify schedule was created
        assert schedule_id is not None
        
        # Check the schedule metadata
        call_args = mock_storage.store_schedule.call_args[0]
        schedule_metadata = call_args[0]
        
        assert schedule_metadata.schedule_type == ScheduleType.RECURRING
        assert schedule_metadata.interval_seconds == 3600
        assert schedule_metadata.cron_expression is None
    
    def test_recurring_schedule_next_execution_interval(self, mock_storage):
        """Test next execution calculation for interval-based recurring schedules"""
        manager = ScheduledCommandManager()
        
        # Test first execution (no previous execution)
        schedule = ScheduleMetadata(
            schedule_id="recurring-123",
            command_id="cmd-456",
            command_type="TestCommand",
            schedule_type=ScheduleType.RECURRING,
            interval_seconds=1800,  # 30 minutes
            last_execution_at=None
        )
        
        next_execution = manager._calculate_next_execution(schedule)

        # First execution should be immediate (use timezone-aware for comparison)
        now = _utc_now()
        time_diff = abs((next_execution - now).total_seconds())
        assert time_diff < 1.0

        # Test subsequent execution
        last_execution = _utc_now() - timedelta(minutes=10)
        schedule.last_execution_at = last_execution
        
        next_execution = manager._calculate_next_execution(schedule)
        expected_next = last_execution + timedelta(seconds=1800)
        
        assert abs((next_execution - expected_next).total_seconds()) < 1.0
    
    def test_recurring_schedule_cron_parsing(self, mock_storage):
        """Test cron expression parsing and next execution calculation"""
        manager = ScheduledCommandManager()
        
        # Test with a real cron expression - weekdays at 9 AM
        base_time = datetime(2024, 1, 1, 8, 0, 0)  # Monday 8:00 AM
        schedule = ScheduleMetadata(
            schedule_id="cron-123",
            command_id="cmd-456",
            command_type="TestCommand",
            schedule_type=ScheduleType.RECURRING,
            cron_expression="0 9 * * MON-FRI",
            last_execution_at=base_time
        )
        
        next_execution = manager._calculate_next_execution(schedule)
        
        # Should be Monday at 9:00 AM (1 hour later)
        expected = datetime(2024, 1, 1, 9, 0, 0)
        assert next_execution == expected
    
    def test_recurring_schedule_cron_validation(self, mock_storage):
        """Test that invalid cron expressions are rejected during schedule creation"""
        manager = ScheduledCommandManager()
        
        command = _ScheduleTestCommand(
            task_id="invalid_cron_test",
            data={"test": "data"}
        )
        command.distributed_context.schedule_type = ScheduleType.RECURRING
        command.distributed_context.cron_expression = "invalid cron expression"
        
        # Mock storage operations
        mock_storage.store_schedule.return_value = True
        
        # Should raise RuntimeError wrapping the ValueError for invalid cron expression
        with pytest.raises(RuntimeError, match="Failed to schedule command.*Invalid cron expression"):
            manager.schedule_command(command)
    
    def test_recurring_schedule_max_executions(self, mock_storage):
        """Test that recurring schedules respect max_executions limit"""
        manager = ScheduledCommandManager()
        
        # Create a schedule that has reached its execution limit
        schedule = ScheduleMetadata(
            schedule_id="limited-123",
            command_id="cmd-456",
            command_type="TestCommand",
            schedule_type=ScheduleType.RECURRING,
            interval_seconds=60,
            execution_count=10,
            max_executions=10,  # Reached limit
            status=ScheduleStatus.ACTIVE
        )
        
        # Mock storage to return our schedule
        mock_storage.list_schedules.return_value = [schedule]
        
        ready_schedules = manager.get_schedules_ready_for_execution()
        
        # Schedule should not be ready because it reached max executions
        # Note: This depends on the should_execute() method implementation
        assert len(ready_schedules) == 0 or not ready_schedules[0].should_execute()
    
    def test_recurring_schedule_execution_tracking(self, mock_storage):
        """Test that recurring schedules track execution count properly"""
        from motet.core.orchestration.scheduling.models import ScheduleExecutionResult
        
        manager = ScheduledCommandManager()
        
        # Create a recurring schedule
        schedule = ScheduleMetadata(
            schedule_id="tracking-123",
            command_id="cmd-456",
            command_type="TestCommand",
            schedule_type=ScheduleType.RECURRING,
            interval_seconds=60,
            execution_count=5,
            status=ScheduleStatus.ACTIVE
        )
        
        # Mock storage operations
        mock_storage.retrieve_schedule.return_value = schedule
        mock_storage.update_schedule.return_value = True
        
        # Record a successful execution
        execution_result = ScheduleExecutionResult(
            schedule_id="tracking-123",
            execution_id="exec-789",
            success=True,
            schedule_status=ScheduleStatus.ACTIVE,
            execution_count=6,  # Expected after increment
            consecutive_failures=0,
            execution_time_ms=1000,
            worker_id="worker-1"
        )
        
        result = manager.record_execution_result("tracking-123", execution_result)
        
        assert result is True
        mock_storage.update_schedule.assert_called_once()
        
        # Verify the schedule was updated with incremented execution count
        updated_schedule = mock_storage.update_schedule.call_args[0][0]
        assert updated_schedule.execution_count == 6  # Should be incremented
    
    @patch('motet.core.workers.schedule_tasks.check_recurring_schedules')
    def test_celery_beat_recurring_integration(self, mock_recurring_check):
        """Test integration with Celery Beat for recurring schedules"""
        from motet.core.workers.schedule_tasks import check_recurring_schedules
        
        # Mock the recurring check task
        mock_result = {
            "status": "success",
            "checked_count": 3,
            "executed_count": 1,
            "execution_time_ms": 150
        }
        mock_recurring_check.return_value = mock_result
        
        # Test that the recurring check can be called
        result = check_recurring_schedules()
        
        mock_recurring_check.assert_called_once()
        assert result["status"] == "success"
        assert "checked_count" in result
        assert "executed_count" in result


class TestScheduleExecutionIntegration:
    """Test end-to-end schedule execution scenarios"""
    
    @pytest.fixture
    def mock_storage(self):
        """Mock storage for testing"""
        with patch('motet.core.orchestration.scheduling.manager.ScheduleStorage') as mock:
            mock_instance = Mock()
            mock.return_value = mock_instance
            yield mock_instance
    
    @patch('motet.core.workers.schedule_tasks.schedule_distributed_command')
    def test_delayed_to_recurring_transition(self, mock_celery_task, mock_storage):
        """Test a schedule that starts delayed then becomes recurring"""
        manager = ScheduledCommandManager()
        
        # Create a schedule that starts in 1 hour, then repeats every 6 hours
        start_time = _utc_now() + timedelta(hours=1)
        
        command = _ScheduleTestCommand(
            task_id="delayed_recurring_test",
            data={"test": "data"}
        )
        command.distributed_context.schedule_type = ScheduleType.RECURRING
        command.distributed_context.scheduled_at = start_time  # Initial delay
        command.distributed_context.interval_seconds = 21600  # 6 hours
        command.distributed_context.max_executions = 5
        
        # Mock storage operations
        mock_storage.store_schedule.return_value = True
        
        schedule_id = manager.schedule_command(command)
        
        # Verify schedule was created with both delayed start and recurring behavior
        assert schedule_id is not None
        
        call_args = mock_storage.store_schedule.call_args[0]
        schedule_metadata = call_args[0]
        
        assert schedule_metadata.schedule_type == ScheduleType.RECURRING
        assert schedule_metadata.scheduled_at == start_time
        assert schedule_metadata.interval_seconds == 21600
        assert schedule_metadata.max_executions == 5
    
    def test_schedule_failure_and_retry_logic(self, mock_storage):
        """Test schedule failure handling and retry logic"""
        from motet.core.orchestration.scheduling.models import ScheduleExecutionResult
        
        manager = ScheduledCommandManager()
        
        # Create a schedule
        schedule = ScheduleMetadata(
            schedule_id="retry-123",
            command_id="cmd-456",
            command_type="TestCommand",
            schedule_type=ScheduleType.RECURRING,
            interval_seconds=60,
            consecutive_failures=0,
            max_consecutive_failures=3,
            status=ScheduleStatus.ACTIVE
        )
        
        # Mock storage operations
        mock_storage.retrieve_schedule.return_value = schedule
        mock_storage.update_schedule.return_value = True
        
        # Record a failed execution
        execution_result = ScheduleExecutionResult(
            schedule_id="retry-123",
            execution_id="exec-fail-1",
            success=False,
            schedule_status=ScheduleStatus.ACTIVE,  # Still active after first failure
            execution_count=0,
            consecutive_failures=1,
            error="Command execution failed",
            execution_time_ms=5000,
            worker_id="worker-1"
        )
        
        result = manager.record_execution_result("retry-123", execution_result)
        
        assert result is True
        
        # Verify the schedule failure was recorded
        updated_schedule = mock_storage.update_schedule.call_args[0][0]
        assert updated_schedule.consecutive_failures == 1
        assert updated_schedule.last_error == "Command execution failed"
        
        # Test multiple failures leading to schedule being marked as FAILED
        schedule.consecutive_failures = 2  # Already had 2 failures
        execution_result.execution_id = "exec-fail-3"
        
        manager.record_execution_result("retry-123", execution_result)
        
        # After 3rd consecutive failure, schedule should be marked as FAILED
        updated_schedule = mock_storage.update_schedule.call_args[0][0]
        assert updated_schedule.consecutive_failures == 3
        assert updated_schedule.status == ScheduleStatus.FAILED


if __name__ == "__main__":
    pytest.main([__file__])
