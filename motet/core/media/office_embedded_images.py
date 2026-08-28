"""
Motet - Office Embedded Image Extraction

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-05-19

Description:
    Extracts embedded image payloads from OOXML office documents such as DOCX
    and PPTX. The extractor returns payload bytes plus stable document-location
    metadata so derivation commands can store images as first-class artifacts
    linked back to their source document. It also adds cheap role/relevance
    metadata to distinguish likely informative images from decorative assets.

Dependencies:
    - zipfile for reading OOXML package contents
    - xml.etree.ElementTree for relationship and drawing metadata parsing
    - dataclasses for structured extraction results
    - motet.core.media.image_processing for optional dimension detection

Usage:
    images = extract_office_embedded_images(payload, content_type)
    for image in images:
        store.put(payload=image.payload, metadata=image.metadata)

Notes:
    - Only OOXML DOCX/PPTX packages are supported in this initial implementation.
    - The extraction is deterministic and local; OCR/captioning remains a separate
      derivation concern so it can use model-aware routing and budgets.
    - Classification is heuristic and intentionally conservative; stored metadata
      can be revised by later OCR/caption passes.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Dict, Iterable, Optional
import mimetypes
import posixpath
import re
import zipfile
from xml.etree import ElementTree


DOCX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PPTX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation"

_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_IMAGE_REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
_SUPPORTED_CONTENT_TYPES = {DOCX_CONTENT_TYPE, PPTX_CONTENT_TYPE}
_DECORATIVE_TERMS = ("logo", "icon", "bullet", "background", "footer", "header", "decorative", "brand", "watermark")
_INFORMATIVE_TERMS = (
    "chart",
    "diagram",
    "screenshot",
    "screen shot",
    "table",
    "graph",
    "architecture",
    "workflow",
    "process",
    "figure",
    "photo",
    "image",
)


@dataclass(frozen=True)
class EmbeddedOfficeImage:
    """Image payload extracted from an OOXML office package."""

    payload: bytes
    content_type: str
    package_path: str
    ordinal: int
    metadata: Dict[str, Any]


def is_office_embedded_image_eligible(content_type: str) -> bool:
    """Return whether embedded-image extraction is supported for a content type."""

    return (content_type or "").strip().lower() in _SUPPORTED_CONTENT_TYPES


def extract_office_embedded_images(payload: bytes, content_type: str) -> list[EmbeddedOfficeImage]:
    """
    Extract embedded image files from a supported OOXML office document.

    Args:
        payload: Raw DOCX/PPTX bytes.
        content_type: Source artifact MIME type.

    Returns:
        Ordered list of embedded image payloads with relationship/location metadata.
    """

    normalized_content_type = (content_type or "").strip().lower()
    if normalized_content_type not in _SUPPORTED_CONTENT_TYPES:
        return []

    with zipfile.ZipFile(_as_seekable_bytes(payload)) as archive:
        relationships = _collect_image_relationships(archive, normalized_content_type)
        media_paths = _iter_media_paths(archive, normalized_content_type)

        images: list[EmbeddedOfficeImage] = []
        seen_paths: set[str] = set()
        for ordinal, package_path in enumerate(_ordered_image_paths(relationships, media_paths), start=1):
            if package_path in seen_paths:
                continue
            seen_paths.add(package_path)
            try:
                image_bytes = archive.read(package_path)
            except KeyError:
                continue
            if not image_bytes:
                continue

            rel_meta = relationships.get(package_path, {})
            metadata = {
                "derivation_method": "office_embedded_image_ooxml_v1",
                "embedded_image_ordinal": ordinal,
                "embedded_image_path": package_path,
                **rel_meta,
            }
            content_type_guess = _image_content_type(package_path)
            metadata.update(_classify_embedded_image(image_bytes, content_type_guess, metadata))
            images.append(
                EmbeddedOfficeImage(
                    payload=image_bytes,
                    content_type=content_type_guess,
                    package_path=package_path,
                    ordinal=ordinal,
                    metadata=metadata,
                )
            )

        return images


def _as_seekable_bytes(payload: bytes) -> Any:
    import io

    return io.BytesIO(payload)


def _collect_image_relationships(archive: zipfile.ZipFile, content_type: str) -> Dict[str, Dict[str, Any]]:
    if content_type == PPTX_CONTENT_TYPE:
        return _collect_pptx_image_relationships(archive)
    if content_type == DOCX_CONTENT_TYPE:
        return _collect_docx_image_relationships(archive)
    return {}


def _collect_pptx_image_relationships(archive: zipfile.ZipFile) -> Dict[str, Dict[str, Any]]:
    relationships: Dict[str, Dict[str, Any]] = {}
    slide_paths = sorted(
        (name for name in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)),
        key=_natural_key,
    )
    for slide_path in slide_paths:
        slide_num = _trailing_number(slide_path)
        aliases = _collect_drawing_aliases(archive, slide_path)
        rels_path = _rels_path_for_part(slide_path)
        for rel_id, target in _read_image_rels(archive, rels_path).items():
            image_path = _resolve_relationship_target(slide_path, target)
            metadata: Dict[str, Any] = {
                "office_document_type": "pptx",
                "slide_num": slide_num,
                "relationship_id": rel_id,
            }
            alias = aliases.get(rel_id)
            if alias:
                metadata.update(alias)
            relationships.setdefault(image_path, metadata)
    return relationships


def _collect_docx_image_relationships(archive: zipfile.ZipFile) -> Dict[str, Dict[str, Any]]:
    relationships: Dict[str, Dict[str, Any]] = {}
    aliases = _collect_drawing_aliases(archive, "word/document.xml")
    for rel_id, target in _read_image_rels(archive, "word/_rels/document.xml.rels").items():
        image_path = _resolve_relationship_target("word/document.xml", target)
        metadata: Dict[str, Any] = {
            "office_document_type": "docx",
            "relationship_id": rel_id,
        }
        alias = aliases.get(rel_id)
        if alias:
            metadata.update(alias)
        relationships.setdefault(image_path, metadata)
    return relationships


def _collect_drawing_aliases(archive: zipfile.ZipFile, part_path: str) -> Dict[str, Dict[str, str]]:
    try:
        xml_bytes = archive.read(part_path)
    except KeyError:
        return {}

    try:
        root = ElementTree.fromstring(xml_bytes)
    except ElementTree.ParseError:
        return {}

    aliases: Dict[str, Dict[str, str]] = {}
    for blip in root.iter():
        embed_id = blip.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed")
        if not embed_id:
            continue

        alias: Dict[str, str] = {}
        parent = _nearest_non_visual_picture_props(root, blip)
        if parent is not None:
            c_nv_pr = _first_descendant_with_local_name(parent, "cNvPr")
            if c_nv_pr is not None:
                name = (c_nv_pr.attrib.get("name") or "").strip()
                descr = (c_nv_pr.attrib.get("descr") or "").strip()
                title = (c_nv_pr.attrib.get("title") or "").strip()
                if name:
                    alias["embedded_image_name"] = name
                if descr:
                    alias["embedded_image_alt_text"] = descr
                if title:
                    alias["embedded_image_title"] = title
        aliases.setdefault(embed_id, alias)
    return aliases


def _nearest_non_visual_picture_props(root: ElementTree.Element, target: ElementTree.Element) -> Optional[ElementTree.Element]:
    # ElementTree does not keep parent pointers; walk picture nodes and check whether
    # the target blip is inside each candidate picture.
    for picture in root.iter():
        if _local_name(picture.tag) not in {"pic", "picSp"}:
            continue
        if any(descendant is target for descendant in picture.iter()):
            return picture
    return None


def _first_descendant_with_local_name(root: ElementTree.Element, local_name: str) -> Optional[ElementTree.Element]:
    for element in root.iter():
        if _local_name(element.tag) == local_name:
            return element
    return None


def _read_image_rels(archive: zipfile.ZipFile, rels_path: str) -> Dict[str, str]:
    try:
        rels_xml = archive.read(rels_path)
    except KeyError:
        return {}

    try:
        root = ElementTree.fromstring(rels_xml)
    except ElementTree.ParseError:
        return {}

    rels: Dict[str, str] = {}
    for rel in root.findall(f"{{{_REL_NS}}}Relationship"):
        rel_type = rel.attrib.get("Type", "")
        target = rel.attrib.get("Target", "")
        rel_id = rel.attrib.get("Id", "")
        if rel_id and target and rel_type == _IMAGE_REL_TYPE:
            rels[rel_id] = target
    return rels


def _iter_media_paths(archive: zipfile.ZipFile, content_type: str) -> list[str]:
    prefix = "ppt/media/" if content_type == PPTX_CONTENT_TYPE else "word/media/"
    return sorted(
        (name for name in archive.namelist() if name.startswith(prefix) and not name.endswith("/")),
        key=_natural_key,
    )


def _ordered_image_paths(relationships: Dict[str, Dict[str, Any]], media_paths: Iterable[str]) -> list[str]:
    ordered = list(relationships.keys())
    ordered.extend(path for path in media_paths if path not in relationships)
    return ordered


def _rels_path_for_part(part_path: str) -> str:
    path = PurePosixPath(part_path)
    return str(path.parent / "_rels" / f"{path.name}.rels")


def _resolve_relationship_target(part_path: str, target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    base = PurePosixPath(part_path).parent
    return posixpath.normpath(str(base / target))


def _image_content_type(path: str) -> str:
    guessed, _encoding = mimetypes.guess_type(path)
    if guessed and guessed.startswith("image/"):
        return guessed
    return "application/octet-stream"


def _classify_embedded_image(payload: bytes, content_type: str, metadata: Dict[str, Any]) -> Dict[str, Any]:
    width, height = _safe_image_dimensions(payload)
    area = width * height if width and height else None
    text_fields = " ".join(
        str(metadata.get(key) or "")
        for key in (
            "embedded_image_path",
            "embedded_image_name",
            "embedded_image_alt_text",
            "embedded_image_title",
        )
    ).lower()

    decorative_hits = [term for term in _DECORATIVE_TERMS if term in text_fields]
    informative_hits = [term for term in _INFORMATIVE_TERMS if term in text_fields]

    score = 0.45
    reasons: list[str] = []
    role = "unknown"

    if width and height:
        if width <= 96 and height <= 96:
            score -= 0.35
            reasons.append("tiny_dimensions")
        elif area is not None and area >= 180_000:
            score += 0.25
            reasons.append("large_dimensions")
        elif area is not None and area >= 40_000:
            score += 0.1
            reasons.append("medium_dimensions")

    if decorative_hits:
        score -= 0.4
        reasons.append(f"decorative_terms:{','.join(decorative_hits)}")
        if "logo" in decorative_hits or "brand" in decorative_hits:
            role = "logo"
        elif "icon" in decorative_hits or "bullet" in decorative_hits:
            role = "icon"
        else:
            role = "decorative"

    if informative_hits:
        score += 0.35
        reasons.append(f"informative_terms:{','.join(informative_hits)}")
        for candidate in ("screenshot", "chart", "diagram", "table", "photo"):
            if candidate in informative_hits:
                role = candidate
                break
        if role == "unknown":
            role = "informative"

    if content_type in {"image/x-emf", "image/x-wmf"}:
        score -= 0.1
        reasons.append("legacy_vector_format")

    score = max(0.0, min(1.0, score))
    if role == "unknown":
        if score < 0.25:
            role = "decorative"
        elif score >= 0.7:
            role = "informative"

    should_ocr = score >= 0.35 and role not in {"decorative", "logo", "icon"}

    classified: Dict[str, Any] = {
        "embedded_image_role": role,
        "embedded_image_relevance_score": round(score, 3),
        "embedded_image_should_ocr": should_ocr,
        "embedded_image_classifier": "heuristic_v1",
        "embedded_image_classifier_reasons": reasons,
    }
    if width and height:
        classified["width"] = width
        classified["height"] = height
    return classified


def _safe_image_dimensions(payload: bytes) -> tuple[Optional[int], Optional[int]]:
    try:
        from .image_processing import detect_image_dimensions

        return detect_image_dimensions(payload)
    except Exception:
        return None, None


def _natural_key(value: str) -> list[Any]:
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", value)]


def _trailing_number(value: str) -> Optional[int]:
    match = re.search(r"(\d+)(?!.*\d)", value)
    return int(match.group(1)) if match else None


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag
