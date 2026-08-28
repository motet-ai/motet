"""
Motet — Agent Skills

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-28

Description:
    Bundle-resident and filesystem SKILL.md discovery, worker registry, catalog
    disclosure, and explicit skill activation. Canonical SkillRef rows ride
    LLMRequest for observability (adapters ignore).

Dependencies:
    - motet.core.skills.parser: Frontmatter + body parsing
    - motet.core.skills.registry: In-process registration per loaded bundle
    - motet.core.skills.assembly: Candidate filtering and message construction

Usage:
    from motet.core.skills import get_skill_registry, assemble_skills_for_turn

Notes:
    - Skill ids are ``{bundle_id}.{name}`` where ``name`` is the SKILL.md frontmatter slug.
    - Agent skill selection via AgentConfig.skill_mode + skill_ids.
"""

from motet.core.skills.assembly import (
    activate_explicit_skills_for_turn,
    activate_skill_record,
    assemble_skills_for_turn,
    build_skill_catalog_for_turn,
    find_skill_by_name_or_id,
)
from motet.core.skills.parser import ParsedSkillDoc, parse_skill_markdown
from motet.core.skills.filesystem import refresh_filesystem_skills
from motet.core.skills.registry import RegisteredSkill, SkillRegistry, get_skill_registry
from motet.core.skills.runners import (
    RunnerArg,
    RunnerSpec,
    RunnersDoc,
    parse_runners_yaml,
    parse_runners_yaml_text,
)
from motet.core.skills.runtime import register_runners_for_skill

__all__ = [
    "assemble_skills_for_turn",
    "activate_explicit_skills_for_turn",
    "activate_skill_record",
    "build_skill_catalog_for_turn",
    "find_skill_by_name_or_id",
    "refresh_filesystem_skills",
    "get_skill_registry",
    "parse_skill_markdown",
    "ParsedSkillDoc",
    "RegisteredSkill",
    "SkillRegistry",
    "RunnerArg",
    "RunnerSpec",
    "RunnersDoc",
    "parse_runners_yaml",
    "parse_runners_yaml_text",
    "register_runners_for_skill",
]
