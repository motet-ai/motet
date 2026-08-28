#!/usr/bin/env python3
"""
Test local model inference via REST API.
"""

import requests
import json
import time

# API endpoint
API_URL = "http://localhost:8000"

def test_local_inference():
    """Test local model inference through the REST API."""
    
    print("=" * 80)
    print("Testing Local Model Inference via REST API")
    print("=" * 80)
    print()
    
    # Create a conversation
    print("1. Creating conversation...")
    create_conv_response = requests.post(
        f"{API_URL}/api/v1/conversations",
        json={
            "tenant_id": "test-tenant",
            "principal_id": "test-user"
        }
    )
    
    if create_conv_response.status_code != 200:
        print(f"❌ Failed to create conversation: {create_conv_response.text}")
        return
    
    conversation_id = create_conv_response.json()["conversation_id"]
    print(f"✅ Conversation created: {conversation_id}")
    print()
    
    # Send a message requesting local inference
    print("2. Sending message with local inference request...")
    print(f"   Model: phi-4-mini (local)")
    print(f"   Provider: local")
    print()
    
    message_data = {
        "content": "Tell me a very short joke about AI in exactly 2 sentences.",
        "conversation_id": conversation_id,
        "tenant_id": "test-tenant",
        "principal_id": "test-user",
        "model_settings": {
            "provider": "local",
            "model_name": "phi-4-mini"
        },
        "temperature": 0.7,
        "max_tokens": 100
    }
    
    print(f"📤 Request payload:")
    print(json.dumps(message_data, indent=2))
    print()
    
    start_time = time.time()
    turn_response = requests.post(
        f"{API_URL}/api/v1/conversations/{conversation_id}/turns",
        json=message_data
    )
    
    if turn_response.status_code != 200:
        print(f"❌ Failed to send message: {turn_response.text}")
        return
    
    turn_data = turn_response.json()
    print(f"✅ Turn created: {turn_data['turn_id']}")
    print()
    
    # Poll for response
    print("3. Waiting for response (local inference may take 30-120 seconds)...")
    turn_id = turn_data["turn_id"]
    max_wait = 180  # 3 minutes
    poll_interval = 2
    
    while time.time() - start_time < max_wait:
        status_response = requests.get(
            f"{API_URL}/api/v1/conversations/{conversation_id}/turns/{turn_id}"
        )
        
        if status_response.status_code != 200:
            print(f"❌ Failed to get turn status: {status_response.text}")
            return
        
        turn_status = status_response.json()
        status = turn_status.get("status", "unknown")
        
        elapsed = time.time() - start_time
        print(f"   Status: {status} (elapsed: {elapsed:.1f}s)", end="\r")
        
        if status == "completed":
            print()  # New line after status updates
            print()
            print("✅ Turn completed!")
            print()
            print("=" * 80)
            print("RESPONSE")
            print("=" * 80)
            print()
            
            assistant_message = turn_status.get("assistant_message", {})
            content = assistant_message.get("content", "No content")
            
            print(content)
            print()
            print("=" * 80)
            print()
            
            # Show metadata
            metadata = turn_status.get("metadata", {})
            if metadata:
                print("Metadata:")
                print(json.dumps(metadata, indent=2))
            
            print()
            print(f"⏱️  Total time: {elapsed:.1f}s")
            return
            
        elif status == "failed":
            print()
            print(f"❌ Turn failed: {turn_status.get('error')}")
            return
        
        time.sleep(poll_interval)
    
    print()
    print(f"⏱️ Timeout waiting for response after {max_wait}s")

if __name__ == "__main__":
    test_local_inference()

