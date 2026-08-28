"""
Tests for WorkerRouter

Tests the core routing engine with all strategies and filters.
"""

import pytest
from unittest.mock import Mock, patch
from typing import Dict, Any, List
from types import SimpleNamespace

from motet.core.workers.routing.worker_router import WorkerRouter, RoutingDecision
from motet.core.workers.routing.strategies.base import RoutingContext, RoutingPriority
from motet.core.distributed.worker_readiness import WorkerState, WorkerInfo


class MockReadinessService:
    """Mock readiness service for testing"""
    
    def __init__(self):
        self.workers = {
            'worker-1': WorkerInfo(
                worker_id='worker-1',
                state=WorkerState.READY,
                capabilities=['model_inference', 'text_processing'],
                active_commands=1,
                max_concurrency=5,
                tool_count=10,
                mcp_tool_count=5,
                warmup_completed=True,
                last_heartbeat=1234567890,
                startup_time=1234567800
            ),
            'worker-2': WorkerInfo(
                worker_id='worker-2',
                state=WorkerState.READY,
                capabilities=['model_inference', 'data_processing'],
                active_commands=3,
                max_concurrency=5,
                tool_count=8,
                mcp_tool_count=3,
                warmup_completed=True,
                last_heartbeat=1234567890,
                startup_time=1234567800
            ),
            'worker-3': WorkerInfo(
                worker_id='worker-3',
                state=WorkerState.BUSY,
                capabilities=['specialized_task'],
                active_commands=5,
                max_concurrency=5,
                tool_count=15,
                mcp_tool_count=8,
                warmup_completed=True,
                last_heartbeat=1234567890,
                startup_time=1234567800
            )
        }
    
    def get_all_workers(self) -> Dict[str, WorkerInfo]:
        return self.workers.copy()
    
    def get_worker_info(self, worker_id: str) -> WorkerInfo:
        return self.workers.get(worker_id)
    
    def get_readiness_stats(self) -> Dict[str, Any]:
        return {
            'total_workers': len(self.workers),
            'ready_workers': sum(1 for w in self.workers.values() if w.state == WorkerState.READY),
            'workers': {wid: {'state': w.state.value} for wid, w in self.workers.items()}
        }


@pytest.fixture
def mock_readiness_service():
    return MockReadinessService()


@pytest.fixture
def worker_router(mock_readiness_service):
    return WorkerRouter(
        readiness_service=mock_readiness_service,
        default_strategy="least_loaded",
        enable_caching=False  # Disable caching for tests
    )


@pytest.fixture
def sample_command():
    """Create a mock command for testing (real types for RoutingContext.from_command)."""
    command = Mock()
    command.get_command_type.return_value = "core.model_inference"
    command.tenant_id = None
    command.session_id = None
    command.preferred_region = None
    command.max_cost = None
    command.require_specific_worker = False
    command.target_worker_id = None
    command.distributed_context = Mock()
    command.distributed_context.required_capabilities = set()
    command.distributed_context.priority = 5
    command.distributed_context.tenant_id = None
    command.distributed_context.principal_id = None
    command.distributed_context.timeout_seconds = 60
    command.distributed_context.target_worker_id = None
    command.distributed_context.preferred_worker_ids = []
    command.distributed_context.worker_affinity = None
    command.distributed_context.avoid_worker_ids = []
    command.distributed_context.preferred_pool_type = None
    return command


class TestWorkerRouter:
    """Test the WorkerRouter core functionality (route_command and helpers are sync)."""

    def test_basic_routing(self, worker_router, sample_command):
        """Test basic command routing"""
        result = worker_router.route_command(sample_command)

        assert isinstance(result, RoutingDecision)
        assert result.selected_worker is not None
        assert result.strategy_used == "least_loaded"
        assert result.available_workers == 3
        assert result.filtered_workers == 2  # Only READY workers
        assert result.error is None

    def test_specific_worker_routing(self, worker_router, sample_command):
        """Test routing to a specific worker"""
        result = worker_router.route_command(
            sample_command,
            target_worker_id="worker-1"
        )

        assert result.selected_worker is not None
        assert result.selected_worker['worker_id'] == "worker-1"
        assert result.strategy_used == "specific_worker"

    def test_strategy_override(self, worker_router, sample_command):
        """Test strategy override functionality"""
        result = worker_router.route_command(
            sample_command,
            strategy_override="round_robin"
        )

        assert result.strategy_used == "round_robin"
        assert result.selected_worker is not None

    def test_tenant_routing(self, worker_router, sample_command):
        """Test tenant-based routing"""
        sample_command.tenant_id = "test-tenant"

        result = worker_router.route_command(sample_command)

        # Should use tenant_affinity strategy
        assert result.strategy_used == "tenant_affinity"
        assert result.selected_worker is not None

    def test_capability_filtering(self, worker_router, sample_command):
        """Test capability-based filtering"""
        sample_command.distributed_context.required_capabilities = {"specialized_task"}

        result = worker_router.route_command(sample_command)

        # Should filter to only worker-3, but it's BUSY, so should fail
        assert result.selected_worker is None
        assert "No workers passed filtering" in result.error

    def test_edge_capability_guard_enforces_edge_worker_id(self, worker_router, sample_command):
        """EDGE_* required capabilities must only route to edge_* workers."""
        sample_command.distributed_context.tenant_id = None
        sample_command.distributed_context.principal_id = None
        # Cloud worker advertises local capability (misconfiguration), but should be excluded.
        worker_router.readiness_service.workers["worker-1"].capabilities.append("edge_clipboard")
        worker_router.readiness_service.workers["edge_14868271"] = WorkerInfo(
            worker_id="edge_14868271",
            state=WorkerState.READY,
            capabilities=["tool_execution", "edge_execution", "edge_clipboard"],
            active_commands=0,
            max_concurrency=5,
            tool_count=5,
            mcp_tool_count=0,
            warmup_completed=True,
            last_heartbeat=1234567890,
            startup_time=1234567800,
        )
        sample_command.distributed_context.required_capabilities = {"edge_clipboard"}

        result = worker_router.route_command(sample_command)

        assert result.selected_worker is not None
        assert result.selected_worker["worker_id"].startswith("edge_")

    def test_edge_capability_guard_fails_without_edge_worker(self, worker_router, sample_command):
        """EDGE_* required capabilities fail when no edge_* worker is available."""
        sample_command.distributed_context.tenant_id = None
        sample_command.distributed_context.principal_id = None
        sample_command.distributed_context.required_capabilities = {"edge_clipboard"}

        # Misconfigured cloud worker advertises local capability.
        worker_router.readiness_service.workers["worker-1"].capabilities.append("edge_clipboard")
        # Ensure no edge_* workers are present.
        worker_router.readiness_service.workers.pop("edge_14868271", None)

        result = worker_router.route_command(sample_command)

        assert result.selected_worker is None
        assert result.error is not None
        assert "No workers passed filtering" in result.error

    def test_tool_execution_backfills_missing_capabilities_for_routing(self, worker_router, sample_command):
        """Router backfills tool_execution required capabilities when command is missing them."""
        sample_command.get_command_type.return_value = "core.tool_execution"
        sample_command.distributed_context.required_capabilities = set()
        sample_command.distributed_context.tenant_id = None
        sample_command.distributed_context.principal_id = None
        sample_command.data = SimpleNamespace(tool_name="core.clipboard_write")

        worker_router.readiness_service.workers["edge_14868271"] = WorkerInfo(
            worker_id="edge_14868271",
            state=WorkerState.READY,
            capabilities=["tool_execution", "edge_execution", "edge_clipboard"],
            active_commands=0,
            max_concurrency=5,
            tool_count=5,
            mcp_tool_count=0,
            warmup_completed=True,
            last_heartbeat=1234567890,
            startup_time=1234567800,
        )

        fake_tool = SimpleNamespace(
            required_capabilities=["TOOL_EXECUTION", "EDGE_EXECUTION", "EDGE_CLIPBOARD"]
        )
        with patch("motet.core.tools.registry.registry.get", return_value=fake_tool):
            result = worker_router.route_command(sample_command)

        assert result.selected_worker is not None
        assert result.selected_worker["worker_id"].startswith("edge_")

    def test_no_workers_available(self, worker_router, sample_command):
        """Test behavior when no workers are available"""
        # Mock empty worker list
        worker_router.readiness_service.workers = {}

        result = worker_router.route_command(sample_command)

        assert result.selected_worker is None
        assert result.error == "No workers available"
        assert result.available_workers == 0

    def test_get_available_workers(self, worker_router):
        """Test getting available workers"""
        workers = worker_router.get_available_workers()

        assert len(workers) == 2  # Only READY workers
        worker_ids = [w['worker_id'] for w in workers]
        assert 'worker-1' in worker_ids
        assert 'worker-2' in worker_ids
        assert 'worker-3' not in worker_ids  # BUSY worker filtered out

    def test_get_routing_stats(self, worker_router):
        """Test routing statistics"""
        sample_command = Mock()
        sample_command.get_command_type.return_value = "TestCommand"
        sample_command.tenant_id = None
        sample_command.session_id = None
        sample_command.preferred_region = None
        sample_command.max_cost = None
        sample_command.require_specific_worker = False
        sample_command.target_worker_id = None
        sample_command.distributed_context = Mock()
        sample_command.distributed_context.required_capabilities = set()
        sample_command.distributed_context.priority = 5
        sample_command.distributed_context.tenant_id = None
        sample_command.distributed_context.principal_id = None
        sample_command.distributed_context.timeout_seconds = 60
        sample_command.distributed_context.target_worker_id = None
        sample_command.distributed_context.preferred_worker_ids = []
        sample_command.distributed_context.worker_affinity = None
        sample_command.distributed_context.avoid_worker_ids = []
        sample_command.distributed_context.preferred_pool_type = None

        worker_router.route_command(sample_command)

        stats = worker_router.get_routing_stats()

        assert 'total_requests' in stats
        assert 'successful_routes' in stats
        assert 'strategy_usage' in stats
        assert 'readiness_stats' in stats

    def test_tenant_routing_info(self, worker_router):
        """Test tenant-specific routing information"""
        tenant_info = worker_router.get_tenant_routing_info("test-tenant")

        assert 'tenant_id' in tenant_info
        assert 'available_workers' in tenant_info
        assert 'worker_details' in tenant_info
        assert tenant_info['tenant_id'] == "test-tenant"


class TestRoutingStrategies:
    """Test various routing strategies (sync)."""

    def test_least_loaded_strategy(self, worker_router, sample_command):
        """Test least loaded strategy selects worker with lowest load"""
        result = worker_router.route_command(
            sample_command,
            strategy_override="least_loaded"
        )

        assert result.selected_worker is not None
        # worker-1 has load 0.2 (1/5), worker-2 has load 0.6 (3/5)
        assert result.selected_worker['worker_id'] == "worker-1"

    def test_capability_optimized_strategy(self, worker_router, sample_command):
        """Test capability optimized strategy"""
        sample_command.distributed_context.required_capabilities = {"model_inference"}

        result = worker_router.route_command(
            sample_command,
            strategy_override="capability_optimized"
        )

        assert result.selected_worker is not None
        assert result.selected_worker['worker_id'] in ["worker-1", "worker-2"]

    def test_multi_tenant_strategy(self, worker_router, sample_command):
        """Test multi-tenant strategy"""
        sample_command.tenant_id = "tenant-123"

        result = worker_router.route_command(
            sample_command,
            strategy_override="multi_tenant"
        )

        assert result.selected_worker is not None
        assert result.strategy_used == "multi_tenant"


class TestRoutingFilters:
    """Test routing filters (sync)."""

    def test_readiness_filter(self, worker_router, sample_command):
        """Test that readiness filter excludes BUSY workers"""
        result = worker_router.route_command(sample_command)

        assert result.filtered_workers == 2
        assert result.selected_worker['worker_id'] in ["worker-1", "worker-2"]

    def test_capability_filter(self, worker_router, sample_command):
        """Test capability filter"""
        sample_command.distributed_context.required_capabilities = {"data_processing"}

        result = worker_router.route_command(sample_command)

        assert result.selected_worker is not None
        assert result.selected_worker['worker_id'] == "worker-2"


class TestErrorHandling:
    """Test error handling scenarios (sync)."""

    def test_invalid_strategy(self, worker_router, sample_command):
        """Test handling of invalid strategy"""
        result = worker_router.route_command(
            sample_command,
            strategy_override="nonexistent_strategy"
        )

        assert result.selected_worker is not None
        assert result.strategy_used == "least_loaded"

    def test_routing_exception(self, worker_router, sample_command):
        """Test handling of routing exceptions"""
        with patch.object(worker_router, '_get_all_workers', side_effect=Exception("Test error")):
            result = worker_router.route_command(sample_command)

            assert result.selected_worker is None
            assert "Routing error" in result.error


class TestEdgeWorkerScopePlumbing:
    """Router worker dicts must carry ADR-0095 edge scope fields so
    EdgeWorkerAffinityFilter can exclude cross-principal edge workers
    at routing time (not just at worker-side rejection)."""

    @staticmethod
    def _edge_worker(worker_id: str, owner_principal: str) -> WorkerInfo:
        return WorkerInfo(
            worker_id=worker_id,
            state=WorkerState.READY,
            capabilities=['model_inference'],
            active_commands=0,
            max_concurrency=5,
            warmup_completed=True,
            last_heartbeat=1234567890,
            startup_time=1234567800,
            owner_principal_id=owner_principal,
            command_scope='principal',
        )

    @pytest.fixture
    def edge_router(self, mock_readiness_service):
        mock_readiness_service.workers['edge_app_builder_motet'] = self._edge_worker(
            'edge_app_builder_motet', 'app-builder/motet'
        )
        mock_readiness_service.workers['edge_app_builder_smoke'] = self._edge_worker(
            'edge_app_builder_smoke', 'app-builder/smoke'
        )
        return WorkerRouter(
            readiness_service=mock_readiness_service,
            default_strategy="least_loaded",
            enable_caching=False,
        )

    def test_get_all_workers_includes_edge_scope_fields(self, edge_router):
        """_get_all_workers must propagate owner/scope fields from WorkerInfo."""
        workers = {w['worker_id']: w for w in edge_router._get_all_workers()}

        edge = workers['edge_app_builder_motet']
        assert edge['owner_principal_id'] == 'app-builder/motet'
        assert edge['command_scope'] == 'principal'
        assert edge['owner_tenant_id'] is None

        cloud = workers['worker-1']
        assert cloud['owner_principal_id'] is None
        assert cloud['command_scope'] is None

    def test_cross_principal_command_never_routes_to_foreign_edge(
        self, edge_router, sample_command
    ):
        """A command with principal app-builder/smoke must never be routed to
        the edge worker owned by app-builder/motet (idle edges score high on
        least_loaded, so this exercises the affinity filter, not luck)."""
        sample_command.distributed_context.principal_id = 'app-builder/smoke'

        for _ in range(10):
            result = edge_router.route_command(sample_command)
            assert result.selected_worker is not None
            assert result.selected_worker['worker_id'] != 'edge_app_builder_motet'

    def test_matching_principal_command_may_route_to_own_edge(
        self, edge_router, sample_command
    ):
        """Sanity: the owning principal's edge worker stays a valid candidate."""
        sample_command.distributed_context.principal_id = 'app-builder/motet'

        result = edge_router.route_command(sample_command)
        assert result.selected_worker is not None
        assert result.selected_worker['worker_id'] != 'edge_app_builder_smoke'


class TestPerformance:
    """Test performance aspects of routing (sync)."""

    def test_routing_performance(self, worker_router, sample_command):
        """Test that routing decisions are made quickly"""
        import time

        start_time = time.time()
        result = worker_router.route_command(sample_command)
        end_time = time.time()

        assert (end_time - start_time) < 0.1
        assert result.decision_time_ms < 100
        assert result.selected_worker is not None

    def test_concurrent_routing(self, worker_router):
        """Test multiple routing requests (sync loop)."""
        commands = []
        for i in range(10):
            cmd = Mock()
            cmd.get_command_type.return_value = f"TestCommand{i}"
            cmd.tenant_id = f"tenant-{i % 3}"
            cmd.session_id = None
            cmd.preferred_region = None
            cmd.max_cost = None
            cmd.require_specific_worker = False
            cmd.target_worker_id = None
            cmd.distributed_context = Mock()
            cmd.distributed_context.required_capabilities = set()
            cmd.distributed_context.priority = 5
            cmd.distributed_context.tenant_id = cmd.tenant_id
            cmd.distributed_context.principal_id = None
            cmd.distributed_context.timeout_seconds = 60
            cmd.distributed_context.target_worker_id = None
            cmd.distributed_context.preferred_worker_ids = []
            cmd.distributed_context.worker_affinity = None
            cmd.distributed_context.avoid_worker_ids = []
            cmd.distributed_context.preferred_pool_type = None
            commands.append(cmd)

        results = [worker_router.route_command(cmd) for cmd in commands]

        assert len(results) == 10
        for res in results:
            assert res.selected_worker is not None
            assert res.error is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
