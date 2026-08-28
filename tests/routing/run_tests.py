#!/usr/bin/env python3
"""
Test Runner for New Routing System

Runs all routing tests and provides a summary of results.
"""

import sys
import subprocess
import time
from pathlib import Path


def run_test_file(test_file: str) -> tuple[bool, str, float]:
    """Run a single test file and return results"""
    print(f"\n🧪 Running {test_file}...")
    start_time = time.time()
    
    try:
        result = subprocess.run([
            sys.executable, "-m", "pytest", 
            test_file, 
            "-v", 
            "--tb=short",
            "--no-header"
        ], capture_output=True, text=True, cwd=Path(__file__).parent.parent.parent)
        
        end_time = time.time()
        duration = end_time - start_time
        
        success = result.returncode == 0
        output = result.stdout + result.stderr
        
        return success, output, duration
        
    except Exception as e:
        end_time = time.time()
        duration = end_time - start_time
        return False, f"Error running test: {e}", duration


def main():
    """Run all routing tests"""
    print("🚀 Running New Routing System Tests")
    print("=" * 50)
    
    test_files = [
        "tests/routing/test_worker_router.py",
        "tests/routing/test_command_executor.py", 
        "tests/routing/test_strategies.py",
        "tests/routing/test_integration.py"
    ]
    
    results = []
    total_start_time = time.time()
    
    for test_file in test_files:
        success, output, duration = run_test_file(test_file)
        results.append((test_file, success, output, duration))
        
        if success:
            print(f"✅ {test_file} - PASSED ({duration:.2f}s)")
        else:
            print(f"❌ {test_file} - FAILED ({duration:.2f}s)")
            print(f"   Error output: {output[:200]}...")
    
    total_end_time = time.time()
    total_duration = total_end_time - total_start_time
    
    # Summary
    print("\n" + "=" * 50)
    print("📊 Test Summary")
    print("=" * 50)
    
    passed = sum(1 for _, success, _, _ in results if success)
    failed = len(results) - passed
    
    print(f"Total Tests: {len(results)}")
    print(f"Passed: {passed} ✅")
    print(f"Failed: {failed} ❌")
    print(f"Total Time: {total_duration:.2f}s")
    
    if failed > 0:
        print("\n❌ Failed Tests:")
        for test_file, success, output, duration in results:
            if not success:
                print(f"  - {test_file}")
                # Show first few lines of error
                lines = output.split('\n')[:10]
                for line in lines:
                    if line.strip():
                        print(f"    {line}")
                print("    ...")
    
    print("\n🎯 Test Categories Covered:")
    print("  - WorkerRouter: Core routing engine")
    print("  - CommandExecutor: Command lifecycle management")
    print("  - Strategies: All 15+ routing strategies")
    print("  - Integration: End-to-end system tests")
    
    if failed == 0:
        print("\n🎉 All tests passed! New routing system is ready.")
        return 0
    else:
        print(f"\n⚠️  {failed} test(s) failed. Please review and fix.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
