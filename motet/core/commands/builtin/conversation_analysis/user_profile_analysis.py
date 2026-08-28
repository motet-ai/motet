"""
Motet - User Profile Analysis Command

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Per-turn user profile classification command using LLM.
    Identifies expertise level, communication preferences, and context indicators.

Dependencies:
    - structlog: Structured logging
    - typing: Type hints
    - Decorator command system

Usage:
    from motet.core.commands.builtin.conversation_analysis.user_profile_analysis import user_profile_analysis
    
    command = user_profile_analysis(
        task_id="task-123",
        conversation_id="conv-456",
        data=UserProfileAnalysisData(
            user_text="Can you help me optimize this algorithm?",
            analysis_model="gpt-4o-mini"
        )
    )
    result = motet.do(command)

Notes:
    - Per-turn classification (not persistent storage)
    - Privacy-friendly approach
    - Uses LLM for nuanced profile understanding
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
    UserProfileAnalysisData,
    UserProfileAnalysisResult,
)
from motet.core.commands.builtin.model import model_inference, ModelInferenceData
from motet.core.types import Message, OutputContract

logger = structlog.get_logger(__name__)


@motet.command(
    description="Infer user-profile signals from conversation (preferences, expertise, constraints) for personalization.",
    timeout_seconds=30, priority=EventPriority.NORMAL)
def user_profile_analysis(
    data: UserProfileAnalysisData) -> Dict[str, Any]:
    """
    Analyze user profile indicators using LLM.
    
    Focused analysis of:
    - Current expertise level and domain
    - Current communication preferences
    - Current context (role mode, decision scope, time pressure)
    
    Args:
        data: UserProfileAnalysisData with user text and context
        
    Returns:
        User profile analysis result
    """
    motet = get_motet_context()
    
    logger.info("user_profile_analysis_started", text_length=len(data.user_text))
    
    try:
        # Build profile-specific prompt
        prompt = _build_profile_prompt(data.user_text, data.conversation_context)
        
        # Create model inference command
        analysis_messages = [
            Message(role="system", content="You are an expert at understanding user expertise, communication style, and context. Provide analysis in exact JSON format."),
            Message(role="user", content=prompt)
        ]
        
        # Provider/model stay absent unless the caller pinned them, so
        # model_inference resolves the operator's configured stack defaults.
        model_settings: Dict[str, Any] = {
            "temperature": 0.1,
            "max_tokens": 400,
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
                        json_schema=UserProfileAnalysisResult.model_json_schema(),
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
            
            profile_data = json.loads(json_content)
            
            logger.info(
                "user_profile_analysis_success",
                expertise_level=profile_data.get("current_expertise", {}).get("level"),
                domain=profile_data.get("current_expertise", {}).get("domain")
            )
            
            return profile_data
        except Exception as e:
            # Fallback on model inference failure
            logger.warning("user_profile_analysis_model_failed", error=str(e))
            fallback = _create_profile_fallback(data.user_text)
            return fallback
    
    except Exception as e:
        logger.error("user_profile_analysis_failed", error=str(e), exc_info=True)
        fallback = _create_profile_fallback(data.user_text)
        return fallback


def _build_profile_prompt(text: str, conversation_context) -> str:
    """Build profile-specific analysis prompt"""
    
    context_str = ""
    if conversation_context:
        context_messages = []
        for msg in conversation_context[-10:]:
            role_prefix = "User" if msg.role == "user" else "Assistant"
            context_messages.append(f"{role_prefix}: {msg.content}")
        if context_messages:
            context_str = f"\n\nConversation History:\n" + "\n".join(context_messages)
    
    return f"""Analyze the user profile indicators from this message: "{text}"{context_str}

Provide analysis in this exact JSON format:
{{
  "current_expertise": {{
    "level": "beginner|intermediate|expert",
    "domain": "business|technical|creative|analytical|general",
    "confidence": 0.0-1.0,
    "evidence": ["indicator1", "indicator2"]
  }},
  "current_communication": {{
    "detail_preference": "brief|moderate|comprehensive",
    "style": "direct|collaborative|exploratory|structured",
    "urgency": "low|medium|high"
  }},
  "current_context": {{
    "role_mode": "individual|presenting|managing|learning",
    "decision_scope": "personal|team|organizational",
    "time_pressure": "relaxed|moderate|urgent"
  }}
}}

Guidelines:
- current_expertise: Indicators of domain knowledge and skill level
- current_communication: Preferences for interaction style
- current_context: Situational indicators (not persistent traits)
- This is per-turn analysis, not persistent profiling
- Look for evidence in language, terminology, and framing

Analyze only observable indicators from this turn."""


def _create_profile_fallback(text: str) -> Dict[str, Any]:
    """Create fallback profile analysis using heuristics"""
    text_lower = text.lower().strip()
    word_count = len(text.split())
    
    # Default profile
    expertise_level = "intermediate"
    domain = "general"
    detail_preference = "moderate"
    style = "direct"
    urgency = "medium"
    role_mode = "individual"
    decision_scope = "personal"
    time_pressure = "moderate"
    
    # Expertise indicators
    if any(term in text_lower for term in [
        "algorithm", "optimization", "architecture", "implementation", "technical", "api", "database"
    ]):
        domain = "technical"
        expertise_level = "intermediate"
    
    if any(term in text_lower for term in [
        "beginner", "learning", "new to", "how do i", "what is", "explain"
    ]):
        expertise_level = "beginner"
        role_mode = "learning"
    
    if any(term in text_lower for term in [
        "advanced", "complex", "sophisticated", "optimize", "performance", "scalability"
    ]):
        expertise_level = "expert"
    
    # Communication style
    if word_count > 50:
        detail_preference = "comprehensive"
        style = "structured"
    elif word_count < 10:
        detail_preference = "brief"
        style = "direct"
    
    if "?" in text and text.count("?") > 1:
        style = "exploratory"
    
    # Context indicators
    if any(term in text_lower for term in ["urgent", "asap", "immediately", "quickly", "now"]):
        urgency = "high"
        time_pressure = "urgent"
    
    if any(term in text_lower for term in ["team", "we", "our", "colleagues"]):
        decision_scope = "team"
        role_mode = "managing"
    
    if any(term in text_lower for term in ["presentation", "meeting", "stakeholders", "board"]):
        role_mode = "presenting"
        decision_scope = "organizational"
    
    return {
        "current_expertise": {
            "level": expertise_level,
            "domain": domain,
            "confidence": 0.3,  # Low confidence for fallback
            "evidence": ["fallback_heuristics"]
        },
        "current_communication": {
            "detail_preference": detail_preference,
            "style": style,
            "urgency": urgency
        },
        "current_context": {
            "role_mode": role_mode,
            "decision_scope": decision_scope,
            "time_pressure": time_pressure
        },
        "fallback": True
    }

