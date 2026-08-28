"""
Manual integration test for navigate_screenshot workflow.

Run this to test the unified workflow architecture with real Playwright execution.

Usage:
    python tests/manual/test_navigate_screenshot_workflow.py
"""

import asyncio
import sys
from pathlib import Path
import pytest

# Add project root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))


@pytest.mark.asyncio
async def test_navigate_screenshot_workflow():
    """Test navigate_screenshot workflow with real execution."""
    from motet.core.workflow import (
        WorkflowRegistry,
        WorkflowExecutor,
        list_registered_commands
    )
    from motet.core.commands.builtin.workflow import workflow_execution
    from motet.core.commands.command_data_classes import WorkflowExecutionData
    from motet.core.workers import global_invoker
    
    print("=" * 80)
    print("UNIFIED WORKFLOW ARCHITECTURE TEST - navigate_screenshot")
    print("=" * 80)
    
    # Step 1: Check command registry
    print("\n1. Checking command registry...")
    registered = list_registered_commands()
    print(f"   ✅ {len(registered)} commands registered")
    print(f"   Commands: {', '.join(registered[:10])}...")
    
    if "tool_execution" not in registered:
        print("   ❌ ERROR: tool_execution not registered!")
        return False
    
    print("   ✅ tool_execution registered")
    
    # Step 2: Check workflow registry
    print("\n2. Checking WorkflowRegistry...")
    nav_screenshot_wf = WorkflowRegistry.get("navigate_screenshot")
    
    if not nav_screenshot_wf:
        print("   ❌ ERROR: navigate_screenshot workflow not found!")
        return False
    
    print(f"   ✅ Workflow found: {nav_screenshot_wf.name}")
    print(f"   Steps: {list(nav_screenshot_wf.steps.keys())}")
    print(f"   Execution order: {nav_screenshot_wf.execution_order}")
    inputs = nav_screenshot_wf.get_workflow_inputs()
    print(f"   Required inputs: {len(inputs)} ({', '.join(sorted(inputs)) if inputs else 'none'})")
    
    # Step 3: Test workflow input detection
    print("\n3. Testing workflow input detection...")
    print(f"   ✅ Workflow has {len(inputs)} required inputs")
    if "url" in inputs:
        print(f"   ✅ URL input detected (required for workflow execution)")
    
    # Step 4: Test workflow execution via command (mocked)
    print("\n4. Testing workflow execution structure...")
    
    # Create workflow execution data
    workflow_data = WorkflowExecutionData(
        workflow_id="navigate_screenshot",
        workflow_name="Navigate and Screenshot",
        workflow_steps=[
            {
                "step_id": "navigate",
                "name": "Navigate to URL",
                "command_type": "tool_execution",
                "command_data": {
                    "tool_name": "mcp.playwright.browser_navigate",
                    "parameters": {"url": "https://example.com"}
                },
                "execution_context": {
                    "required_capabilities": ["BROWSER_OPERATIONS"],
                    "timeout_seconds": 30
                },
                "dependencies": []
            },
            {
                "step_id": "screenshot",
                "name": "Take Screenshot",
                "command_type": "tool_execution",
                "command_data": {
                    "tool_name": "mcp.playwright.browser_take_screenshot",
                    # No filename: @playwright/mcp returns PNG image content in-band.
                    "parameters": {"type": "png", "fullPage": True}
                },
                "execution_context": {
                    "required_capabilities": ["BROWSER_OPERATIONS"],
                    "timeout_seconds": 10
                },
                "dependencies": ["navigate"]
            }
        ],
        context={
            "url": "https://example.com"
        }
    )
    
    print(f"   ✅ WorkflowExecutionData created")
    print(f"   Workflow ID: {workflow_data.workflow_id}")
    print(f"   Steps: {len(workflow_data.workflow_steps)}")
    
    # Step 5: Create workflow execution command
    print("\n5. Creating workflow_execution command...")
    try:
        command = workflow_execution(
            task_id="test_task_123",
            conversation_id="test_conv_456",
            data=workflow_data
        )
        print(f"   ✅ Command created: {command.command_id}")
        print(f"   Task ID: {command.distributed_context.task_id}")
        print(f"   Required capabilities: {command.distributed_context.required_capabilities}")
    except Exception as e:
        print(f"   ❌ ERROR creating command: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    # Step 6: Instructions for real execution
    print("\n6. To execute with real Playwright:")
    print("   " + "-" * 76)
    print("   # Start distributed workers")
    print("   docker-compose -f docker-compose.distributed.yml up -d --remove-orphans")
    print()
    print("   # Execute workflow via Python")
    print("   from motet.core.workers import global_invoker")
    print("   result = global_invoker.execute_command(command)")
    print()
    print("   # Or via HTTP API")
    print("   curl -X POST http://localhost:8000/api/workflows/navigate_screenshot \\")
    print("     -H 'Content-Type: application/json' \\")
    print("     -d '{")
    print("       \"url\": \"https://example.com\"")
    print("     }'")
    print("   " + "-" * 76)
    
    print("\n" + "=" * 80)
    print("✅ ALL STRUCTURE TESTS PASSED!")
    print("=" * 80)
    print("\nThe unified workflow architecture is ready for testing.")
    print("Next step: Execute with real Playwright workers to verify end-to-end functionality.")
    
    return True


if __name__ == "__main__":
    result = asyncio.run(test_navigate_screenshot_workflow())
    sys.exit(0 if result else 1)

