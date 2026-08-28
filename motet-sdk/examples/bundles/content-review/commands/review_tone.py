"""
Motet SDK - Content Review Example: Tone Review Command

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-03-18

Description:
Evaluate content tone, voice, and audience appropriateness using LLM
inference.  Assesses whether the writing style matches the intended
audience and suggests adjustments.

Dependencies:
- motet_sdk: command decorator and MotetContext typing
- pydantic: structured command inputs

Usage:
  content-review.review_tone(content="...", audience="technical developers")

Notes:
- One of four review perspectives used by coordinate_reviews.
- The audience parameter shapes the evaluation criteria — a blog post
  for executives is evaluated differently than API docs for developers.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict

from pydantic import Field

from motet_sdk import BaseCommandData, MotetContext, WorkerCapability, motet


class ReviewToneData(BaseCommandData):
    """Input for content-review.review_tone."""

    content: str = Field(..., description="Content to review for tone")
    audience: str = Field(default="general", description="Target audience")
    provider: str = Field(default="openai", description="LLM provider")
    model_name: str = Field(default="gpt-4o-mini", description="LLM model name")


_TONE_PROMPT = """\
You are a communications specialist.  Evaluate the TONE of the TEXT below \
for the intended AUDIENCE.

Assess:
- Voice consistency (formal/informal, active/passive)
- Audience appropriateness (jargon level, assumed knowledge)
- Readability (sentence length, paragraph structure)
- Engagement (compelling vs dry, call-to-action presence)

Output ONLY a JSON object with:
- "tone": detected tone in 2-3 words (e.g. "formal and authoritative")
- "audience_fit": "excellent" | "good" | "fair" | "poor"
- "suggestions": array of specific improvement suggestions
- "summary": one-sentence overall assessment
- "score": integer 1-10 (10 = perfect for audience)

AUDIENCE: {audience}

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
    return {"tone": "unknown", "audience_fit": "fair", "suggestions": [], "summary": "Unable to parse review.", "score": 5}


@motet.command(
    timeout_seconds=60,
    required_capabilities=[WorkerCapability.MODEL_INFERENCE],
)
def review_tone(data: ReviewToneData, motet: MotetContext) -> Dict[str, Any]:
    """Evaluate content tone and audience appropriateness."""
    result = motet.models.infer(
        provider=data.provider,
        model_name=data.model_name,
        messages=[{"role": "user", "content": _TONE_PROMPT.format(content=data.content, audience=data.audience)}],
        temperature=0.2,
        max_tokens=1500,
    )

    review = _parse_review(result.get("content") or result.get("response") or "")

    return {
        "perspective": "tone",
        "tone": review.get("tone", "unknown"),
        "audience_fit": review.get("audience_fit", "fair"),
        "suggestions": review.get("suggestions", []),
        "summary": review.get("summary", ""),
        "score": review.get("score", 5),
    }
