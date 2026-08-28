"""
Motet - Tone Analysis Command

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Tone and emotional state analysis command using LLM.
    Identifies emotional tone, urgency, satisfaction, and communication style.

Dependencies:
    - structlog: Structured logging
    - typing: Type hints
    - Decorator command system

Usage:
    from motet.core.commands.builtin.conversation_analysis.tone_analysis import tone_analysis
    
    command = tone_analysis(
        task_id="task-123",
        conversation_id="conv-456",
        data=ToneAnalysisData(
            user_text="I'm frustrated with this issue",
            analysis_model="gpt-4o-mini"
        )
    )
    result = motet.do(command)

Notes:
    - Analyzes emotional state and communication style
    - Uses LLM for nuanced tone understanding
    - Includes fallback for LLM failures
    - Part of parallel conversation analysis system
    - Refactored to use concise command composition helpers (motet.do)
    - Leaves provider/model unset so the operator's configured stack model is
      used; pin analysis_model/analysis_provider only to override it
    - Attaches an output contract so capable adapters constrain
      generation to the result schema
"""

import structlog
import json
from typing import Dict, Any
from motet import motet
from motet.core.commands.decorator import get_motet_context
from motet.core.workers.observers import EventPriority
from motet.core.commands.builtin.conversation_analysis.data_classes import (
    ToneAnalysisData,
    ToneAnalysisResult,
)
from motet.core.commands.builtin.model import model_inference, ModelInferenceData
from motet.core.types import Message, OutputContract

logger = structlog.get_logger(__name__)


@motet.command(
    description="Detect user tone and emotional state from the message to adapt reply style and urgency.",
    timeout_seconds=30, priority=EventPriority.NORMAL)
def tone_analysis(
    data: ToneAnalysisData) -> Dict[str, Any]:
    """
    Analyze user tone and emotional state using LLM.
    
    Focused analysis of:
    - Emotional state (frustrated, excited, confused, etc.)
    - Urgency level
    - Satisfaction level
    - Communication style
    
    Args:
        data: ToneAnalysisData with user text and context
        
    Returns:
        Tone analysis result with emotion, urgency, satisfaction, style, confidence
    """
    motet = get_motet_context()
    
    logger.info("tone_analysis_started", text_length=len(data.user_text))
    
    try:
        # Build tone-specific prompt
        prompt = _build_tone_prompt(data.user_text, data.conversation_context)
        
        # Create model inference command
        analysis_messages = [
            Message(role="system", content="You are an expert at analyzing emotional tone and communication style. Provide analysis in exact JSON format."),
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
                        json_schema=ToneAnalysisResult.model_json_schema(),
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
            
            tone_data = json.loads(json_content)
            
            logger.info(
                "tone_analysis_success",
                emotion=tone_data.get("emotion"),
                urgency=tone_data.get("urgency")
            )
            
            return tone_data
        except Exception as e:
            # Fallback on model inference failure
            logger.warning("tone_analysis_model_failed", error=str(e))
            fallback = _create_tone_fallback(data.user_text)
            return fallback
    
    except Exception as e:
        logger.error("tone_analysis_failed", error=str(e), exc_info=True)
        fallback = _create_tone_fallback(data.user_text)
        return fallback


def _build_tone_prompt(text: str, conversation_context) -> str:
    """Build tone-specific analysis prompt"""
    
    context_str = ""
    if conversation_context:
        context_messages = []
        for msg in conversation_context[-10:]:
            role_prefix = "User" if msg.role == "user" else "Assistant"
            context_messages.append(f"{role_prefix}: {msg.content}")
        if context_messages:
            context_str = f"\n\nConversation History:\n" + "\n".join(context_messages)
    
    return f"""Analyze the tone and emotional state of this message: "{text}"{context_str}

Provide analysis in this exact JSON format:
{{
  "emotion": "frustrated|excited|confused|confident|neutral|anxious|satisfied",
  "urgency": "low|medium|high",
  "satisfaction": "low|medium|high",
  "communication_style": "direct|collaborative|exploratory|structured",
  "confidence": 0.0-1.0
}}

Guidelines:
- emotion: Primary emotional state expressed
- urgency: How urgent or time-sensitive the request is
- satisfaction: User's satisfaction level with interaction
- communication_style: How the user prefers to communicate
- confidence: Your confidence in this assessment

Consider conversation history for emotional progression."""


def _create_tone_fallback(text: str) -> Dict[str, Any]:
    """Create fallback tone analysis using heuristics"""
    text_lower = text.lower().strip()
    
    emotion = "neutral"
    urgency = "medium"
    satisfaction = "medium"
    style = "direct"
    
    # Simple heuristics
    if any(word in text_lower for word in ["urgent", "asap", "immediately", "now", "quickly"]):
        urgency = "high"
    if any(word in text_lower for word in ["frustrated", "annoyed", "angry", "disappointed"]):
        emotion = "frustrated"
        satisfaction = "low"
    elif any(word in text_lower for word in ["excited", "great", "amazing", "wonderful"]):
        emotion = "excited"
        satisfaction = "high"
    elif any(word in text_lower for word in ["confused", "unclear", "don't understand"]):
        emotion = "confused"
    
    if "?" in text and text.count("?") > 1:
        style = "exploratory"
    
    return {
        "emotion": emotion,
        "urgency": urgency,
        "satisfaction": satisfaction,
        "communication_style": style,
        "confidence": 0.3,  # Low confidence for fallback
        "fallback": True
    }

