"""
Motet - Canonical Multimodal Renderer AVIF Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-27

Description:
    Unit tests for model-input image normalization in the canonical multimodal
    renderer. These tests ensure artifact-backed AVIF inputs are converted before
    provider adapters receive canonical `MediaPart` data when AVIF conversion
    is enabled. HEIC stays refused unless its own flag is on.

Dependencies:
    - pytest: Test runner and monkeypatch fixture
    - motet.core.models.rendering.canonical: Renderer under test
    - motet.core.media.image_processing: AVIF-to-JPEG normalization helper

Usage:
    pytest tests/unit/core/providers/test_canonical_multimodal_renderer_avif.py

Notes:
    - AVIF decode uses Pillow native support (11.3+ wheels). Tests that encode
      a real AVIF skip when this Pillow build has no AVIF feature.
    - HEIC conversion is a separate operator opt-in and is not implied by AVIF.
"""

from __future__ import annotations

import base64
import io

import pytest


class FakeArtifactStore:
    """Minimal artifact store for renderer tests."""

    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def get(self, artifact_id: str, **_: object) -> bytes:
        assert artifact_id == "avif-artifact"
        return self.payload


def _require_native_avif() -> None:
    from PIL import features

    if not features.check("avif"):
        pytest.skip("Pillow wheel does not include native AVIF")


def _synthetic_avif_bytes() -> bytes:
    from PIL import Image

    _require_native_avif()
    source = io.BytesIO()
    Image.new("RGB", (8, 8), (255, 0, 0)).save(source, format="AVIF")
    return source.getvalue()


def _ftyp_header(brand: bytes) -> bytes:
    return b"\x00\x00\x00\x1cftyp" + brand + b"\x00\x00\x00\x00" + brand


def test_renderer_normalizes_avif_artifact_before_base64(monkeypatch: pytest.MonkeyPatch) -> None:
    from motet.core.models.rendering import canonical
    from motet.core.models.rendering.base import RenderingContext
    from motet.core.types import MediaPart, Message, TextPart

    seen: dict[str, object] = {}

    def fake_normalize(image_bytes: bytes, content_type: str) -> tuple[bytes, str]:
        seen["bytes"] = image_bytes
        seen["content_type"] = content_type
        return b"jpeg-bytes", "image/jpeg"

    monkeypatch.setattr(canonical, "normalize_model_image_bytes", fake_normalize)

    renderer = canonical.CanonicalMultimodalRenderer()
    rendered = renderer.render(
        [
            Message(
                role="user",
                content="fallback",
                content_parts=[
                    TextPart(text="analyze this"),
                    MediaPart(
                        media_type="image",
                        artifact_id="avif-artifact",
                        mime_type="image/avif",
                    ),
                ],
            )
        ],
        context=RenderingContext(
            provider="openai",
            model_name="gpt-5.4",
            tenant_id="tenant-1",
            principal_id="principal-1",
            motet_id="default",
            artifact_store=FakeArtifactStore(b"avif-bytes"),
        ),
    )

    image_part = rendered[0].content_parts[1]
    assert seen == {"bytes": b"avif-bytes", "content_type": "image/avif"}
    assert image_part.mime_type == "image/jpeg"
    assert base64.b64decode(image_part.base64_data) == b"jpeg-bytes"


def test_normalize_model_image_bytes_rejects_avif_when_feature_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from motet.core.media.image_processing import normalize_model_image_bytes

    monkeypatch.delenv("MOTET_ENABLE_AVIF_CONVERSION", raising=False)
    monkeypatch.delenv("MOTET_ENABLE_HEIC_HEIF_CONVERSION", raising=False)

    with pytest.raises(ValueError, match="MOTET_ENABLE_AVIF_CONVERSION=1"):
        normalize_model_image_bytes(b"avif-bytes", "image/avif")


def test_normalize_model_image_bytes_converts_avif_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from motet.core.media import image_processing

    monkeypatch.setenv("MOTET_ENABLE_AVIF_CONVERSION", "1")
    monkeypatch.delenv("MOTET_ENABLE_HEIC_HEIF_CONVERSION", raising=False)
    image_processing._heic_opener_registered = False

    converted, content_type = image_processing.normalize_model_image_bytes(
        _synthetic_avif_bytes(),
        "image/avif",
    )

    assert content_type == "image/jpeg"
    assert converted.startswith(b"\xff\xd8\xff")


def test_resize_image_bytes_honors_format_override_without_resizing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from motet.core.media import image_processing

    monkeypatch.setenv("MOTET_ENABLE_AVIF_CONVERSION", "1")
    monkeypatch.delenv("MOTET_ENABLE_HEIC_HEIF_CONVERSION", raising=False)
    image_processing._heic_opener_registered = False

    converted = image_processing.resize_image_bytes(
        _synthetic_avif_bytes(),
        max_side=1600,
        format_override="JPEG",
    )

    assert converted.startswith(b"\xff\xd8\xff")


def test_detect_image_content_type_from_bytes_corrects_mislabeled_avif() -> None:
    from motet.core.media.image_processing import detect_image_content_type_from_bytes

    fake_avif_header = _ftyp_header(b"avif")

    assert (
        detect_image_content_type_from_bytes(fake_avif_header, fallback="image/jpeg")
        == "image/avif"
    )


@pytest.mark.parametrize(
    ("content_type", "flag_name"),
    [
        ("image/heic", "MOTET_ENABLE_HEIC_HEIF_CONVERSION"),
        ("image/heif", "MOTET_ENABLE_HEIC_HEIF_CONVERSION"),
    ],
)
def test_normalize_model_image_bytes_rejects_heic_heif_when_feature_disabled(
    monkeypatch: pytest.MonkeyPatch,
    content_type: str,
    flag_name: str,
) -> None:
    from motet.core.media.image_processing import normalize_model_image_bytes

    monkeypatch.delenv("MOTET_ENABLE_AVIF_CONVERSION", raising=False)
    monkeypatch.delenv("MOTET_ENABLE_HEIC_HEIF_CONVERSION", raising=False)

    with pytest.raises(ValueError, match=f"{flag_name}=1"):
        normalize_model_image_bytes(b"heic-bytes", content_type)


def test_avif_flag_does_not_enable_heic_conversion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from motet.core.media.image_processing import (
        detect_image_dimensions,
        normalize_model_image_bytes,
    )

    monkeypatch.setenv("MOTET_ENABLE_AVIF_CONVERSION", "1")
    monkeypatch.delenv("MOTET_ENABLE_HEIC_HEIF_CONVERSION", raising=False)

    heic_header = _ftyp_header(b"heic")
    with pytest.raises(ValueError, match="MOTET_ENABLE_HEIC_HEIF_CONVERSION=1"):
        normalize_model_image_bytes(heic_header, "image/heic")
    with pytest.raises(ValueError, match="MOTET_ENABLE_HEIC_HEIF_CONVERSION=1"):
        detect_image_dimensions(heic_header)


def test_avif_enabled_does_not_import_pillow_heif(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import builtins

    from motet.core.media import image_processing

    monkeypatch.setenv("MOTET_ENABLE_AVIF_CONVERSION", "1")
    monkeypatch.delenv("MOTET_ENABLE_HEIC_HEIF_CONVERSION", raising=False)
    image_processing._heic_opener_registered = False

    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "pillow_heif" or name.startswith("pillow_heif."):
            raise AssertionError("AVIF path must not import pillow_heif")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    image_processing.normalize_model_image_bytes(_synthetic_avif_bytes(), "image/avif")
