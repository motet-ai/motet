"""
Motet — in-process skill registry

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-28

Description:
    Tracks bundle-loaded skills on the current worker: metadata, filesystem path
    to SKILL.md, and optional deploy targeting snapshot for future scope checks.

Dependencies:
    - dataclasses, threading, typing

Usage:
    from motet.core.skills.registry import get_skill_registry

    reg = get_skill_registry()
    reg.register_bundle_skill(...)
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional


@dataclass
class RegisteredSkill:
    """Runtime record for one loaded skill."""

    skill_id: str
    bundle_id: str
    name: str
    description: str
    skill_md_path: Path
    source: str = "bundle"
    bundle_version: Optional[str] = None
    targeting: Optional[Dict[str, Any]] = None


class SkillRegistry:
    """Thread-safe registry keyed by canonical ``skill_id`` (``bundle_id.name``)."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._by_id: Dict[str, RegisteredSkill] = {}

    def register(self, rec: RegisteredSkill) -> None:
        with self._lock:
            self._by_id[rec.skill_id] = rec

    def unregister_bundle(self, bundle_id: str) -> None:
        prefix = f"{bundle_id}."
        with self._lock:
            for key in list(self._by_id.keys()):
                if key.startswith(prefix):
                    del self._by_id[key]

    def unregister_skill(self, skill_id: str) -> None:
        with self._lock:
            self._by_id.pop(skill_id, None)

    def unregister_source(self, source: str) -> None:
        """Remove all skills registered from a non-bundle source such as project or user."""
        normalized = str(source or "").strip()
        if not normalized:
            return
        with self._lock:
            for key, rec in list(self._by_id.items()):
                if rec.source == normalized:
                    del self._by_id[key]

    def get(self, skill_id: str) -> Optional[RegisteredSkill]:
        with self._lock:
            return self._by_id.get(skill_id)

    def list_all(self) -> List[RegisteredSkill]:
        with self._lock:
            return list(self._by_id.values())

    def iter_bundle(self, bundle_id: str) -> Iterator[RegisteredSkill]:
        prefix = f"{bundle_id}."
        with self._lock:
            for rec in self._by_id.values():
                if rec.skill_id.startswith(prefix):
                    yield rec


_registry: Optional[SkillRegistry] = None
_registry_lock = threading.Lock()


def get_skill_registry() -> SkillRegistry:
    global _registry
    with _registry_lock:
        if _registry is None:
            _registry = SkillRegistry()
        return _registry
