#!/usr/bin/env python3
"""
Test script for local model inference via scheduled command.

This script demonstrates how to create a scheduled command that uses local model inference.
"""

import requests
import json
import time
from datetime import datetime, timedelta

# API endpoint
API_URL = "http://localhost:8000"

def create_local_inference_schedule():
    """Create a scheduled command for local model inference."""
    
    # Schedule to run in 10 seconds
    scheduled_time = datetime.now() + timedelta(seconds=10)
    
    schedule_data = {
        "name": "Test Local Model Inference",
        "command_type": "model_inference",
        "command_data": {
            "messages": [
                {
                    "role": "user",
                    "content": "Tell me a short joke about AI in 2 sentences."
                }
            ],
            "model_settings": {
                "model_name": "phi-4-mini",  # Using the local model
                "provider": "local",
                "temperature": 0.7,
                "max_tokens": 100
            }
        },
        "schedule_type": "once",
        "scheduled_at": scheduled_time.isoformat(),
        "timeout_seconds": 120,  # 2 minutes timeout for local inference
        "priority": 5,
        "max_retries": 1,
        "target_worker_id": "cloud_worker2",  # Target the worker with local inference enabled
        "tenant_id": "test-tenant",
        "created_by": "test-user"
    }
    
    print("📅 Creating scheduled local model inference...")
    print(f"   Model: phi-4-mini (local)")
    print(f"   Scheduled for: {scheduled_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"   Target worker: cloud_worker2")
    print()
    
    response = requests.post(
        f"{API_URL}/schedules/",
        json=schedule_data,
        headers={"Content-Type": "application/json"}
    )
    
    if response.status_code == 200:
        result = response.json()
        schedule_id = result.get("schedule_id")
        print(f"✅ Schedule created successfully!")
        print(f"   Schedule ID: {schedule_id}")
        print(f"   Status: {result.get('status')}")
        print()
        return schedule_id
    else:
        print(f"❌ Failed to create schedule: {response.status_code}")
        print(f"   Error: {response.text}")
        return None

def check_schedule_status(schedule_id: str):
    """Check the status of a scheduled command."""
    print(f"🔍 Checking schedule status: {schedule_id}")
    
    response = requests.get(f"{API_URL}/schedules/{schedule_id}")
    
    if response.status_code == 200:
        result = response.json()
        print(f"   Status: {result.get('status')}")
        print(f"   Executions: {result.get('execution_count', 0)}")
        
        if result.get('last_execution'):
            last_exec = result['last_execution']
            print(f"   Last execution: {last_exec.get('status')}")
            if last_exec.get('result'):
                print(f"   Result preview: {str(last_exec['result'])[:200]}...")
        print()
        return result
    else:
        print(f"❌ Failed to get schedule: {response.status_code}")
        return None

def main():
    print("=" * 80)
    print("Local Model Inference - Scheduled Command Test")
    print("=" * 80)
    print()
    
    # Step 1: Create the schedule
    schedule_id = create_local_inference_schedule()
    
    if not schedule_id:
        print("Failed to create schedule. Exiting.")
        return
    
    # Step 2: Wait for execution
    print("⏳ Waiting for scheduled execution...")
    print("   (Checking status every 5 seconds for 2 minutes)")
    print()
    
    max_checks = 24  # 2 minutes / 5 seconds
    for i in range(max_checks):
        time.sleep(5)
        
        result = check_schedule_status(schedule_id)
        
        if result and result.get('last_execution'):
            last_exec = result['last_execution']
            if last_exec.get('status') == 'completed':
                print("🎉 Inference completed successfully!")
                print()
                print("=" * 80)
                print("RESULT:")
                print("=" * 80)
                print(json.dumps(last_exec.get('result'), indent=2))
                print("=" * 80)
                return
            elif last_exec.get('status') == 'failed':
                print("❌ Inference failed!")
                print(f"   Error: {last_exec.get('error')}")
                return
    
    print("⏱️  Timeout waiting for execution. Check the ops dashboard for details.")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⚠️  Test interrupted by user")
    except Exception as e:
        print(f"\n\n❌ Error: {e}")
        import traceback
        traceback.print_exc()

