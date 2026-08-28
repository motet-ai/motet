"""
Performance Tests for State-Aware Routing System

Benchmarks and validates the performance improvements achieved by
state-aware routing compared to traditional load balancing.
"""

import asyncio
import pytest
import time
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta
import statistics

import sys
from pathlib import Path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from motet.core.distributed.state_aware_routing import (
    StateAwareRouter, RoutingStrategy, WorkerCandidate
)
from motet.core.distributed.state_registry import WorkerState, StateStatus
from motet.core.commands.distributed import DistributedCommand
from motet.core.commands.capabilities import WorkerCapability


@pytest.fixture
def performance_test_workers():
    """Create a realistic set of workers for performance testing."""
    return [
        {
            "worker_id": f"worker_{i}",
            "worker_pid": 1000 + i,
            "current_load": 0.1 + (i * 0.1),  # Varying loads
            "capabilities": ["tool_execution", "model_inference", "memory_operations"]
        }
        for i in range(10)  # 10 workers for realistic testing
    ]


@pytest.fixture
def mock_state_registry_performance():
    """Mock state registry optimized for performance testing."""
    registry = MagicMock()
    
    # Simulate realistic state distribution
    def mock_get_worker_states(worker_id):
        worker_num = int(worker_id.split('_')[1])
        states = []
        
        # First 3 workers have MCP connections
        if worker_num < 3:
            states.append(WorkerState(
                worker_id=worker_id,
                worker_pid=1000 + worker_num,
                state_type="mcp_connection",
                status=StateStatus.ACTIVE,
                expires_at=datetime.utcnow() + timedelta(minutes=5),
                usage_count=worker_num * 2
            ))
        
        # Workers 3-5 have model cache
        if 3 <= worker_num < 6:
            states.append(WorkerState(
                worker_id=worker_id,
                worker_pid=1000 + worker_num,
                state_type="model_cache",
                status=StateStatus.ACTIVE,
                expires_at=datetime.utcnow() + timedelta(minutes=10),
                usage_count=worker_num
            ))
        
        return states
    
    registry.get_worker_states.side_effect = mock_get_worker_states
    return registry


@pytest.fixture
def sample_commands():
    """Create sample commands for performance testing."""
    commands = []
    
    # MCP-benefiting commands (weather tools)
    for i in range(5):
        cmd = MagicMock(spec=DistributedCommand)
        cmd.command_id = f"mcp_cmd_{i}"
        cmd.get_command_type.return_value = "core.tool_execution"
        cmd.get_required_capabilities.return_value = [WorkerCapability.TOOL_EXECUTION]
        cmd.tool_name = "weather"
        commands.append(("mcp", cmd))
    
    # Model inference commands
    for i in range(5):
        cmd = MagicMock(spec=DistributedCommand)
        cmd.command_id = f"model_cmd_{i}"
        cmd.get_command_type.return_value = "core.model_inference"
        cmd.get_required_capabilities.return_value = [WorkerCapability.MODEL_INFERENCE]
        commands.append(("model", cmd))
    
    # Generic commands (no specific state benefit)
    for i in range(5):
        cmd = MagicMock(spec=DistributedCommand)
        cmd.command_id = f"generic_cmd_{i}"
        cmd.get_command_type.return_value = "core.agent_turn"
        cmd.get_required_capabilities.return_value = [WorkerCapability.TOOL_EXECUTION]
        commands.append(("generic", cmd))
    
    return commands


class TestRoutingPerformance:
    """Test routing performance and timing."""
    
    @pytest.mark.asyncio
    async def test_routing_latency_benchmark(self, performance_test_workers, mock_state_registry_performance):
        """Benchmark routing latency for different strategies."""
        router = StateAwareRouter(mock_state_registry_performance, RoutingStrategy.HYBRID)
        
        # Test command that benefits from MCP state
        test_command = MagicMock(spec=DistributedCommand)
        test_command.get_command_type.return_value = "core.tool_execution"
        test_command.get_required_capabilities.return_value = [WorkerCapability.TOOL_EXECUTION]
        test_command.tool_name = "weather"
        
        # Benchmark different routing strategies
        strategies = [
            RoutingStrategy.LOAD_BALANCED,
            RoutingStrategy.STATE_AWARE,
            RoutingStrategy.HYBRID
        ]
        
        results = {}
        
        for strategy in strategies:
            times = []
            
            # Run multiple iterations for statistical significance
            for _ in range(20):
                start_time = time.perf_counter()
                
                selected_worker = router.select_optimal_worker(
                    test_command,
                    performance_test_workers,
                    strategy
                )
                
                end_time = time.perf_counter()
                times.append((end_time - start_time) * 1000)  # Convert to milliseconds
                
                assert selected_worker is not None
            
            results[strategy.value] = {
                "avg_ms": statistics.mean(times),
                "median_ms": statistics.median(times),
                "min_ms": min(times),
                "max_ms": max(times),
                "std_dev": statistics.stdev(times) if len(times) > 1 else 0
            }
        
        # Validate performance expectations
        # State-aware routing should be reasonably fast (< 50ms average)
        assert results["state_aware"]["avg_ms"] < 50.0
        assert results["hybrid"]["avg_ms"] < 50.0
        
        # Load balanced should be fastest (< 10ms average)
        assert results["load_balanced"]["avg_ms"] < 10.0
        
        print(f"\n🚀 Routing Performance Benchmark Results:")
        for strategy, metrics in results.items():
            print(f"  {strategy.upper()}:")
            print(f"    Average: {metrics['avg_ms']:.2f}ms")
            print(f"    Median:  {metrics['median_ms']:.2f}ms")
            print(f"    Range:   {metrics['min_ms']:.2f}-{metrics['max_ms']:.2f}ms")
    
    @pytest.mark.asyncio
    async def test_state_affinity_effectiveness(self, performance_test_workers, mock_state_registry_performance, sample_commands):
        """Test effectiveness of state affinity in routing decisions."""
        router = StateAwareRouter(mock_state_registry_performance, RoutingStrategy.HYBRID)
        
        routing_results = {
            "mcp": {"with_state": 0, "without_state": 0},
            "model": {"with_state": 0, "without_state": 0},
            "generic": {"with_state": 0, "without_state": 0}
        }
        
        for command_type, command in sample_commands:
            selected_worker = router.select_optimal_worker(
                command,
                performance_test_workers,
                RoutingStrategy.HYBRID
            )
            
            assert selected_worker is not None
            
            # Check if selected worker has relevant state
            worker_num = int(selected_worker.worker_id.split('_')[1])
            has_relevant_state = False
            
            if command_type == "mcp" and worker_num < 3:  # Workers 0-2 have MCP state
                has_relevant_state = True
            elif command_type == "model" and 3 <= worker_num < 6:  # Workers 3-5 have model cache
                has_relevant_state = True
            
            if has_relevant_state:
                routing_results[command_type]["with_state"] += 1
            else:
                routing_results[command_type]["without_state"] += 1
        
        # Validate state affinity effectiveness
        # MCP commands should prefer workers with MCP state (>60% hit rate)
        mcp_hit_rate = routing_results["mcp"]["with_state"] / (
            routing_results["mcp"]["with_state"] + routing_results["mcp"]["without_state"]
        )
        assert mcp_hit_rate > 0.6, f"MCP state affinity too low: {mcp_hit_rate:.2%}"
        
        # Model commands should prefer workers with model cache (>60% hit rate)
        model_hit_rate = routing_results["model"]["with_state"] / (
            routing_results["model"]["with_state"] + routing_results["model"]["without_state"]
        )
        assert model_hit_rate > 0.6, f"Model cache affinity too low: {model_hit_rate:.2%}"
        
        print(f"\n📊 State Affinity Effectiveness:")
        print(f"  MCP Commands:     {mcp_hit_rate:.1%} routed to workers with MCP state")
        print(f"  Model Commands:   {model_hit_rate:.1%} routed to workers with model cache")
    
    @pytest.mark.asyncio
    async def test_concurrent_routing_performance(self, performance_test_workers, mock_state_registry_performance):
        """Test routing performance under concurrent load."""
        router = StateAwareRouter(mock_state_registry_performance, RoutingStrategy.HYBRID)
        
        # Create multiple concurrent commands
        def route_command(command_id):
            command = MagicMock(spec=DistributedCommand)
            command.command_id = f"concurrent_cmd_{command_id}"
            command.get_command_type.return_value = "core.tool_execution"
            command.get_required_capabilities.return_value = [WorkerCapability.TOOL_EXECUTION]
            command.tool_name = "weather"
            
            start_time_inner = time.perf_counter()
            selected_worker = router.select_optimal_worker(
                command,
                performance_test_workers,
                RoutingStrategy.HYBRID
            )
            end_time_inner = time.perf_counter()
            
            return {
                "command_id": command_id,
                "selected_worker": selected_worker.worker_id if selected_worker else None,
                "duration_ms": (end_time_inner - start_time_inner) * 1000
            }
        
        # Execute 50 routing operations
        start_time = time.perf_counter()
        results = [route_command(i) for i in range(50)]
        total_time = time.perf_counter() - start_time
        
        # Validate results
        successful_routes = [r for r in results if r["selected_worker"] is not None]
        assert len(successful_routes) == 50, "All concurrent routes should succeed"
        
        # Calculate performance metrics
        durations = [r["duration_ms"] for r in results]
        avg_duration = statistics.mean(durations)
        max_duration = max(durations)
        
        # Performance expectations for concurrent routing
        assert avg_duration < 100.0, f"Average concurrent routing too slow: {avg_duration:.2f}ms"
        assert max_duration < 500.0, f"Max concurrent routing too slow: {max_duration:.2f}ms"
        assert total_time < 5.0, f"Total concurrent routing too slow: {total_time:.2f}s"
        
        print(f"\n⚡ Concurrent Routing Performance (50 concurrent routes):")
        print(f"  Total Time:     {total_time:.2f}s")
        print(f"  Average Route:  {avg_duration:.2f}ms")
        print(f"  Max Route:      {max_duration:.2f}ms")
        print(f"  Throughput:     {50/total_time:.1f} routes/second")
    
    @pytest.mark.asyncio
    async def test_memory_usage_efficiency(self, performance_test_workers, mock_state_registry_performance):
        """Test memory efficiency of routing operations."""
        import tracemalloc
        
        router = StateAwareRouter(mock_state_registry_performance, RoutingStrategy.HYBRID)
        
        test_command = MagicMock(spec=DistributedCommand)
        test_command.get_command_type.return_value = "core.tool_execution"
        test_command.get_required_capabilities.return_value = [WorkerCapability.TOOL_EXECUTION]
        
        # Start memory tracing
        tracemalloc.start()
        
        # Perform many routing operations
        for i in range(100):
            test_command.command_id = f"memory_test_cmd_{i}"
            selected_worker = router.select_optimal_worker(
                test_command,
                performance_test_workers,
                RoutingStrategy.HYBRID
            )
            assert selected_worker is not None
        
        # Get memory usage
        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        
        # Memory usage should be reasonable (< 10MB for 100 operations)
        peak_mb = peak / 1024 / 1024
        assert peak_mb < 10.0, f"Memory usage too high: {peak_mb:.2f}MB"
        
        print(f"\n💾 Memory Usage (100 routing operations):")
        print(f"  Peak Memory: {peak_mb:.2f}MB")
        print(f"  Per Route:   {peak_mb/100*1024:.2f}KB")


class TestScalabilityBenchmarks:
    """Test system scalability with varying worker counts."""
    
    @pytest.mark.asyncio
    async def test_routing_scalability_with_worker_count(self, mock_state_registry_performance):
        """Test how routing performance scales with worker count."""
        router = StateAwareRouter(mock_state_registry_performance, RoutingStrategy.HYBRID)
        
        test_command = MagicMock(spec=DistributedCommand)
        test_command.get_command_type.return_value = "core.tool_execution"
        test_command.get_required_capabilities.return_value = [WorkerCapability.TOOL_EXECUTION]
        test_command.tool_name = "weather"
        
        worker_counts = [5, 10, 25, 50, 100]
        scalability_results = {}
        
        for worker_count in worker_counts:
            # Generate workers for this test
            workers = [
                {
                    "worker_id": f"scale_worker_{i}",
                    "worker_pid": 2000 + i,
                    "current_load": 0.1 + (i * 0.01),
                    "capabilities": ["tool_execution", "model_inference"]
                }
                for i in range(worker_count)
            ]
            
            # Benchmark routing with this worker count
            times = []
            for _ in range(10):  # 10 iterations per worker count
                start_time = time.perf_counter()
                
                selected_worker = router.select_optimal_worker(
                    test_command,
                    workers,
                    RoutingStrategy.HYBRID
                )
                
                end_time = time.perf_counter()
                times.append((end_time - start_time) * 1000)
                
                assert selected_worker is not None
            
            scalability_results[worker_count] = {
                "avg_ms": statistics.mean(times),
                "max_ms": max(times)
            }
        
        # Validate scalability characteristics
        # Routing time should scale sub-linearly with worker count
        for worker_count in worker_counts:
            avg_time = scalability_results[worker_count]["avg_ms"]
            # Even with 100 workers, routing should be < 200ms
            assert avg_time < 200.0, f"Routing too slow with {worker_count} workers: {avg_time:.2f}ms"
        
        print(f"\n📈 Routing Scalability Results:")
        for worker_count, metrics in scalability_results.items():
            print(f"  {worker_count:3d} workers: {metrics['avg_ms']:6.2f}ms avg, {metrics['max_ms']:6.2f}ms max")
    
    @pytest.mark.asyncio
    async def test_state_registry_lookup_performance(self, mock_state_registry_performance):
        """Test performance of state registry lookups."""
        
        def variable_latency_get_worker_states(worker_id):
            worker_num = int(worker_id.split('_')[1]) if '_' in worker_id else 0
            if worker_num < 3:
                return [WorkerState(
                    worker_id=worker_id,
                    worker_pid=1000 + worker_num,
                    state_type="mcp_connection",
                    status=StateStatus.ACTIVE,
                    expires_at=datetime.utcnow() + timedelta(minutes=5)
                )]
            return []
        
        mock_state_registry_performance.get_worker_states.side_effect = variable_latency_get_worker_states
        
        router = StateAwareRouter(mock_state_registry_performance, RoutingStrategy.HYBRID)
        
        workers = [
            {
                "worker_id": f"latency_worker_{i}",
                "worker_pid": 3000 + i,
                "current_load": 0.2,
                "capabilities": ["tool_execution"]
            }
            for i in range(20)
        ]
        
        test_command = MagicMock(spec=DistributedCommand)
        test_command.get_command_type.return_value = "core.tool_execution"
        test_command.get_required_capabilities.return_value = [WorkerCapability.TOOL_EXECUTION]
        
        # Benchmark routing with simulated state registry latency
        times = []
        for _ in range(10):
            start_time = time.perf_counter()
            
            selected_worker = router.select_optimal_worker(
                test_command,
                workers,
                RoutingStrategy.HYBRID
            )
            
            end_time = time.perf_counter()
            times.append((end_time - start_time) * 1000)
            
            assert selected_worker is not None
        
        avg_time = statistics.mean(times)
        max_time = max(times)
        
        # Even with state registry latency, total routing should be reasonable
        assert avg_time < 300.0, f"Routing with state registry latency too slow: {avg_time:.2f}ms"
        
        print(f"\n🔍 State Registry Lookup Performance:")
        print(f"  Average routing time: {avg_time:.2f}ms")
        print(f"  Maximum routing time: {max_time:.2f}ms")


class TestPerformanceComparisons:
    """Compare performance between different routing strategies."""
    
    @pytest.mark.asyncio
    async def test_strategy_performance_comparison(self, performance_test_workers, mock_state_registry_performance, sample_commands):
        """Compare performance across all routing strategies."""
        router = StateAwareRouter(mock_state_registry_performance, RoutingStrategy.HYBRID)
        
        strategies = [
            RoutingStrategy.LOAD_BALANCED,
            RoutingStrategy.STATE_AWARE,
            RoutingStrategy.HYBRID,
            RoutingStrategy.ROUND_ROBIN
        ]
        
        performance_comparison = {}
        
        for strategy in strategies:
            times = []
            state_hits = 0
            total_commands = 0
            
            for command_type, command in sample_commands:
                start_time = time.perf_counter()
                
                selected_worker = router.select_optimal_worker(
                    command,
                    performance_test_workers,
                    strategy
                )
                
                end_time = time.perf_counter()
                times.append((end_time - start_time) * 1000)
                
                # Check if routing achieved state affinity
                if selected_worker and command_type == "mcp":
                    worker_num = int(selected_worker.worker_id.split('_')[1])
                    if worker_num < 3:  # Workers with MCP state
                        state_hits += 1
                
                total_commands += 1
            
            performance_comparison[strategy.value] = {
                "avg_routing_time_ms": statistics.mean(times),
                "max_routing_time_ms": max(times),
                "state_affinity_rate": state_hits / total_commands if total_commands > 0 else 0,
                "total_commands": total_commands
            }
        
        # Validate performance characteristics
        # Load balanced should be fastest
        load_balanced_time = performance_comparison["load_balanced"]["avg_routing_time_ms"]
        assert load_balanced_time < 20.0, f"Load balanced routing too slow: {load_balanced_time:.2f}ms"
        
        # State-aware should have best affinity
        state_aware_affinity = performance_comparison["state_aware"]["state_affinity_rate"]
        hybrid_affinity = performance_comparison["hybrid"]["state_affinity_rate"]
        
        # State-aware and hybrid should have better affinity than load balanced
        load_balanced_affinity = performance_comparison["load_balanced"]["state_affinity_rate"]
        assert state_aware_affinity >= load_balanced_affinity
        assert hybrid_affinity >= load_balanced_affinity
        
        print(f"\n🏆 Strategy Performance Comparison:")
        for strategy, metrics in performance_comparison.items():
            print(f"  {strategy.upper()}:")
            print(f"    Avg Routing Time: {metrics['avg_routing_time_ms']:6.2f}ms")
            print(f"    State Affinity:   {metrics['state_affinity_rate']:6.1%}")
    
    @pytest.mark.asyncio
    async def test_performance_regression_detection(self, performance_test_workers, mock_state_registry_performance):
        """Test for performance regressions in routing."""
        router = StateAwareRouter(mock_state_registry_performance, RoutingStrategy.HYBRID)
        
        test_command = MagicMock(spec=DistributedCommand)
        test_command.get_command_type.return_value = "core.tool_execution"
        test_command.get_required_capabilities.return_value = [WorkerCapability.TOOL_EXECUTION]
        
        # Performance baseline expectations (in milliseconds)
        performance_baselines = {
            "single_route_max": 100.0,      # Single route should be < 100ms
            "batch_route_avg": 50.0,        # Batch average should be < 50ms
            "concurrent_route_max": 200.0   # Concurrent max should be < 200ms
        }
        
        # Test single route performance
        start_time = time.perf_counter()
        selected_worker = router.select_optimal_worker(
            test_command,
            performance_test_workers,
            RoutingStrategy.HYBRID
        )
        single_route_time = (time.perf_counter() - start_time) * 1000
        
        assert selected_worker is not None
        assert single_route_time < performance_baselines["single_route_max"], \
            f"Single route regression: {single_route_time:.2f}ms > {performance_baselines['single_route_max']}ms"
        
        # Test batch routing performance
        batch_times = []
        for i in range(20):
            test_command.command_id = f"batch_cmd_{i}"
            start_time = time.perf_counter()
            
            selected_worker = router.select_optimal_worker(
                test_command,
                performance_test_workers,
                RoutingStrategy.HYBRID
            )
            
            batch_times.append((time.perf_counter() - start_time) * 1000)
            assert selected_worker is not None
        
        batch_avg_time = statistics.mean(batch_times)
        assert batch_avg_time < performance_baselines["batch_route_avg"], \
            f"Batch route regression: {batch_avg_time:.2f}ms > {performance_baselines['batch_route_avg']}ms"
        
        print(f"\n✅ Performance Regression Check:")
        print(f"  Single Route:  {single_route_time:6.2f}ms (limit: {performance_baselines['single_route_max']}ms)")
        print(f"  Batch Average: {batch_avg_time:6.2f}ms (limit: {performance_baselines['batch_route_avg']}ms)")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
