"""
Motet - Context Analysis Command

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Context dependency analysis command using LLM.
    Identifies missing information, clarification needs, and resolves references.

Dependencies:
    - structlog: Structured logging
    - typing: Type hints
    - Decorator command system

Usage:
    from motet.core.commands.builtin.conversation_analysis.context_analysis import context_analysis
    
    command = context_analysis(
        task_id="task-123",
        conversation_id="conv-456",
        data=ContextAnalysisData(
            user_text="Can you explain that again?",
            conversation_context=[...],
            analysis_model="gpt-4o-mini"
        )
    )
    result = motet.do(command)

Notes:
    - Identifies context dependencies and missing information
    - Uses LLM for reference resolution
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
    ContextAnalysisData,
    ContextAnalysisResult,
)
from motet.core.commands.builtin.model import model_inference, ModelInferenceData
from motet.core.types import Message, OutputContract

logger = structlog.get_logger(__name__)


@motet.command(
    description="Analyze which prior conversation context and dependencies matter for answering the current user request.",
    timeout_seconds=30, priority=EventPriority.NORMAL)
def context_analysis(
    data: ContextAnalysisData) -> Dict[str, Any]:
    """
    Analyze context dependencies using LLM.
    
    Focused analysis of:
    - Clarification needs
    - References to previous conversation
    - Missing information
    - Resolved pronoun references
    
    Args:
        data: ContextAnalysisData with user text and context
        
    Returns:
        Context analysis result
    """
    motet = get_motet_context()
    
    logger.info("context_analysis_started", text_length=len(data.user_text))
    
    try:
        # Build context-specific prompt
        prompt = _build_context_prompt(data.user_text, data.conversation_context)
        
        # Create model inference command
        analysis_messages = [
            Message(role="system", content="You are an expert at analyzing context dependencies and resolving references. Provide analysis in exact JSON format."),
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
                        json_schema=ContextAnalysisResult.model_json_schema(),
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
            
            context_data = json.loads(json_content)
            
            logger.info(
                "context_analysis_success",
                needs_clarification=context_data.get("needs_clarification"),
                references_previous=context_data.get("references_previous")
            )
            
            return context_data
        except Exception as e:
            # Fallback on model inference failure
            logger.warning("context_analysis_model_failed", error=str(e))
            fallback = _create_context_fallback(data.user_text, data.conversation_context)
            return fallback
    
    except Exception as e:
        logger.error("context_analysis_failed", error=str(e), exc_info=True)
        fallback = _create_context_fallback(data.user_text, data.conversation_context)
        return fallback


def _build_context_prompt(text: str, conversation_context) -> str:
    """Build context-specific analysis prompt"""
    
    context_str = ""
    if conversation_context:
        context_messages = []
        for msg in conversation_context[-10:]:
            role_prefix = "User" if msg.role == "user" else "Assistant"
            context_messages.append(f"{role_prefix}: {msg.content}")
        if context_messages:
            context_str = f"\n\nConversation History:\n" + "\n".join(context_messages)
    
    return f"""Analyze the context dependencies of this message: "{text}"{context_str}

Provide analysis in this exact JSON format:
{{
  "needs_clarification": true|false,
  "references_previous": true|false,
  "missing_info": ["item1", "item2"],
  "resolved_references": {{"this": "specific reference", "that": "another reference"}}
}}

Guidelines:
- needs_clarification: Whether the request is ambiguous or needs more information
- references_previous: Whether it references earlier conversation (pronouns, "that", "it", etc.)
- missing_info: List of information needed to fully address the request
- resolved_references: Map pronouns/references to what they refer to from context

Analyze both explicit and implicit references."""


def _create_context_fallback(text: str, conversation_context) -> Dict[str, Any]:
    """Create fallback context analysis using heuristics"""
    text_lower = text.lower().strip()
    word_count = len(text.split())
    
    # Check for reference indicators
    references_previous = any(word in text_lower for word in [
        "that", "this", "it", "them", "those", "these",
        "again", "also", "too", "same", "previous", "earlier"
    ])
    
    # Check for clarification needs
    needs_clarification = any(phrase in text_lower for phrase in [
        "what do you mean", "unclear", "don't understand",
        "explain", "clarify", "confused", "elaborate"
    ])
    
    # Check for vague terms that might need more info
    has_context = conversation_context and len(conversation_context) > 0
    missing_info = []
    
    if "?" in text and word_count < 5:
        needs_clarification = True
    
    if any(word in text_lower for word in ["who", "what", "where", "when", "why", "how"]) and not has_context:
        missing_info.append("More context needed for question")
    
    resolved_references = {}
    if references_previous and has_context:
        # Try to find what "that" or "it" might refer to
        if "that" in text_lower or "it" in text_lower:
            # Get last substantive message
            for msg in reversed(conversation_context):
                if msg.role == "assistant" and len(msg.content) > 20:
                    resolved_references["that"] = msg.content[:100] + "..."
                    break
    
    return {
        "needs_clarification": needs_clarification,
        "references_previous": references_previous,
        "missing_info": missing_info,
        "resolved_references": resolved_references,
        "fallback": True
    }

