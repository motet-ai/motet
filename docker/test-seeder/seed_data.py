#!/usr/bin/env python3
"""
Test Data Seeder for Distributed AI Runtime

This script seeds the distributed system with test data for comprehensive testing.
It creates initial memories, conversations, and test scenarios.
"""

import os
import sys
import json
import time
import asyncio
import httpx
import redis.asyncio as redis
from pathlib import Path
from typing import Dict, Any, List

class TestDataSeeder:
    """Seeds test data into the distributed AI stack."""
    
    def __init__(self):
        self.api_url = os.getenv('MOTET_API_URL', 'http://localhost:8000')
        self.redis_url = os.getenv('MOTET_REDIS_URL', 'redis://localhost:6379')
        self.postgres_url = os.getenv('MOTET_POSTGRES_URL')
        
        self.client = None
        self.redis_client = None
        
    async def initialize(self):
        """Initialize connections."""
        self.client = httpx.AsyncClient(base_url=self.api_url, timeout=30.0)
        self.redis_client = redis.from_url(self.redis_url)
        
        # Wait for services to be ready
        await self._wait_for_services()
    
    async def _wait_for_services(self):
        """Wait for all services to be ready."""
        print("Waiting for services to be ready...")
        
        max_retries = 60  # 5 minutes
        retry_count = 0
        
        while retry_count < max_retries:
            try:
                # Check API health
                response = await self.client.get("/health")
                if response.status_code == 200:
                    print("API service ready")
                    break
            except Exception as e:
                print(f"API not ready: {e}")
            
            retry_count += 1
            await asyncio.sleep(5)
        
        if retry_count >= max_retries:
            raise TimeoutError("Services did not become ready in time")
    
    async def seed_all_data(self):
        """Seed all test data."""
        print("Starting test data seeding...")
        
        try:
            # Load test scenarios
            test_data = self._load_test_scenarios()
            
            # Seed different types of data
            await self._seed_basic_conversations(test_data)
            await self._seed_memory_data(test_data)
            await self._seed_tool_data(test_data)
            await self._seed_performance_baselines(test_data)
            
            print("Test data seeding completed successfully")
            
        except Exception as e:
            print(f"Error during seeding: {e}")
            raise
    
    def _load_test_scenarios(self) -> Dict[str, Any]:
        """Load test scenarios from JSON file."""
        scenarios_file = Path("/app/test_data/test_scenarios.json")
        
        if not scenarios_file.exists():
            raise FileNotFoundError(f"Test scenarios file not found: {scenarios_file}")
        
        with open(scenarios_file, 'r') as f:
            return json.load(f)
    
    async def _seed_basic_conversations(self, test_data: Dict[str, Any]):
        """Seed basic conversation data."""
        print("Seeding basic conversations...")
        
        basic_scenarios = test_data.get("basic_scenarios", {})
        
        for scenario_name, scenario in basic_scenarios.items():
            try:
                # Create conversation
                response = await self.client.post("/chat", json={
                    "messages": scenario["messages"],
                    "stream": False
                })
                
                if response.status_code == 200:
                    print(f"  ✓ Created conversation: {scenario_name}")
                    
                    # Tag the conversation for testing
                    await self.client.post("/tool", json={
                        "name": "memory_tag",
                        "params": {
                            "tags": ["test_data", "basic_scenario", scenario_name],
                            "op": "add"
                        }
                    })
                else:
                    print(f"  ✗ Failed to create conversation: {scenario_name}")
                
                # Small delay between requests
                await asyncio.sleep(0.5)
                
            except Exception as e:
                print(f"  ✗ Error creating conversation {scenario_name}: {e}")
    
    async def _seed_memory_data(self, test_data: Dict[str, Any]):
        """Seed memory and tagging data."""
        print("Seeding memory data...")
        
        # Create test memories with various tags
        test_memories = [
            {
                "content": "This is a test memory about artificial intelligence",
                "tags": ["ai", "test_data", "technology"]
            },
            {
                "content": "Test memory about distributed systems architecture",
                "tags": ["distributed", "architecture", "test_data"]
            },
            {
                "content": "Performance testing memory entry",
                "tags": ["performance", "testing", "benchmark", "test_data"]
            },
            {
                "content": "User interface testing conversation",
                "tags": ["ui", "interface", "testing", "test_data"]
            },
            {
                "content": "Tool execution test memory",
                "tags": ["tools", "execution", "test_data"]
            }
        ]
        
        for i, memory in enumerate(test_memories):
            try:
                # Create a conversation to generate the memory
                response = await self.client.post("/chat", json={
                    "messages": [{"role": "user", "content": f"Remember this: {memory['content']}"}],
                    "stream": False
                })
                
                if response.status_code == 200:
                    # Tag the memory
                    await self.client.post("/tool", json={
                        "name": "memory_tag",
                        "params": {
                            "tags": memory["tags"],
                            "op": "add"
                        }
                    })
                    print(f"  ✓ Created memory {i+1}/{len(test_memories)}")
                else:
                    print(f"  ✗ Failed to create memory {i+1}")
                
                await asyncio.sleep(0.3)
                
            except Exception as e:
                print(f"  ✗ Error creating memory {i+1}: {e}")
    
    async def _seed_tool_data(self, test_data: Dict[str, Any]):
        """Seed tool-related test data."""
        print("Seeding tool data...")
        
        # Create test notes
        test_notes = [
            "Test note for distributed system validation",
            "Performance benchmark note",
            "UI testing note with special characters: !@#$%",
            "Tool execution test note",
            "Memory integration test note"
        ]
        
        for i, note_text in enumerate(test_notes):
            try:
                response = await self.client.post("/tool", json={
                    "name": "note",
                    "params": {"text": note_text}
                })
                
                if response.status_code == 200:
                    print(f"  ✓ Created test note {i+1}/{len(test_notes)}")
                else:
                    print(f"  ✗ Failed to create test note {i+1}")
                
                await asyncio.sleep(0.2)
                
            except Exception as e:
                print(f"  ✗ Error creating test note {i+1}: {e}")
    
    async def _seed_performance_baselines(self, test_data: Dict[str, Any]):
        """Seed performance baseline data."""
        print("Seeding performance baselines...")
        
        # Store performance baselines in Redis for quick access during testing
        baselines = test_data.get("performance_baselines", {})
        
        try:
            for category, values in baselines.items():
                key = f"test:baseline:{category}"
                await self.redis_client.hset(key, mapping=values)
                print(f"  ✓ Stored baseline: {category}")
            
            # Set expiration for baseline data (24 hours)
            for category in baselines.keys():
                key = f"test:baseline:{category}"
                await self.redis_client.expire(key, 86400)
            
        except Exception as e:
            print(f"  ✗ Error storing baselines: {e}")
    
    async def verify_seeded_data(self):
        """Verify that seeded data is accessible."""
        print("Verifying seeded data...")
        
        verification_tests = [
            ("Health check", "GET", "/health", None),
            ("Memory list", "GET", "/memories?limit=5", None),
            ("Tool list", "GET", "/tools", None),
            ("Memory search", "POST", "/memories/find", {"tags": ["test_data"], "limit": 3})
        ]
        
        for test_name, method, endpoint, payload in verification_tests:
            try:
                if method == "GET":
                    response = await self.client.get(endpoint)
                else:
                    response = await self.client.post(endpoint, json=payload)
                
                if response.status_code == 200:
                    print(f"  ✓ {test_name}")
                else:
                    print(f"  ✗ {test_name} - Status: {response.status_code}")
                
            except Exception as e:
                print(f"  ✗ {test_name} - Error: {e}")
    
    async def cleanup(self):
        """Cleanup connections."""
        if self.client:
            await self.client.aclose()
        if self.redis_client:
            await self.redis_client.close()

async def main():
    """Main seeder entry point."""
    seeder = TestDataSeeder()
    
    try:
        await seeder.initialize()
        await seeder.seed_all_data()
        await seeder.verify_seeded_data()
        
        print("\n🎉 Test data seeding completed successfully!")
        
    except Exception as e:
        print(f"\n❌ Test data seeding failed: {e}")
        sys.exit(1)
    finally:
        await seeder.cleanup()

if __name__ == '__main__':
    asyncio.run(main())
