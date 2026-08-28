"""
Motet - Office Embedded Image Extraction Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-05-19

Description:
    Unit tests for deterministic DOCX/PPTX embedded image extraction used by
    artifact derivation commands.

Dependencies:
    - io and zipfile for constructing lightweight OOXML fixtures
    - motet.core.media.office_embedded_images extraction helpers

Usage:
    pytest tests/unit/core/media/test_office_embedded_images.py

Notes:
    - Fixtures are minimal OOXML ZIP packages that contain only relationship and
      media entries required by the extractor.
"""

from __future__ import annotations

import io
import zipfile

from motet.core.media.office_embedded_images import (
    DOCX_CONTENT_TYPE,
    PPTX_CONTENT_TYPE,
    extract_office_embedded_images,
    is_office_embedded_image_eligible,
)


def _png_bytes(width: int = 640, height: int = 360) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color=(255, 255, 255)).save(buffer, format="PNG")
    return buffer.getvalue()


PNG_BYTES = _png_bytes()
ICON_PNG_BYTES = _png_bytes(width=32, height=32)
JPEG_BYTES = b"\xff\xd8\xffembedded-image"


def _zip(entries: dict[str, bytes | str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path, payload in entries.items():
            archive.writestr(path, payload)
    return buffer.getvalue()


def test_extract_pptx_embedded_images_with_slide_metadata() -> None:
    pptx = _zip(
        {
            "ppt/slides/slide1.xml": """
                <p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
                       xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
                       xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
                       xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">
                  <p:cSld><p:spTree><p:pic>
                    <p:nvPicPr><p:cNvPr id="1" name="Product Chart" descr="Quarterly chart"/></p:nvPicPr>
                    <p:blipFill><a:blip r:embed="rId2"/></p:blipFill>
                  </p:pic></p:spTree></p:cSld>
                </p:sld>
            """,
            "ppt/slides/_rels/slide1.xml.rels": """
                <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
                  <Relationship Id="rId2"
                    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
                    Target="../media/image1.png"/>
                </Relationships>
            """,
            "ppt/media/image1.png": PNG_BYTES,
        }
    )

    images = extract_office_embedded_images(pptx, PPTX_CONTENT_TYPE)

    assert len(images) == 1
    assert images[0].payload == PNG_BYTES
    assert images[0].content_type == "image/png"
    assert images[0].package_path == "ppt/media/image1.png"
    assert images[0].metadata["slide_num"] == 1
    assert images[0].metadata["relationship_id"] == "rId2"
    assert images[0].metadata["embedded_image_name"] == "Product Chart"
    assert images[0].metadata["embedded_image_alt_text"] == "Quarterly chart"
    assert images[0].metadata["embedded_image_role"] == "chart"
    assert images[0].metadata["embedded_image_should_ocr"] is True
    assert images[0].metadata["width"] == 640
    assert images[0].metadata["height"] == 360


def test_extract_docx_embedded_images_with_relationship_metadata() -> None:
    docx = _zip(
        {
            "word/document.xml": """
                <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
                            xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
                            xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
                            xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">
                  <w:body><w:p><w:r><w:drawing><pic:pic>
                    <pic:nvPicPr><pic:cNvPr id="1" name="Architecture Diagram" descr="System diagram"/></pic:nvPicPr>
                    <pic:blipFill><a:blip r:embed="rId5"/></pic:blipFill>
                  </pic:pic></w:drawing></w:r></w:p></w:body>
                </w:document>
            """,
            "word/_rels/document.xml.rels": """
                <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
                  <Relationship Id="rId5"
                    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
                    Target="media/image1.jpeg"/>
                </Relationships>
            """,
            "word/media/image1.jpeg": JPEG_BYTES,
        }
    )

    images = extract_office_embedded_images(docx, DOCX_CONTENT_TYPE)

    assert len(images) == 1
    assert images[0].payload == JPEG_BYTES
    assert images[0].content_type == "image/jpeg"
    assert images[0].metadata["office_document_type"] == "docx"
    assert images[0].metadata["relationship_id"] == "rId5"
    assert images[0].metadata["embedded_image_name"] == "Architecture Diagram"


def test_extract_pptx_marks_tiny_logo_as_decorative_without_ocr() -> None:
    pptx = _zip(
        {
            "ppt/slides/slide1.xml": """
                <p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
                       xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
                       xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
                  <p:cSld><p:spTree><p:pic>
                    <p:nvPicPr><p:cNvPr id="1" name="Company Logo" descr="Brand logo"/></p:nvPicPr>
                    <p:blipFill><a:blip r:embed="rId1"/></p:blipFill>
                  </p:pic></p:spTree></p:cSld>
                </p:sld>
            """,
            "ppt/slides/_rels/slide1.xml.rels": """
                <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
                  <Relationship Id="rId1"
                    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
                    Target="../media/logo.png"/>
                </Relationships>
            """,
            "ppt/media/logo.png": ICON_PNG_BYTES,
        }
    )

    images = extract_office_embedded_images(pptx, PPTX_CONTENT_TYPE)

    assert len(images) == 1
    assert images[0].metadata["embedded_image_role"] == "logo"
    assert images[0].metadata["embedded_image_should_ocr"] is False
    assert images[0].metadata["embedded_image_relevance_score"] < 0.35


def test_unsupported_content_type_is_not_eligible() -> None:
    assert is_office_embedded_image_eligible("application/pdf") is False
    assert extract_office_embedded_images(b"not a zip", "application/pdf") == []
