#!/usr/bin/env python3
"""
Test script for local model inference - direct execution (no scheduling).

This script directly invokes the model inference command without using schedules,
providing immediate results for testing local inference.
"""

import sys
import os

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from motet.core.commands.builtin.model import ModelCommandService
from motet.core.types import Message
from motet.core.workers import global_invoker
import json
import time

def test_local_inference_direct():
    """Test local model inference with direct command execution."""
    
    print("=" * 80)
    print("Local Model Inference - Direct Execution Test")
    print("=" * 80)
    print()
    
    # Initialize the global invoker
    print("🔧 Initializing command invoker...")
    global_invoker.initialize()
    print("✅ Invoker initialized")
    print()
    
    # Create a simple test message
    messages = [
        Message(
            role="user",
            content="Tell me a very short joke about AI in exactly 2 sentences."
        )
    ]
    
    # Model settings for local inference (ADR-0042)
    # Setting provider="local" triggers local inference
    # Model name is resolved via the LocalInferenceManager's model registry
    model_settings = {
        "provider": "local",  # Triggers local inference
        "model_name": "phi-4-mini"  # Resolved via model registry
    }
    
    print("📋 Test Configuration:")
    print(f"   Model: {model_settings['model_name']}")
    print(f"   Provider: {model_settings['provider']}")
    print(f"   Target Worker: cloud_worker2")
    print(f"   Message: {messages[0].content}")
    print()
    
    # Create the model inference command
    print("🏗️  Creating model inference command...")
    # Note: provider="local" in model_settings triggers local inference
    command = ModelCommandService.create_inference(
        task_id="test-local-inference-direct",
        messages=messages,
        model_settings=model_settings,  # provider="local" triggers local inference
        temperature=0.7,
        max_tokens=100,
        # Distributed parameters
        conversation_id="test-conversation",
        tenant_id="test-tenant",
        principal_id="test-user",
        timeout_seconds=120,  # 2 minutes for local inference
        target_worker_id="cloud_worker2"  # Force to worker with local inference
    )
    
    print(f"✅ Command created")
    print(f"   Command type: {command.get_command_type()}")
    print(f"   Target worker: {command.distributed_context.target_worker_id}")
    print()
    
    # Execute the command
    print("🚀 Executing command...")
    print("   (This may take 30-120 seconds for local inference)")
    print()
    
    start_time = time.time()
    
    try:
        result = global_invoker.execute_command(command)
        
        execution_time = time.time() - start_time
        
        print("=" * 80)
        print("✅ INFERENCE COMPLETED")
        print("=" * 80)
        print(f"⏱️  Execution time: {execution_time:.2f} seconds")
        print()
        
        # Display result
        if result:
            print("📊 Result Status:", result.get("status", "unknown"))
            print()
            
            if result.get("status") == "completed":
                inner_result = result.get("result", {})
                
                if inner_result.get("status") == "success":
                    response = inner_result.get("response", {})
                    
                    print("🤖 AI Response:")
                    print("-" * 80)
                    if isinstance(response, dict):
                        content = response.get("content", str(response))
                        print(content)
                    else:
                        print(response)
                    print("-" * 80)
                    print()
                    
                    # Display metadata
                    if "model" in inner_result:
                        print(f"📝 Model Used: {inner_result['model']}")
                    if "usage" in inner_result:
                        usage = inner_result["usage"]
                        print(f"📊 Token Usage:")
                        print(f"   Prompt: {usage.get('prompt_tokens', 'N/A')}")
                        print(f"   Completion: {usage.get('completion_tokens', 'N/A')}")
                        print(f"   Total: {usage.get('total_tokens', 'N/A')}")
                    
                    print()
                    print("=" * 80)
                    print("Full Result:")
                    print("=" * 80)
                    print(json.dumps(result, indent=2, default=str))
                    print("=" * 80)
                    
                else:
                    print("❌ Inference failed!")
                    print(f"   Error: {inner_result.get('error', 'Unknown error')}")
                    print()
                    print("Full result:")
                    print(json.dumps(result, indent=2, default=str))
            
            elif result.get("status") == "failed":
                print("❌ Command execution failed!")
                print(f"   Error: {result.get('error', 'Unknown error')}")
                print()
                print("Full result:")
                print(json.dumps(result, indent=2, default=str))
            
            else:
                print(f"⚠️  Unexpected status: {result.get('status')}")
                print()
                print("Full result:")
                print(json.dumps(result, indent=2, default=str))
        
        else:
            print("❌ No result returned from command execution")
        
        return result
        
    except Exception as e:
        execution_time = time.time() - start_time
        print("=" * 80)
        print("❌ ERROR DURING EXECUTION")
        print("=" * 80)
        print(f"⏱️  Time until error: {execution_time:.2f} seconds")
        print(f"🔥 Exception: {type(e).__name__}")
        print(f"📝 Message: {str(e)}")
        print()
        
        import traceback
        print("Stack trace:")
        print("-" * 80)
        traceback.print_exc()
        print("-" * 80)
        
        return None

def main():
    try:
        result = test_local_inference_direct()
        
        if result and result.get("status") == "completed":
            inner_result = result.get("result", {})
            if inner_result.get("status") == "success":
                print()
                print("🎉 Test completed successfully!")
                sys.exit(0)
            else:
                print()
                print("⚠️  Test completed but inference failed")
                sys.exit(1)
        else:
            print()
            print("⚠️  Test did not complete successfully")
            sys.exit(1)
            
    except KeyboardInterrupt:
        print("\n\n⚠️  Test interrupted by user")
        sys.exit(130)
    except Exception as e:
        print(f"\n\n❌ Fatal error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()

