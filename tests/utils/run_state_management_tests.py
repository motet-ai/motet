#!/usr/bin/env python3
"""
Test Runner for State Management System

Runs comprehensive tests for the horizontally scalable state management
system including unit tests, integration tests, API tests, and performance benchmarks.

Usage:
    python tests/distributed/run_state_management_tests.py [options]

Options:
    --unit          Run only unit tests
    --integration   Run only integration tests
    --api           Run only API tests
    --performance   Run only performance benchmarks
    --coverage      Run with coverage reporting
    --verbose       Verbose output
    --fast          Skip slow performance tests
"""

import sys
import subprocess
import argparse
from pathlib import Path
import time


class StateManagementTestRunner:
    """Comprehensive test runner for state management system."""
    
    def __init__(self):
        self.test_dir = Path(__file__).parent
        self.project_root = self.test_dir.parent.parent
        
        self.test_modules = {
            "unit": [
                "test_state_registry.py",
                "test_state_aware_routing.py"
            ],
            "integration": [
                "test_state_management_integration.py"
            ],
            "api": [
                # Note: execution_api.py was removed per ADR-0012 (replaced with direct Redis monitoring)
                # Execution tracking is now available via /api/v1/debug and /api/v1/workers endpoints
            ],
            "performance": [
                "test_performance_benchmarks.py"
            ]
        }
    
    def run_tests(self, test_types=None, coverage=False, verbose=False, fast=False):
        """Run specified test types."""
        if test_types is None:
            test_types = ["unit", "integration", "api"]
            if not fast:
                test_types.append("performance")
        
        print("🚀 Running State Management System Tests")
        print("=" * 50)
        
        total_start_time = time.time()
        results = {}
        
        for test_type in test_types:
            if test_type not in self.test_modules:
                print(f"❌ Unknown test type: {test_type}")
                continue
            
            print(f"\n📋 Running {test_type.upper()} tests...")
            
            start_time = time.time()
            success = self._run_test_type(test_type, coverage, verbose, fast)
            duration = time.time() - start_time
            
            results[test_type] = {
                "success": success,
                "duration": duration
            }
            
            status = "✅ PASSED" if success else "❌ FAILED"
            print(f"   {status} in {duration:.2f}s")
        
        total_duration = time.time() - total_start_time
        
        # Print summary
        print("\n" + "=" * 50)
        print("📊 Test Results Summary")
        print("=" * 50)
        
        passed = sum(1 for r in results.values() if r["success"])
        total = len(results)
        
        for test_type, result in results.items():
            status = "✅ PASSED" if result["success"] else "❌ FAILED"
            print(f"  {test_type.upper():12} {status} ({result['duration']:.2f}s)")
        
        print(f"\nOverall: {passed}/{total} test suites passed in {total_duration:.2f}s")
        
        if passed == total:
            print("🎉 All tests passed! State management system is ready.")
            return True
        else:
            print("💥 Some tests failed. Please review the output above.")
            return False
    
    def _run_test_type(self, test_type, coverage, verbose, fast):
        """Run tests for a specific type."""
        modules = self.test_modules[test_type]
        
        # Handle empty test lists (e.g., API tests that were removed)
        if not modules:
            print(f"   ℹ️  No {test_type} tests configured (may have been migrated/removed)")
            return True
        
        for module in modules:
            module_path = self.test_dir / module
            
            if not module_path.exists():
                print(f"   ⚠️  Test module not found: {module}")
                continue
            
            print(f"   🧪 Running {module}...")
            
            # Build pytest command
            cmd = ["python", "-m", "pytest", str(module_path)]
            
            if coverage:
                cmd.extend([
                    "--cov=motet.core.distributed.state_registry",
                    "--cov=motet.core.distributed.state_aware_routing",
                    # Note: execution_api / Flower-replacement monitors retired (#230)
                    "--cov-report=term-missing"
                ])
            
            if verbose:
                cmd.append("-v")
            else:
                cmd.append("-q")
            
            if fast and test_type == "performance":
                cmd.extend(["-k", "not test_concurrent_routing_performance"])
            
            # Add markers for performance tests
            if test_type == "performance":
                cmd.extend(["-m", "not slow or not performance"])
            
            # Run the test
            try:
                result = subprocess.run(
                    cmd,
                    cwd=self.project_root,
                    capture_output=not verbose,
                    text=True,
                    timeout=300  # 5 minute timeout per module
                )
                
                if result.returncode != 0:
                    if not verbose:
                        print(f"      ❌ Failed with output:")
                        print(f"         STDOUT: {result.stdout}")
                        print(f"         STDERR: {result.stderr}")
                    return False
                else:
                    print(f"      ✅ {module} passed")
                    
            except subprocess.TimeoutExpired:
                print(f"      ⏰ {module} timed out")
                return False
            except Exception as e:
                print(f"      💥 {module} error: {e}")
                return False
        
        return True
    
    def check_dependencies(self):
        """Check if required test dependencies are available."""
        print("🔍 Checking test dependencies...")
        
        required_packages = [
            "pytest",
            "pytest-asyncio",
            "pytest-cov",
            "httpx"
        ]
        
        missing_packages = []
        
        for package in required_packages:
            try:
                __import__(package.replace("-", "_"))
                print(f"   ✅ {package}")
            except ImportError:
                print(f"   ❌ {package} (missing)")
                missing_packages.append(package)
        
        if missing_packages:
            print(f"\n💡 Install missing packages:")
            print(f"   pip install {' '.join(missing_packages)}")
            return False
        
        return True
    
    def run_quick_smoke_test(self):
        """Run a quick smoke test to verify basic functionality."""
        print("💨 Running quick smoke test...")
        
        try:
            # Add project root to Python path
            import sys
            sys.path.insert(0, str(self.project_root))
            
            # Test basic imports
            from motet.core.distributed.state_registry import EphemeralStateRegistry
            from motet.core.distributed.state_aware_routing import StateAwareRouter
            # Note: execution_api was removed per ADR-0012 (replaced with direct Redis monitoring)
            # Execution tracking is now available via /api/v1/debug and /api/v1/workers endpoints
            
            print("   ✅ All imports successful")
            
            # Test basic instantiation
            from unittest.mock import AsyncMock
            mock_redis = AsyncMock()
            registry = EphemeralStateRegistry(mock_redis)
            router = StateAwareRouter(registry)
            
            print("   ✅ Basic instantiation successful")
            print("   🎯 Smoke test passed!")
            return True
            
        except Exception as e:
            print(f"   ❌ Smoke test failed: {e}")
            return False


def main():
    """Main test runner entry point."""
    parser = argparse.ArgumentParser(
        description="Run state management system tests",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python run_state_management_tests.py                    # Run all tests
    python run_state_management_tests.py --unit             # Run only unit tests
    python run_state_management_tests.py --performance      # Run only performance tests
    python run_state_management_tests.py --coverage --verbose  # Run with coverage and verbose output
    python run_state_management_tests.py --fast             # Skip slow performance tests
        """
    )
    
    parser.add_argument("--unit", action="store_true", help="Run only unit tests")
    parser.add_argument("--integration", action="store_true", help="Run only integration tests")
    parser.add_argument("--api", action="store_true", help="Run only API tests")
    parser.add_argument("--performance", action="store_true", help="Run only performance benchmarks")
    parser.add_argument("--coverage", action="store_true", help="Run with coverage reporting")
    parser.add_argument("--verbose", action="store_true", help="Verbose output")
    parser.add_argument("--fast", action="store_true", help="Skip slow performance tests")
    parser.add_argument("--smoke", action="store_true", help="Run only smoke test")
    parser.add_argument("--check-deps", action="store_true", help="Check dependencies only")
    
    args = parser.parse_args()
    
    runner = StateManagementTestRunner()
    
    # Check dependencies if requested
    if args.check_deps:
        success = runner.check_dependencies()
        sys.exit(0 if success else 1)
    
    # Run smoke test if requested
    if args.smoke:
        success = runner.run_quick_smoke_test()
        sys.exit(0 if success else 1)
    
    # Check dependencies first
    if not runner.check_dependencies():
        print("\n❌ Missing dependencies. Please install them first.")
        sys.exit(1)
    
    # Determine test types to run
    test_types = []
    if args.unit:
        test_types.append("unit")
    if args.integration:
        test_types.append("integration")
    if args.api:
        test_types.append("api")
    if args.performance:
        test_types.append("performance")
    
    # If no specific types requested, run default set
    if not test_types:
        test_types = None  # Will use default in run_tests
    
    # Run the tests
    success = runner.run_tests(
        test_types=test_types,
        coverage=args.coverage,
        verbose=args.verbose,
        fast=args.fast
    )
    
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
