#!/usr/bin/env python3
"""Test worker targeting via API"""

import requests
import json

API_URL = "http://localhost:8000/api/v1/commands/run"

# Example 1: Target specific worker
print("=" * 60)
print("Test 1: Target specific worker (cloud_worker1)")
print("=" * 60)

response1 = requests.post(
    API_URL,
    json={
        "command_type": "hello_world",
        "command_data": {
            "message": "Testing worker targeting!",
            "repeat": 2
        },
        "target_worker_id": "cloud_worker1",  # Target specific worker
        "timeout_seconds": 30
    },
    headers={
        "X-Principal-Id": "test-user",
        "X-Tenant-Id": "default"
    }
)

print(f"Status: {response1.status_code}")
print(f"Response: {json.dumps(response1.json(), indent=2)}")
print()

# Example 2: Use required capabilities
print("=" * 60)
print("Test 2: Use required capabilities")
print("=" * 60)

response2 = requests.post(
    API_URL,
    json={
        "command_type": "hello_world",
        "command_data": {
            "message": "Testing capability-based routing!",
            "repeat": 2
        },
        "required_capabilities": ["tool_execution"],  # Route to workers with this capability
        "timeout_seconds": 30
    },
    headers={
        "X-Principal-Id": "test-user",
        "X-Tenant-Id": "default"
    }
)

print(f"Status: {response2.status_code}")
print(f"Response: {json.dumps(response2.json(), indent=2)}")
print()

# Example 3: Avoid specific workers
print("=" * 60)
print("Test 3: Avoid specific workers")
print("=" * 60)

response3 = requests.post(
    API_URL,
    json={
        "command_type": "hello_world",
        "command_data": {
            "message": "Testing worker avoidance!",
            "repeat": 2
        },
        "avoid_worker_ids": ["cloud_worker2"],  # Avoid this worker
        "timeout_seconds": 30
    },
    headers={
        "X-Principal-Id": "test-user",
        "X-Tenant-Id": "default"
    }
)

print(f"Status: {response3.status_code}")
print(f"Response: {json.dumps(response3.json(), indent=2)}")
print()

print("✅ All tests completed!")
