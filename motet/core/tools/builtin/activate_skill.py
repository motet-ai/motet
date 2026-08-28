"""
Motet - Agent skill activation tool

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-04-25

Description:
    Exposes progressive-disclosure Agent Skills activation to the model. The
    catalog tells the model which skills exist; this tool loads one full SKILL.md
    body plus a bounded resource listing when a matching task requires it.

Dependencies:
    - pydantic for the LLM-visible tool schema
    - motet.core.skills assembly and registry helpers
    - motet.core.tools.protocol for consistent tool responses

Usage:
    The model calls core.activate_skill with either {"name": "pdf"} or
    {"skill_id": "skills-vendor-demo.pdf"} after seeing the skill catalog.

Notes:
    - The tool does not eagerly read bundled resources referenced by the skill.
      It lists them so the model can load specific files as needed.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field, model_validator

from motet.core.skills.assembly import activate_skill_record, find_skill_by_name_or_id
from motet.core.tools.protocol import err, ok

from ..registry import ToolRegistry


class Params(BaseModel):
    name: Optional[str] = Field(
        default=None,
        description="Skill frontmatter name to activate, e.g. 'pdf'.",
    )
    skill_id: Optional[str] = Field(
        default=None,
        description="Canonical skill id to activate, e.g. 'skills-vendor-demo.pdf'.",
    )

    @model_validator(mode="after")
    def _require_identifier(self) -> "Params":
        if not (self.name or self.skill_id):
            raise ValueError("Either name or skill_id is required")
        return self


def _fmt(result: Dict[str, Any]) -> str:
    payload = result.get("result") or {}
    if isinstance(payload, dict):
        sid = payload.get("skill_id") or payload.get("name") or "unknown"
        return f"activated_skill(skill_id={sid})"
    return "activated_skill"


def run(params: Dict[str, Any]) -> Dict[str, Any]:
    rec = find_skill_by_name_or_id(
        name=params.get("name"),
        skill_id=params.get("skill_id"),
    )
    if rec is None:
        return err(
            "Skill not found",
            meta={
                "name": params.get("name"),
                "skill_id": params.get("skill_id"),
            },
        )

    try:
        _message, _ref, payload = activate_skill_record(rec)
    except Exception as e:
        return err(
            f"Failed to activate skill: {e}",
            meta={"skill_id": rec.skill_id, "name": rec.name},
        )
    return ok(payload)


def register(registry: ToolRegistry) -> None:
    registry.register(
        name="core.activate_skill",
        description=(
            "Load the full instructions for one Agent Skill from the available skill catalog. "
            "Call this before using a matching skill."
        ),
        func=run,
        tool_schema=Params,
        category="meta",
        keywords=["skill", "agent skill", "activate skill", "SKILL.md"],
        observation_formatter=_fmt,
        contextualize_observation=True,
    )


__all__ = ["register"]
