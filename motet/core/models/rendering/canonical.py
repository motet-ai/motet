"""
Motet - Canonical Multimodal Message Renderer

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-19

Description:
    Materializes Motet internal `Message.content_parts` into a canonical, provider-agnostic form
    suitable for translation by provider adapters.

    Specifically, for `MediaPart(media_type="image")` that reference artifacts, this renderer:
    - Fetches bytes from the artifact store (scoped by tenant/principal/motet)
    - Enforces `max_images` and `max_image_bytes` limits (safety belt)
    - Normalizes unsupported image containers (for example AVIF) to model-ready JPEG bytes
    - Stores the base64 payload on `MediaPart.base64_data`

    Provider adapters are responsible for translating the resulting canonical messages into
    provider wire formats (OpenAI Responses/Chat, Anthropic, etc.).

    This renderer is intentionally conservative:
    - It treats `Message.content_parts` as authoritative when present and non-empty.
    - It never inlines base64 into `Message.content`.
    - It fails fast on unsupported media types.

Dependencies:
    - base64: encode image bytes into base64 for `MediaPart.base64_data`
    - motet.core.types: Message, TextPart, MediaPart
    - motet.core.models.rendering.base: RenderingContext

Usage:
    renderer = CanonicalMultimodalRenderer()
    rendered = renderer.render(messages, context=ctx)
"""

from __future__ import annotations

import base64
from typing import List

from ...types import MediaPart, Message, TextPart
from ...media.image_processing import normalize_model_image_bytes
from .base import ProviderMessageRenderer, RenderingContext


class CanonicalMultimodalRenderer(ProviderMessageRenderer):
    """Canonical multimodal renderer supporting ADR-0062/ADR-0064 parts."""

    def render(self, messages: List[Message], *, context: RenderingContext) -> List[Message]:
        image_count = 0
        out: List[Message] = []

        for m in messages:
            parts = getattr(m, "content_parts", None) or []
            if parts:
                new_parts: List[object] = []
                for part in parts:
                    if isinstance(part, TextPart):
                        new_parts.append(part)
                        continue

                    # ADR-0064: generalized media part. For now, only images are materialized here.
                    if isinstance(part, MediaPart):
                        if part.media_type != "image":
                            raise ValueError(
                                f"Unsupported media_type for canonical multimodal renderer: {part.media_type}"
                            )
                        image_count += 1
                        if image_count > context.max_images:
                            raise ValueError(f"max_images exceeded (max_images={context.max_images})")

                        # If already materialized, keep as-is (still enforce count/budgets best-effort).
                        if isinstance(part.base64_data, str) and part.base64_data:
                            new_parts.append(part)
                            continue

                        if not part.artifact_id:
                            raise ValueError(
                                "Canonical multimodal renderer requires MediaPart.artifact_id when base64_data is not provided."
                            )
                        if not isinstance(part.mime_type, str) or not part.mime_type.startswith("image/"):
                            raise ValueError(
                                f"Canonical multimodal renderer requires MediaPart.mime_type like image/* (got {part.mime_type!r})"
                            )

                        payload = context.artifact_store.get(
                            str(part.artifact_id),
                            tenant_id=context.tenant_id,
                            principal_id=context.principal_id,
                            motet_id=context.motet_id,
                        )
                        if payload is None:
                            raise ValueError(f"Image artifact not found: {part.artifact_id}")
                        if isinstance(payload, dict):
                            raise ValueError(f"Image artifact payload is not bytes: {part.artifact_id}")
                        if isinstance(payload, str):
                            payload = payload.encode("utf-8")
                        if not isinstance(payload, (bytes, bytearray)):
                            raise ValueError(f"Unsupported image payload type: {type(payload).__name__}")

                        payload_bytes = bytes(payload)
                        payload_bytes, mime_type = normalize_model_image_bytes(
                            payload_bytes,
                            part.mime_type,
                        )
                        if len(payload_bytes) > context.max_image_bytes:
                            raise ValueError(
                                f"Image artifact exceeds max_image_bytes ({len(payload_bytes)} > {context.max_image_bytes})"
                            )

                        b64 = base64.b64encode(payload_bytes).decode("ascii")
                        new_parts.append(part.model_copy(update={"base64_data": b64, "mime_type": mime_type}))
                        continue

                    # Unknown part type (should not happen if parts are typed)
                    raise ValueError(f"Unsupported content part type: {type(part).__name__}")

                out.append(m.model_copy(update={"content_parts": new_parts}))
                continue

            # No parts: pass through unchanged.
            out.append(m)

        return out

