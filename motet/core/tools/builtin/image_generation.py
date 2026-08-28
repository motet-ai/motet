"""
Motet - Image Generation Tool

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Agent-facing built-in tool for image generation. Wraps the distributed
    `core.image_generation` command so agentic loops can generate images from a text prompt.
    Generated images are stored as artifacts (ArtifactKind.GENERATED_IMAGE) and returned as
    artifact IDs / canonical MediaParts, which the agent loop surfaces and persists.

Dependencies:
    - pydantic: Tool parameter schema validation
    - ToolRegistry: Built-in tool registration
    - core.image_generation: Distributed image-generation command

Usage:
    Tool call: core.image_generation({"prompt": "a watercolor fox in a misty forest"})

Notes:
    - Provider/model routing follows precedence; capability-gated on image generation.
    - The tool is capability-gated by the command, which fails fast on text-only models.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

from ..registry import ToolRegistry


class ImageGenerationParams(BaseModel):
    """Parameters for generating one or more images from a text prompt."""

    prompt: str = Field(..., description="Text description of the image(s) to generate.")
    n: int = Field(default=1, ge=1, le=4, description="Number of images to generate.")
    size: Optional[str] = Field(
        default=None,
        description="Optional image size as WIDTHxHEIGHT or 'auto' (e.g. '1024x1024').",
    )
    quality: Optional[str] = Field(
        default=None,
        description="Optional provider quality hint (e.g. 'high').",
    )


def _get_motet_context_optional() -> Any:
    """Return current MotetContext if the tool runs inside tool_execution."""

    try:
        from motet.core.commands.decorator import get_motet_context

        return get_motet_context()
    except Exception:
        return None


def run(params: Dict[str, Any]) -> Dict[str, Any]:
    """Execute image generation and return stored artifact IDs / MediaParts."""

    parsed = ImageGenerationParams(**(params or {}))
    motet = _get_motet_context_optional()
    if motet is None:
        return {
            "status": "error",
            "error": "Motet context is required to generate images",
            "artifact_ids": [],
            "media": [],
        }

    from motet.core.commands.command_data_classes import ImageGenerationData
    from motet.core.commands.builtin.model import image_generation

    result = motet.do(
        image_generation,
        data=ImageGenerationData(
            prompt=parsed.prompt,
            n=parsed.n,
            size=parsed.size,
            quality=parsed.quality,
        ),
    )
    if not isinstance(result, dict):
        result = {}

    return {
        "status": "ok",
        "prompt": parsed.prompt,
        "image_count": result.get("image_count", len(result.get("artifact_ids", []) or [])),
        "artifact_ids": result.get("artifact_ids", []),
        "media": result.get("media", []),
        "provider": result.get("provider"),
        "model_name": result.get("model_name"),
    }


def _format_observation(result: Dict[str, Any]) -> str:
    status = result.get("status", "unknown")
    count = int(result.get("image_count") or 0)
    model = result.get("model_name") or "image-model"
    return f"image_generation(status={status}, images={count}, model={model})"


def register(registry: ToolRegistry) -> None:
    registry.register(
        name="core.image_generation",
        description=(
            "Generate one or more images from a text prompt. Use when the user asks to create, "
            "draw, render, or illustrate an image. Returns stored image artifact IDs. To display a "
            "generated image to the user, embed each returned artifact_id in your reply as a "
            "Markdown image using the artifact URI scheme, e.g. ![a blue cat](artifact:<artifact_id>). "
            "The chat surface resolves artifact: image links to the stored image. Do NOT paste base64 "
            "or invent an http URL; always reference images by their artifact_id."
        ),
        func=run,
        tool_schema=ImageGenerationParams,
        category="media",
        contextualize_observation=True,
        observation_formatter=_format_observation,
        default_timeout_seconds=120.0,
        suggested_max_calls=3,
        cost_class="high",
        keywords=[
            "image",
            "images",
            "generate image",
            "create image",
            "draw",
            "illustration",
            "picture",
            "render",
            "art",
            "dall-e",
            "gpt-image",
        ],
        required_capabilities=["tool_execution", "model_inference"],
    )


__all__ = ["ImageGenerationParams", "register", "run"]
