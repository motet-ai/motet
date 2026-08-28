"""
Motet - Intent Analysis Command

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Intent classification command using LLM for conversation analysis.
    Identifies primary user intent and recommends reasoning strategy.

Dependencies:
    - structlog: Structured logging
    - typing: Type hints
    - Decorator command system

Usage:
    from motet.core.commands.builtin.conversation_analysis.intent_analysis import intent_analysis
    
    command = intent_analysis(
        task_id="task-123",
        conversation_id="conv-456",
        data=IntentAnalysisData(
            user_text="Help me solve this problem"
        )
    )
    result = motet.do(command)

Notes:
    - Focused analysis of user intent and strategy recommendation
    - Uses LLM for contextual understanding
    - Includes fallback for LLM failures
    - Part of parallel conversation analysis system
    - Refactored to use concise command composition helpers (motet.do)
    - Leaves provider/model unset so the operator's configured stack model is
      used; pin analysis_model/analysis_provider only to override it
    - Attaches an output contract built from IntentAnalysisResult so
      capable adapters constrain generation to the schema
"""

import structlog
import json
from typing import Dict, Any
from motet import motet
from motet.core.commands.decorator import get_motet_context
from motet.core.workers.observers import EventPriority
from motet.core.commands.builtin.conversation_analysis.data_classes import (
    IntentAnalysisData,
    IntentAnalysisResult,
)
from motet.core.commands.builtin.model import model_inference, ModelInferenceData
from motet.core.types import Message, OutputContract

logger = structlog.get_logger(__name__)


@motet.command(
    description="Classify user intent from the latest message and conversation context using an LLM (goals, request type, next-action hints).",
    timeout_seconds=30, priority=EventPriority.NORMAL)
def intent_analysis(
    data: IntentAnalysisData) -> Dict[str, Any]:
    """
    Analyze user intent using LLM.
    
    Focused analysis of:
    - Primary intent classification
    - Confidence scoring
    - Strategy recommendation
    
    Args:
        data: IntentAnalysisData with user text and context
        
    Returns:
        Intent analysis result with primary and confidence
    """
    motet = get_motet_context()
    
    logger.info("intent_analysis_started", text_length=len(data.user_text))
    
    try:
        # Build intent-specific prompt
        prompt = _build_intent_prompt(data.user_text, data.conversation_context)
        
        # Create model inference command
        analysis_messages = [
            Message(role="system", content="You are an expert at classifying user intent. Provide analysis in exact JSON format requested."),
            Message(role="user", content=prompt)
        ]
        
        # Provider/model stay absent unless the caller pinned them, so
        # model_inference resolves the operator's configured stack defaults.
        model_settings: Dict[str, Any] = {
            "temperature": 0.1,
            "max_tokens": 300,
            # Analysis classifiers do not need provider-native built-in tools.
            "enable_tools": False,
        }
        if data.analysis_provider:
            model_settings["provider"] = data.analysis_provider
        if data.analysis_model:
            model_settings["model_name"] = data.analysis_model
        
        # Execute model inference (ADR-0052: use motet.do() for automatic unwrapping)
        try:
            response_data = motet.do(
                model_inference,
                data=ModelInferenceData(
                    messages=analysis_messages,
                    model_settings=model_settings,
                    # ADR-0114: adapters that can constrain output will (schema on
                    # OpenAI/Gemini, GBNF locally, forced tool on Anthropic).
                    # Others degrade to the prompt and the fallback below.
                    output_contract=OutputContract(
                        format="json",
                        json_schema=IntentAnalysisResult.model_json_schema(),
                    ),
                )
            )
            
            response_content = response_data.get("content", "")
            
            # Parse JSON
            json_content = response_content.strip()
            if json_content.startswith('```json'):
                json_content = json_content[7:]
                if json_content.endswith('```'):
                    json_content = json_content[:-3]
                json_content = json_content.strip()
            elif json_content.startswith('```'):
                json_content = json_content[3:]
                if json_content.endswith('```'):
                    json_content = json_content[:-3]
                json_content = json_content.strip()
            
            intent_data = json.loads(json_content)
            
            logger.info(
                "intent_analysis_success",
                intent=intent_data.get("primary"),
                confidence=intent_data.get("confidence"),
            )
            
            return intent_data
        except Exception as e:
            # Fallback on model inference failure
            logger.warning("intent_analysis_model_failed", error=str(e))
            fallback = _create_intent_fallback(data.user_text)
            return fallback
    
    except Exception as e:
        logger.error("intent_analysis_failed", error=str(e), exc_info=True)
        fallback = _create_intent_fallback(data.user_text)
        return fallback


def _build_intent_prompt(text: str, conversation_context) -> str:
    """Build intent-specific analysis prompt"""
    
    context_str = ""
    if conversation_context:
        context_messages = []
        for msg in conversation_context[-10:]:  # Last 10 messages for context
            role_prefix = "User" if msg.role == "user" else "Assistant"
            context_messages.append(f"{role_prefix}: {msg.content}")
        if context_messages:
            context_str = f"\n\nConversation History:\n" + "\n".join(context_messages)
    
    return f"""Analyze this user message: "{text}"{context_str}

Provide analysis in this exact JSON format:
{{
  "primary": "greeting|question|research|brainstorm|collaborate|analyze|plan|compare|task_request|context_question",
  "confidence": 0.0-1.0
}}

Intent Guidelines:
- greeting: Simple greetings and social interactions
- question: Information-seeking queries with context awareness
- research: Information gathering and investigation tasks
- brainstorm: Creative ideation and alternative generation
- collaborate: Cooperative work and input seeking
- analyze: Deep analysis and evaluation tasks
- plan: Strategic planning and step-by-step development
- compare: Comparative analysis and trade-off evaluation
- task_request: Action-oriented requests and execution
- context_question: Questions dependent on conversation history

Consider conversation history when classifying. Resolve pronouns and implicit references using context."""


def _create_intent_fallback(text: str) -> Dict[str, Any]:
    """Create fallback intent analysis using heuristics"""
    text_lower = text.lower().strip()
    
    intent = "task_request"

    if any(greeting in text_lower for greeting in ["hi", "hello", "hey", "good morning", "good afternoon"]):
        intent = "greeting"
    elif text_lower.endswith("?"):
        intent = "question"
    elif any(word in text_lower for word in ["brainstorm", "ideas", "alternatives", "options"]):
        intent = "brainstorm"

    return {
        "primary": intent,
        "confidence": 0.3,  # Low confidence for fallback
        "fallback": True
    }

