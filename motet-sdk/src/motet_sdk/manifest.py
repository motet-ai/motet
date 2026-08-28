"""
Motet SDK - Bundle manifest schema and validation.

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Bundle authors have a manifest.yaml in the bundle root. This module provides
the schema and validation helpers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

import yaml
from pydantic import BaseModel, Field


class BundleManifest(BaseModel):
    """Schema for bundle manifest.yaml (format_version 1)."""

    format_version: str = Field(..., description="Manifest format version, e.g. '1'")
    name: str = Field(..., description="Bundle name (slug)")
    version: str = Field(..., description="Semantic version, e.g. '0.1.0'")
    description: str = Field(default="", description="Short description of the bundle")

    model_config = {"extra": "allow"}


def load_manifest(path: Path) -> BundleManifest:
    """Load and parse manifest.yaml from a bundle directory."""
    manifest_path = path / "manifest.yaml"
    if not manifest_path.exists():
        raise FileNotFoundError(f"manifest.yaml not found in {path}")
    raw = yaml.safe_load(manifest_path.read_text())
    if not raw:
        raise ValueError("manifest.yaml is empty")
    return BundleManifest.model_validate(raw)


def validate_manifest(path: Path) -> Optional[str]:
    """
    Validate manifest at path. Returns None if valid, else error message.
    """
    try:
        load_manifest(path)
        return None
    except Exception as e:
        return str(e)
