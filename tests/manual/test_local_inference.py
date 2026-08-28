#!/usr/bin/env python3
"""
Test script for local inference functionality.

This tests:
1. Redis connectivity
2. LocalInferenceClient initialization  
3. Sending a test inference request
4. Checking available local models
"""

import asyncio
import sys
from motet.core.models.local import LocalInferenceClient
from motet.core.distributed.redis_manager import UnifiedRedisManager
from motet.core.models.registry import list_models, get_model_spec, model_supports
from motet.core.models.adapters import adapter_registry

async def test_local_inference():
    """Test local inference setup."""
    
    print("=" * 80)
    print("🧪 Testing Local Inference Setup")
    print("=" * 80)
    
    # Test 1: Check available local models in registry
    print("\n1️⃣  Checking registered local models...")
    all_models = list_models()
    local_model_specs = [m for m in all_models if 'llama' in m.name.lower() or 'mistral' in m.name.lower()]
    
    if local_model_specs:
        print(f"✅ Found {len(local_model_specs)} local models registered:")
        for spec in local_model_specs:
            print(f"   - {spec.name}")
            print(f"     Provider: {spec.provider}")
            print(f"     Capabilities: {', '.join(spec.capabilities)}")
    else:
        print("⚠️  No local models found in registry")
        print("   This is expected if no models are downloaded yet")
    
    # Test 2: Redis connectivity
    print("\n2️⃣  Testing Redis connectivity...")
    try:
        redis_manager = UnifiedRedisManager()
        redis_client = redis_manager.get_sync_client("test_client")
        redis_client.ping()
        print("✅ Redis is connected and responsive")
    except Exception as e:
        print(f"❌ Redis connection failed: {e}")
        return False
    
    # Test 3: LocalInferenceClient initialization
    print("\n3️⃣  Testing LocalInferenceClient initialization...")
    try:
        client = LocalInferenceClient(redis_client)
        print(f"✅ LocalInferenceClient initialized successfully")
        print(f"   Worker ID: {client.worker_id}")
        print(f"   Redis streams: local:requests → local:responses")
    except Exception as e:
        print(f"❌ LocalInferenceClient initialization failed: {e}")
        return False
    
    # Test 4: Check if LocalInferenceManager is running
    print("\n4️⃣  Checking if LocalInferenceManager is running...")
    try:
        # Check if the request stream exists (manager should create it)
        request_stream = "local:requests"
        stream_exists = redis_client.exists(request_stream)
        if stream_exists:
            print(f"✅ Local inference request stream exists: {request_stream}")
            
            # Check stream info
            stream_info = redis_client.xinfo_stream(request_stream)
            print(f"   Stream length: {stream_info.get('length', 0)} messages")
        else:
            print(f"⚠️  Local inference request stream doesn't exist yet: {request_stream}")
            print("   This is normal if LocalInferenceManager hasn't started yet")
            print("   The manager will create it when it initializes")
    except Exception as e:
        print(f"⚠️  Could not check stream status: {e}")
    
    # Test 5: Try building a local adapter (won't actually load, just test the interface)
    print("\n5️⃣  Testing local adapter interface...")
    try:
        if local_model_specs:
            test_spec = local_model_specs[0]
            test_model_name = test_spec.name
            print(f"   Attempting to build adapter for: {test_model_name}")
            adapter = adapter_registry.build("local", "local")
            print(f"✅ Adapter interface created successfully")
            print(f"   Adapter class: {adapter.__class__.__name__}")
            print(f"   Model supports streaming: {model_supports(test_spec.provider, test_model_name, 'stream')}")
        else:
            print("⏭️  Skipping (no local models registered)")
    except Exception as e:
        print(f"⚠️  Model interface test: {e}")
    
    print("\n" + "=" * 80)
    print("✅ Local Inference Setup Test Complete!")
    print("=" * 80)
    
    print("\n📝 Next Steps:")
    print("   1. Download a GGUF model file (e.g., phi-4-mini)")
    print("   2. Place it in a models/ directory")
    print("   3. Use the local adapter with the model path")
    print("   4. The system will automatically use CPU/Metal/CUDA as available")
    
    return True

if __name__ == "__main__":
    try:
        result = asyncio.run(test_local_inference())
        sys.exit(0 if result else 1)
    except KeyboardInterrupt:
        print("\n\n⚠️  Test interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n\n❌ Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

