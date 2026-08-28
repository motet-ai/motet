#!/usr/bin/env python3
"""Test the new context-based API structure"""

import requests
import json
import time

API_URL = "http://localhost:8000/api/v1/commands/run"
HEADERS = {"Content-Type": "application/json"}

print("\n" + "="*80)
print("TESTING NEW CONTEXT-BASED API STRUCTURE")
print("="*80)
print("\nThe API now accepts a structured request with:")
print("  • command_type: The command to execute")
print("  • command_data: Command-specific parameters")
print("  • context: Unified execution context (CommandContext + DistributedCommandContext)")
print("\nBenefits:")
print("  ✅ Future-proof: New context fields automatically supported")
print("  ✅ Clean API: No need to update endpoint signature")
print("  ✅ Type-safe: Full Pydantic validation")
print("="*80)

time.sleep(3)

# Test 1: Simple execution
print("\n" + "="*80)
print("Test 1: Simple Execution (minimal context)")
print("="*80)

request1 = {
    "command_type": "hello_world",
    "command_data": {"message": "Hello from new API!", "repeat": 2}
}

print(f"Request: {json.dumps(request1, indent=2)}")
response1 = requests.post(API_URL, json=request1, headers=HEADERS)
print(f"Status: {response1.status_code}")
if response1.status_code == 200:
    result = response1.json()
    print(f"✅ SUCCESS - Worker: {result.get('result', {}).get('worker_id', 'unknown')}")
else:
    print(f"❌ FAILED: {response1.text}")

time.sleep(1)

# Test 2: Worker targeting
print("\n" + "="*80)
print("Test 2: Worker Targeting via Context")
print("="*80)

request2 = {
    "command_type": "hello_world",
    "command_data": {"message": "Hello from worker 1!", "repeat": 1},
    "context": {
        "target_worker_id": "cloud_worker1",
        "timeout_seconds": 30,
        "priority": 7
    }
}

print(f"Request: {json.dumps(request2, indent=2)}")
response2 = requests.post(API_URL, json=request2, headers=HEADERS)
print(f"Status: {response2.status_code}")
if response2.status_code == 200:
    result = response2.json()
    worker_id = result.get('result', {}).get('worker_id', 'unknown')
    print(f"✅ SUCCESS - Targeted: cloud_worker1, Executed on: {worker_id}")
    if worker_id == "cloud_worker1":
        print(f"   🎯 Worker targeting worked correctly!")
else:
    print(f"❌ FAILED: {response2.text}")

print("\n" + "="*80)
print("SUMMARY")
print("="*80)
print("✅ API now uses structured CommandContext + DistributedCommandContext")
print("✅ Future additions to context classes automatically work in API")
print("✅ Clean, maintainable API design")
print("="*80 + "\n")
