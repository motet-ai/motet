"""
Motet - Conversation Analysis Module

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-22

Description:
    Modular conversation analysis system using parallel decorated commands.
    Exports main orchestrator, dimension commands, and service classes.

Dependencies:
    - All conversation analysis commands
    - Data classes for request/response models
    - Service classes for convenience

Usage:
    # Main orchestrator
    from motet.core.commands.builtin.conversation_analysis import (
        conversation_analysis,
        ConversationAnalysisService
    )
    
    # Individual dimensions (for testing or selective use)
    from motet.core.commands.builtin.conversation_analysis import (
        intent_analysis,
        tone_analysis,
        complexity_analysis,
        context_analysis,
        user_profile_analysis
    )
    
    # Data classes
    from motet.core.commands.builtin.conversation_analysis import (
        ConversationAnalysisData,
        IntentAnalysisData,
        ToneAnalysisData
    )

Notes:
    - Main entry point: conversation_analysis() orchestrator
    - All dimensions execute in parallel via motet.join()
    - Service class provides convenience wrapper for command creation
"""

from typing import List, Optional

# Export main orchestrator
from motet.core.commands.builtin.conversation_analysis.conversation_analysis import conversation_analysis

# Export individual dimension commands
from motet.core.commands.builtin.conversation_analysis.intent_analysis import intent_analysis
from motet.core.commands.builtin.conversation_analysis.tone_analysis import tone_analysis
from motet.core.commands.builtin.conversation_analysis.complexity_analysis import complexity_analysis
from motet.core.commands.builtin.conversation_analysis.context_analysis import context_analysis
from motet.core.commands.builtin.conversation_analysis.user_profile_analysis import user_profile_analysis

# Export data classes
from motet.core.commands.builtin.conversation_analysis.data_classes import (
    ConversationAnalysisData,
    IntentAnalysisData,
    ToneAnalysisData,
    ComplexityAnalysisData,
    ContextAnalysisData,
    UserProfileAnalysisData,
    IntentAnalysisResult,
    ToneAnalysisResult,
    ComplexityAnalysisResult,
    ContextAnalysisResult,
    UserProfileAnalysisResult
)

# Export types from core (for backward compatibility)
from motet.core.types import Message


class ConversationAnalysisService:
    """
    Service class for creating conversation analysis commands.
    
    Provides convenient API for creating conversation_analysis decorator-based
    commands with proper parameter handling and default values.
    """
    
    @staticmethod
    def create_analysis(
        task_id: str,
        messages: List[Message],
        conversation_id: str = "",
        conversation_context: Optional[List[Message]] = None,
        analysis_model: Optional[str] = None,
        analysis_dimensions: Optional[List[str]] = None,
        **distributed_params
    ):
        """
        Create a distributed conversation analysis command using the new parallel pattern.
        
        Args:
            task_id: Task identifier
            messages: Messages to analyze (latest must be from user)
            conversation_id: Conversation identifier
            conversation_context: Conversation history for context
            analysis_model: Model override; unset inherits the turn or stack
            analysis_dimensions: Dimensions to analyze (default: intent, context)
            **distributed_params: Additional distributed command parameters (tenant_id, principal_id, etc.)
        
        Returns:
            Decorated command instance ready for execution via motet.do()
        
        Example:
            ```python
            from motet.core.commands.builtin.conversation_analysis import ConversationAnalysisService
            from motet.core.types import Message
            
            # Create command
            command = ConversationAnalysisService.create_analysis(
                task_id="task-123",
                messages=[Message(role="user", content="Help me solve this")],
                conversation_id="conv-456",
                analysis_dimensions=["intent", "complexity", "context"]
            )
            
            # Execute via motet
            result = motet.do(command)
            
            # Access results
            if result.get("status") == "success":
                analysis_data = result.get("data", {})
                intent = analysis_data.get("intent", {})
                print(f"Intent: {intent.get('primary')}")
                print(f"Confidence: {intent.get('confidence')}")
            ```
        """
        # Build kwargs to avoid overriding Pydantic defaults with None
        data_kwargs = {
            "messages": messages,
            "conversation_context": conversation_context,
        }
        if analysis_model is not None:
            data_kwargs["analysis_model"] = analysis_model
        if analysis_dimensions is not None:
            data_kwargs["analysis_dimensions"] = analysis_dimensions
        
        command_data = ConversationAnalysisData(**data_kwargs)
        
        return conversation_analysis(
            task_id=task_id,
            data=command_data,
            conversation_id=conversation_id,
            **distributed_params
        )


# Export service class
__all__ = [
    # Main orchestrator
    "conversation_analysis",
    
    # Individual dimensions
    "intent_analysis",
    "tone_analysis",
    "complexity_analysis",
    "context_analysis",
    "user_profile_analysis",
    
    # Data classes
    "ConversationAnalysisData",
    "IntentAnalysisData",
    "ToneAnalysisData",
    "ComplexityAnalysisData",
    "ContextAnalysisData",
    "UserProfileAnalysisData",
    "IntentAnalysisResult",
    "ToneAnalysisResult",
    "ComplexityAnalysisResult",
    "ContextAnalysisResult",
    "UserProfileAnalysisResult",
    
    # Service class
    "ConversationAnalysisService",
    
    # Types
    "Message"
]

