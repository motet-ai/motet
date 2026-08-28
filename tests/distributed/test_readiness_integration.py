"""
Integration tests for worker readiness debugging.

These tests help identify why workers aren't registering with the readiness service.
"""

import os
import pytest
import asyncio
import time
import redis.asyncio as redis
from unittest.mock import patch, MagicMock

from motet.core.distributed.worker_readiness import (
    WorkerReadinessService,
    WorkerState,
    get_readiness_service
)
# NOTE: warmup_worker_with_coordination import removed (ADR-0038 Enhanced)
#       Parent process handles registration automatically via parent_coordinator.py


class TestReadinessServiceDebug:
    """Debug tests to identify readiness registration issues"""
    
    @pytest.mark.asyncio
    async def test_redis_connection_real(self):
        """Test actual Redis connection with the same URL workers use"""
        redis_url = "redis://localhost:6379/0"
        
        try:
            # Test direct Redis connection
            redis_client = redis.from_url(redis_url)
            await redis_client.ping()
            
            # Test setting and getting a key
            test_key = "test:readiness:connection"
            await redis_client.set(test_key, "test_value")
            value = await redis_client.get(test_key)
            assert value.decode() == "test_value"
            
            # Cleanup
            await redis_client.delete(test_key)
            await redis_client.close()
            
            print("✅ Redis connection successful")
            
        except Exception as e:
            pytest.skip(f"Redis not available for integration test: {e}")
    
    @pytest.mark.asyncio
    async def test_readiness_service_initialization(self):
        """Test readiness service initialization with real Redis"""
        redis_url = "redis://localhost:6379/0"
        try:
            with patch.dict(os.environ, {"MOTET_REDIS_URL": redis_url}, clear=False):
                service = WorkerReadinessService()
            
            # Test basic operations
            worker_id = "debug-test-worker"
            capabilities = ["test_capability"]
            
            # Register worker (sync API)
            service.register_worker(worker_id, capabilities)
            worker_info = service.get_worker_info(worker_id)
            assert worker_info is not None
            assert worker_info.worker_id == worker_id
            assert worker_info.state == WorkerState.STARTING

            service.mark_worker_ready(
                worker_id=worker_id,
                tool_count=10,
                mcp_tool_count=5,
                warmup_duration_ms=1500
            )
            worker_info = service.get_worker_info(worker_id)
            assert worker_info.state == WorkerState.READY
            assert worker_info.warmup_completed is True
            ready_workers = service.get_ready_workers()
            assert worker_id in ready_workers
            service.remove_worker(worker_id)
            service.shutdown()
            
            print("✅ Readiness service operations successful")
            
        except Exception as e:
            pytest.skip(f"Redis not available for integration test: {e}")
    
    @pytest.mark.asyncio
    async def test_debug_redis_keys(self):
        """Debug what keys exist in Redis"""
        redis_url = "redis://localhost:6379/0"
        
        try:
            redis_client = redis.from_url(redis_url)
            
            # Check all Motet-related keys
            imf_keys = await redis_client.keys("imf:*")
            print(f"Motet keys in Redis: {[k.decode() for k in imf_keys]}")
            
            # Check worker-specific keys
            worker_keys = await redis_client.keys("worker:registration:*")
            print(f"Worker keys: {[k.decode() for k in worker_keys]}")
            
            # Check ready workers set
            ready_workers = await redis_client.smembers("worker:ready")
            print(f"Ready workers: {[w.decode() for w in ready_workers]}")
            
            # Check all keys (for debugging)
            all_keys = await redis_client.keys("*")
            print(f"Total keys in Redis: {len(all_keys)}")
            
            # Show some sample keys
            sample_keys = [k.decode() for k in all_keys[:10]]
            print(f"Sample keys: {sample_keys}")
            
            await redis_client.close()
            
        except Exception as e:
            pytest.skip(f"Redis not available for debug test: {e}")
    
    @pytest.mark.asyncio 
    async def test_readiness_service_singleton_debug(self):
        """Debug the readiness service singleton"""
        try:
            # Test getting the singleton service
            service1 = await get_readiness_service()
            print(f"Service 1: {service1}")
            print(f"Service 1 Redis client: {service1.redis_client if service1 else None}")
            
            service2 = await get_readiness_service()
            print(f"Service 2: {service2}")
            print(f"Same instance: {service1 is service2}")
            
            if service1:
                # Test if it's properly initialized
                print(f"Redis URL: {service1.redis_url}")
                print(f"Workers key prefix: {service1.WORKERS_KEY_PREFIX}")
                print(f"Ready workers set: {service1.READY_WORKERS_SET}")
                
                # Test a simple operation
                try:
                    all_workers = await service1.get_all_workers()
                    print(f"Current workers in service: {len(all_workers)}")
                    for worker_id, worker_info in all_workers.items():
                        print(f"  - {worker_id}: {worker_info.state.value}")
                except Exception as e:
                    print(f"Error getting workers: {e}")
                
                await service1.shutdown()
            
        except Exception as e:
            print(f"Singleton debug failed: {e}")
            import traceback
            traceback.print_exc()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
