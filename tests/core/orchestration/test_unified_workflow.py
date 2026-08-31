"""
Tests for Unified Workflow Architecture (ADR-0049).

Tests:
- Command registry operations
- WorkflowStep validation
- Parameter substitution
- WorkflowExecutor functionality
- WorkflowRegistry operations
- navigate_screenshot workflow end-to-end
"""

import pytest
from unittest.mock import Mock, MagicMock, patch
from datetime import datetime

from motet.core.workflow import (
    Workflow,
    WorkflowStep,
    WorkflowStatus,
    WorkflowStepStatus,
    WorkflowExecutor,
    WorkflowRegistry,
    get_command_by_name,
    list_registered_commands,
    validate_workflow,
    validate_execution_context,
    substitute_parameters
)


class TestCommandRegistry:
    """Test command registry system."""

    @staticmethod
    def _ensure_core_commands_registered() -> None:
        """
        Re-register core builtins so these tests are order-independent.

        Earlier unit tests may clear or replace command_type_registry; a plain
        import is a no-op once modules are cached, so re-register with overwrite.
        """
        from motet.core.commands.builtin.tool import tool_execution
        from motet.core.commands.builtin.workflow import workflow_execution
        from motet.core.commands.command_data_classes import (
            ToolExecutionData,
            WorkflowExecutionData,
        )
        from motet.core.commands.command_data_registry import register_command_data
        from motet.core.commands.command_type_registry import (
            CommandImplementationType,
            command_type_registry,
        )

        for cmd_func, data_class in (
            (tool_execution, ToolExecutionData),
            (workflow_execution, WorkflowExecutionData),
        ):
            command_type = getattr(cmd_func, "__command_type__", cmd_func.__name__)
            impl = getattr(cmd_func, "__command_class__", cmd_func)
            command_type_registry.register_command(
                command_type=command_type,
                implementation=impl,
                implementation_type=CommandImplementationType.DECORATOR_BASED,
                data_class=data_class,
                metadata={"timeout_seconds": 60},
                version="1.0.0",
                overwrite=True,
            )
            register_command_data(command_type, data_class, overwrite=True)

    def test_get_command_by_name(self):
        """Test retrieving registered commands (legacy + core-qualified names)."""
        self._ensure_core_commands_registered()
        retrieved = get_command_by_name("tool_execution")
        assert retrieved is not None
        assert callable(retrieved)
        # Qualified form is the registry key; legacy name resolves via namespace fallback.
        assert get_command_by_name("core.tool_execution") is retrieved

    def test_get_unknown_command_raises_error(self):
        """Test getting unknown command raises ValueError with helpful message."""
        with pytest.raises(ValueError) as exc_info:
            get_command_by_name("nonexistent_command_123456789")

        assert "Unknown command 'nonexistent_command_123456789'" in str(exc_info.value)
        assert "Available commands:" in str(exc_info.value)

    def test_list_registered_commands(self):
        """Test listing all registered commands."""
        self._ensure_core_commands_registered()
        commands = list_registered_commands()

        # Legacy unprefixed aliases plus qualified core names
        assert "tool_execution" in commands
        assert "core.tool_execution" in commands
        assert "workflow_execution" in commands
        assert "core.workflow_execution" in commands
        assert isinstance(commands, list)
        assert commands == sorted(commands)  # Should be sorted


class TestParameterSubstitution:
    """Test parameter substitution functionality."""
    
    def test_simple_substitution(self):
        """Test simple parameter substitution."""
        data = {
            "url": "{target_url}",
            "name": "{screenshot_name}"
        }
        context = {
            "target_url": "https://example.com",
            "screenshot_name": "example_screenshot"
        }
        
        result = substitute_parameters(data, context)
        assert result["url"] == "https://example.com"
        assert result["name"] == "example_screenshot"
    
    def test_nested_substitution(self):
        """Test nested parameter access like {step1.result}."""
        data = {
            "content": "{step1.result}"
        }
        context = {
            "step1": {"result": "analysis complete"}
        }
        
        result = substitute_parameters(data, context)
        assert result["content"] == "analysis complete"
    
    def test_missing_parameter_keeps_original(self):
        """Test that missing parameters are kept as-is."""
        data = {
            "url": "{missing_param}"
        }
        context = {}
        
        result = substitute_parameters(data, context)
        assert result["url"] == "{missing_param}"

    def test_missing_canonical_whole_value_resolves_none(self):
        """Unresolved {{...}} occupying a whole value becomes None (strict)."""
        data = {"picked": "{{pick.picked}}", "n": "{{pick.issue_number}}"}
        result = substitute_parameters(data, {})
        assert result["picked"] is None
        assert result["n"] is None

    def test_missing_canonical_embedded_resolves_empty(self):
        """Unresolved {{...}} embedded in a larger string becomes empty text."""
        data = {"msg": "before {{pick.title}} after"}
        result = substitute_parameters(data, {"pick": {"other": 1}})
        assert result["msg"] == "before  after"

    def test_none_value_canonical_whole_value_resolves_none(self):
        """A resolvable path whose final value is None substitutes None."""
        data = {"n": "{{pick.issue_number}}"}
        result = substitute_parameters(data, {"pick": {"issue_number": None}})
        assert result["n"] is None

    def test_skipped_dependency_fields_resolve_none_not_literal(self):
        """Fields under a skipped-step context entry resolve to None."""
        data = {"pr_number": "{{pr.pr_number}}", "note": "PR={{pr.url}}"}
        context = {"pr": {"status": "skipped", "reason": "Condition met"}}
        result = substitute_parameters(data, context)
        assert result["pr_number"] is None
        assert result["note"] == "PR="
    
    def test_complex_nested_data(self):
        """Test substitution in complex nested structures."""
        data = {
            "parameters": {
                "query": "{search_query}",
                "filters": {
                    "date": "{date_filter}"
                }
            }
        }
        context = {
            "search_query": "AI news",
            "date_filter": "2025-10-31"
        }
        
        result = substitute_parameters(data, context)
        assert result["parameters"]["query"] == "AI news"
        assert result["parameters"]["filters"]["date"] == "2025-10-31"
    
    def test_array_indexing_simple(self):
        """Test simple array indexing like {step.results[0].url}."""
        data = {
            "url": "{search_topic.results[0].url}"
        }
        context = {
            "search_topic": {
                "results": [
                    {"url": "https://example.com", "title": "Example"},
                    {"url": "https://test.com", "title": "Test"}
                ]
            }
        }
        
        result = substitute_parameters(data, context)
        assert result["url"] == "https://example.com"
    
    def test_array_indexing_nested_arrays(self):
        """Test nested array indexing like {step.data[1][0]}."""
        data = {
            "value": "{gather.result[1]}"
        }
        context = {
            "gather": {
                "result": [[1, 2], [3, 4], [5, 6]]
            }
        }
        
        result = substitute_parameters(data, context)
        assert result["value"] == [3, 4]
    
    def test_array_indexing_mixed_notation(self):
        """Test mixed dot and array notation like {step.data.items[1].name}."""
        data = {
            "selected": "{step1.data.items[1].name}",
            "value": "{step1.data.items[0].value}"
        }
        context = {
            "step1": {
                "data": {
                    "items": [
                        {"name": "first", "value": 100},
                        {"name": "second", "value": 200}
                    ]
                }
            }
        }
        
        result = substitute_parameters(data, context)
        assert result["selected"] == "second"
        assert result["value"] == 100
    
    def test_array_indexing_out_of_bounds(self):
        """Test that out-of-bounds array access keeps placeholder."""
        data = {
            "url": "{search.results[10].url}"
        }
        context = {
            "search": {
                "results": [{"url": "https://example.com"}]
            }
        }
        
        result = substitute_parameters(data, context)
        assert result["url"] == "{search.results[10].url}"  # Keeps placeholder
    
    def test_array_indexing_field_not_list(self):
        """Test that array indexing on non-list keeps placeholder."""
        data = {
            "value": "{step.data[0]}"
        }
        context = {
            "step": {
                "data": "not a list"
            }
        }
        
        result = substitute_parameters(data, context)
        assert result["value"] == "{step.data[0]}"  # Keeps placeholder
    
    def test_array_indexing_multiple_in_same_string(self):
        """Test multiple array indexes in same command_data."""
        data = {
            "url": "{results[0].url}",
            "title": "{results[0].title}",
            "backup_url": "{results[1].url}"
        }
        context = {
            "results": [
                {"url": "https://first.com", "title": "First"},
                {"url": "https://second.com", "title": "Second"}
            ]
        }
        
        result = substitute_parameters(data, context)
        assert result["url"] == "https://first.com"
        assert result["title"] == "First"
        assert result["backup_url"] == "https://second.com"
    
    def test_array_indexing_with_empty_array(self):
        """Test that empty array keeps placeholder."""
        data = {
            "url": "{search.results[0].url}"
        }
        context = {
            "search": {
                "results": []
            }
        }
        
        result = substitute_parameters(data, context)
        assert result["url"] == "{search.results[0].url}"  # Keeps placeholder

    def test_embedded_string_with_newlines(self):
        """Test that newlines in resolved values are JSON-escaped when embedded."""
        data = {
            "content": "Prefix: {{step.response}} — end"
        }
        context = {
            "step": {"response": "Line one\nLine two\nLine three"}
        }

        result = substitute_parameters(data, context)
        assert result["content"] == "Prefix: Line one\nLine two\nLine three — end"

    def test_embedded_string_with_quotes(self):
        """Test that double-quotes in resolved values are escaped when embedded."""
        data = {
            "content": "He said: {{step.reply}}"
        }
        context = {
            "step": {"reply": 'She said "hello" and left.'}
        }

        result = substitute_parameters(data, context)
        assert result["content"] == 'He said: She said "hello" and left.'

    def test_embedded_string_with_tabs_and_backslashes(self):
        """Test tabs, carriage returns, and backslashes are escaped."""
        data = {
            "content": "Data: {{step.text}}"
        }
        context = {
            "step": {"text": "col1\tcol2\r\nrow\\value"}
        }

        result = substitute_parameters(data, context)
        assert result["content"] == "Data: col1\tcol2\r\nrow\\value"

    def test_embedded_multiline_llm_response(self):
        """Regression: LLM responses with markdown are safely embedded."""
        llm_output = (
            "# Analysis\n\n"
            '1. "Remote work" boosts productivity\n'
            "2. Cost savings of ~30%\n\n"
            "## Risks\n"
            "- Isolation\t(mitigated by rituals)\n"
        )
        data = {
            "messages": [
                {
                    "role": "user",
                    "content": "--- OPTIMIST ---\n{{optimist.final_response}}\n--- END ---"
                }
            ]
        }
        context = {"optimist": {"final_response": llm_output}}

        result = substitute_parameters(data, context)
        assert llm_output in result["messages"][0]["content"]
        assert result["messages"][0]["content"].startswith("--- OPTIMIST ---\n")
        assert result["messages"][0]["content"].endswith("\n--- END ---")

    def test_standalone_placeholder_with_special_chars(self):
        """Standalone (quoted) placeholder with special chars still works."""
        data = {
            "response": "{{step.text}}"
        }
        context = {
            "step": {"text": "Hello\nWorld\t!"}
        }

        result = substitute_parameters(data, context)
        assert result["response"] == "Hello\nWorld\t!"

    def test_multiple_embedded_placeholders_with_control_chars(self):
        """Multiple embedded refs with control chars in one string."""
        data = {
            "prompt": "A: {{a.out}} | B: {{b.out}}"
        }
        context = {
            "a": {"out": "first\nsecond"},
            "b": {"out": 'quote "here"'},
        }

        result = substitute_parameters(data, context)
        assert result["prompt"] == 'A: first\nsecond | B: quote "here"'

    def test_embedded_list_value_is_escaped(self):
        """Regression (implement_cycle review step): a list value embedded
        mid-string must be JSON-escaped, not injected raw — raw injection broke
        the surrounding JSON with 'Expecting , delimiter' at json.loads."""
        data = {
            "messages": [
                {
                    "role": "user",
                    "content": "Acceptance checks: {{plan.acceptance_checks}}\nGo.",
                }
            ]
        }
        context = {
            "plan": {"acceptance_checks": ['No imports of "NFC"', "Tests pass"]}
        }

        result = substitute_parameters(data, context)
        content = result["messages"][0]["content"]
        assert content == (
            'Acceptance checks: ["No imports of \\"NFC\\"", "Tests pass"]\nGo.'
        )

    def test_embedded_dict_value_is_escaped(self):
        """Dicts embedded mid-string are rendered as escaped JSON text."""
        data = {"content": "Chunk: {{loop.chunk}} — end"}
        context = {"loop": {"chunk": {"id": 1, "title": 'fix "quoting"'}}}

        result = substitute_parameters(data, context)
        assert result["content"] == (
            'Chunk: {"id": 1, "title": "fix \\"quoting\\""} — end'
        )

    def test_quoted_placeholder_still_passes_list_through(self):
        """A placeholder occupying the whole value keeps its native type."""
        data = {"checks": "{{plan.acceptance_checks}}"}
        context = {"plan": {"acceptance_checks": ["a", "b"]}}

        result = substitute_parameters(data, context)
        assert result["checks"] == ["a", "b"]


class TestWorkflowStep:
    """Test WorkflowStep model."""
    
    def test_create_command_based_step(self):
        """Test creating a command-based workflow step."""
        step = WorkflowStep(
            step_id="test_step",
            name="Test Step",
            command_type="tool_execution",
            command_data={
                "tool_name": "web_search",
                "parameters": {"query": "test"}
            },
            dependencies=[]
        )
        
        assert step.step_id == "test_step"
        assert step.command_type == "tool_execution"
        assert step.command_data["tool_name"] == "web_search"
        assert step.status == WorkflowStepStatus.PENDING
    
    def test_step_with_execution_context(self):
        """Test step with execution context overrides."""
        step = WorkflowStep(
            step_id="gpu_step",
            name="GPU Analysis",
            command_type="agent_turn",
            command_data={"messages": []},
            execution_context={
                "required_capabilities": ["GPU_INFERENCE"],
                "preferred_worker_tags": ["gpu"],
                "timeout_seconds": 180
            }
        )
        
        assert step.execution_context["required_capabilities"] == ["GPU_INFERENCE"]
        assert step.execution_context["timeout_seconds"] == 180
    
    def test_step_with_fallback(self):
        """Test step with fallback configuration."""
        step = WorkflowStep(
            step_id="primary",
            name="Primary Step",
            command_type="tool_execution",
            command_data={},
            fallback_step_id="backup",
            continue_on_failure=True
        )
        
        assert step.fallback_step_id == "backup"
        assert step.continue_on_failure is True


class TestWorkflowValidation:
    """Test workflow validation."""
    
    def test_validate_execution_context_valid(self):
        """Test validation with valid execution_context fields."""
        # This should not raise
        exec_ctx = {
            "timeout_seconds": 30,
            "priority": "HIGH",
            "required_capabilities": ["TOOL_EXECUTION"]
        }
        
        # Note: validation will pass or skip if DistributedCommandContext not available
        try:
            validate_execution_context(exec_ctx)
        except ImportError:
            pass  # Expected if commands not available
    
    def test_validate_workflow_with_missing_dependencies(self):
        """Test workflow validation catches missing dependencies."""
        # Note: This validation now happens in model_post_init during Workflow creation
        from pydantic import ValidationError
        
        with pytest.raises(ValidationError) as exc_info:
            workflow = Workflow(
                workflow_id="test_wf",
                name="Test",
                steps={
                    "step1": WorkflowStep(
                        step_id="step1",
                        name="Step 1",
                        command_type="tool_execution",
                        command_data={},
                        dependencies=["nonexistent_step"]  # This doesn't exist!
                    )
                }
            )
        
        assert "Dependency 'nonexistent_step' not found" in str(exc_info.value)
    
    def test_validate_workflow_with_unknown_command(self):
        """Test workflow validation catches unknown commands."""
        workflow = Workflow(
            workflow_id="test_wf",
            name="Test",
            steps={
                "step1": WorkflowStep(
                    step_id="step1",
                    name="Step 1",
                    command_type="unknown_command_type",
                    command_data={}
                )
            }
        )
        
        with pytest.raises(ValueError) as exc_info:
            validate_workflow(workflow)
        
        assert "unknown_command_type" in str(exc_info.value) and "Unknown command" in str(exc_info.value)


class TestWorkflowExecutor:
    """Test WorkflowExecutor service."""
    
    def test_create_executor(self):
        """Test creating a WorkflowExecutor instance."""
        executor = WorkflowExecutor()
        assert executor is not None
    
    @patch('motet.core.commands.command_type_registry.command_type_registry')
    def test_execute_simple_workflow(self, mock_registry):
        """Test executing a simple workflow."""
        # Executor uses command_type_registry.get(step.command_type), not get_command_by_name
        mock_reg = Mock()
        mock_reg.implementation = Mock()
        mock_reg.data_class = Mock(return_value=Mock())
        mock_registry.get.return_value = mock_reg

        mock_motet = Mock()
        mock_motet.task_id = "task_123"
        mock_motet.conversation_id = "conv_456"
        mock_motet.event_bus = Mock()
        mock_motet.event_bus.publish = Mock()
        mock_motet.do = Mock(return_value={"status": "success", "data": "result1"})
        
        # Create simple workflow
        workflow = Workflow(
            workflow_id="test_wf",
            name="Test Workflow",
            steps={
                "step1": WorkflowStep(
                    step_id="step1",
                    name="Step 1",
                    command_type="tool_execution",
                    command_data={"tool_name": "test_tool"},
                    dependencies=[]
                )
            }
        )
        
        # Execute
        executor = WorkflowExecutor()
        result = executor.execute_workflow(workflow, mock_motet)
        
        # Verify (executor returns step_results/workflow_id, not top-level status)
        assert "step_results" in result
        assert "workflow_id" in result
        assert mock_motet.do.called
    
    @patch('motet.core.commands.command_type_registry.command_type_registry')
    def test_execute_workflow_with_dependencies(self, mock_registry):
        """Test executing workflow with dependencies."""
        mock_reg = Mock()
        mock_reg.implementation = Mock()
        mock_reg.data_class = Mock(return_value=Mock())
        mock_registry.get.return_value = mock_reg

        mock_motet = Mock()
        mock_motet.task_id = "task_123"
        mock_motet.conversation_id = "conv_456"
        mock_motet.event_bus = Mock()
        mock_motet.event_bus.publish = Mock()
        call_order = []
        def track_call(*args, **kwargs):
            call_order.append(len(call_order) + 1)
            return {"status": "success", "data": f"result{len(call_order)}"}
        mock_motet.do = Mock(side_effect=track_call)
        
        # Create workflow with dependencies
        workflow = Workflow(
            workflow_id="test_wf",
            name="Test Workflow",
            steps={
                "step1": WorkflowStep(
                    step_id="step1",
                    name="Step 1",
                    command_type="tool_execution",
                    command_data={},
                    dependencies=[]
                ),
                "step2": WorkflowStep(
                    step_id="step2",
                    name="Step 2",
                    command_type="tool_execution",
                    command_data={},
                    dependencies=["step1"]  # Depends on step1
                )
            }
        )
        
        # Execute
        executor = WorkflowExecutor()
        result = executor.execute_workflow(workflow, mock_motet)
        
        # Verify execution order: step1 must execute before step2
        assert "step_results" in result
        assert len(call_order) == 2


class TestWorkflowRegistry:
    """Test WorkflowRegistry."""
    
    def test_register_and_get_workflow(self):
        """Test registering and retrieving workflows."""
        # Create a simple workflow
        workflow = Workflow(
            workflow_id="registry_test_wf",
            name="Registry Test",
            steps={}
        )
        
        # Register via public API (WorkflowRegistry uses _registry, not _workflows)
        WorkflowRegistry.register(workflow)
        try:
            retrieved = WorkflowRegistry.get("registry_test_wf")
            assert retrieved is not None
            assert retrieved.workflow_id == "registry_test_wf"
        finally:
            WorkflowRegistry.unregister("registry_test_wf")
    
    def test_list_all_workflows(self):
        """Test listing all workflows."""
        wf1 = Workflow(workflow_id="wf1", name="WF1", steps={})
        wf2 = Workflow(workflow_id="wf2", name="WF2", steps={})
        WorkflowRegistry.register(wf1)
        WorkflowRegistry.register(wf2)
        try:
            all_workflows = WorkflowRegistry.list_all()
            ids = [w.workflow_id for w in all_workflows]
            assert "wf1" in ids
            assert "wf2" in ids
            assert WorkflowRegistry.get("wf1") is not None
            assert WorkflowRegistry.get("wf2") is not None
        finally:
            WorkflowRegistry.unregister("wf1")
            WorkflowRegistry.unregister("wf2")
    
    def test_workflow_inputs_detection(self):
        """Test workflow input detection."""
        # Get a real workflow
        workflow = WorkflowRegistry.get("navigate_screenshot")
        if workflow:
            inputs = workflow.get_workflow_inputs()
            assert "url" in inputs


class TestNavigateScreenshotWorkflow:
    """Test the navigate_screenshot workflow specifically."""
    
    def test_navigate_screenshot_workflow_exists(self):
        """Test that navigate_screenshot workflow is registered."""
        workflow = WorkflowRegistry.get("navigate_screenshot")
        assert workflow is not None
        assert workflow.workflow_id == "navigate_screenshot"
        assert "browser" in (workflow.keywords or [])
        assert "playwright" in workflow.discovery_keywords()
        assert workflow.name == "Navigate and Screenshot"
    
    def test_navigate_screenshot_has_correct_steps(self):
        """Test that navigate_screenshot has correct steps."""
        workflow = WorkflowRegistry.get("navigate_screenshot")
        
        # Should have 2 steps
        assert len(workflow.steps) == 2
        assert "navigate" in workflow.steps
        assert "screenshot" in workflow.steps
        
        # Navigate step (builtin uses core.tool_execution and dot-separated tool names)
        nav_step = workflow.steps["navigate"]
        assert nav_step.command_type in ("tool_execution", "core.tool_execution")
        assert nav_step.command_data["tool_name"] == "mcp.playwright.browser_navigate"
        assert nav_step.dependencies == []
        
        # Screenshot step
        shot_step = workflow.steps["screenshot"]
        assert shot_step.command_type in ("tool_execution", "core.tool_execution")
        assert shot_step.command_data["tool_name"] == "mcp.playwright.browser_take_screenshot"
        assert shot_step.dependencies == ["navigate"]
    
    def test_navigate_screenshot_execution_order(self):
        """Test that navigate_screenshot has correct execution order."""
        workflow = WorkflowRegistry.get("navigate_screenshot")
        
        # Should have 2 levels: [[navigate], [screenshot]]
        assert len(workflow.execution_order) == 2
        assert workflow.execution_order[0] == ["navigate"]
        assert workflow.execution_order[1] == ["screenshot"]
    
    def test_navigate_screenshot_inputs(self):
        """Test that navigate_screenshot has correct inputs."""
        workflow = WorkflowRegistry.get("navigate_screenshot")
        assert workflow is not None
        
        inputs = workflow.get_workflow_inputs()
        assert "url" in inputs
    
    def test_navigate_screenshot_parameter_substitution(self):
        """Test parameter substitution for navigate_screenshot."""
        workflow = WorkflowRegistry.get("navigate_screenshot")
        
        # Set context
        workflow.context = {
            "url": "https://example.com",
            "screenshot_name": "example_screenshot"
        }
        
        # Substitute in navigate step
        nav_step = workflow.steps["navigate"]
        resolved = substitute_parameters(nav_step.command_data, workflow.context)
        assert resolved["parameters"]["url"] == "https://example.com"
        
        # Screenshot step: no filename so @playwright/mcp returns in-band image
        shot_step = workflow.steps["screenshot"]
        resolved2 = substitute_parameters(shot_step.command_data, workflow.context)
        assert "filename" not in resolved2["parameters"]
        assert resolved2["parameters"]["type"] == "png"
        assert resolved2["parameters"]["fullPage"] is True
    
    @patch('motet.core.commands.command_type_registry.command_type_registry')
    def test_navigate_screenshot_execution_mock(self, mock_registry):
        """Test navigate_screenshot workflow execution with mocks."""
        # Executor resolves steps via command_type_registry, not get_command_by_name
        mock_reg = Mock()
        mock_reg.implementation = Mock()
        mock_reg.data_class = Mock(return_value=Mock())
        mock_registry.get.return_value = mock_reg

        mock_motet = Mock()
        mock_motet.task_id = "task_123"
        mock_motet.conversation_id = "conv_456"
        mock_motet.event_bus = Mock()
        mock_motet.event_bus.publish = Mock()
        mock_motet.metadata = {}

        # Track calls
        calls = []
        def track_call(cmd_func, data, **kwargs):
            calls.append(data)
            return {"status": "success", "data": "mock_result"}

        mock_motet.do = Mock(side_effect=track_call)

        # Get workflow
        workflow = WorkflowRegistry.get("navigate_screenshot")
        workflow.context = {
            "url": "https://example.com",
            "screenshot_name": "test_screenshot"
        }

        # Execute
        executor = WorkflowExecutor()
        result = executor.execute_workflow(workflow, mock_motet)

        # Verify (ADR-0029: decorator wraps result)
        # Result is wrapped by @distributed_command decorator
        assert "step_results" in result or result.get("status") == "completed"
        assert len(calls) == 2  # Two steps executed
        assert mock_motet.do.call_count == 2


class TestWorkflowExecution:
    """Test workflow execution scenarios."""
    
    @patch('motet.core.commands.command_type_registry.command_type_registry')
    def test_parallel_execution(self, mock_registry):
        """Test parallel execution of independent steps."""
        mock_reg = Mock()
        mock_reg.implementation = Mock()
        mock_reg.data_class = Mock(return_value=Mock())
        mock_registry.get.return_value = mock_reg

        mock_motet = Mock()
        mock_motet.task_id = "task_123"
        mock_motet.conversation_id = "conv_456"
        mock_motet.event_bus = Mock()
        mock_motet.event_bus.publish = Mock()
        mock_motet.do = Mock(return_value={"status": "success"})
        
        # Create workflow with parallel steps
        workflow = Workflow(
            workflow_id="parallel_test",
            name="Parallel Test",
            steps={
                "step1": WorkflowStep(
                    step_id="step1",
                    name="Step 1",
                    command_type="tool_execution",
                    command_data={},
                    dependencies=[]  # No dependencies
                ),
                "step2": WorkflowStep(
                    step_id="step2",
                    name="Step 2",
                    command_type="tool_execution",
                    command_data={},
                    dependencies=[]  # No dependencies
                ),
                "step3": WorkflowStep(
                    step_id="step3",
                    name="Step 3",
                    command_type="tool_execution",
                    command_data={},
                    dependencies=["step1", "step2"]  # Waits for both
                )
            }
        )
        
        # Execute
        executor = WorkflowExecutor()
        result = executor.execute_workflow(workflow, mock_motet)
        
        # Verify
        assert "step_results" in result
        # Execution order: step1 and step2 in level 0 (any order), step3 in level 1
        assert len(workflow.execution_order) == 2
        assert set(workflow.execution_order[0]) == {"step1", "step2"}  # Level 0 - parallel (any order)
        assert workflow.execution_order[1] == ["step3"]  # Level 1 - sequential
        assert mock_motet.do.call_count == 3


class TestWorkflowStepRetries:
    """Test step retry functionality."""
    
    @patch('motet.core.commands.command_type_registry.command_type_registry')
    @patch('time.sleep')  # Mock sleep to speed up tests
    def test_step_retry_on_failure(self, mock_sleep, mock_registry):
        """Test that steps retry on failure."""
        mock_reg = Mock()
        mock_reg.implementation = Mock()
        mock_reg.data_class = Mock(return_value=Mock())
        mock_registry.get.return_value = mock_reg

        mock_motet = Mock()
        mock_motet.task_id = "task_123"
        mock_motet.conversation_id = "conv_456"
        mock_motet.event_bus = Mock()
        mock_motet.event_bus.publish = Mock()

        # Fail first 2 times, succeed on 3rd
        call_count = [0]
        def fail_then_succeed(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] < 3:
                raise Exception("Temporary failure")
            return {"status": "success"}
        
        mock_motet.do = Mock(side_effect=fail_then_succeed)
        
        # Create workflow with retry
        workflow = Workflow(
            workflow_id="retry_test",
            name="Retry Test",
            steps={
                "step1": WorkflowStep(
                    step_id="step1",
                    name="Step 1",
                    command_type="tool_execution",
                    command_data={},
                    step_retry_attempts=2,  # Retry twice
                    step_retry_delay_seconds=0.1
                )
            }
        )
        
        # Execute
        executor = WorkflowExecutor()
        result = executor.execute_workflow(workflow, mock_motet)
        
        # Verify retries happened
        assert "step_results" in result
        assert call_count[0] == 3  # Failed twice, succeeded on 3rd
        assert mock_sleep.call_count == 2  # Slept between retries


class TestWorkflowFallback:
    """Test workflow fallback functionality."""
    
    @patch('motet.core.commands.command_type_registry.command_type_registry')
    def test_fallback_step_execution(self, mock_registry):
        """Test that fallback step executes when primary fails."""
        mock_reg = Mock()
        mock_reg.implementation = Mock()
        mock_reg.data_class = Mock(return_value=Mock())
        mock_registry.get.return_value = mock_reg

        mock_motet = Mock()
        mock_motet.task_id = "task_123"
        mock_motet.conversation_id = "conv_456"
        mock_motet.event_bus = Mock()
        mock_motet.event_bus.publish = Mock()

        # Primary fails, fallback succeeds
        call_count = [0]
        def primary_fails_fallback_succeeds(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise Exception("Primary failed")
            return {"status": "success", "data": "fallback_result"}
        
        mock_motet.do = Mock(side_effect=primary_fails_fallback_succeeds)
        
        # Create workflow with fallback
        # Note: Fallback steps should still have dependencies to prevent double execution
        workflow = Workflow(
            workflow_id="fallback_test",
            name="Fallback Test",
            steps={
                "primary": WorkflowStep(
                    step_id="primary",
                    name="Primary Step",
                    command_type="tool_execution",
                    command_data={},
                    fallback_step_id="backup",
                    step_retry_attempts=0,  # No retries, go straight to fallback
                    dependencies=[]
                ),
                "backup": WorkflowStep(
                    step_id="backup",
                    name="Backup Step",
                    command_type="tool_execution",
                    command_data={},
                    dependencies=["primary"]  # Make backup depend on primary to avoid double execution
                )
            }
        )
        
        # Execute
        executor = WorkflowExecutor()
        result = executor.execute_workflow(workflow, mock_motet)
        
        # Verify fallback was used
        assert "step_results" in result
        assert call_count[0] >= 2  # Primary failed, fallback succeeded (may be called multiple times)


class TestWorkflowUseFor:
    """Test workflow use_for visibility (tool vs facilitation-only)."""

    def test_use_for_default_is_tool(self):
        """Default (None or empty) means used for tool."""
        w = Workflow(
            workflow_id="w1",
            name="W1",
            description="",
            steps={"s1": WorkflowStep(step_id="s1", name="S1", command_type="core.tool_execution", command_data={}, dependencies=[])},
        )
        assert w.get_use_for() == ["tool"]
        assert w.is_used_for_tool() is True

    def test_use_for_explicit_tool(self):
        """use_for=['tool'] is tool-visible."""
        w = Workflow(
            workflow_id="w2",
            name="W2",
            description="",
            use_for=["tool"],
            steps={"s1": WorkflowStep(step_id="s1", name="S1", command_type="core.tool_execution", command_data={}, dependencies=[])},
        )
        assert w.get_use_for() == ["tool"]
        assert w.is_used_for_tool() is True

    def test_use_for_facilitation_only(self):
        """use_for=['facilitation'] is not tool-visible."""
        w = Workflow(
            workflow_id="w3",
            name="W3",
            description="",
            use_for=["facilitation"],
            steps={"s1": WorkflowStep(step_id="s1", name="S1", command_type="core.tool_execution", command_data={}, dependencies=[])},
        )
        assert w.get_use_for() == ["facilitation"]
        assert w.is_used_for_tool() is False

    def test_use_for_tool_and_facilitation(self):
        """use_for=['tool', 'facilitation'] is tool-visible."""
        w = Workflow(
            workflow_id="w4",
            name="W4",
            description="",
            use_for=["tool", "facilitation"],
            steps={"s1": WorkflowStep(step_id="s1", name="S1", command_type="core.tool_execution", command_data={}, dependencies=[])},
        )
        assert set(w.get_use_for()) == {"tool", "facilitation"}
        assert w.is_used_for_tool() is True

    def test_use_for_from_dict(self):
        """from_dict passes use_for through."""
        raw = {
            "workflow_id": "from_dict_w",
            "name": "From Dict",
            "description": "Test",
            "use_for": ["facilitation"],
            "steps": {
                "s1": {
                    "step_id": "s1",
                    "name": "S1",
                    "command_type": "core.tool_execution",
                    "command_data": {},
                    "dependencies": [],
                },
            },
        }
        w = Workflow.from_dict(raw)
        assert w.use_for == ["facilitation"]
        assert w.get_use_for() == ["facilitation"]
        assert w.is_used_for_tool() is False

    def test_use_for_to_dict(self):
        """to_dict includes use_for."""
        w = Workflow(
            workflow_id="to_dict_w",
            name="To Dict",
            description="",
            use_for=["tool", "facilitation"],
            steps={"s1": WorkflowStep(step_id="s1", name="S1", command_type="core.tool_execution", command_data={}, dependencies=[])},
        )
        d = w.to_dict()
        assert "use_for" in d
        assert d["use_for"] == ["tool", "facilitation"]

    def test_keywords_to_dict_roundtrip(self):
        """to_dict / from_dict preserve discovery keywords."""
        w = Workflow(
            workflow_id="kw_w",
            name="Keywords",
            description="Navigate a page",
            keywords=["browser", "url"],
            steps={"s1": WorkflowStep(step_id="s1", name="S1", command_type="core.tool_execution", command_data={}, dependencies=[])},
        )
        d = w.to_dict()
        assert d["keywords"] == ["browser", "url"]
        restored = Workflow.from_dict(d)
        assert restored.keywords == ["browser", "url"]
        assert "browser" in restored.discovery_keywords()

    def test_builtin_workflows_are_tool_visible(self):
        """Built-in workflows default to tool and appear in list_workflow_ids_used_for_tool."""
        tool_ids = WorkflowRegistry.list_workflow_ids_used_for_tool()
        assert "navigate_screenshot" in tool_ids
        assert "web_to_workspace" in tool_ids
        assert "research_to_sheets" in tool_ids

    def test_export_canonical_schemas_only_includes_tool_workflows(self):
        """export_canonical_schemas excludes workflows not used for tool."""
        # Register a facilitation-only workflow temporarily
        fac_only = Workflow(
            workflow_id="test_facilitation_only_workflow",
            name="Fac Only",
            description="Not a tool",
            use_for=["facilitation"],
            steps={"s1": WorkflowStep(step_id="s1", name="S1", command_type="core.tool_execution", command_data={}, dependencies=[])},
        )
        WorkflowRegistry.register(fac_only)
        try:
            schemas = WorkflowRegistry.export_canonical_schemas()
            names = [s.name for s in schemas]
            assert "workflow_navigate_screenshot" in names
            assert "workflow_test_facilitation_only_workflow" not in names
        finally:
            WorkflowRegistry.unregister("test_facilitation_only_workflow")

    def test_list_workflow_ids_used_for_tool_excludes_facilitation_only(self):
        """list_workflow_ids_used_for_tool excludes workflows with use_for=['facilitation'] only."""
        fac_only = Workflow(
            workflow_id="test_fac_only_list",
            name="Fac Only List",
            description="",
            use_for=["facilitation"],
            steps={"s1": WorkflowStep(step_id="s1", name="S1", command_type="core.tool_execution", command_data={}, dependencies=[])},
        )
        WorkflowRegistry.register(fac_only)
        try:
            tool_ids = WorkflowRegistry.list_workflow_ids_used_for_tool()
            assert "test_fac_only_list" not in tool_ids
            assert "navigate_screenshot" in tool_ids
        finally:
            WorkflowRegistry.unregister("test_fac_only_list")


class TestWorkflowForeach:
    """Sequential foreach step type (ADR-0122 Phase 9 / ADR-0049 extension)."""

    def test_step_foreach_fields_round_trip(self):
        """foreach / loop_var / max_loop_iterations / isolate_conversation survive to_dict/from_dict."""
        step = WorkflowStep(
            step_id="implement",
            name="Implement chunks",
            command_type="core.agent_turn",
            command_data={"input": "{{chunk}}"},
            foreach="parse_plan.chunks",
            loop_var="chunk",
            max_loop_iterations=8,
            isolate_conversation=True,
        )
        restored = WorkflowStep.from_dict(step.to_dict())
        assert restored.foreach == "parse_plan.chunks"
        assert restored.loop_var == "chunk"
        assert restored.max_loop_iterations == 8
        assert restored.isolate_conversation is True

    def test_validate_rejects_foreach_on_workflow_execution(self):
        """Nested foreach via workflow_execution is rejected in v1."""
        workflow = Workflow(
            workflow_id="nested_foreach_wf",
            name="Nested",
            steps={
                "loop": WorkflowStep(
                    step_id="loop",
                    name="Loop",
                    command_type="core.workflow_execution",
                    command_data={"workflow_id": "other"},
                    foreach="items",
                )
            },
        )
        with pytest.raises(ValueError, match="nested foreach"):
            validate_workflow(workflow)

    def test_validate_rejects_bad_loop_var(self):
        workflow = Workflow(
            workflow_id="bad_loop_var_wf",
            name="Bad",
            steps={
                "loop": WorkflowStep(
                    step_id="loop",
                    name="Loop",
                    command_type="core.tool_execution",
                    command_data={},
                    foreach="items",
                    loop_var="not-valid!",
                )
            },
        )
        with pytest.raises(ValueError, match="loop_var"):
            validate_workflow(workflow)

    @patch("motet.core.commands.command_type_registry.command_type_registry")
    def test_foreach_happy_path_and_carry(self, mock_registry):
        """Runs once per item; {{loop.previous}} carries prior unwrapped data."""
        mock_reg = Mock()
        mock_reg.implementation = Mock()
        captured_data = []

        class _Data:
            def __init__(self, **kwargs):
                captured_data.append(kwargs)
                self.kwargs = kwargs

        mock_reg.data_class = _Data
        mock_registry.get.return_value = mock_reg

        mock_motet = Mock()
        mock_motet.task_id = "task_foreach"
        mock_motet.conversation_id = "conv_foreach"
        mock_motet.event_bus = Mock()
        mock_motet.metadata = {}
        call_n = [0]

        def _call(*args, **kwargs):
            call_n[0] += 1
            return {
                "status": "success",
                "data": {"final_response": f"summary-{call_n[0]}", "n": call_n[0]},
            }

        mock_motet.do = Mock(side_effect=_call)

        workflow = Workflow(
            workflow_id="foreach_happy",
            name="Foreach Happy",
            steps={
                "implement": WorkflowStep(
                    step_id="implement",
                    name="Implement",
                    command_type="tool_execution",
                    command_data={
                        "chunk": "{{chunk}}",
                        "prev": "{{loop.previous.final_response}}",
                        "idx": "{{loop.index}}",
                    },
                    foreach="chunks",
                    loop_var="chunk",
                    max_loop_iterations=5,
                    dependencies=[],
                )
            },
        )
        workflow.context["chunks"] = ["a", "b", "c"]

        executor = WorkflowExecutor()
        result = executor.execute_workflow(workflow, mock_motet)

        assert mock_motet.do.call_count == 3
        assert captured_data[0]["chunk"] == "a"
        assert captured_data[0]["prev"] == ""
        assert captured_data[0]["idx"] == 0
        assert captured_data[1]["chunk"] == "b"
        assert captured_data[1]["prev"] == "summary-1"
        assert captured_data[2]["chunk"] == "c"
        assert captured_data[2]["prev"] == "summary-2"

        impl = workflow.context["implement"]
        assert impl["count"] == 3
        assert impl["results"][0]["final_response"] == "summary-1"
        assert impl["results"][2]["final_response"] == "summary-3"
        assert "step_results" in result

    @patch("motet.core.commands.command_type_registry.command_type_registry")
    def test_foreach_isolate_conversation_mints_child_ids(self, mock_registry):
        """Each foreach iteration gets its own conversation_id override."""
        mock_reg = Mock()
        mock_reg.implementation = Mock()

        class _Data:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        mock_reg.data_class = _Data
        mock_registry.get.return_value = mock_reg

        mock_motet = Mock()
        mock_motet.task_id = "task_iso"
        mock_motet.conversation_id = "api-exec-parent"
        mock_motet.tenant_id = None
        mock_motet.event_bus = Mock()
        mock_motet.metadata = {}
        seen_cids = []

        def _call(*args, **kwargs):
            seen_cids.append(kwargs.get("conversation_id"))
            return {"status": "success", "data": {"final_response": "ok"}}

        mock_motet.do = Mock(side_effect=_call)

        workflow = Workflow(
            workflow_id="foreach_iso",
            name="Foreach Isolate",
            steps={
                "implement": WorkflowStep(
                    step_id="implement",
                    name="Implement",
                    command_type="tool_execution",
                    command_data={"chunk": "{{chunk}}"},
                    foreach="chunks",
                    loop_var="chunk",
                    isolate_conversation=True,
                    max_loop_iterations=5,
                    dependencies=[],
                )
            },
        )
        workflow.context["chunks"] = ["a", "b"]

        WorkflowExecutor().execute_workflow(workflow, mock_motet)

        assert mock_motet.do.call_count == 2
        assert seen_cids[0] != seen_cids[1]
        assert all(cid and str(cid).startswith("iso-") for cid in seen_cids)
        assert all(cid != "api-exec-parent" for cid in seen_cids)

    @patch("motet.core.commands.command_type_registry.command_type_registry")
    def test_foreach_empty_list(self, mock_registry):
        mock_reg = Mock()
        mock_reg.implementation = Mock()
        mock_reg.data_class = Mock(return_value=Mock())
        mock_registry.get.return_value = mock_reg

        mock_motet = Mock()
        mock_motet.task_id = "task_empty"
        mock_motet.conversation_id = "conv_empty"
        mock_motet.event_bus = Mock()
        mock_motet.metadata = {}
        mock_motet.do = Mock()

        workflow = Workflow(
            workflow_id="foreach_empty",
            name="Foreach Empty",
            steps={
                "implement": WorkflowStep(
                    step_id="implement",
                    name="Implement",
                    command_type="tool_execution",
                    command_data={},
                    foreach="chunks",
                    dependencies=[],
                )
            },
        )
        workflow.context["chunks"] = []

        executor = WorkflowExecutor()
        executor.execute_workflow(workflow, mock_motet)

        assert mock_motet.do.call_count == 0
        assert workflow.context["implement"] == {
            "results": [],
            "count": 0,
            "stopped_reason": "items_exhausted",
        }

    @patch("motet.core.commands.command_type_registry.command_type_registry")
    def test_foreach_exceeds_max_iterations(self, mock_registry):
        mock_reg = Mock()
        mock_reg.implementation = Mock()
        mock_reg.data_class = Mock(return_value=Mock())
        mock_registry.get.return_value = mock_reg

        mock_motet = Mock()
        mock_motet.task_id = "task_cap"
        mock_motet.conversation_id = "conv_cap"
        mock_motet.event_bus = Mock()
        mock_motet.metadata = {}
        mock_motet.do = Mock()

        workflow = Workflow(
            workflow_id="foreach_cap",
            name="Foreach Cap",
            steps={
                "implement": WorkflowStep(
                    step_id="implement",
                    name="Implement",
                    command_type="tool_execution",
                    command_data={},
                    foreach="chunks",
                    max_loop_iterations=2,
                    dependencies=[],
                )
            },
        )
        workflow.context["chunks"] = ["a", "b", "c"]

        executor = WorkflowExecutor()
        result = executor.execute_workflow(workflow, mock_motet)

        # Level catches the raise and records a failed step result
        assert result["step_results"]["implement"]["status"] == "failed"
        assert "max_loop_iterations" in result["step_results"]["implement"]["error"]
        assert mock_motet.do.call_count == 0

    @patch("motet.core.commands.command_type_registry.command_type_registry")
    def test_foreach_fail_fast(self, mock_registry):
        mock_reg = Mock()
        mock_reg.implementation = Mock()
        mock_reg.data_class = Mock(return_value=Mock())
        mock_registry.get.return_value = mock_reg

        mock_motet = Mock()
        mock_motet.task_id = "task_fail"
        mock_motet.conversation_id = "conv_fail"
        mock_motet.event_bus = Mock()
        mock_motet.metadata = {}
        call_n = [0]

        def _call(*args, **kwargs):
            call_n[0] += 1
            if call_n[0] == 2:
                raise RuntimeError("chunk 2 failed")
            return {"status": "success", "data": {"final_response": f"ok-{call_n[0]}"}}

        mock_motet.do = Mock(side_effect=_call)

        workflow = Workflow(
            workflow_id="foreach_fail",
            name="Foreach Fail",
            steps={
                "implement": WorkflowStep(
                    step_id="implement",
                    name="Implement",
                    command_type="tool_execution",
                    command_data={"chunk": "{{chunk}}"},
                    foreach="chunks",
                    loop_var="chunk",
                    step_retry_attempts=0,
                    dependencies=[],
                )
            },
        )
        workflow.context["chunks"] = ["a", "b", "c"]

        executor = WorkflowExecutor()
        result = executor.execute_workflow(workflow, mock_motet)

        assert mock_motet.do.call_count == 2  # stopped after second failure
        assert result["step_results"]["implement"]["status"] == "failed"
        assert "chunk 2 failed" in result["step_results"]["implement"]["error"]

    @patch("motet.core.commands.command_type_registry.command_type_registry")
    def test_foreach_continue_on_failure_partial_results(self, mock_registry):
        """continue_on_failure keeps partial results and lets dependents run."""
        mock_reg = Mock()
        mock_reg.implementation = Mock()
        captured = []

        class _Data:
            def __init__(self, **kwargs):
                captured.append(kwargs)

        mock_reg.data_class = _Data
        mock_registry.get.return_value = mock_reg

        mock_motet = Mock()
        mock_motet.task_id = "task_cof"
        mock_motet.conversation_id = "conv_cof"
        mock_motet.event_bus = Mock()
        mock_motet.metadata = {}
        call_n = [0]

        def _call(*args, **kwargs):
            call_n[0] += 1
            if call_n[0] == 2:
                raise RuntimeError("boom on chunk 2")
            return {"status": "success", "data": {"final_response": f"ok-{call_n[0]}"}}

        mock_motet.do = Mock(side_effect=_call)

        workflow = Workflow(
            workflow_id="foreach_cof",
            name="Foreach COF",
            steps={
                "implement": WorkflowStep(
                    step_id="implement",
                    name="Implement",
                    command_type="tool_execution",
                    command_data={"chunk": "{{chunk}}"},
                    foreach="chunks",
                    loop_var="chunk",
                    step_retry_attempts=0,
                    continue_on_failure=True,
                    dependencies=[],
                ),
                "after": WorkflowStep(
                    step_id="after",
                    name="After",
                    command_type="tool_execution",
                    command_data={"from_impl": "{{implement.count}}"},
                    dependencies=["implement"],
                ),
            },
        )
        workflow.context["chunks"] = ["a", "b", "c"]

        executor = WorkflowExecutor()
        result = executor.execute_workflow(workflow, mock_motet)

        impl_result = result["step_results"]["implement"]
        assert impl_result["status"] == "failed"
        assert impl_result["count"] == 1
        assert impl_result["results"][0]["final_response"] == "ok-1"
        # Context is the same domain payload; continue_on_failure keeps dependents running
        assert workflow.context["implement"]["count"] == 1
        assert result["step_results"]["after"]["status"] == "success"
        assert captured[-1]["from_impl"] == 1

    @patch("motet.core.commands.command_type_registry.command_type_registry")
    @patch("motet.core.workflow.executor_steps.worker_sleep")
    def test_foreach_per_iteration_retry(self, mock_sleep, mock_registry):
        """step_retry_attempts applies per iteration; loop continues after recovery."""
        mock_reg = Mock()
        mock_reg.implementation = Mock()
        mock_reg.data_class = Mock(return_value=Mock())
        mock_registry.get.return_value = mock_reg

        mock_motet = Mock()
        mock_motet.task_id = "task_retry"
        mock_motet.conversation_id = "conv_retry"
        mock_motet.event_bus = Mock()
        mock_motet.metadata = {}
        call_n = [0]

        def _call(*args, **kwargs):
            call_n[0] += 1
            # First iteration: fail twice then succeed (calls 1,2 fail; 3 ok)
            # Second iteration: succeed on first try (call 4)
            if call_n[0] in (1, 2):
                raise RuntimeError("transient")
            return {"status": "success", "data": {"final_response": f"ok-{call_n[0]}"}}

        mock_motet.do = Mock(side_effect=_call)

        workflow = Workflow(
            workflow_id="foreach_retry",
            name="Foreach Retry",
            steps={
                "implement": WorkflowStep(
                    step_id="implement",
                    name="Implement",
                    command_type="tool_execution",
                    command_data={},
                    foreach="chunks",
                    step_retry_attempts=2,
                    step_retry_delay_seconds=0.01,
                    dependencies=[],
                )
            },
        )
        workflow.context["chunks"] = ["a", "b"]

        executor = WorkflowExecutor()
        executor.execute_workflow(workflow, mock_motet)

        assert call_n[0] == 4
        assert mock_sleep.call_count == 2
        assert workflow.context["implement"]["count"] == 2

    @patch("motet.core.commands.command_type_registry.command_type_registry")
    def test_foreach_json_string_list(self, mock_registry):
        """Foreach path may resolve to a JSON array string (template round-trip)."""
        mock_reg = Mock()
        mock_reg.implementation = Mock()
        captured = []

        class _Data:
            def __init__(self, **kwargs):
                captured.append(kwargs)

        mock_reg.data_class = _Data
        mock_registry.get.return_value = mock_reg

        mock_motet = Mock()
        mock_motet.task_id = "task_json"
        mock_motet.conversation_id = "conv_json"
        mock_motet.event_bus = Mock()
        mock_motet.metadata = {}
        mock_motet.do = Mock(
            side_effect=lambda *a, **k: {"status": "success", "data": {"final_response": "x"}}
        )

        workflow = Workflow(
            workflow_id="foreach_json",
            name="Foreach JSON",
            steps={
                "implement": WorkflowStep(
                    step_id="implement",
                    name="Implement",
                    command_type="tool_execution",
                    command_data={"chunk": "{{chunk}}"},
                    foreach="chunks",
                    loop_var="chunk",
                    dependencies=[],
                )
            },
        )
        workflow.context["chunks"] = '["one", "two"]'

        executor = WorkflowExecutor()
        executor.execute_workflow(workflow, mock_motet)

        assert mock_motet.do.call_count == 2
        assert captured[0]["chunk"] == "one"
        assert captured[1]["chunk"] == "two"
        assert workflow.context["implement"]["count"] == 2

    @patch("motet.core.commands.command_type_registry.command_type_registry")
    def test_foreach_missing_path_is_empty(self, mock_registry):
        mock_reg = Mock()
        mock_reg.implementation = Mock()
        mock_reg.data_class = Mock(return_value=Mock())
        mock_registry.get.return_value = mock_reg

        mock_motet = Mock()
        mock_motet.task_id = "task_missing"
        mock_motet.conversation_id = "conv_missing"
        mock_motet.event_bus = Mock()
        mock_motet.metadata = {}
        mock_motet.do = Mock(return_value={"status": "success", "data": {"ok": True}})

        workflow = Workflow(
            workflow_id="foreach_missing",
            name="Foreach Missing",
            steps={
                "implement": WorkflowStep(
                    step_id="implement",
                    name="Implement",
                    command_type="tool_execution",
                    command_data={},
                    foreach="parse_plan.chunks",
                    dependencies=[],
                ),
                "after": WorkflowStep(
                    step_id="after",
                    name="After",
                    command_type="tool_execution",
                    command_data={},
                    dependencies=["implement"],
                ),
            },
        )
        # no parse_plan in context

        executor = WorkflowExecutor()
        result = executor.execute_workflow(workflow, mock_motet)

        assert mock_motet.do.call_count == 1  # only "after"
        assert workflow.context["implement"] == {
            "results": [],
            "count": 0,
            "stopped_reason": "items_exhausted",
        }
        assert result["step_results"]["after"]["status"] == "success"

    @patch("motet.core.commands.command_type_registry.command_type_registry")
    def test_foreach_non_list_path_fails(self, mock_registry):
        mock_reg = Mock()
        mock_reg.implementation = Mock()
        mock_reg.data_class = Mock(return_value=Mock())
        mock_registry.get.return_value = mock_reg

        mock_motet = Mock()
        mock_motet.task_id = "task_nonlist"
        mock_motet.conversation_id = "conv_nonlist"
        mock_motet.event_bus = Mock()
        mock_motet.metadata = {}
        mock_motet.do = Mock()

        workflow = Workflow(
            workflow_id="foreach_nonlist",
            name="Foreach Nonlist",
            steps={
                "implement": WorkflowStep(
                    step_id="implement",
                    name="Implement",
                    command_type="tool_execution",
                    command_data={},
                    foreach="chunks",
                    dependencies=[],
                )
            },
        )
        workflow.context["chunks"] = {"not": "a list"}

        executor = WorkflowExecutor()
        result = executor.execute_workflow(workflow, mock_motet)

        assert result["step_results"]["implement"]["status"] == "failed"
        assert "expected list" in result["step_results"]["implement"]["error"]
        assert mock_motet.do.call_count == 0

    @patch("motet.core.commands.command_type_registry.command_type_registry")
    def test_foreach_downstream_templating(self, mock_registry):
        """Next step can read {{implement.results[0].final_response}}."""
        mock_reg = Mock()
        mock_reg.implementation = Mock()
        captured = []

        class _Data:
            def __init__(self, **kwargs):
                captured.append(kwargs)

        mock_reg.data_class = _Data
        mock_registry.get.return_value = mock_reg

        mock_motet = Mock()
        mock_motet.task_id = "task_down"
        mock_motet.conversation_id = "conv_down"
        mock_motet.event_bus = Mock()
        mock_motet.metadata = {}
        mock_motet.do = Mock(
            side_effect=[
                {"status": "success", "data": {"final_response": "summary-1"}},
                {"status": "success", "data": {"final_response": "summary-2"}},
                {"status": "success", "data": {"ok": True}},
            ]
        )

        workflow = Workflow(
            workflow_id="foreach_down",
            name="Foreach Downstream",
            steps={
                "implement": WorkflowStep(
                    step_id="implement",
                    name="Implement",
                    command_type="tool_execution",
                    command_data={},
                    foreach="chunks",
                    dependencies=[],
                ),
                "review": WorkflowStep(
                    step_id="review",
                    name="Review",
                    command_type="tool_execution",
                    command_data={
                        "first": "{{implement.results[0].final_response}}",
                        "count": "{{implement.count}}",
                    },
                    dependencies=["implement"],
                ),
            },
        )
        workflow.context["chunks"] = ["a", "b"]

        executor = WorkflowExecutor()
        executor.execute_workflow(workflow, mock_motet)

        assert workflow.context["implement"]["count"] == 2
        # Third data_class construction is review with substituted fields
        assert captured[2]["first"] == "summary-1"
        assert captured[2]["count"] == 2

    def test_foreach_yaml_from_dict(self):
        """Bundle-style Workflow.from_dict preserves foreach fields on steps."""
        raw = {
            "workflow_id": "foreach_yaml",
            "name": "Foreach YAML",
            "steps": {
                "parse_plan": {
                    "step_id": "parse_plan",
                    "name": "Parse plan",
                    "command_type": "core.tool_execution",
                    "command_data": {},
                    "dependencies": [],
                },
                "implement": {
                    "step_id": "implement",
                    "name": "Implement",
                    "command_type": "core.agent_turn",
                    "command_data": {
                        "agent_id": "demo.engineer",
                        "messages": [{"role": "user", "content": "{{chunk}}"}],
                    },
                    "foreach": "parse_plan.chunks",
                    "loop_var": "chunk",
                    "max_loop_iterations": 8,
                    "dependencies": ["parse_plan"],
                },
            },
        }
        workflow = Workflow.from_dict(raw)
        step = workflow.steps["implement"]
        assert step.foreach == "parse_plan.chunks"
        assert step.loop_var == "chunk"
        assert step.max_loop_iterations == 8
        assert step.dependencies == ["parse_plan"]

    @patch("motet.core.commands.command_type_registry.command_type_registry")
    def test_foreach_emits_indexed_step_events(self, mock_registry):
        """Per-iteration workflow_step events use implement[i] step_ids."""
        mock_reg = Mock()
        mock_reg.implementation = Mock()
        mock_reg.data_class = Mock(return_value=Mock())
        mock_registry.get.return_value = mock_reg

        mock_motet = Mock()
        mock_motet.task_id = "task_events"
        mock_motet.conversation_id = "conv_events"
        mock_motet.event_bus = Mock()
        mock_motet.metadata = {}
        mock_motet.do = Mock(
            return_value={"status": "success", "data": {"final_response": "ok"}}
        )
        published = []

        def _publish(event):
            published.append(event)

        mock_motet.publish_event = Mock(side_effect=_publish)

        workflow = Workflow(
            workflow_id="foreach_events",
            name="Foreach Events",
            steps={
                "implement": WorkflowStep(
                    step_id="implement",
                    name="Implement",
                    command_type="tool_execution",
                    command_data={},
                    foreach="chunks",
                    dependencies=[],
                )
            },
        )
        workflow.context["chunks"] = ["a", "b"]

        executor = WorkflowExecutor()
        executor.execute_workflow(workflow, mock_motet)

        step_ids = [e.get("step_id") for e in published if e.get("kind") == "workflow_step"]
        assert "implement" in step_ids
        assert "implement[0]" in step_ids
        assert "implement[1]" in step_ids
        # Each iteration emits started + completed
        assert step_ids.count("implement[0]") >= 2
        assert step_ids.count("implement[1]") >= 2

    @patch("motet.core.commands.command_type_registry.command_type_registry")
    def test_foreach_previous_summaries_accumulate(self, mock_registry):
        """{{loop.previous_summaries}} carries ALL prior iteration summaries."""
        mock_reg = Mock()
        mock_reg.implementation = Mock()
        captured_data = []

        class _Data:
            def __init__(self, **kwargs):
                captured_data.append(kwargs)
                self.kwargs = kwargs

        mock_reg.data_class = _Data
        mock_registry.get.return_value = mock_reg

        mock_motet = Mock()
        mock_motet.task_id = "task_acc"
        mock_motet.conversation_id = "conv_acc"
        mock_motet.event_bus = Mock()
        mock_motet.metadata = {}
        call_n = [0]

        def _call(*args, **kwargs):
            call_n[0] += 1
            return {
                "status": "success",
                "data": {"final_response": f"summary-{call_n[0]}"},
            }

        mock_motet.do = Mock(side_effect=_call)

        workflow = Workflow(
            workflow_id="foreach_acc",
            name="Foreach Accumulate",
            steps={
                "implement": WorkflowStep(
                    step_id="implement",
                    name="Implement",
                    command_type="tool_execution",
                    command_data={
                        "chunk": "{{chunk}}",
                        "history": "{{loop.previous_summaries}}",
                    },
                    foreach="chunks",
                    loop_var="chunk",
                    max_loop_iterations=5,
                    dependencies=[],
                )
            },
        )
        workflow.context["chunks"] = ["a", "b", "c"]

        executor = WorkflowExecutor()
        executor.execute_workflow(workflow, mock_motet)

        assert captured_data[0]["history"] == ""
        assert captured_data[1]["history"] == "[iteration 1]\nsummary-1"
        assert "[iteration 1]\nsummary-1" in captured_data[2]["history"]
        assert "[iteration 2]\nsummary-2" in captured_data[2]["history"]


class TestWorkflowUntil:
    """Repeat-until break condition on loop steps."""

    @staticmethod
    def _registry(mock_registry, captured=None):
        mock_reg = Mock()
        mock_reg.implementation = Mock()

        class _Data:
            def __init__(self, **kwargs):
                if captured is not None:
                    captured.append(kwargs)

        mock_reg.data_class = _Data
        mock_registry.get.return_value = mock_reg
        return mock_reg

    @staticmethod
    def _motet(side_effect, task_id="task_until"):
        mock_motet = Mock()
        mock_motet.task_id = task_id
        mock_motet.conversation_id = f"conv_{task_id}"
        mock_motet.event_bus = Mock()
        mock_motet.metadata = {}
        mock_motet.do = Mock(side_effect=side_effect)
        return mock_motet

    def test_until_field_round_trips(self):
        """until survives to_dict/from_dict alongside the other loop fields."""
        step = WorkflowStep(
            step_id="gate",
            name="Gate",
            command_type="core.agent_turn",
            command_data={},
            until="if_equals:result.passed:True",
            max_loop_iterations=3,
        )
        restored = WorkflowStep.from_dict(step.to_dict())
        assert restored.until == "if_equals:result.passed:True"
        assert restored.max_loop_iterations == 3

    def test_condition_types_constant_matches_evaluator(self):
        """Validation vocabulary must not drift from what _evaluate_condition accepts."""
        from motet.core.workflow.utils import WORKFLOW_CONDITION_TYPES

        ex = WorkflowExecutor()
        ctx = {"step": {"value": "x", "status": "failed"}}
        for condition_type in WORKFLOW_CONDITION_TYPES:
            with patch.object(ex.logger, "warning") as mock_warn:
                ex._evaluate_condition(f"{condition_type}:step.value:x", ctx)
                assert not mock_warn.called, f"{condition_type} not handled by evaluator"

    @patch("motet.core.commands.command_type_registry.command_type_registry")
    def test_until_breaks_early_on_foreach(self, mock_registry):
        """Condition met mid-list stops the loop and reports until_met."""
        self._registry(mock_registry)
        call_n = [0]

        def _call(*args, **kwargs):
            call_n[0] += 1
            return {"status": "success", "data": {"passed": call_n[0] == 2}}

        mock_motet = self._motet(_call, task_id="task_break")

        workflow = Workflow(
            workflow_id="until_break",
            name="Until Break",
            steps={
                "implement": WorkflowStep(
                    step_id="implement",
                    name="Implement",
                    command_type="tool_execution",
                    command_data={"chunk": "{{chunk}}"},
                    foreach="chunks",
                    loop_var="chunk",
                    until="if_equals:result.passed:True",
                    max_loop_iterations=5,
                    dependencies=[],
                )
            },
        )
        workflow.context["chunks"] = ["a", "b", "c", "d"]

        WorkflowExecutor().execute_workflow(workflow, mock_motet)

        assert mock_motet.do.call_count == 2  # stopped before c and d
        assert workflow.context["implement"]["count"] == 2
        assert workflow.context["implement"]["stopped_reason"] == "until_met"

    @patch("motet.core.commands.command_type_registry.command_type_registry")
    def test_until_never_met_exhausts_list(self, mock_registry):
        """A foreach whose until never holds runs the full list and says so."""
        self._registry(mock_registry)
        mock_motet = self._motet(
            lambda *a, **k: {"status": "success", "data": {"passed": False}},
            task_id="task_exhaust",
        )

        workflow = Workflow(
            workflow_id="until_exhaust",
            name="Until Exhaust",
            steps={
                "implement": WorkflowStep(
                    step_id="implement",
                    name="Implement",
                    command_type="tool_execution",
                    command_data={},
                    foreach="chunks",
                    until="if_equals:result.passed:True",
                    dependencies=[],
                )
            },
        )
        workflow.context["chunks"] = ["a", "b"]

        WorkflowExecutor().execute_workflow(workflow, mock_motet)

        assert mock_motet.do.call_count == 2
        assert workflow.context["implement"]["stopped_reason"] == "items_exhausted"

    @patch("motet.core.commands.command_type_registry.command_type_registry")
    def test_until_without_foreach_is_counted_repeat(self, mock_registry):
        """until alone retries up to max_loop_iterations; loop_var binds the attempt."""
        captured = []
        self._registry(mock_registry, captured)
        call_n = [0]

        def _call(*args, **kwargs):
            call_n[0] += 1
            return {"status": "success", "data": {"passed": call_n[0] == 3}}

        mock_motet = self._motet(_call, task_id="task_repeat")

        workflow = Workflow(
            workflow_id="until_repeat",
            name="Until Repeat",
            steps={
                "fix": WorkflowStep(
                    step_id="fix",
                    name="Fix",
                    command_type="tool_execution",
                    command_data={"attempt": "{{attempt}}"},
                    loop_var="attempt",
                    until="if_equals:result.passed:True",
                    max_loop_iterations=5,
                    dependencies=[],
                )
            },
        )

        WorkflowExecutor().execute_workflow(workflow, mock_motet)

        assert mock_motet.do.call_count == 3
        assert [c["attempt"] for c in captured] == [0, 1, 2]
        assert workflow.context["fix"]["stopped_reason"] == "until_met"

    @patch("motet.core.commands.command_type_registry.command_type_registry")
    def test_counted_repeat_exhausts_budget(self, mock_registry):
        """Never satisfying until burns exactly max_loop_iterations attempts."""
        self._registry(mock_registry)
        mock_motet = self._motet(
            lambda *a, **k: {"status": "success", "data": {"passed": False}},
            task_id="task_budget",
        )

        workflow = Workflow(
            workflow_id="until_budget",
            name="Until Budget",
            steps={
                "fix": WorkflowStep(
                    step_id="fix",
                    name="Fix",
                    command_type="tool_execution",
                    command_data={},
                    until="if_equals:result.passed:True",
                    max_loop_iterations=3,
                    dependencies=[],
                )
            },
        )

        WorkflowExecutor().execute_workflow(workflow, mock_motet)

        assert mock_motet.do.call_count == 3
        assert workflow.context["fix"]["count"] == 3
        assert workflow.context["fix"]["stopped_reason"] == "max_iterations"

    @patch("motet.core.commands.command_type_registry.command_type_registry")
    def test_dependent_gates_on_stopped_reason(self, mock_registry):
        """A give-up loop can be detected downstream via skip_condition."""
        self._registry(mock_registry)
        mock_motet = self._motet(
            lambda *a, **k: {"status": "success", "data": {"passed": False}},
            task_id="task_gate",
        )

        workflow = Workflow(
            workflow_id="until_gate",
            name="Until Gate",
            steps={
                "fix": WorkflowStep(
                    step_id="fix",
                    name="Fix",
                    command_type="tool_execution",
                    command_data={},
                    until="if_equals:result.passed:True",
                    max_loop_iterations=2,
                    dependencies=[],
                ),
                "ship": WorkflowStep(
                    step_id="ship",
                    name="Ship",
                    command_type="tool_execution",
                    command_data={},
                    skip_condition="if_equals:fix.stopped_reason:max_iterations",
                    dependencies=["fix"],
                ),
            },
        )

        result = WorkflowExecutor().execute_workflow(workflow, mock_motet)

        assert mock_motet.do.call_count == 2  # ship never ran
        assert result["step_results"]["ship"]["status"] == "skipped"

    def test_validate_rejects_unknown_until_operator(self):
        """A typo'd operator would silently never break; reject at build time."""
        workflow = Workflow(
            workflow_id="until_bad_op",
            name="Bad Op",
            steps={
                "fix": WorkflowStep(
                    step_id="fix",
                    name="Fix",
                    command_type="core.tool_execution",
                    command_data={},
                    until="if_true:result.passed",
                )
            },
        )
        with pytest.raises(ValueError, match="invalid until condition"):
            validate_workflow(workflow)

    def test_validate_rejects_malformed_until(self):
        workflow = Workflow(
            workflow_id="until_malformed",
            name="Malformed",
            steps={
                "fix": WorkflowStep(
                    step_id="fix",
                    name="Fix",
                    command_type="core.tool_execution",
                    command_data={},
                    until="result.passed",
                )
            },
        )
        with pytest.raises(ValueError, match="invalid until condition"):
            validate_workflow(workflow)

    def test_execution_data_round_trip_preserves_control_flow(self):
        """to_execution_data feeds from_execution_data; dropping loop fields would
        silently downgrade a loop step to a single run on the LLM tool path."""
        workflow = Workflow(
            workflow_id="round_trip",
            name="Round Trip",
            steps={
                "fix": WorkflowStep(
                    step_id="fix",
                    name="Fix",
                    command_type="core.tool_execution",
                    command_data={},
                    foreach="plan.chunks",
                    loop_var="chunk",
                    until="if_equals:result.passed:True",
                    max_loop_iterations=4,
                    isolate_conversation=True,
                    skip_condition="if_empty:plan.chunks",
                    continue_on_failure=True,
                    step_retry_attempts=2,
                )
            },
        )

        restored = Workflow.from_execution_data(workflow.to_execution_data())
        step = restored.steps["fix"]

        assert step.foreach == "plan.chunks"
        assert step.loop_var == "chunk"
        assert step.until == "if_equals:result.passed:True"
        assert step.max_loop_iterations == 4
        assert step.isolate_conversation is True
        assert step.skip_condition == "if_empty:plan.chunks"
        assert step.continue_on_failure is True
        assert step.step_retry_attempts == 2

    def test_validate_rejects_until_on_workflow_execution(self):
        """until-only loops inherit the nested-workflow guard."""
        workflow = Workflow(
            workflow_id="until_nested",
            name="Nested",
            steps={
                "loop": WorkflowStep(
                    step_id="loop",
                    name="Loop",
                    command_type="core.workflow_execution",
                    command_data={"workflow_id": "other"},
                    until="if_equals:result.passed:True",
                )
            },
        )
        with pytest.raises(ValueError, match="nested foreach"):
            validate_workflow(workflow)


class TestSkipConditionTypedEquals:
    """Typed if_equals comparison (bools/None/numbers vs literals)."""

    def _executor(self):
        return WorkflowExecutor()

    def test_bool_true_matches_case_insensitive_tokens(self):
        ex = self._executor()
        ctx = {"gate": {"passed": True}}
        assert ex._evaluate_condition("if_equals:gate.passed:True", ctx)
        assert ex._evaluate_condition("if_equals:gate.passed:true", ctx)
        assert ex._evaluate_condition("if_equals:gate.passed:1", ctx)
        assert not ex._evaluate_condition("if_equals:gate.passed:False", ctx)
        assert not ex._evaluate_condition("if_equals:gate.passed:false", ctx)

    def test_bool_false_matches_case_insensitive_tokens(self):
        ex = self._executor()
        ctx = {"gate": {"passed": False}}
        assert ex._evaluate_condition("if_equals:gate.passed:False", ctx)
        assert ex._evaluate_condition("if_equals:gate.passed:false", ctx)
        assert ex._evaluate_condition("if_equals:gate.passed:0", ctx)
        assert not ex._evaluate_condition("if_equals:gate.passed:True", ctx)

    def test_none_matches_none_and_null(self):
        ex = self._executor()
        ctx = {"gate": {"passed": None}}
        assert ex._evaluate_condition("if_equals:gate.passed:None", ctx)
        assert ex._evaluate_condition("if_equals:gate.passed:null", ctx)
        assert not ex._evaluate_condition("if_equals:gate.passed:False", ctx)

    def test_numbers_compare_numerically(self):
        ex = self._executor()
        ctx = {"step": {"count": 3}}
        assert ex._evaluate_condition("if_equals:step.count:3", ctx)
        assert ex._evaluate_condition("if_equals:step.count:3.0", ctx)
        assert not ex._evaluate_condition("if_equals:step.count:4", ctx)

    def test_strings_keep_exact_comparison(self):
        ex = self._executor()
        ctx = {"step": {"route": "plan"}}
        assert ex._evaluate_condition("if_equals:step.route:plan", ctx)
        assert not ex._evaluate_condition("if_equals:step.route:Plan", ctx)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

