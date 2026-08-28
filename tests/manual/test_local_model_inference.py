#!/usr/bin/env python3
"""
Test script for local model inference with a downloaded GGUF model.

This tests actual inference using a downloaded model:
1. Checks if model file exists
2. Tests LocalAdapter
3. Tests both completion and streaming
"""

import asyncio
import sys
from pathlib import Path
from motet.core.models.adapters import adapter_registry
from motet.core.types import LLMRequest, Message

async def test_local_model():
    """Test local model inference with downloaded GGUF file."""
    
    print("=" * 80)
    print("🧪 Testing Local Model Inference")
    print("=" * 80)
    
    # Check if model file exists
    model_path = Path("/app/models/Llama-3-8B-Instruct-Q4_K_M.gguf")
    print(f"\n1️⃣  Checking for model file...")
    
    if not model_path.exists():
        print(f"❌ Model file not found: {model_path}")
        print("\n📥 To download the model, run:")
        print("   cd /app/models")
        print("   curl -L -o Llama-3-8B-Instruct-Q4_K_M.gguf \\")
        print('     "https://huggingface.co/bartowski/Meta-Llama-3-8B-Instruct-GGUF/resolve/main/Meta-Llama-3-8B-Instruct-Q4_K_M.gguf"')
        return False
    
    file_size_mb = model_path.stat().st_size / (1024 * 1024)
    print(f"✅ Model file found: {model_path}")
    print(f"   Size: {file_size_mb:.2f} MB")
    
    # Test 2: Build the adapter
    print(f"\n2️⃣  Building LocalAdapter...")
    try:
        adapter = adapter_registry.build("local", "local")
        print(f"✅ Adapter built successfully")
        print(f"   Adapter class: {adapter.__class__.__name__}")
    except Exception as e:
        print(f"❌ Failed to build model: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    # Test 3: Test completion (non-streaming)
    print(f"\n3️⃣  Testing completion (non-streaming)...")
    try:
        test_prompt = "What is the capital of France? Answer in one sentence."
        print(f"   Prompt: {test_prompt}")
        print(f"   Generating... (this may take 30-60 seconds on CPU)")
        
        # Create message list
        messages = [Message(role="user", content=test_prompt)]
        
        response = adapter.complete(
            LLMRequest(
                messages=messages,
                model_settings={"provider": "local", "model_name": "phi-4-mini"},
            )
        )
        
        print(f"\n✅ Completion successful!")
        print(f"   Response: {response.content[:200]}...")
        print(f"   Length: {len(response.content)} characters")
    except Exception as e:
        print(f"❌ Completion failed: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    # Test 4: Test streaming
    print(f"\n4️⃣  Testing streaming...")
    try:
        test_prompt = "Count from 1 to 5."
        print(f"   Prompt: {test_prompt}")
        print(f"   Streaming tokens: ", end="", flush=True)
        
        token_count = 0
        stream_messages = [Message(role="user", content=test_prompt)]
        for event in adapter.stream(
            LLMRequest(
                messages=stream_messages,
                model_settings={"provider": "local", "model_name": "phi-4-mini"},
            )
        ):
            if hasattr(event, "text"):
                print(event.text, end="", flush=True)
                token_count += 1
        
        print(f"\n\n✅ Streaming successful!")
        print(f"   Tokens streamed: {token_count}")
    except Exception as e:
        print(f"\n❌ Streaming failed: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    print("\n" + "=" * 80)
    print("✅ Local Model Inference Test Complete!")
    print("=" * 80)
    
    print("\n📊 Performance Notes:")
    print("   - CPU inference is slower but works everywhere")
    print("   - For faster inference, use GPU or Apple Silicon with Metal")
    print("   - First inference is slower due to model loading")
    print("   - Subsequent inferences are much faster (cached)")
    
    return True

if __name__ == "__main__":
    try:
        result = asyncio.run(test_local_model())
        sys.exit(0 if result else 1)
    except KeyboardInterrupt:
        print("\n\n⚠️  Test interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n\n❌ Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

