"""
Motet - Complexity Analysis Command

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Task complexity assessment command using LLM.
    Identifies complexity level, estimated turns, scope, tool requirements, and expertise needed.

Dependencies:
    - structlog: Structured logging
    - typing: Type hints
    - Decorator command system

Usage:
    from motet.core.commands.builtin.conversation_analysis.complexity_analysis import complexity_analysis
    
    command = complexity_analysis(
        task_id="task-123",
        conversation_id="conv-456",
        data=ComplexityAnalysisData(
            user_text="Compare economic policies of France and Germany",
            analysis_model="gpt-4o-mini"
        )
    )
    result = motet.do(command)

Notes:
    - Assesses task complexity and resource requirements
    - Uses LLM for contextual complexity understanding
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
    ComplexityAnalysisData,
    ComplexityAnalysisResult,
)
from motet.core.commands.builtin.model import model_inference, ModelInferenceData
from motet.core.types import Message, OutputContract

logger = structlog.get_logger(__name__)


@motet.command(
    description="Estimate task complexity from the user message and context to guide reasoning strategy and tool budget.",
    timeout_seconds=30, priority=EventPriority.NORMAL)
def complexity_analysis(
    data: ComplexityAnalysisData) -> Dict[str, Any]:
    """
    Analyze task complexity using LLM.
    
    Focused analysis of:
    - Complexity level (simple/moderate/complex)
    - Estimated conversation turns needed
    - Scope (narrow/focused/broad/multi_domain)
    - Tool requirements (none/basic/advanced/specialized)
    - Expertise needed (beginner/intermediate/expert)
    
    Args:
        data: ComplexityAnalysisData with user text and context
        
    Returns:
        Complexity analysis result
    """
    motet = get_motet_context()
    
    logger.info("complexity_analysis_started", text_length=len(data.user_text))
    
    try:
        # Build complexity-specific prompt
        prompt = _build_complexity_prompt(data.user_text, data.conversation_context)
        
        # Create model inference command
        analysis_messages = [
            Message(role="system", content="You are an expert at assessing task complexity and resource requirements. Provide analysis in exact JSON format."),
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
                        json_schema=ComplexityAnalysisResult.model_json_schema(),
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
            
            complexity_data = json.loads(json_content)
            
            logger.info(
                "complexity_analysis_success",
                level=complexity_data.get("level"),
                estimated_turns=complexity_data.get("estimated_turns")
            )
            
            return complexity_data
        except Exception as e:
            # Fallback on model inference failure
            logger.warning("complexity_analysis_model_failed", error=str(e))
            fallback = _create_complexity_fallback(data.user_text)
            return fallback
    
    except Exception as e:
        logger.error("complexity_analysis_failed", error=str(e), exc_info=True)
        fallback = _create_complexity_fallback(data.user_text)
        return fallback


def _build_complexity_prompt(text: str, conversation_context) -> str:
    """Build complexity-specific analysis prompt"""
    
    context_str = ""
    if conversation_context:
        context_messages = []
        for msg in conversation_context[-10:]:
            role_prefix = "User" if msg.role == "user" else "Assistant"
            context_messages.append(f"{role_prefix}: {msg.content}")
        if context_messages:
            context_str = f"\n\nConversation History:\n" + "\n".join(context_messages)
    
    return f"""Analyze the complexity of this task: "{text}"{context_str}

Provide analysis in this exact JSON format:
{{
  "level": "simple|moderate|complex",
  "estimated_turns": 1-10,
  "scope": "narrow|focused|broad|multi_domain",
  "tool_requirements": "none|basic|advanced|specialized",
  "expertise_needed": "beginner|intermediate|expert"
}}

Guidelines:
- level: Overall complexity (simple: 1 step, moderate: 2-4 steps, complex: 5+ steps)
- estimated_turns: Expected back-and-forth exchanges needed
- scope: How broad the task spans (narrow: single topic, broad: multiple topics, multi_domain: cross-disciplinary)
- tool_requirements: External tools needed (none: conversation only, basic: simple lookups, advanced: complex tools, specialized: domain-specific tools)
- expertise_needed: Domain expertise required

Consider conversation history for context."""


def _create_complexity_fallback(text: str) -> Dict[str, Any]:
    """Create fallback complexity analysis using heuristics"""
    text_lower = text.lower().strip()
    word_count = len(text.split())
    
    # Simple heuristics based on query characteristics
    level = "moderate"  # Matches Complexity Literal type
    estimated_turns = 3
    scope = "focused"
    tool_requirements = "basic"
    expertise = "intermediate"
    
    # Adjust based on length
    if word_count < 10:
        level = "simple"  # Matches Complexity Literal type
        estimated_turns = 1
        scope = "narrow"
        tool_requirements = "none"
        expertise = "beginner"
    elif word_count > 30:
        level = "complex"  # Matches Complexity Literal type
        estimated_turns = 5
        scope = "broad"
        tool_requirements = "advanced"
        expertise = "expert"
    else:
        level = "moderate"  # Matches Complexity Literal type
    
    # Check for complexity indicators
    if any(word in text_lower for word in ["compare", "analyze", "evaluate", "comprehensive"]):
        level = "complex"  # Matches Complexity Literal type
        estimated_turns = min(estimated_turns + 2, 10)
        scope = "broad"
    
    if any(word in text_lower for word in ["search", "lookup", "find", "calculate"]):
        tool_requirements = "basic"
    
    if any(word in text_lower for word in ["data analysis", "research", "technical", "scientific"]):
        tool_requirements = "advanced"
        expertise = "expert"
    
    return {
        "level": level,
        "estimated_turns": estimated_turns,
        "scope": scope,
        "tool_requirements": tool_requirements,
        "expertise_needed": expertise,
        "fallback": True
    }

