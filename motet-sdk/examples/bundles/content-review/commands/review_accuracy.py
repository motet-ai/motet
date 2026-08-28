"""
Motet SDK - Content Review Example: Accuracy Review Command

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-03-18

Description:
Fact-check content by identifying claims, assessing their verifiability,
and flagging unsupported or potentially inaccurate assertions.  Uses LLM
inference to evaluate each claim's confidence level.

Dependencies:
- motet_sdk: command decorator and MotetContext typing
- pydantic: structured command inputs

Usage:
  content-review.review_accuracy(content="Our platform processes 1M requests/sec...")

Notes:
- One of four review perspectives used by coordinate_reviews.
- This is an LLM-based heuristic check, not a definitive fact-verification
  service.  Flagged claims should be verified by a human.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List

from pydantic import Field

from motet_sdk import BaseCommandData, MotetContext, WorkerCapability, motet


class ReviewAccuracyData(BaseCommandData):
    """Input for content-review.review_accuracy."""

    content: str = Field(..., description="Content to fact-check")
    provider: str = Field(default="openai", description="LLM provider")
    model_name: str = Field(default="gpt-4o-mini", description="LLM model name")


_ACCURACY_PROMPT = """\
You are a fact-checker.  Analyze the TEXT below and identify factual claims.

For each claim, assess:
- "claim": the factual statement
- "confidence": "verified" | "plausible" | "questionable" | "unsupported"
- "reason": why you assigned this confidence level

Output ONLY a JSON object with:
- "claims": array of claim objects
- "summary": one-sentence overall accuracy assessment
- "score": integer 1-10 (10 = all claims well-supported)

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
    return {"claims": [], "summary": "Unable to parse review.", "score": 5}


@motet.command(
    timeout_seconds=60,
    required_capabilities=[WorkerCapability.MODEL_INFERENCE],
)
def review_accuracy(data: ReviewAccuracyData, motet: MotetContext) -> Dict[str, Any]:
    """Fact-check content by evaluating claims and their support level."""
    result = motet.models.infer(
        provider=data.provider,
        model_name=data.model_name,
        messages=[{"role": "user", "content": _ACCURACY_PROMPT.format(content=data.content)}],
        temperature=0.2,
        max_tokens=1500,
    )

    review = _parse_review(result.get("content") or result.get("response") or "")
    claims = review.get("claims", [])
    flagged = [c for c in claims if c.get("confidence") in ("questionable", "unsupported")]

    return {
        "perspective": "accuracy",
        "claims": claims,
        "flagged_claims": flagged,
        "summary": review.get("summary", ""),
        "score": review.get("score", 5),
        "claim_count": len(claims),
        "flagged_count": len(flagged),
    }
