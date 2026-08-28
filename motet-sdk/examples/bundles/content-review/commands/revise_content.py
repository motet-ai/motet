"""
Motet SDK - Content Review Example: Content Revision Command

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-03-18

Description:
Rewrite content incorporating synthesized feedback from all review
perspectives.  Preserves the author's voice and intent while addressing
the prioritized issues identified during review.

Dependencies:
- motet_sdk: command decorator and MotetContext typing
- pydantic: structured command inputs

Usage:
  Called by coordinate_reviews via motet.do() after feedback synthesis:
    revised = motet.do(revise_content, data=ReviseContentData(...))

Notes:
- The final step in the content-review pipeline.
- Uses a constrained prompt to avoid over-editing — the goal is to fix
  identified issues, not rewrite from scratch.
"""

from __future__ import annotations

from typing import Any, Dict

from pydantic import Field

from motet_sdk import BaseCommandData, MotetContext, WorkerCapability, motet


class ReviseContentData(BaseCommandData):
    """Input for content-review.revise_content."""

    original_content: str = Field(..., description="Original content to revise")
    feedback_report: str = Field(..., description="Synthesized feedback report")
    audience: str = Field(default="general", description="Target audience")
    provider: str = Field(default="openai", description="LLM provider")
    model_name: str = Field(default="gpt-4o-mini", description="LLM model name")


_REVISION_PROMPT = """\
You are a skilled editor.  Revise the ORIGINAL TEXT below based on the \
FEEDBACK REPORT.  The text is written for the following AUDIENCE.

Rules:
- Fix all Priority Fixes and Improvements from the feedback
- Preserve the author's voice and intent
- Do not add new information that wasn't in the original
- Keep the same general length (do not significantly expand or shrink)
- Output ONLY the revised text — no commentary or explanations

AUDIENCE: {audience}

FEEDBACK REPORT:
{feedback_report}

ORIGINAL TEXT:
{original_content}
"""


@motet.command(
    timeout_seconds=90,
    required_capabilities=[WorkerCapability.MODEL_INFERENCE],
)
def revise_content(data: ReviseContentData, motet: MotetContext) -> Dict[str, Any]:
    """Revise content based on synthesized multi-perspective feedback."""
    prompt = _REVISION_PROMPT.format(
        original_content=data.original_content,
        feedback_report=data.feedback_report,
        audience=data.audience,
    )

    result = motet.models.infer(
        provider=data.provider,
        model_name=data.model_name,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=3000,
    )

    revised = result.get("content") or result.get("response") or ""

    return {
        "revised_content": revised,
        "audience": data.audience,
        "original_length": len(data.original_content),
        "revised_length": len(revised),
    }
