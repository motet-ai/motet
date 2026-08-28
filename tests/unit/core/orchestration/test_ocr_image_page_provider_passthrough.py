"""
Motet - OCR Image Page Provider Passthrough Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
Unit tests that verify `ocr_image_page` keeps supported OCR providers when invoking
`model_inference`, preventing accidental provider/model mismatch.

Dependencies:
- pytest: Test framework
- motet.core.commands.builtin.derivation: Command under test
- motet.core.commands.command_data_classes: OCRImagePageData model

Usage:
pytest tests/unit/core/orchestration/test_ocr_image_page_provider_passthrough.py

Notes:
- Uses a fake MotetContext and does not require external services.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from motet.core.commands.builtin import derivation as derivation_mod
from motet.core.commands.command_data_classes import OCRImagePageData


class _FakeMotet:
    """Minimal motet context for `ocr_image_page` unit tests."""

    def __init__(self) -> None:
        self.tenant_id = "tenant-test"
        self.principal_id = "principal-test"
        self.motet_id = "motet-test"
        self.calls: List[Dict[str, Any]] = []

    def log_fields(self, **kwargs: Any) -> Dict[str, Any]:
        return kwargs

    def do(self, _command: Any, *, data: Any, timeout_seconds: Optional[int] = None) -> Dict[str, Any]:
        settings = dict(getattr(data, "model_settings", {}) or {})
        self.calls.append(
            {
                "provider": settings.get("provider"),
                "model_name": settings.get("model_name"),
                "timeout_seconds": timeout_seconds,
            }
        )
        return {"content": "Line one\nLine two"}


def test_ocr_image_page_preserves_moonshot_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """moonshot provider should be forwarded to model_inference unchanged."""
    fake_motet = _FakeMotet()
    monkeypatch.setattr(derivation_mod, "get_motet_context", lambda: fake_motet)

    result = derivation_mod.ocr_image_page.__wrapped__(
        OCRImagePageData(
            image_artifact_id="artifact-img-1",
            content_type="image/png",
            page_num=1,
            source_artifact_id="artifact-pdf-1",
            model_provider="moonshot",
            model_name="kimi-k2.5",
        )
    )

    assert fake_motet.calls
    assert fake_motet.calls[0]["provider"] == "moonshot"
    assert fake_motet.calls[0]["model_name"] == "kimi-k2.5"
    assert result["text"] == "Line one\nLine two"
    assert result["attempts"][0]["provider"] == "moonshot"


def test_ocr_image_page_unknown_provider_falls_back_to_openai(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown providers should still use openai fallback behavior."""
    fake_motet = _FakeMotet()
    monkeypatch.setattr(derivation_mod, "get_motet_context", lambda: fake_motet)

    derivation_mod.ocr_image_page.__wrapped__(
        OCRImagePageData(
            image_artifact_id="artifact-img-2",
            content_type="image/png",
            page_num=2,
            source_artifact_id="artifact-pdf-2",
            model_provider="unknown-provider",
            model_name="some-model",
        )
    )

    assert fake_motet.calls
    assert fake_motet.calls[0]["provider"] == "openai"
