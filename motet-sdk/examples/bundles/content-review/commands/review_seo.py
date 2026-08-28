"""
Motet SDK - Content Review Example: SEO Review Command

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-03-18

Description:
Analyze content for search-engine optimization: keyword usage, heading
structure, readability for crawlers, and meta-description quality.

This command is intentionally used as the *optional* review in the
coordinate_reviews orchestrator to demonstrate motet.maybe() — the
pipeline succeeds even if SEO analysis is unavailable or fails.

Dependencies:
- motet_sdk: command decorator and MotetContext typing
- pydantic: structured command inputs

Usage:
  content-review.review_seo(content="...", content_type="blog_post")

Notes:
- In coordinate_reviews, this is called via motet.maybe() so that
  failure does not block the rest of the pipeline.
- In the workflow YAML, it runs as a regular step alongside the others.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict

from pydantic import Field

from motet_sdk import BaseCommandData, MotetContext, WorkerCapability, motet


class ReviewSeoData(BaseCommandData):
    """Input for content-review.review_seo."""

    content: str = Field(..., description="Content to review for SEO")
    content_type: str = Field(
        default="article",
        description="Content type (article, blog_post, landing_page, documentation)",
    )
    provider: str = Field(default="openai", description="LLM provider")
    model_name: str = Field(default="gpt-4o-mini", description="LLM model name")


_SEO_PROMPT = """\
You are an SEO specialist.  Analyze the TEXT below (a {content_type}) for \
search-engine optimization.

Evaluate:
- Keyword usage and density (are key terms present naturally?)
- Heading structure (H1/H2/H3 hierarchy if applicable)
- Readability for search crawlers (clear structure, descriptive text)
- Meta-description potential (could a good snippet be extracted?)
- Internal/external link opportunities

Output ONLY a JSON object with:
- "keywords": array of detected key terms
- "recommendations": array of specific SEO improvements
- "heading_structure": "good" | "fair" | "poor" | "none"
- "summary": one-sentence overall SEO assessment
- "score": integer 1-10 (10 = excellent SEO)

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
    return {"keywords": [], "recommendations": [], "heading_structure": "none", "summary": "Unable to parse review.", "score": 5}


@motet.command(
    timeout_seconds=60,
    required_capabilities=[WorkerCapability.MODEL_INFERENCE],
)
def review_seo(data: ReviewSeoData, motet: MotetContext) -> Dict[str, Any]:
    """Analyze content for search-engine optimization opportunities."""
    result = motet.models.infer(
        provider=data.provider,
        model_name=data.model_name,
        messages=[{"role": "user", "content": _SEO_PROMPT.format(content=data.content, content_type=data.content_type)}],
        temperature=0.2,
        max_tokens=1500,
    )

    review = _parse_review(result.get("content") or result.get("response") or "")

    return {
        "perspective": "seo",
        "keywords": review.get("keywords", []),
        "recommendations": review.get("recommendations", []),
        "heading_structure": review.get("heading_structure", "none"),
        "summary": review.get("summary", ""),
        "score": review.get("score", 5),
    }
