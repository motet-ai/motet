"""
Motet SDK - Content Review Example: Grammar Review Command

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-03-18

Description:
Analyze content for grammar, spelling, punctuation, and sentence structure
issues using LLM inference.  Returns structured findings with severity
levels so downstream commands can prioritize fixes.

Dependencies:
- motet_sdk: command decorator and MotetContext typing
- pydantic: structured command inputs

Usage:
  content-review.review_grammar(content="Your text here...")

Notes:
- One of four review perspectives used by coordinate_reviews.
- Called via motet.join() alongside review_tone and review_accuracy.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List

from pydantic import Field

from motet_sdk import BaseCommandData, MotetContext, WorkerCapability, motet


class ReviewGrammarData(BaseCommandData):
    """Input for content-review.review_grammar."""

    content: str = Field(..., description="Content to review for grammar issues")
    provider: str = Field(default="openai", description="LLM provider")
    model_name: str = Field(default="gpt-4o-mini", description="LLM model name")


_GRAMMAR_PROMPT = """\
You are a professional copy editor.  Review the TEXT below for grammar, \
spelling, punctuation, and sentence structure issues.

For each issue found, provide:
- "issue": brief description
- "severity": "error" | "warning" | "suggestion"
- "original": the problematic text
- "fix": the corrected text

Output ONLY a JSON object with:
- "issues": array of issue objects
- "summary": one-sentence overall assessment
- "score": integer 1-10 (10 = flawless)

TEXT:
{content}
"""


def _parse_review(text: str) -> Dict[str, Any]:
    """Extract JSON review from LLM output with fallback."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group())
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
    return {"issues": [], "summary": "Unable to parse review.", "score": 5}


@motet.command(
    timeout_seconds=60,
    required_capabilities=[WorkerCapability.MODEL_INFERENCE],
)
def review_grammar(data: ReviewGrammarData, motet: MotetContext) -> Dict[str, Any]:
    """Review content for grammar, spelling, and punctuation issues."""
    result = motet.models.infer(
        provider=data.provider,
        model_name=data.model_name,
        messages=[{"role": "user", "content": _GRAMMAR_PROMPT.format(content=data.content)}],
        temperature=0.2,
        max_tokens=1500,
    )

    review = _parse_review(result.get("content") or result.get("response") or "")

    return {
        "perspective": "grammar",
        "issues": review.get("issues", []),
        "summary": review.get("summary", ""),
        "score": review.get("score", 5),
        "issue_count": len(review.get("issues", [])),
    }
