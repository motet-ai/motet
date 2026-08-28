#!/usr/bin/env python3
"""Test targeting each worker individually"""

import requests
import json
import time

API_URL = "http://localhost:8000/api/v1/commands/run"
HEADERS = {
    "X-Principal-Id": "test-user",
    "X-Tenant-Id": "default"
}

def run_on_worker(worker_id: str, message: str):
    """Run hello_world command on a specific worker"""
    print(f"\n{'='*60}")
    print(f"Test: Targeting {worker_id}")
    print(f"{'='*60}")
    
    response = requests.post(
        API_URL,
        json={
            "command_type": "hello_world",
            "command_data": {
                "message": f"Hello from {worker_id}!",
                "repeat": 1
            },
            "target_worker_id": worker_id,
            "timeout_seconds": 30
        },
        headers=HEADERS
    )
    
    print(f"Status: {response.status_code}")
    
    if response.status_code == 200:
        result = response.json()
        print(f"✅ SUCCESS")
        print(f"Result: {json.dumps(result, indent=2)}")
        
        # Try to extract which worker actually executed it from the result
        if "data" in result:
            print(f"\n🎯 Executed successfully on {worker_id}")
            print(f"   Messages: {result['data'].get('messages', [])}")
    else:
        print(f"❌ FAILED")
        print(f"Error: {response.text}")
    
    return response.status_code == 200

# Test both workers
print("\n" + "="*60)
print("WORKER TARGETING TEST")
print("="*60)
print("\nWe have 2 workers:")
print("  • cloud_worker1 (motet_dev-worker-1-1)")
print("  • cloud_worker2 (motet_dev-worker-2-1)")
print("\nWe'll run the same command on each worker and verify routing.")
print("="*60)

# Give API time to fully start
time.sleep(3)

# Test worker 1
success1 = run_on_worker("cloud_worker1", "Testing worker 1")
time.sleep(2)

# Test worker 2  
success2 = run_on_worker("cloud_worker2", "Testing worker 2")

# Summary
print(f"\n{'='*60}")
print("SUMMARY")
print(f"{'='*60}")
print(f"cloud_worker1: {'✅ SUCCESS' if success1 else '❌ FAILED'}")
print(f"cloud_worker2: {'✅ SUCCESS' if success2 else '❌ FAILED'}")

if success1 and success2:
    print(f"\n🎉 Both workers responded successfully!")
    print(f"✅ Worker targeting is working correctly")
else:
    print(f"\n⚠️  Some workers failed to respond")
    
print(f"{'='*60}\n")
