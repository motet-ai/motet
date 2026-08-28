"""
Motet SDK - Content Review Example: Feedback Synthesis Command

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-03-18

Description:
Combine feedback from multiple review perspectives (grammar, tone,
accuracy, SEO) into a single prioritized feedback report.  Uses LLM
inference to resolve conflicting suggestions and rank action items.

Dependencies:
- motet_sdk: command decorator and MotetContext typing
- pydantic: structured command inputs

Usage:
  Called by coordinate_reviews via motet.do() after all reviews complete:
    feedback = motet.do(synthesize_feedback, data=SynthesizeFeedbackData(...))

Notes:
- Receives heterogeneous review outputs and produces a unified report.
- Prioritizes issues by severity and impact across all perspectives.
- SEO review data is optional (may be None if motet.maybe() skipped it).
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from pydantic import Field

from motet_sdk import BaseCommandData, MotetContext, WorkerCapability, motet


class SynthesizeFeedbackData(BaseCommandData):
    """Input for content-review.synthesize_feedback."""

    content: str = Field(..., description="Original content that was reviewed")
    grammar: Dict[str, Any] = Field(default_factory=dict, description="Grammar review results")
    tone: Dict[str, Any] = Field(default_factory=dict, description="Tone review results")
    accuracy: Dict[str, Any] = Field(default_factory=dict, description="Accuracy review results")
    seo: Optional[Dict[str, Any]] = Field(default=None, description="SEO review results (may be None)")
    provider: str = Field(default="openai", description="LLM provider")
    model_name: str = Field(default="gpt-4o-mini", description="LLM model name")


_SYNTHESIS_PROMPT = """\
You are a senior editor synthesizing feedback from multiple reviewers.

Below are review results from four perspectives on the same content.  \
Create a unified feedback report that:
1. Prioritizes the most impactful issues first
2. Resolves any conflicting suggestions between reviewers
3. Groups related feedback together
4. Provides clear, actionable recommendations

Output a well-structured markdown report with:
- **Overall Assessment** (2-3 sentences)
- **Priority Fixes** (numbered list of must-fix items)
- **Improvements** (numbered list of should-fix items)
- **Minor Suggestions** (bullet list of nice-to-have changes)
- **Scores** (table of reviewer scores)

GRAMMAR REVIEW:
{grammar}

TONE REVIEW:
{tone}

ACCURACY REVIEW:
{accuracy}

SEO REVIEW:
{seo}
"""


@motet.command(
    timeout_seconds=90,
    required_capabilities=[WorkerCapability.MODEL_INFERENCE],
)
def synthesize_feedback(data: SynthesizeFeedbackData, motet: MotetContext) -> Dict[str, Any]:
    """Synthesize multiple review perspectives into a unified feedback report."""
    seo_text = json.dumps(data.seo, indent=2) if data.seo else "(SEO review was not available)"

    prompt = _SYNTHESIS_PROMPT.format(
        grammar=json.dumps(data.grammar, indent=2),
        tone=json.dumps(data.tone, indent=2),
        accuracy=json.dumps(data.accuracy, indent=2),
        seo=seo_text,
    )

    result = motet.models.infer(
        provider=data.provider,
        model_name=data.model_name,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=2000,
    )

    report = result.get("content") or result.get("response") or ""

    scores = {
        "grammar": data.grammar.get("score", 0),
        "tone": data.tone.get("score", 0),
        "accuracy": data.accuracy.get("score", 0),
    }
    if data.seo:
        scores["seo"] = data.seo.get("score", 0)

    score_values = [v for v in scores.values() if v]
    avg_score = round(sum(score_values) / len(score_values), 1) if score_values else 0

    return {
        "report": report,
        "scores": scores,
        "average_score": avg_score,
        "perspectives_included": list(scores.keys()),
        "seo_included": data.seo is not None,
    }
