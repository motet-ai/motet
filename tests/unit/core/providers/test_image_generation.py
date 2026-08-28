"""
Motet - Image Generation Tests (ADR-0113)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Unit tests for image generation (ADR-0113): the mock and OpenAI adapter
    `generate_images()` implementations, the canonical capability flag, and the
    `image_generation` distributed command (capability gating + artifact storage).

Dependencies:
    - pytest: Test framework
    - motet.core.types: Canonical image-generation request/response types
    - motet.core.models.adapters.providers: Adapter implementations

Usage:
    pytest tests/unit/core/providers/test_image_generation.py

Notes:
    - The command test fakes MotetContext and adapter selection to stay pure-unit
      (no Redis / distributed stack).
"""

from __future__ import annotations

import base64
from typing import Any, Dict, List, Optional

import pytest

from motet.core.artifacts.types import ArtifactKind
from motet.core.models.adapters.base import CapabilityDescriptor
from motet.core.models.adapters.providers.mock import MockAdapter
from motet.core.models.adapters.providers.openai_responses import OpenAIResponsesAdapter
from motet.core.types import (
    GeneratedImage,
    ImageGenerationRequest,
    ImageGenerationResponse,
)


# ------------------------------------------------------------------------------------------------
# Mock adapter
# ------------------------------------------------------------------------------------------------


def test_mock_adapter_advertises_image_generation() -> None:
    adapter = MockAdapter(provider="mock", adapter_name="mock")
    caps = adapter.capabilities(model="mock")
    assert caps.supports_image_generation is True


def test_mock_adapter_generate_images_returns_n_pngs() -> None:
    adapter = MockAdapter(provider="mock", adapter_name="mock")
    resp = adapter.generate_images(ImageGenerationRequest(prompt="a fox", n=3))
    assert isinstance(resp, ImageGenerationResponse)
    assert len(resp.images) == 3
    for img in resp.images:
        assert img.mime_type == "image/png"
        # Decodes to a valid (1x1) PNG starting with the PNG magic bytes.
        raw = base64.b64decode(img.base64_data or "")
        assert raw[:8] == b"\x89PNG\r\n\x1a\n"


# ------------------------------------------------------------------------------------------------
# OpenAI adapter (stubbed client)
# ------------------------------------------------------------------------------------------------


class _FakeImageItem:
    def __init__(self, b64: Optional[str] = None, url: Optional[str] = None, revised: Optional[str] = None) -> None:
        self.b64_json = b64
        self.url = url
        self.revised_prompt = revised


class _FakeImagesResult:
    def __init__(self, items: List[_FakeImageItem]) -> None:
        self.data = items
        self.usage = None


class _FakeImagesNamespace:
    def __init__(self) -> None:
        self.captured: Dict[str, Any] = {}

    def generate(self, **kwargs: Any) -> _FakeImagesResult:
        self.captured = kwargs
        return _FakeImagesResult([_FakeImageItem(b64="QUJD", revised="a revised prompt")])


class _FakeOpenAIClient:
    def __init__(self) -> None:
        self.images = _FakeImagesNamespace()


def test_openai_generate_images_maps_b64(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = OpenAIResponsesAdapter(provider="openai", adapter_name="responses", credentials={"api_key": "x"})
    fake = _FakeOpenAIClient()
    monkeypatch.setattr(adapter, "_client", lambda: fake)

    resp = adapter.generate_images(
        ImageGenerationRequest(
            prompt="a watercolor fox",
            n=1,
            size="1024x1024",
            model_settings={"model_name": "gpt-image-1"},
        )
    )
    assert len(resp.images) == 1
    assert resp.images[0].base64_data == "QUJD"
    assert resp.images[0].revised_prompt == "a revised prompt"
    assert resp.model == "gpt-image-1"
    # GPT Image models must NOT receive response_format (only DALL·E does).
    assert "response_format" not in fake.images.captured
    assert fake.images.captured["size"] == "1024x1024"


def test_openai_generate_images_passes_response_format_for_dalle(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = OpenAIResponsesAdapter(provider="openai", adapter_name="responses", credentials={"api_key": "x"})
    fake = _FakeOpenAIClient()
    monkeypatch.setattr(adapter, "_client", lambda: fake)

    adapter.generate_images(
        ImageGenerationRequest(
            prompt="a watercolor fox",
            model_settings={"model_name": "dall-e-3", "response_format": "b64_json"},
        )
    )
    assert fake.images.captured.get("response_format") == "b64_json"


# ------------------------------------------------------------------------------------------------
# image_generation command (faked MotetContext + adapter selection)
# ------------------------------------------------------------------------------------------------


class _FakeMotet:
    tenant_id = "tenant"
    principal_id = "principal"
    motet_id = "default"
    task_id = "task-1"
    command_id = "cmd-1"
    conversation_id = "conv-1"
    metadata: Dict[str, Any] = {}
    distributed_context = None

    def __init__(self) -> None:
        self.created: List[Any] = []

    def log_fields(self, **kw: Any) -> Dict[str, Any]:
        return kw

    def do(self, cmd: Any, data: Any = None) -> Dict[str, Any]:
        # Emulate create_artifact returning a stored artifact id.
        self.created.append(data)
        return {"artifact_id": f"art-{len(self.created)}"}


class _Selection:
    adapter_name = "mock"
    provider = "mock"
    source = "test"


def _patch_command_helpers(monkeypatch: pytest.MonkeyPatch, adapter: Any) -> _FakeMotet:
    from motet.core.commands.builtin import model as model_mod
    from motet.core.models.adapters import adapter_registry

    fake = _FakeMotet()
    monkeypatch.setattr(model_mod, "get_motet_context", lambda: fake)
    monkeypatch.setattr(model_mod, "_check_budget_before_inference", lambda *a, **k: None)
    monkeypatch.setattr(model_mod, "_get_provider_credentials", lambda **k: None)
    monkeypatch.setattr(
        model_mod,
        "_select_adapter_and_effective_model_settings",
        lambda **k: (_Selection(), {"model_name": "mock"}, None, None),
    )
    monkeypatch.setattr(model_mod, "_track_inference_cost", lambda result, motet, **k: result)
    monkeypatch.setattr(adapter_registry, "build", lambda *a, **k: adapter)
    return fake


def test_image_generation_command_stores_artifacts(monkeypatch: pytest.MonkeyPatch) -> None:
    from motet.core.commands.builtin import model as model_mod
    from motet.core.commands.command_data_classes import ImageGenerationData

    adapter = MockAdapter(provider="mock", adapter_name="mock")
    fake = _patch_command_helpers(monkeypatch, adapter)

    data = ImageGenerationData(
        prompt="a fox in a forest",
        n=2,
        model_settings={"provider": "mock", "model_name": "mock"},
    )
    result = model_mod.image_generation.__wrapped__(data)

    assert result["image_count"] == 2
    assert len(result["artifact_ids"]) == 2
    assert len(result["media"]) == 2
    assert result["media"][0]["media_type"] == "image"
    assert result["media"][0]["artifact_id"].startswith("art-")
    # Each stored artifact used the GENERATED_IMAGE kind.
    assert all(d.kind == ArtifactKind.GENERATED_IMAGE.value for d in fake.created)


class _NoImageGenAdapter:
    provider = "mock"
    adapter_name = "noimg"

    def capabilities(self, *, model: str) -> CapabilityDescriptor:
        return CapabilityDescriptor(provider="mock", model=model, supports_image_generation=False)

    def generate_images(self, request: ImageGenerationRequest) -> ImageGenerationResponse:  # pragma: no cover
        raise AssertionError("generate_images must not be called when capability gate fails")


def test_image_generation_command_capability_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    from motet.core.commands.builtin import model as model_mod
    from motet.core.commands.command_data_classes import ImageGenerationData

    adapter = _NoImageGenAdapter()
    _patch_command_helpers(monkeypatch, adapter)

    data = ImageGenerationData(
        prompt="a fox",
        model_settings={"provider": "mock", "model_name": "text-only"},
    )
    with pytest.raises(ValueError, match="does not support image generation"):
        model_mod.image_generation.__wrapped__(data)
