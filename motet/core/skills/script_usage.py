"""
Motet — Agent Skill script usage metadata

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-04-27

Description:
    Parses optional per-skill script usage metadata. This metadata is a
    convenience layer for model guidance: SKILL.md remains the canonical
    Agent Skills instruction surface, while scripts.yaml can describe common
    commands, inputs, and outputs in a structured way.

Dependencies:
    - dataclasses, pathlib, typing
    - PyYAML for YAML parsing

Usage:
    from motet.core.skills.script_usage import parse_script_usage_yaml

    doc = parse_script_usage_yaml(skill_dir / "scripts.yaml")

Notes:
    - The manifest lives beside SKILL.md as scripts.yaml or usage.yaml.
    - Paths are skill-relative and cannot be absolute or escape the skill dir.
    - This parser does not register tools or grant permissions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Tuple


_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_NAME_MAX_LEN = 64
_DESCRIPTION_MAX_LEN = 1024
_COMMAND_MAX_LEN = 2048


@dataclass(frozen=True)
class ScriptUsageIO:
    """One declared script input or output."""

    name: str
    type: str = ""
    description: str = ""
    content_types: Tuple[str, ...] = field(default_factory=tuple)
    recommended_path: str = ""

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"name": self.name}
        if self.type:
            out["type"] = self.type
        if self.description:
            out["description"] = self.description
        if self.content_types:
            out["content_types"] = list(self.content_types)
        if self.recommended_path:
            out["recommended_path"] = self.recommended_path
        return out


@dataclass(frozen=True)
class ScriptUsage:
    """Structured usage guidance for one skill script."""

    name: str
    path: str
    description: str
    command: str
    inputs: Tuple[ScriptUsageIO, ...] = field(default_factory=tuple)
    outputs: Tuple[ScriptUsageIO, ...] = field(default_factory=tuple)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "description": self.description,
            "command": self.command,
            "inputs": [item.to_dict() for item in self.inputs],
            "outputs": [item.to_dict() for item in self.outputs],
        }


@dataclass(frozen=True)
class ScriptUsageDoc:
    """Parsed scripts.yaml or usage.yaml document."""

    scripts: Tuple[ScriptUsage, ...]
    raw: Dict[str, Any]

    def to_list(self) -> List[Dict[str, Any]]:
        return [script.to_dict() for script in self.scripts]


def parse_script_usage_yaml(path: Path) -> ScriptUsageDoc:
    """Read and validate script usage metadata from disk."""
    text = path.read_text(encoding="utf-8", errors="replace")
    return parse_script_usage_yaml_text(text, source_hint=str(path))


def parse_script_usage_yaml_text(text: str, source_hint: str = "") -> ScriptUsageDoc:
    """Parse scripts.yaml content from a string."""
    source = source_hint or "scripts.yaml"
    try:
        import yaml  # type: ignore[import]

        loaded = yaml.safe_load(text)
    except Exception as exc:
        raise ValueError(f"Invalid YAML in {source}: {exc}") from exc

    if loaded is None:
        return ScriptUsageDoc(scripts=(), raw={})
    if not isinstance(loaded, dict):
        raise ValueError(f"script usage manifest top-level must be a mapping in {source}")

    scripts_raw = loaded.get("scripts")
    if scripts_raw is None:
        return ScriptUsageDoc(scripts=(), raw=loaded)
    if not isinstance(scripts_raw, list):
        raise ValueError(f"script usage field 'scripts' must be a list in {source}")

    parsed: List[ScriptUsage] = []
    seen_names: set[str] = set()
    for index, raw_entry in enumerate(scripts_raw):
        if not isinstance(raw_entry, dict):
            raise ValueError(f"scripts[{index}] must be a mapping in {source}")
        script = _parse_one_script(raw_entry, index=index, source=source)
        if script.name in seen_names:
            raise ValueError(f"duplicate script usage name {script.name!r} in {source}")
        seen_names.add(script.name)
        parsed.append(script)

    return ScriptUsageDoc(scripts=tuple(parsed), raw=loaded)


def is_script_usage_manifest_path(path: str) -> bool:
    """Return whether a bundle-relative path is a per-skill usage manifest."""
    normalized = path.replace("\\", "/")
    return (
        normalized.startswith("skills/")
        and (
            normalized.endswith("/scripts.yaml")
            or normalized.endswith("/scripts.yml")
            or normalized.endswith("/usage.yaml")
            or normalized.endswith("/usage.yml")
        )
    )


def _parse_one_script(raw: Dict[str, Any], *, index: int, source: str) -> ScriptUsage:
    where = f"scripts[{index}] in {source}"
    name = _required_str(raw, "name", where=where)
    if len(name) > _NAME_MAX_LEN:
        raise ValueError(f"script usage 'name' exceeds {_NAME_MAX_LEN} characters in {where}")
    if not _NAME_RE.match(name):
        raise ValueError(
            "script usage 'name' must use lowercase letters, digits, '-' or '_' "
            f"(and start with a letter or digit) in {where}"
        )

    path = _required_str(raw, "path", where=where)
    _validate_relative_path(path, field_name="path", where=where)

    description = _optional_str(raw, "description", default="", where=where)
    if len(description) > _DESCRIPTION_MAX_LEN:
        raise ValueError(
            f"script usage 'description' exceeds {_DESCRIPTION_MAX_LEN} characters in {where}"
        )

    command = _required_str(raw, "command", where=where)
    if len(command) > _COMMAND_MAX_LEN:
        raise ValueError(f"script usage 'command' exceeds {_COMMAND_MAX_LEN} characters in {where}")

    return ScriptUsage(
        name=name,
        path=path,
        description=description,
        command=command,
        inputs=_parse_io_list(raw.get("inputs"), field_name="inputs", where=where),
        outputs=_parse_io_list(raw.get("outputs"), field_name="outputs", where=where),
    )


def _parse_io_list(raw: Any, *, field_name: str, where: str) -> Tuple[ScriptUsageIO, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError(f"script usage '{field_name}' must be a list in {where}")
    out: List[ScriptUsageIO] = []
    seen_names: set[str] = set()
    for index, item in enumerate(raw):
        item_where = f"{field_name}[{index}] in {where}"
        if not isinstance(item, dict):
            raise ValueError(f"{item_where} must be a mapping")
        name = _required_str(item, "name", where=item_where)
        if name in seen_names:
            raise ValueError(f"duplicate {field_name} name {name!r} in {where}")
        seen_names.add(name)
        recommended_path = _optional_str(item, "recommended_path", default="", where=item_where)
        if recommended_path and not recommended_path.startswith("/scratch/"):
            raise ValueError(
                f"script usage '{field_name}' recommended_path must be under /scratch in {item_where}"
            )
        content_types = _optional_str_list(item, "content_types", where=item_where)
        out.append(
            ScriptUsageIO(
                name=name,
                type=_optional_str(item, "type", default="", where=item_where),
                description=_optional_str(item, "description", default="", where=item_where),
                content_types=tuple(content_types),
                recommended_path=recommended_path,
            )
        )
    return tuple(out)


def _validate_relative_path(value: str, *, field_name: str, where: str) -> None:
    p = Path(value)
    if p.is_absolute() or ".." in p.parts or not value.strip():
        raise ValueError(
            f"script usage '{field_name}' must be a skill-relative path without '..' in {where}"
        )


def _required_str(raw: Dict[str, Any], field_name: str, *, where: str) -> str:
    if field_name not in raw:
        raise ValueError(f"script usage requires '{field_name}' in {where}")
    return _coerce_str(raw[field_name], field_name=field_name, where=where)


def _optional_str(raw: Dict[str, Any], field_name: str, *, default: str, where: str) -> str:
    if field_name not in raw or raw[field_name] is None:
        return default
    return _coerce_str(raw[field_name], field_name=field_name, where=where)


def _coerce_str(value: Any, *, field_name: str, where: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"script usage field '{field_name}' must be a string in {where}")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"script usage field '{field_name}' must not be empty in {where}")
    return normalized


def _optional_str_list(raw: Dict[str, Any], field_name: str, *, where: str) -> List[str]:
    value = raw.get(field_name)
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"script usage field '{field_name}' must be a list in {where}")
    out: List[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(
                f"script usage field '{field_name}[{index}]' must be a non-empty string in {where}"
            )
        out.append(item.strip())
    return out
