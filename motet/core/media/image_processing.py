"""
Motet - Image Processing Utilities

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-27

Description:
    Utilities for processing images (resizing, dimension detection) for derivations.
    Used by the Derivation Pipeline to create optimized image versions
    for LLM vision models. AVIF decode uses Pillow's built-in libavif/dav1d
    support (Pillow 11.3+ wheels). HEIC/HEIF decode is off unless an operator
    sets MOTET_ENABLE_HEIC_HEIF_CONVERSION and installs pillow-heif themselves.

Dependencies:
    - Pillow (PIL): Image processing, including native AVIF when the wheel
      includes libavif (Pillow 11.3+)
    - io: Byte streams
    - os: Feature flag checks for optional AVIF and HEIC/HEIF conversion

Usage:
    from motet.core.media.image_processing import resize_image_bytes, detect_image_dimensions
    
    dimensions = detect_image_dimensions(image_bytes)
    resized = resize_image_bytes(image_bytes, max_side=1600, quality=85)
"""

import io
import os
import structlog
from typing import Tuple, Optional, Dict, Any

logger = structlog.get_logger(__name__)

# Lazy import PIL to avoid requiring it in all images
# Only imported when image processing functions are actually called
_heic_opener_registered = False


def _env_flag_enabled(name: str) -> bool:
    """Return whether an environment flag is truthy."""
    return os.getenv(name, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _avif_conversion_enabled() -> bool:
    """Return whether optional AVIF decoding is enabled for this runtime."""
    return _env_flag_enabled("MOTET_ENABLE_AVIF_CONVERSION")


def _heic_heif_conversion_enabled() -> bool:
    """Return whether optional HEIC/HEIF decoding is enabled for this runtime."""
    return _env_flag_enabled("MOTET_ENABLE_HEIC_HEIF_CONVERSION")


def _pillow_avif_available() -> bool:
    """Return whether this Pillow build can decode AVIF natively."""
    try:
        from PIL import features

        return bool(features.check("avif"))
    except Exception:
        return False


def _get_pil_image():
    """Lazy import PIL.Image to avoid import errors in images that don't need it."""
    try:
        from PIL import Image
        return Image
    except ImportError:
        raise ImportError(
            "Pillow (PIL) is required for image processing. "
            "Install with: pip install Pillow>=11.3.0"
        )


def _register_heic_opener_if_enabled() -> None:
    """Register pillow-heif only when the HEIC flag is on. Never for AVIF."""
    global _heic_opener_registered
    if _heic_opener_registered:
        return
    try:
        from pillow_heif import register_heif_opener

        register_heif_opener()
        _heic_opener_registered = True
    except ImportError as e:
        raise ValueError(
            "HEIC/HEIF conversion requires the optional pillow-heif package, "
            "which is not included in default Motet images"
        ) from e


def _effective_image_content_type(
    image_bytes: bytes,
    content_type: Optional[str] = None,
) -> str:
    """Prefer magic-byte AVIF/HEIC over a caller-supplied MIME type."""
    fallback = (content_type or "application/octet-stream").lower()
    sniffed = detect_image_content_type_from_bytes(image_bytes, fallback=fallback)
    if sniffed in AVIF_CONTENT_TYPES or sniffed in HEIC_HEIF_CONTENT_TYPES:
        return sniffed
    return fallback


def _guard_optional_decode(content_type: str) -> None:
    """Refuse AVIF/HEIC unless the matching flag (and codec) is available."""
    if content_type in AVIF_CONTENT_TYPES:
        if not _avif_conversion_enabled():
            raise ValueError(
                f"{content_type} conversion requires "
                "MOTET_ENABLE_AVIF_CONVERSION=1"
            )
        if not _pillow_avif_available():
            raise ValueError(
                f"{content_type} conversion requires Pillow 11.3+ with AVIF "
                "enabled in the wheel (libavif/dav1d)"
            )
        return
    if content_type in HEIC_HEIF_CONTENT_TYPES:
        if not _heic_heif_conversion_enabled():
            raise ValueError(
                f"{content_type} conversion requires "
                "MOTET_ENABLE_HEIC_HEIF_CONVERSION=1 because HEIC/HEIF codec "
                "dependencies may carry additional license obligations"
            )
        _register_heic_opener_if_enabled()


def _open_pil_image(image_bytes: bytes, content_type: Optional[str] = None):
    """Open image bytes after gating optional AVIF/HEIC decode."""
    Image = _get_pil_image()
    _guard_optional_decode(_effective_image_content_type(image_bytes, content_type))
    return Image.open(io.BytesIO(image_bytes))

# Default derivation parameters
DEFAULT_THUMB_MAX_SIDE = 512
DEFAULT_BASE_MAX_SIDE = 1600
DEFAULT_DETAIL_MAX_SIDE = 2048
DEFAULT_JPEG_QUALITY = 85
DEFAULT_PNG_OPTIMIZE = True
MODEL_INPUT_CONTENT_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
AVIF_CONTENT_TYPES = {"image/avif"}
HEIC_HEIF_CONTENT_TYPES = {"image/heic", "image/heif"}


def is_image_content_type(content_type: str) -> bool:
    """
    Check if content type represents an image.
    
    Args:
        content_type: MIME type string
        
    Returns:
        True if content type is an image type
    """
    return content_type.startswith("image/")


def detect_image_content_type_from_bytes(
    image_bytes: bytes,
    fallback: str = "application/octet-stream",
) -> str:
    """
    Infer image MIME type from magic bytes, falling back to the supplied value.

    Browser upload metadata can be wrong when users rename files. We keep this
    lightweight and conservative so storage metadata matches the actual payload
    for model rendering and derivation policy.
    """

    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    if len(image_bytes) >= 12 and image_bytes[4:8] == b"ftyp":
        brand = image_bytes[8:12]
        if brand in {b"avif", b"avis"}:
            return "image/avif"
        if brand in {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"}:
            return "image/heif"
    return fallback


def detect_image_dimensions(image_bytes: bytes) -> Tuple[int, int]:
    """
    Detect width and height of an image from bytes.
    
    Args:
        image_bytes: Raw image bytes
        
    Returns:
        Tuple of (width, height)
        
    Raises:
        ValueError: If image cannot be decoded
    """
    try:
        img = _open_pil_image(image_bytes)
        return img.size  # Returns (width, height)
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"Failed to detect image dimensions: {e}")


def resize_image_bytes(
    image_bytes: bytes,
    max_side: int,
    quality: int = DEFAULT_JPEG_QUALITY,
    format_override: Optional[str] = None,
) -> bytes:
    """
    Resize image bytes to a maximum side length while preserving aspect ratio.
    
    Args:
        image_bytes: Raw image bytes
        max_side: Maximum width or height (whichever is larger)
        quality: JPEG quality (1-100, only used for JPEG output)
        format_override: Force output format ("JPEG", "PNG", "WEBP"). If None, preserves original format.
        
    Returns:
        Resized image bytes
        
    Raises:
        ValueError: If image cannot be processed
    """
    try:
        Image = _get_pil_image()
        img = _open_pil_image(image_bytes)
        original_format = img.format or "JPEG"
        
        # Determine output format before any early return. Callers may force
        # JPEG/WebP for model-safe derivations even when no resize is needed.
        output_format = format_override or original_format
        if output_format not in ["JPEG", "PNG", "WEBP"]:
            output_format = "JPEG"

        # Calculate new dimensions preserving aspect ratio
        width, height = img.size
        if width <= max_side and height <= max_side:
            # Image is already small enough, but still honor format_override.
            if output_format == "JPEG" and img.mode in ("RGBA", "LA", "P"):
                background = Image.new("RGB", img.size, (255, 255, 255))
                if img.mode == "P":
                    img = img.convert("RGBA")
                background.paste(img, mask=img.split()[-1] if img.mode == "RGBA" else None)
                img = background
            elif output_format == "JPEG" and img.mode != "RGB":
                img = img.convert("RGB")
            output = io.BytesIO()
            small_image_save_kwargs: Dict[str, Any] = {"format": output_format}
            if output_format == "JPEG":
                small_image_save_kwargs["quality"] = quality
                small_image_save_kwargs["optimize"] = True
            elif output_format == "PNG":
                small_image_save_kwargs["optimize"] = DEFAULT_PNG_OPTIMIZE
            img.save(output, **small_image_save_kwargs)
            return output.getvalue()
        
        # Calculate scaling factor
        if width > height:
            new_width = max_side
            new_height = int(height * (max_side / width))
        else:
            new_height = max_side
            new_width = int(width * (max_side / height))
        
        # Resize with high-quality resampling (Lanczos)
        resized = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
        
        # Convert RGBA to RGB for JPEG
        if output_format == "JPEG" and resized.mode in ("RGBA", "LA", "P"):
            background = Image.new("RGB", resized.size, (255, 255, 255))
            if resized.mode == "P":
                resized = resized.convert("RGBA")
            background.paste(resized, mask=resized.split()[-1] if resized.mode == "RGBA" else None)
            resized = background
        elif output_format == "JPEG" and resized.mode != "RGB":
            resized = resized.convert("RGB")
        
        # Save to bytes
        output = io.BytesIO()
        save_kwargs: Dict[str, Any] = {"format": output_format}
        
        if output_format == "JPEG":
            save_kwargs["quality"] = quality
            save_kwargs["optimize"] = True
        elif output_format == "PNG":
            save_kwargs["optimize"] = DEFAULT_PNG_OPTIMIZE
        
        resized.save(output, **save_kwargs)
        return output.getvalue()
        
    except Exception as e:
        raise ValueError(f"Failed to resize image: {e}")


def normalize_model_image_bytes(
    image_bytes: bytes,
    content_type: str,
    quality: int = DEFAULT_JPEG_QUALITY,
) -> Tuple[bytes, str]:
    """
    Convert image bytes to a model-supported MIME type when required.

    Most hosted vision model APIs accept JPEG, PNG, GIF, and WebP, but not AVIF
    or HEIC/HEIF. Motet stores originals unchanged, then normalizes unsupported
    formats at the model-rendering boundary. AVIF conversion uses Pillow native
    decode when MOTET_ENABLE_AVIF_CONVERSION=1. HEIC/HEIF conversion stays off
    unless MOTET_ENABLE_HEIC_HEIF_CONVERSION=1 and an operator-installed
    pillow-heif is present.
    """
    normalized_content_type = _effective_image_content_type(image_bytes, content_type)
    if normalized_content_type in MODEL_INPUT_CONTENT_TYPES:
        return image_bytes, normalized_content_type
    if not normalized_content_type.startswith("image/"):
        return image_bytes, normalized_content_type

    try:
        Image = _get_pil_image()
        img = _open_pil_image(image_bytes, content_type=normalized_content_type)
        img.load()

        if img.mode in ("RGBA", "LA", "P"):
            background = Image.new("RGB", img.size, (255, 255, 255))
            if img.mode == "P":
                img = img.convert("RGBA")
            alpha = img.split()[-1] if img.mode in ("RGBA", "LA") else None
            background.paste(img, mask=alpha)
            img = background
        elif img.mode != "RGB":
            img = img.convert("RGB")

        output = io.BytesIO()
        img.save(output, format="JPEG", quality=quality, optimize=True)
        converted = output.getvalue()
        logger.info(
            "model_image_normalized",
            source_content_type=normalized_content_type,
            target_content_type="image/jpeg",
            source_bytes=len(image_bytes),
            target_bytes=len(converted),
        )
        return converted, "image/jpeg"
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(
            f"Failed to convert {normalized_content_type!r} image to model-supported JPEG: {e}"
        )


def generate_image_derivations(
    image_bytes: bytes,
    content_type: str,
    generate_thumb: bool = True,
    generate_base: bool = True,
    generate_detail: bool = False,
) -> Dict[str, Any]:
    """
    Generate multiple image derivations (thumb, base, detail) from source image.
    
    Args:
        image_bytes: Raw source image bytes
        content_type: MIME type of source image
        generate_thumb: Whether to generate thumbnail (default: True)
        generate_base: Whether to generate base derivation (default: True)
        generate_detail: Whether to generate detail derivation (default: False, lazy)
        
    Returns:
        Dict with derivation_name -> (bytes, metadata) mapping:
        {
            "thumb": (bytes, {"width": int, "height": int, "max_side": int, ...}),
            "base": (bytes, {...}),
            "detail": (bytes, {...})  # if generate_detail=True
        }
        
    Raises:
        ValueError: If image cannot be processed
    """
    derivations = {}
    
    # Detect original dimensions
    original_width, original_height = detect_image_dimensions(image_bytes)
    
    # Determine output format (preserve original if possible)
    output_format = "JPEG"  # Default
    if content_type == "image/png":
        output_format = "PNG"
    elif content_type == "image/webp":
        output_format = "WEBP"
    
    # Generate thumbnail
    if generate_thumb:
        try:
            thumb_bytes = resize_image_bytes(
                image_bytes,
                max_side=DEFAULT_THUMB_MAX_SIDE,
                quality=75,  # Lower quality for thumbnails
                format_override=output_format,
            )
            thumb_width, thumb_height = detect_image_dimensions(thumb_bytes)
            derivations["thumb"] = (
                thumb_bytes,
                {
                    "width": thumb_width,
                    "height": thumb_height,
                    "max_side": DEFAULT_THUMB_MAX_SIDE,
                    "quality": 75,
                    "format": output_format,
                    "bytes": len(thumb_bytes),
                },
            )
        except Exception as e:
            logger.warning("thumb_generation_failed", error=str(e))
    
    # Generate base (default for LLM)
    if generate_base:
        try:
            base_bytes = resize_image_bytes(
                image_bytes,
                max_side=DEFAULT_BASE_MAX_SIDE,
                quality=DEFAULT_JPEG_QUALITY,
                format_override=output_format,
            )
            base_width, base_height = detect_image_dimensions(base_bytes)
            derivations["base"] = (
                base_bytes,
                {
                    "width": base_width,
                    "height": base_height,
                    "max_side": DEFAULT_BASE_MAX_SIDE,
                    "quality": DEFAULT_JPEG_QUALITY,
                    "format": output_format,
                    "bytes": len(base_bytes),
                },
            )
        except Exception as e:
            logger.warning("base_generation_failed", error=str(e))
    
    # Generate detail (high-res for text extraction)
    if generate_detail:
        try:
            detail_bytes = resize_image_bytes(
                image_bytes,
                max_side=DEFAULT_DETAIL_MAX_SIDE,
                quality=90,  # Higher quality for detail
                format_override=output_format,
            )
            detail_width, detail_height = detect_image_dimensions(detail_bytes)
            derivations["detail"] = (
                detail_bytes,
                {
                    "width": detail_width,
                    "height": detail_height,
                    "max_side": DEFAULT_DETAIL_MAX_SIDE,
                    "quality": 90,
                    "format": output_format,
                    "bytes": len(detail_bytes),
                },
            )
        except Exception as e:
            logger.warning("detail_generation_failed", error=str(e))
    
    # Add original dimensions to all derivations
    for name, (_, metadata) in derivations.items():
        metadata["original_width"] = original_width
        metadata["original_height"] = original_height
    
    return derivations

