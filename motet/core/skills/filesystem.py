"""
Motet — filesystem Agent Skills discovery

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-04-25

Description:
    Scans project, user, and configured `.agents/skills` directories for
    agentskills-compatible SKILL.md files and registers them in the process-local
    SkillRegistry for catalog disclosure and activation.

Dependencies:
    - os, pathlib, typing
    - motet.core.config.Config for optional discovery roots
    - motet.core.skills.parser and registry for SKILL.md loading

Usage:
    from motet.core.skills.filesystem import refresh_filesystem_skills

    refresh_filesystem_skills(project_root=Path.cwd())

Notes:
    - Filesystem skill IDs use stable synthetic namespaces: project.*, user.*,
      and configured.*. Bundle skills keep their bundle_id.name IDs.
    - Malformed filesystem skills are skipped with warnings so one bad local
      skill does not break an agent turn.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

import structlog

from motet.core.config import Config
from motet.core.skills.parser import parse_skill_markdown
from motet.core.skills.registry import RegisteredSkill, get_skill_registry

logger = structlog.get_logger(__name__)


_FILESYSTEM_SOURCES = ("project", "user", "configured")


def _split_configured_paths(raw: Optional[str]) -> List[Path]:
    if not raw:
        return []
    paths: List[Path] = []
    for chunk in raw.replace(os.pathsep, ",").split(","):
        value = chunk.strip()
        if value:
            paths.append(Path(value).expanduser())
    return paths


def _candidate_skill_dirs(root: Path) -> Iterable[Path]:
    if not root.exists():
        return []
    if (root / "SKILL.md").is_file():
        return [root]
    if not root.is_dir():
        return []
    return [child for child in sorted(root.iterdir()) if child.is_dir() and (child / "SKILL.md").is_file()]


def _register_skill_dir(skill_dir: Path, *, source: str, namespace: str) -> Optional[RegisteredSkill]:
    md_path = skill_dir / "SKILL.md"
    try:
        doc = parse_skill_markdown(md_path)
    except Exception as e:
        logger.warning(
            "filesystem_skill_parse_failed",
            source=source,
            path=str(md_path),
            error=str(e),
        )
        return None

    if doc.name != skill_dir.name:
        logger.warning(
            "filesystem_skill_name_dir_mismatch",
            source=source,
            path=str(md_path),
            dir_name=skill_dir.name,
            name=doc.name,
        )

    rec = RegisteredSkill(
        skill_id=f"{namespace}.{doc.name}",
        bundle_id=namespace,
        name=doc.name,
        description=doc.description,
        skill_md_path=md_path,
        source=source,
        bundle_version=None,
    )
    get_skill_registry().register(rec)
    return rec


def refresh_filesystem_skills(
    *,
    project_root: Optional[Path] = None,
    configured_paths: Optional[Sequence[Path]] = None,
    include_user: bool = True,
    config: Optional[Config] = None,
) -> List[RegisteredSkill]:
    """
    Refresh filesystem skill registrations and return the loaded records.

    The refresh is idempotent for filesystem sources: prior project/user/configured
    records are removed before scanning current roots.
    """
    cfg = config or Config()
    if not bool(getattr(cfg, "enable_filesystem_skills", True)):
        return []

    reg = get_skill_registry()
    for source in _FILESYSTEM_SOURCES:
        reg.unregister_source(source)

    roots: List[tuple[str, str, Path]] = []
    configured_project_root = getattr(cfg, "project_root", None)
    root = project_root or (Path(configured_project_root).expanduser() if configured_project_root else Path.cwd())
    roots.append(("project", "project", root / ".agents" / "skills"))

    if include_user:
        roots.append(("user", "user", Path.home() / ".agents" / "skills"))

    explicit_paths = list(configured_paths or [])
    explicit_paths.extend(_split_configured_paths(getattr(cfg, "skill_paths", None)))
    for idx, path in enumerate(explicit_paths):
        roots.append(("configured", f"configured{idx + 1}", path.expanduser()))

    registered: List[RegisteredSkill] = []
    seen_dirs = set()
    for source, namespace, root_path in roots:
        for skill_dir in _candidate_skill_dirs(root_path):
            try:
                resolved = skill_dir.resolve()
            except Exception:
                resolved = skill_dir
            if resolved in seen_dirs:
                continue
            seen_dirs.add(resolved)
            rec = _register_skill_dir(skill_dir, source=source, namespace=namespace)
            if rec:
                registered.append(rec)

    return registered


__all__ = ["refresh_filesystem_skills"]
