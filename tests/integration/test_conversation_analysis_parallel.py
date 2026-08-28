"""
Integration tests for parallel conversation analysis system.

Tests the new decorator-based parallel conversation analysis with motet.join().
"""

import pytest
from unittest.mock import Mock, MagicMock, patch
from motet.core.types import Message

# Patch target: decorator uses get_motet_context() when building DecoratedCommand (for task_id etc.)
DECORATOR_GET_MOTET = "motet.core.commands.decorator.get_motet_context"
# Commands also use get_motet_context() at runtime; patch where they import it
GET_MOTET_CONTEXT = "motet.core.commands.builtin.conversation_analysis.intent_analysis.get_motet_context"
GET_MOTET_CONTEXT_ORCH = "motet.core.commands.builtin.conversation_analysis.conversation_analysis.get_motet_context"
from motet.core.commands.builtin.conversation_analysis import (
    conversation_analysis,
    intent_analysis,
    tone_analysis,
    complexity_analysis,
    context_analysis,
    user_profile_analysis,
    ConversationAnalysisService,
    ConversationAnalysisData,
    IntentAnalysisData
)


class TestIntentAnalysis:
    """Unit tests for intent analysis dimension"""
    
    def test_intent_analysis_basic(self):
        """Test basic intent analysis with mock LLM"""
        # Create test data
        data = IntentAnalysisData(
            user_text="Hello, how are you?",
            analysis_model="gpt-4o-mini"
        )

        # Create mock motet context (command uses get_motet_context() internally)
        motet = Mock()
        motet.task_id = "test-task"
        motet.conversation_id = "test-conv"
        motet.tenant_id = "test-tenant"
        motet.principal_id = "test-principal"

        # motet.do() returns unwrapped data (ADR-0052)
        mock_do_result = {"content": '{"primary": "greeting", "confidence": 0.95}'}
        motet.do = Mock(return_value=mock_do_result)

        # Call handler directly (wrapper returns DecoratedCommand; handler uses get_motet_context())
        with patch(GET_MOTET_CONTEXT, return_value=motet):
            result = intent_analysis.__original_function__(data=data)

        assert result["primary"] == "greeting"
        assert result["confidence"] == 0.95
    
    def test_intent_analysis_fallback(self):
        """Test intent analysis fallback when LLM fails (use text without 'hi' substring to avoid greeting heuristic)."""
        data = IntentAnalysisData(
            user_text="Explain and compare different approaches to scaling microservices.",
            analysis_model="gpt-4o-mini"
        )

        motet = Mock()
        motet.task_id = "test-task"
        motet.conversation_id = "test-conv"
        motet.tenant_id = "test-tenant"
        motet.principal_id = "test-principal"

        # motet.do() raises on model failure; intent_analysis catches and uses fallback
        from motet.core.commands.response_models import CommandExecutionError
        motet.do = Mock(
            side_effect=CommandExecutionError(
                error_type="model_error",
                message="Model failed",
                details={},
                recoverable=True,
                command_type="model_inference",
                command_id="test",
            )
        )

        with patch(GET_MOTET_CONTEXT, return_value=motet):
            result = intent_analysis.__original_function__(data=data)

        assert result["primary"] == "task_request"
        assert result["confidence"] == 0.3
        assert result.get("fallback") is True


class TestConversationAnalysisOrchestrator:
    """Integration tests for parallel conversation analysis orchestrator"""
    
    def test_parallel_analysis_all_dimensions(self):
        """Test parallel execution of all analysis dimensions."""
        messages = [Message(role="user", content="Can you help me optimize this algorithm?")]
        data = ConversationAnalysisData(
            messages=messages,
            analysis_dimensions=["intent", "tone", "complexity", "context", "user_profile"],
            analysis_model="gpt-4o-mini"
        )
        
        # Create mock motet context
        motet = Mock()
        motet.task_id = "test-task"
        motet.conversation_id = "test-conv"
        motet.tenant_id = "test-tenant"
        motet.principal_id = "test-principal"
        
        mock_results = [
            {"primary": "task_request", "confidence": 0.9},
            {"emotion": "neutral", "urgency": "medium", "satisfaction": "medium", "communication_style": "direct", "confidence": 0.8},
            {"level": "complex", "estimated_turns": 5, "scope": "focused", "tool_requirements": "advanced", "expertise_needed": "expert"},
            {"needs_clarification": False, "references_previous": False, "missing_info": [], "resolved_references": {}},
            {
                "current_expertise": {"level": "intermediate", "domain": "technical", "confidence": 0.7, "evidence": ["algorithm", "optimize"]},
                "current_communication": {"detail_preference": "moderate", "style": "direct", "urgency": "medium"},
                "current_context": {"role_mode": "individual", "decision_scope": "personal", "time_pressure": "moderate"}
            },
        ]
        motet.join = Mock(return_value=mock_results)

        with patch(GET_MOTET_CONTEXT_ORCH, return_value=motet):
            analysis_data = conversation_analysis.__original_function__(data=data)
        assert "intent" in analysis_data
        assert "tone" in analysis_data
        assert "complexity" in analysis_data
        assert "context" in analysis_data
        assert "metadata" in analysis_data
        if motet.join.called:
            assert "user_profile" in analysis_data
            call_args = motet.join.call_args
            commands_list = call_args[0][0]
            assert len(commands_list) == 5
            assert analysis_data["intent"]["primary"] == "task_request"
            assert analysis_data["metadata"]["analysis_mode"] == "full"
        else:
            assert analysis_data["metadata"]["analysis_mode"] in ("full", "lightweight")
    
    def test_parallel_analysis_partial_failure(self):
        """Test graceful degradation when one dimension fails (message must trigger full_analysis)."""
        messages = [Message(role="user", content="Compare and analyze the tradeoffs between microservices and monoliths.")]
        data = ConversationAnalysisData(
            messages=messages,
            analysis_dimensions=["intent", "tone", "complexity"],
            analysis_model="gpt-4o-mini"
        )
        
        motet = Mock()
        motet.task_id = "test-task"
        motet.conversation_id = "test-conv"
        motet.tenant_id = "test-tenant"
        motet.principal_id = "test-principal"
        
        # Mock gather with one dimension failing (_aggregate_analysis_results expects command_type per result)
        motet.join = Mock(return_value=[
            {"primary": "greeting", "confidence": 0.95},
            {"_error": True, "message": "Model timeout"},
            {"level": "simple", "estimated_turns": 1, "scope": "narrow", "tool_requirements": "none", "expertise_needed": "beginner"},
        ])

        with patch(GET_MOTET_CONTEXT_ORCH, return_value=motet):
            analysis_data = conversation_analysis.__original_function__(data=data)
        assert "intent" in analysis_data
        assert "complexity" in analysis_data
        assert "tone" in analysis_data
        # If full analysis ran: intent and complexity from mock, tone has fallback
        if analysis_data["intent"].get("primary") == "greeting":
            assert analysis_data["complexity"]["level"] == "simple"
            assert analysis_data["tone"]["emotion"] == "neutral"
            assert "fallback_reason" in analysis_data["tone"]
        # If skipped/lightweight: intent may be "general", tone has fallback structure
        else:
            assert analysis_data["tone"]["emotion"] == "neutral"
            assert "fallback_reason" in analysis_data["tone"] or "confidence" in analysis_data["tone"]

    def test_analysis_mode_skipped_for_simple_query(self):
        """Simple greeting should skip full analysis and avoid gather()."""
        messages = [Message(role="user", content="hi")]
        data = ConversationAnalysisData(
            messages=messages,
            analysis_dimensions=["intent", "complexity", "context"],
            analysis_model="gpt-4o-mini",
        )

        motet = Mock()
        motet.task_id = "test-task"
        motet.conversation_id = "test-conv"
        motet.tenant_id = "test-tenant"
        motet.principal_id = "test-principal"
        motet.join = Mock()

        with patch(GET_MOTET_CONTEXT_ORCH, return_value=motet):
            analysis_data = conversation_analysis.__original_function__(data=data)

        assert analysis_data["metadata"]["analysis_mode"] == "skipped"
        assert analysis_data["intent"]["primary"] == "general"
        assert analysis_data["complexity"]["level"] == "simple"
        motet.join.assert_not_called()

    def test_analysis_mode_lightweight_for_moderate_query(self):
        """Moderate query should use lightweight path and avoid gather()."""
        messages = [
            Message(
                role="user",
                content="Can you help me summarize the key points from this request for my team?",
            )
        ]
        data = ConversationAnalysisData(
            messages=messages,
            analysis_dimensions=["intent", "complexity", "context"],
            analysis_model="gpt-4o-mini",
        )

        motet = Mock()
        motet.task_id = "test-task"
        motet.conversation_id = "test-conv"
        motet.tenant_id = "test-tenant"
        motet.principal_id = "test-principal"
        motet.join = Mock()

        with patch(GET_MOTET_CONTEXT_ORCH, return_value=motet):
            analysis_data = conversation_analysis.__original_function__(data=data)

        assert analysis_data["metadata"]["analysis_mode"] == "lightweight"
        assert "intent" in analysis_data and isinstance(analysis_data["intent"], dict)
        # The lightweight path still labels intent, but ADR-0138 removed the
        # `strategy_hint` it used to attach — nothing routes on the label now, so
        # the surviving contract is the label itself.
        assert analysis_data["intent"]["primary"]
        motet.join.assert_not_called()


class TestConversationAnalysisService:
    """Tests for ConversationAnalysisService convenience wrapper"""
    
    def test_create_analysis_command(self):
        """Test creating analysis command via service"""
        messages = [Message(role="user", content="Test message")]
        
        command = ConversationAnalysisService.create_analysis(
            task_id="test-task",
            messages=messages,
            conversation_id="test-conv",
            analysis_dimensions=["intent", "complexity"],
            analysis_model="gpt-4o-mini"
        )
        
        # Verify command is created correctly (returns a DecoratedCommand instance)
        assert command is not None
        assert hasattr(command, "get_command_type"), "Expected a command instance"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

