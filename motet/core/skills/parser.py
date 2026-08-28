"""
Motet — SKILL.md parsing

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Parses agentskills-style SKILL.md files: optional YAML frontmatter with
    ``name`` and ``description`` plus markdown body used for progressive
    disclosure / injection.

Dependencies:
    - dataclasses, pathlib, typing
    - yaml (optional; frontmatter requires PyYAML when present)

Usage:
    from motet.core.skills.parser import parse_skill_markdown

    doc = parse_skill_markdown(path)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


@dataclass(frozen=True)
class ParsedSkillDoc:
    """Materialized SKILL.md: frontmatter fields + markdown body."""

    name: str
    description: str
    body: str
    raw_frontmatter: Dict[str, Any]


_FRONT_MATTER_RE = re.compile(
    r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n(.*)\Z",
    re.DOTALL,
)

_SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SKILL_NAME_MAX_LEN = 64
_SKILL_DESCRIPTION_MAX_LEN = 1024
_SKILL_COMPATIBILITY_MAX_LEN = 500


def _required_non_empty_string(
    loaded: Dict[str, Any],
    key: str,
    *,
    source_hint: str,
) -> str:
    """Return required frontmatter string field after strict type/empty checks."""
    if key not in loaded:
        raise ValueError(f"SKILL frontmatter requires '{key}' in {source_hint}")
    value = loaded.get(key)
    if not isinstance(value, str):
        raise ValueError(
            f"SKILL frontmatter field '{key}' must be a string in {source_hint}"
        )
    normalized = value.strip()
    if not normalized:
        raise ValueError(
            f"SKILL frontmatter requires non-empty '{key}' in {source_hint}"
        )
    return normalized


def _validate_optional_frontmatter_fields(
    loaded: Dict[str, Any], *, source_hint: str
) -> None:
    """Validate public-spec optional SKILL.md fields when present."""
    compatibility = loaded.get("compatibility")
    if compatibility is not None:
        if not isinstance(compatibility, str):
            raise ValueError(
                f"SKILL frontmatter field 'compatibility' must be a string in {source_hint}"
            )
        if len(compatibility.strip()) > _SKILL_COMPATIBILITY_MAX_LEN:
            raise ValueError(
                f"SKILL frontmatter field 'compatibility' exceeds {_SKILL_COMPATIBILITY_MAX_LEN} characters in {source_hint}"
            )

    metadata = loaded.get("metadata")
    if metadata is not None:
        if not isinstance(metadata, dict):
            raise ValueError(
                f"SKILL frontmatter field 'metadata' must be a mapping in {source_hint}"
            )
        for k, v in metadata.items():
            if not isinstance(k, str) or not isinstance(v, str):
                raise ValueError(
                    f"SKILL frontmatter field 'metadata' must be string-to-string in {source_hint}"
                )

    allowed_tools = loaded.get("allowed-tools")
    if allowed_tools is not None and not isinstance(allowed_tools, str):
        raise ValueError(
            f"SKILL frontmatter field 'allowed-tools' must be a string in {source_hint}"
        )


def parse_skill_markdown(path: Path) -> ParsedSkillDoc:
    """
    Read ``path`` and extract frontmatter + body.

    Raises:
        ValueError: missing file, missing name/description, or invalid YAML.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    return parse_skill_markdown_text(text, source_hint=str(path))


def parse_skill_markdown_text(text: str, source_hint: str = "") -> ParsedSkillDoc:
    """Parse SKILL.md content from a string (tests and zip extraction)."""
    raw = text.lstrip("\ufeff").strip()
    m = _FRONT_MATTER_RE.match(raw)
    if not m:
        raise ValueError(
            f"SKILL.md must start with YAML frontmatter (---) in {source_hint or 'document'}"
        )
    fm_raw, body = m.group(1), m.group(2).lstrip("\n")
    try:
        import yaml  # type: ignore[import]

        loaded = yaml.safe_load(fm_raw) or {}
    except Exception as e:
        raise ValueError(f"Invalid YAML frontmatter in {source_hint}: {e}") from e
    if not isinstance(loaded, dict):
        raise ValueError(f"SKILL frontmatter must be a mapping in {source_hint}")
    source = source_hint or "document"
    name = _required_non_empty_string(loaded, "name", source_hint=source)
    description = _required_non_empty_string(
        loaded, "description", source_hint=source
    )
    if len(name) > _SKILL_NAME_MAX_LEN:
        raise ValueError(
            f"SKILL frontmatter field 'name' exceeds {_SKILL_NAME_MAX_LEN} characters in {source}"
        )
    if not _SKILL_NAME_RE.match(name):
        raise ValueError(
            "SKILL frontmatter field 'name' must use lowercase letters, numbers, and single hyphens only "
            f"in {source}"
        )
    if len(description) > _SKILL_DESCRIPTION_MAX_LEN:
        raise ValueError(
            f"SKILL frontmatter field 'description' exceeds {_SKILL_DESCRIPTION_MAX_LEN} characters in {source}"
        )
    _validate_optional_frontmatter_fields(loaded, source_hint=source)
    return ParsedSkillDoc(
        name=name,
        description=description,
        body=body.strip(),
        raw_frontmatter=dict(loaded),
    )


def skill_dir_matches_name(skill_dir_name: str, frontmatter_name: str) -> bool:
    """Directory segment under ``skills/`` should match the declared slug (lint helper)."""
    return skill_dir_name.strip() == frontmatter_name.strip()
