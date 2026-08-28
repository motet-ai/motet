"""
Motet SDK - Content Review Example: Multi-Perspective Review Coordinator

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-03-18

Description:
Orchestrate multiple review perspectives on the same content, synthesize
their feedback, and produce a revised version.  This is the showcase
command for the content-review bundle, demonstrating three SDK composition
patterns in a single pipeline:

  1. motet.join() — run DIFFERENT commands in parallel
  2. motet.maybe() — optional step with graceful degradation
  3. motet.do()   — sequential command chaining

Dependencies:
- motet_sdk: command decorator and MotetContext typing
- pydantic: structured command inputs
- Sibling commands: review_grammar, review_tone, review_accuracy,
  review_seo, synthesize_feedback, revise_content

Usage:
  content-review.coordinate_reviews(
    content="Your draft text here...",
    audience="technical developers",
    content_type="blog_post"
  )

Notes:
- The workflow YAML (content_review.yaml) achieves the same pipeline
  declaratively.  This command shows the programmatic alternative,
  which gives finer control over error handling and data flow.
- The SEO review is deliberately called via motet.maybe() to show how
  optional steps work — if SEO analysis fails or is unavailable, the
  pipeline still completes successfully with the other three reviews.
"""

from __future__ import annotations

from typing import Any, Dict

from pydantic import Field

from motet_sdk import BaseCommandData, MotetContext, WorkerCapability, motet


class CoordinateReviewsData(BaseCommandData):
    """Input for content-review.coordinate_reviews."""

    content: str = Field(..., description="Content to review and revise")
    audience: str = Field(
        default="general",
        description="Target audience (e.g. 'technical developers', 'executives', 'general public')",
    )
    content_type: str = Field(
        default="article",
        description="Content type (article, blog_post, landing_page, documentation)",
    )
    provider: str = Field(default="openai", description="LLM provider for all review steps")
    model_name: str = Field(default="gpt-4o-mini", description="LLM model for all review steps")


@motet.command(
    timeout_seconds=300,
    required_capabilities=[WorkerCapability.MODEL_INFERENCE],
)
def coordinate_reviews(data: CoordinateReviewsData, motet: MotetContext) -> Dict[str, Any]:
    """
    Orchestrate multi-perspective content review, synthesis, and revision.

    Demonstrates:
    - motet.join()  — three different review commands in parallel
    - motet.maybe() — optional SEO review (graceful skip on failure)
    - motet.do()    — sequential synthesis and revision
    """
    from .review_accuracy import ReviewAccuracyData, review_accuracy
    from .review_grammar import ReviewGrammarData, review_grammar
    from .review_seo import ReviewSeoData, review_seo
    from .review_tone import ReviewToneData, review_tone
    from .revise_content import ReviseContentData, revise_content
    from .synthesize_feedback import SynthesizeFeedbackData, synthesize_feedback

    gp, gm = data.provider, data.model_name

    # ── Step 1: Run three review perspectives in parallel ────────────
    # motet.join() with DIFFERENT command types — each analyses the same
    # content through a distinct lens.  All three execute concurrently
    # on separate workers.
    grammar_result, tone_result, accuracy_result = motet.join([
        (review_grammar, ReviewGrammarData(content=data.content, provider=gp, model_name=gm)),
        (review_tone, ReviewToneData(content=data.content, audience=data.audience, provider=gp, model_name=gm)),
        (review_accuracy, ReviewAccuracyData(content=data.content, provider=gp, model_name=gm)),
    ])

    # ── Step 2: Optional SEO review ─────────────────────────────────
    # motet.maybe() returns (data, None) on success or (None, error) on
    # failure — the pipeline continues either way.
    seo_result, seo_error = motet.maybe(
        review_seo,
        data=ReviewSeoData(
            content=data.content,
            content_type=data.content_type,
            provider=gp,
            model_name=gm,
        ),
    )
    if seo_error:
        seo_result = None

    # ── Step 3: Synthesize all feedback into a unified report ───────
    # Sequential motet.do() — waits for the result before continuing.
    feedback = motet.do(
        synthesize_feedback,
        data=SynthesizeFeedbackData(
            content=data.content,
            grammar=grammar_result,
            tone=tone_result,
            accuracy=accuracy_result,
            seo=seo_result,
            provider=gp,
            model_name=gm,
        ),
    )

    # ── Step 4: Revise the content based on synthesized feedback ────
    revised = motet.do(
        revise_content,
        data=ReviseContentData(
            original_content=data.content,
            feedback_report=feedback.get("report", ""),
            audience=data.audience,
            provider=gp,
            model_name=gm,
        ),
    )

    return {
        "original_content": data.content,
        "reviews": {
            "grammar": grammar_result,
            "tone": tone_result,
            "accuracy": accuracy_result,
            "seo": seo_result,
        },
        "seo_skipped": seo_error is not None,
        "feedback_report": feedback.get("report", ""),
        "average_score": feedback.get("average_score", 0),
        "revised_content": revised.get("revised_content", ""),
    }
