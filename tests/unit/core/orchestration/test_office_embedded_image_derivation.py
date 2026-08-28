"""
Motet - Office Embedded Image Derivation Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-05-19

Description:
    Unit tests for the distributed derivation command that stores embedded
    DOCX/PPTX images as derived artifacts and dispatches image resizing.

Dependencies:
    - types.SimpleNamespace for artifact metadata stubs
    - io and zipfile for lightweight OOXML fixtures
    - motet.core.commands.builtin.derivation command module

Usage:
    pytest tests/unit/core/orchestration/test_office_embedded_image_derivation.py

Notes:
    - Tests call the command's wrapped function directly with a patched Motet
      context to avoid worker/Celery dependencies.
"""

from __future__ import annotations

import io
import zipfile
from types import SimpleNamespace
from typing import Any

from motet.core.artifacts import ArtifactKind
from motet.core.media.office_embedded_images import PPTX_CONTENT_TYPE
from motet.core.commands.command_data_classes import CreateArtifactData, DeriveOfficeEmbeddedImagesData


def _png_bytes(width: int = 640, height: int = 360) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color=(255, 255, 255)).save(buffer, format="PNG")
    return buffer.getvalue()


PNG_BYTES = _png_bytes()


def _minimal_pptx_with_image() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "ppt/slides/slide1.xml",
            """
            <p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
                   xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
                   xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
              <p:cSld><p:spTree><p:pic>
                <p:nvPicPr><p:cNvPr id="1" name="Revenue Chart" descr="Quarterly chart"/></p:nvPicPr>
                <p:blipFill><a:blip r:embed="rId1"/></p:blipFill>
              </p:pic></p:spTree></p:cSld>
            </p:sld>
            """,
        )
        archive.writestr(
            "ppt/slides/_rels/slide1.xml.rels",
            """
            <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
              <Relationship Id="rId1"
                Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
                Target="../media/image1.png"/>
            </Relationships>
            """,
        )
        archive.writestr("ppt/media/image1.png", PNG_BYTES)
    return buffer.getvalue()


class _ArtifactStoreStub:
    def __init__(self) -> None:
        self.puts: list[dict[str, Any]] = []
        self.source_meta = SimpleNamespace(
            id="source-1",
            content_type=PPTX_CONTENT_TYPE,
            metadata={"filename": "deck.pptx"},
            expires_at=None,
            created_at=0.0,
        )

    def get_metadata(self, artifact_id: str) -> Any:
        return self.source_meta if artifact_id == "source-1" else None

    def get(self, artifact_id: str) -> bytes:
        return _minimal_pptx_with_image() if artifact_id == "source-1" else b""

    def list(self, **_kwargs: Any) -> list[Any]:
        return []

    def put(self, **kwargs: Any) -> str:
        self.puts.append(kwargs)
        return f"embedded-{len(self.puts)}"


class _MotetStub:
    task_id = "task-1"
    conversation_id = "conv-1"
    tenant_id = "tenant-1"
    principal_id = "principal-1"
    motet_id = "motet-1"

    def __init__(self) -> None:
        self.artifact_store = _ArtifactStoreStub()
        self.dispatched: list[Any] = []

    def dispatch(self, commands: list[Any]) -> list[str]:
        self.dispatched.extend(commands)
        return ["task-child"]

    def log_fields(self, **extra: Any) -> dict[str, Any]:
        return extra


def test_derive_office_embedded_images_stores_images_and_dispatches_resize(monkeypatch) -> None:
    from motet.core.commands.builtin import derivation

    motet = _MotetStub()
    monkeypatch.setattr(derivation, "get_motet_context", lambda: motet)

    result = derivation.derive_office_embedded_images.__wrapped__(
        DeriveOfficeEmbeddedImagesData(source_artifact_id="source-1", image_derivation_names=["thumb", "base"])
    )

    assert result["status"] == "success"
    assert result["source_artifact_id"] == "source-1"
    assert len(result["embedded_images"]) == 1
    assert len(motet.artifact_store.puts) == 1
    stored = motet.artifact_store.puts[0]
    assert stored["payload"] == PNG_BYTES
    assert stored["content_type"] == "image/png"
    assert stored["kind"] == ArtifactKind.DERIVED_EMBEDDED_IMAGE
    assert stored["source_artifact_id"] == "source-1"
    assert stored["metadata"]["slide_num"] == 1
    assert stored["metadata"]["source_filename"] == "deck.pptx"
    assert stored["metadata"]["embedded_image_role"] == "chart"
    assert stored["metadata"]["embedded_image_should_ocr"] is True
    assert len(motet.dispatched) == 2
    resize_child = next(command for command in motet.dispatched if hasattr(command.data, "derivation_names"))
    ocr_child = next(command for command in motet.dispatched if hasattr(command.data, "image_artifact_id"))
    assert resize_child.data.source_artifact_id == "embedded-1"
    assert resize_child.data.derivation_names == ["thumb", "base"]
    assert ocr_child.data.source_artifact_id == "source-1"
    assert ocr_child.data.image_artifact_id == "embedded-1"
    assert result["ocr_derivations"]["child_command_ids"] == [ocr_child.command_id]


def test_derive_office_embedded_images_skips_ocr_for_decorative_images(monkeypatch) -> None:
    from motet.core.commands.builtin import derivation
    from motet.core.media import office_embedded_images

    motet = _MotetStub()
    monkeypatch.setattr(derivation, "get_motet_context", lambda: motet)
    monkeypatch.setattr(
        office_embedded_images,
        "extract_office_embedded_images",
        lambda *_args, **_kwargs: [
            SimpleNamespace(
                payload=PNG_BYTES,
                content_type="image/png",
                package_path="ppt/media/logo.png",
                ordinal=1,
                metadata={
                    "embedded_image_path": "ppt/media/logo.png",
                    "embedded_image_role": "logo",
                    "embedded_image_relevance_score": 0.1,
                    "embedded_image_should_ocr": False,
                },
            )
        ],
    )

    result = derivation.derive_office_embedded_images.__wrapped__(
        DeriveOfficeEmbeddedImagesData(source_artifact_id="source-1", image_derivation_names=["thumb", "base"])
    )

    assert result["embedded_images"][0]["metadata"]["embedded_image_role"] == "logo"
    assert len(motet.dispatched) == 1
    assert hasattr(motet.dispatched[0].data, "derivation_names")
    assert result["ocr_derivations"]["child_command_ids"] == []


class _CreateArtifactMotetStub:
    task_id = "task-1"
    tenant_id = "tenant-1"
    principal_id = "principal-1"
    motet_id = "motet-1"

    def __init__(self) -> None:
        self.dispatched: list[Any] = []
        self.artifact_store = SimpleNamespace(put=lambda **_kwargs: "source-1")

    def resolve_conversation_id(self, conversation_id: str | None = None) -> str:
        return conversation_id or "conv-1"

    def dispatch(self, commands: list[Any]) -> list[str]:
        self.dispatched.extend(commands)
        return [f"task-{len(self.dispatched)}"]

    def log_fields(self, **extra: Any) -> dict[str, Any]:
        return extra


def test_create_artifact_dispatches_embedded_image_derivation_for_pptx(monkeypatch) -> None:
    from motet.core.commands.builtin import artifacts

    motet = _CreateArtifactMotetStub()
    monkeypatch.setattr(artifacts, "get_motet_context", lambda: motet)

    result = artifacts.create_artifact.__wrapped__(
        CreateArtifactData(
            payload=_minimal_pptx_with_image(),
            content_type=PPTX_CONTENT_TYPE,
            filename="deck.pptx",
            trigger_derivations=True,
        )
    )

    derivation_types = [item["type"] for item in result["derivations"]]
    assert "text" in derivation_types
    assert "embedded_images" in derivation_types
    embedded_child = next(command for command in motet.dispatched if command.data.__class__ is DeriveOfficeEmbeddedImagesData)
    assert embedded_child.data.source_artifact_id == "source-1"
    assert embedded_child.data.image_derivation_names == ["thumb", "base"]


class _OCRArtifactStoreStub:
    def __init__(self) -> None:
        self.puts: list[dict[str, Any]] = []
        self.source_meta = SimpleNamespace(
            id="source-1",
            content_type=PPTX_CONTENT_TYPE,
            metadata={"filename": "deck.pptx"},
            source_artifact_id=None,
            expires_at=None,
            created_at=0.0,
            tenant_id="tenant-1",
            principal_id="principal-1",
            motet_id="motet-1",
        )
        self.image_meta = SimpleNamespace(
            id="embedded-1",
            content_type="image/png",
            metadata={
                "embedded_image_path": "ppt/media/image1.png",
                "embedded_image_ordinal": 1,
                "relationship_id": "rId1",
                "office_document_type": "pptx",
                "slide_num": 1,
                "embedded_image_alt_text": "Architecture diagram",
            },
            source_artifact_id="source-1",
            expires_at=None,
            created_at=0.0,
            tenant_id="tenant-1",
            principal_id="principal-1",
            motet_id="motet-1",
        )

    def get_metadata(self, artifact_id: str) -> Any:
        if artifact_id == "source-1":
            return self.source_meta
        if artifact_id == "embedded-1":
            return self.image_meta
        return None

    def get(self, artifact_id: str) -> bytes:
        return PNG_BYTES if artifact_id == "embedded-1" else b"source-payload"

    def list(self, **_kwargs: Any) -> list[Any]:
        return []

    def put(self, **kwargs: Any) -> str:
        self.puts.append(kwargs)
        return "ocr-1"


class _OCREmbeddedImageMotetStub:
    task_id = "task-1"
    conversation_id = "conv-1"
    tenant_id = "tenant-1"
    principal_id = "principal-1"
    motet_id = "motet-1"

    def __init__(self) -> None:
        self.artifact_store = _OCRArtifactStoreStub()
        self.dispatched: list[Any] = []
        self.stack = SimpleNamespace(
            config=SimpleNamespace(
                artifact_rag_enabled=True,
                artifact_rag_index_on_derivation=True,
            )
        )

    def do(self, _command: Any, data: Any) -> dict[str, Any]:
        assert data.image_artifact_id == "embedded-1"
        return {"text": "Revenue chart Q1 Q2", "attempts": [{"model_name": "vision-test"}]}

    def dispatch(self, commands: list[Any]) -> list[str]:
        self.dispatched.extend(commands)
        return ["task-index"]

    def log_fields(self, **extra: Any) -> dict[str, Any]:
        return extra


def test_ocr_embedded_image_stores_ocr_text_and_dispatches_rag_index(monkeypatch) -> None:
    from motet.core.commands.builtin import derivation

    motet = _OCREmbeddedImageMotetStub()
    monkeypatch.setattr(derivation, "get_motet_context", lambda: motet)

    result = derivation.ocr_embedded_image.__wrapped__(
        derivation.OCREmbeddedImageData(
            source_artifact_id="source-1",
            image_artifact_id="embedded-1",
            content_type="image/png",
        )
    )

    assert result["status"] == "success"
    assert result["ocr_artifact_id"] == "ocr-1"
    assert len(motet.artifact_store.puts) == 1
    stored = motet.artifact_store.puts[0]
    assert stored["payload"] == "Revenue chart Q1 Q2"
    assert stored["content_type"] == "text/plain"
    assert stored["kind"] == ArtifactKind.DERIVED_OCR
    assert stored["source_artifact_id"] == "source-1"
    assert stored["metadata"]["embedded_image_artifact_id"] == "embedded-1"
    assert stored["metadata"]["slide_num"] == 1
    assert stored["metadata"]["embedded_image_alt_text"] == "Architecture diagram"
    assert len(motet.dispatched) == 1
    child = motet.dispatched[0]
    assert child.data.source_artifact_id == "source-1"
    assert child.data.derived_artifact_id == "ocr-1"
