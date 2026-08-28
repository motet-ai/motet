"""
Motet — skill relevance + turn assembly

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Builds skill catalogs from the worker registry, detects explicit user
    activations, loads SKILL.md bodies on demand, and returns system Messages
    plus SkillRef rows.

Dependencies:
    - hashlib, re, structlog, typing
    - motet.core.types.Message, SkillRef
    - motet.core.skills.parser, motet.core.skills.registry

Usage:
    from motet.core.skills.assembly import assemble_skills_for_turn
"""

from __future__ import annotations

import hashlib
import re
from html import escape
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

import structlog

from motet.core.skills.parser import parse_skill_markdown
from motet.core.skills.registry import RegisteredSkill, get_skill_registry
from motet.core.skills.script_usage import parse_script_usage_yaml
from motet.core.types import Message, SkillRef

logger = structlog.get_logger(__name__)

_ACTIVATE_SKILL_TOOL = "core.activate_skill"
_WORKSPACE_SHELL_TOOL = "core.workspace_shell_exec"
_FAIL_FAST_SHELL_PRELUDE = "set -euo pipefail"


_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_+-]*")
_STOPWORDS = {
    "about",
    "after",
    "also",
    "and",
    "any",
    "are",
    "ask",
    "asks",
    "can",
    "check",
    "does",
    "file",
    "first",
    "for",
    "from",
    "has",
    "have",
    "into",
    "its",
    "make",
    "out",
    "please",
    "produce",
    "save",
    "skill",
    "that",
    "the",
    "then",
    "this",
    "use",
    "user",
    "want",
    "wants",
    "what",
    "when",
    "with",
}


def _normalize_allowlist(skill_ids: Optional[Sequence[str]]) -> Set[str]:
    if not skill_ids:
        return set()
    return {str(s).strip() for s in skill_ids if str(s).strip()}


def _source_priority(source: str) -> int:
    """Lower numbers win when several skills share the same visible name."""
    order = {
        "project": 0,
        "configured": 1,
        "user": 2,
        "bundle": 3,
        "core": 4,
    }
    return order.get(str(source or "").strip().lower(), 50)


def _tokens(text: str, *, include_stopwords: bool = False) -> Set[str]:
    tokens = {m.group(0) for m in _TOKEN_RE.finditer(text.lower())}
    if include_stopwords:
        return tokens
    return {token for token in tokens if len(token) >= 3 and token not in _STOPWORDS}


def _skill_relevance_score(user_text: str, rec: RegisteredSkill) -> int:
    """Score skill relevance so strong name/id hits survive the max-skills cap."""
    if not user_text.strip():
        return 0

    user_all_tokens = _tokens(user_text, include_stopwords=True)
    user_terms = _tokens(user_text)
    if not user_terms and not user_all_tokens:
        return 0

    skill_id_tail = rec.skill_id.rsplit(".", 1)[-1].lower()
    name_tokens = _tokens(rec.name.replace("-", " "), include_stopwords=True)
    id_tokens = _tokens(skill_id_tail.replace("-", " "), include_stopwords=True)
    description_tokens = _tokens(rec.description)

    score = 0

    # The strongest signal is the user naming the skill directly: "Use the PDF skill".
    if rec.name.lower() in user_all_tokens or skill_id_tail in user_all_tokens:
        score += 100
    if name_tokens & user_all_tokens:
        score += 60 * len(name_tokens & user_all_tokens)
    if id_tokens & user_all_tokens:
        score += 50 * len(id_tokens & user_all_tokens)

    description_hits = user_terms & description_tokens
    score += 10 * len(description_hits)

    # Preserve a small amount of phrase/sub-string recall without letting common
    # words such as "this" or "skill" make unrelated skills look equivalent.
    haystack = f"{rec.name}\n{skill_id_tail}\n{rec.description}".lower()
    for term in user_terms:
        if term in haystack and term not in description_hits:
            score += 2

    return score


def _skill_relevant(user_text: str, rec: RegisteredSkill) -> bool:
    """Return whether a skill has any positive relevance signal."""
    return _skill_relevance_score(user_text, rec) > 0


def _rank_relevant_skills(user_text: str, candidates: Sequence[RegisteredSkill]) -> List[RegisteredSkill]:
    scored = [
        (score, rec)
        for rec in candidates
        if (score := _skill_relevance_score(user_text, rec)) > 0
    ]
    scored.sort(key=lambda item: (-item[0], item[1].skill_id))
    return [rec for _, rec in scored]


def _candidate_skills(
    skill_allowlist: Optional[Sequence[str]],
    *,
    discovery_mode: bool = False,
) -> List[RegisteredSkill]:
    allow = _normalize_allowlist(skill_allowlist)
    if not allow and not discovery_mode:
        return []

    reg = get_skill_registry()
    candidates: List[RegisteredSkill] = []
    if discovery_mode:
        discovered = sorted(reg.list_all(), key=lambda r: r.skill_id)
        if allow:
            allow_set = set(allow)
            candidates = [rec for rec in discovered if rec.skill_id in allow_set]
        else:
            candidates = discovered
    else:
        for sid in sorted(allow):
            rec = reg.get(sid)
            if rec:
                candidates.append(rec)
    return candidates


def _catalog_visible_skills(candidates: Sequence[RegisteredSkill]) -> List[RegisteredSkill]:
    by_name: Dict[str, RegisteredSkill] = {}
    for rec in candidates:
        key = rec.name.strip().lower()
        existing = by_name.get(key)
        if existing is None:
            by_name[key] = rec
            continue
        current_key = (_source_priority(rec.source), rec.skill_id)
        existing_key = (_source_priority(existing.source), existing.skill_id)
        if current_key < existing_key:
            by_name[key] = rec
    return sorted(by_name.values(), key=lambda r: (_source_priority(r.source), r.name, r.skill_id))


def _skill_ref_for_record(rec: RegisteredSkill, *, bundle_version_by_id: Optional[dict] = None) -> SkillRef:
    bv_map = bundle_version_by_id or {}
    return SkillRef(
        skill_id=rec.skill_id,
        name=rec.name,
        bundle_id=rec.bundle_id,
        bundle_version=bv_map.get(rec.bundle_id) or rec.bundle_version,
        source=rec.source,
    )


def build_skill_catalog_for_turn(
    skill_allowlist: Optional[Sequence[str]],
    *,
    discovery_mode: bool = False,
    bundle_version_by_id: Optional[dict] = None,
) -> Tuple[List[Message], List[SkillRef], List[RegisteredSkill]]:
    """Build a compact Agent Skills catalog without loading full SKILL.md bodies."""
    candidates = _candidate_skills(skill_allowlist, discovery_mode=discovery_mode)
    visible = _catalog_visible_skills(candidates)
    if not visible:
        return [], [], []

    lines = [
        "# Available Agent Skills",
        "",
        "The following skills provide specialized instructions for matching tasks.",
        f"When a task matches a skill description, call `{_ACTIVATE_SKILL_TOOL}` with the skill's `name` or `skill_id` before proceeding.",
        "Do not assume the full workflow until the skill has been activated.",
        "",
        "<available_skills>",
    ]
    for rec in visible:
        lines.extend(
            [
                "  <skill>",
                f"    <name>{rec.name}</name>",
                f"    <skill_id>{rec.skill_id}</skill_id>",
                f"    <description>{rec.description}</description>",
                f"    <source>{rec.source}</source>",
                "  </skill>",
            ]
        )
    lines.append("</available_skills>")
    refs = [_skill_ref_for_record(rec, bundle_version_by_id=bundle_version_by_id) for rec in visible]
    return [
        Message(
            role="system",
            content="\n".join(lines),
            metadata={
                "content_kind": "agent_skill_catalog",
                "skill_ids": [rec.skill_id for rec in visible],
                "activation_tool": _ACTIVATE_SKILL_TOOL,
            },
        )
    ], refs, visible


def _is_hidden_runtime_resource(path: str) -> bool:
    parts = path.split("/")
    return "__pycache__" in parts or path.endswith((".pyc", ".pyo"))


def _safe_resource_paths(skill_dir: Path, *, limit: int = 50) -> List[str]:
    resources: List[str] = []
    try:
        for path in sorted(skill_dir.rglob("*")):
            if not path.is_file() or path.name == "SKILL.md":
                continue
            try:
                rel = path.relative_to(skill_dir).as_posix()
            except ValueError:
                continue
            if _is_hidden_runtime_resource(rel):
                continue
            resources.append(rel)
            if len(resources) >= limit:
                break
    except Exception as e:
        logger.debug("skill_resource_listing_failed", skill_dir=str(skill_dir), error=str(e))
    return resources


def _categorized_resource_paths(resources: Sequence[str]) -> Dict[str, List[str]]:
    """Group Agent Skill resources by the public directory conventions."""
    groups: Dict[str, List[str]] = {
        "scripts": [],
        "references": [],
        "assets": [],
        "other": [],
    }
    for path in resources:
        top = path.split("/", 1)[0]
        if top in {"scripts", "references", "assets"}:
            groups[top].append(path)
        else:
            groups["other"].append(path)
    return groups


def _resource_groups_block(resource_groups: Dict[str, List[str]]) -> str:
    lines: List[str] = []
    for group_name in ("scripts", "references", "assets", "other"):
        paths = resource_groups.get(group_name) or []
        if not paths:
            continue
        lines.append(f"  <{group_name}>")
        lines.extend(f"    <file>{escape(path)}</file>" for path in paths)
        lines.append(f"  </{group_name}>")
    if not lines:
        return ""
    return "\n<skill_resource_groups>\n" + "\n".join(lines) + "\n</skill_resource_groups>"


def _load_script_usage(skill_dir: Path) -> Tuple[List[Dict[str, object]], Optional[str]]:
    """Load optional script usage metadata from scripts.yaml or usage.yaml."""
    for filename in ("scripts.yaml", "scripts.yml", "usage.yaml", "usage.yml"):
        path = skill_dir / filename
        if not path.is_file():
            continue
        try:
            return [item for item in parse_script_usage_yaml(path).to_list()], None
        except Exception as e:
            logger.warning(
                "skill_script_usage_parse_failed",
                path=str(path),
                error=str(e),
            )
            return [], str(e)
    return [], None


def _bundle_exec_config_for_skill(rec: RegisteredSkill) -> Dict[str, object]:
    """Read bundle config/exec.yaml for activation-time execution hints."""
    if rec.source != "bundle":
        return {}
    skill_dir = rec.skill_md_path.parent
    skills_dir = skill_dir.parent
    if skills_dir.name != "skills":
        return {}
    bundle_root = skills_dir.parent
    for filename in ("exec.yaml", "exec.yml"):
        path = bundle_root / "config" / filename
        if not path.is_file():
            continue
        try:
            import yaml  # type: ignore[import]

            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return {}


def _runtime_capabilities_from_config(exec_config: Dict[str, object]) -> List[str]:
    raw = exec_config.get("runtime_capabilities")
    if not isinstance(raw, list):
        return []
    out: List[str] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            out.append(item.strip().lower().replace("_", "-"))
    return out


def _script_usage_block(scripts: Sequence[Dict[str, object]]) -> str:
    if not scripts:
        return ""
    lines = [
        "\n<script_usage>",
        "  <guidance>Use these commands as examples. When combining multiple shell steps, start "
        f"the command with `{_FAIL_FAST_SHELL_PRELUDE}` so validation or setup failures produce "
        "a failed process_status instead of being hidden by a later successful command.</guidance>",
    ]
    for script in scripts:
        lines.extend(
            [
                "  <script>",
                f"    <name>{escape(str(script.get('name', '') or ''))}</name>",
                f"    <path>{escape(str(script.get('path', '') or ''))}</path>",
                f"    <description>{escape(str(script.get('description', '') or ''))}</description>",
                f"    <command>{escape(str(script.get('command', '') or ''))}</command>",
            ]
        )
        inputs = script.get("inputs")
        if isinstance(inputs, list) and inputs:
            lines.append("    <inputs>")
            for item in inputs:
                if isinstance(item, dict):
                    lines.append(
                        "      <input"
                        f" name=\"{escape(str(item.get('name', '') or ''))}\""
                        f" type=\"{escape(str(item.get('type', '') or ''))}\""
                        f" recommended_path=\"{escape(str(item.get('recommended_path', '') or ''))}\""
                        " />"
                    )
            lines.append("    </inputs>")
        outputs = script.get("outputs")
        if isinstance(outputs, list) and outputs:
            lines.append("    <outputs>")
            for item in outputs:
                if isinstance(item, dict):
                    lines.append(
                        "      <output"
                        f" name=\"{escape(str(item.get('name', '') or ''))}\""
                        f" type=\"{escape(str(item.get('type', '') or ''))}\""
                        f" recommended_path=\"{escape(str(item.get('recommended_path', '') or ''))}\""
                        " />"
                    )
            lines.append("    </outputs>")
        lines.append("  </script>")
    lines.append("</script_usage>")
    return "\n".join(lines)


def _runner_tools_for_skill(rec: RegisteredSkill) -> List[Dict[str, str]]:
    """Return bundle-declared runner tools already registered for this skill."""
    if rec.source != "bundle" or not rec.bundle_id or not rec.name:
        return []

    try:
        from motet.core.tools import registry as tool_registry
    except Exception as e:
        logger.debug(
            "skill_runner_tool_listing_registry_unavailable",
            skill_id=rec.skill_id,
            error=str(e),
        )
        return []

    prefix = f"{rec.bundle_id}.{rec.name}."
    try:
        items = tool_registry.list_items() or {}
    except Exception as e:
        logger.debug(
            "skill_runner_tool_listing_failed",
            skill_id=rec.skill_id,
            prefix=prefix,
            error=str(e),
        )
        return []

    out: List[Dict[str, str]] = []
    for name in sorted(n for n in items if n.startswith(prefix)):
        tool = items.get(name)
        out.append(
            {
                "name": name,
                "description": str(getattr(tool, "description", "") or ""),
                "category": str(getattr(tool, "category", "") or ""),
            }
        )
    return out


def _activated_skill_content(
    rec: RegisteredSkill,
    *,
    runner_tools: Optional[Sequence[Dict[str, str]]] = None,
) -> Tuple[
    str,
    str,
    List[str],
    str,
    Dict[str, List[str]],
    List[Dict[str, object]],
    Optional[str],
    Optional[str],
]:
    doc = parse_skill_markdown(rec.skill_md_path)
    material = doc.body.strip() or doc.description
    skill_dir = rec.skill_md_path.parent
    resources = _safe_resource_paths(skill_dir)
    resource_groups = _categorized_resource_paths(resources)
    resource_lines = "\n".join(f"  <file>{path}</file>" for path in resources)
    resources_block = f"\n<skill_resources>\n{resource_lines}\n</skill_resources>" if resources else ""
    resource_groups_block = _resource_groups_block(resource_groups)
    script_usage, script_usage_error = _load_script_usage(skill_dir)
    script_usage_block = _script_usage_block(script_usage)
    exec_config = _bundle_exec_config_for_skill(rec)
    runtime_capabilities = _runtime_capabilities_from_config(exec_config)
    allowed_tools = doc.raw_frontmatter.get("allowed-tools")
    allowed_tools_value = allowed_tools.strip() if isinstance(allowed_tools, str) and allowed_tools.strip() else None
    allowed_tools_block = (
        "\n<allowed_tools_guidance>"
        f"{escape(allowed_tools_value)}"
        "</allowed_tools_guidance>\n"
        "The allowed-tools frontmatter is portability guidance only; Motet does not grant tool access from it."
        if allowed_tools_value
        else ""
    )
    tools = list(runner_tools or [])
    tool_lines = "\n".join(
        (
            "  <tool>"
            f"<name>{tool.get('name', '')}</name>"
            f"<description>{tool.get('description', '')}</description>"
            "</tool>"
        )
        for tool in tools
    )
    tools_block = (
        "\n<skill_runner_tools>\n"
        f"{tool_lines}\n"
        "</skill_runner_tools>\n"
        "Use these callable tools for this skill when their typed entrypoints match the task."
        if tools
        else ""
    )
    runtime_block = (
        "\n  <runtime_capabilities>"
        + "".join(f"<capability>{escape(cap)}</capability>" for cap in runtime_capabilities)
        + "</runtime_capabilities>"
        if runtime_capabilities
        else ""
    )
    execution_block = (
        "\n<skill_execution>\n"
        f"  <tool>{_WORKSPACE_SHELL_TOOL}</tool>\n"
        "  <working_directory>/scratch</working_directory>\n"
        f"  <skill_directory>/scratch/skills/{rec.name}</skill_directory>\n"
        f"{runtime_block}\n"
        "  <notes>Use this activation-gated shell tool to run commands documented by this skill. "
        "Materialize user files with input_artifacts and persist generated files with output_paths. "
        "When a document attachment has both an extracted-text artifact and a source_artifact_id, "
        "pass the source_artifact_id to input_artifacts for binary file operations. "
        "Prefer writing generated files under /scratch and declaring output_paths so the tool returns artifact ids "
        "and previews instead of using cat for file transfer. After each call, inspect process_status and returncode; "
        "if process_status is failed, use stdout/stderr to correct the command or script arguments. "
        f"When running more than one shell step, begin with `{_FAIL_FAST_SHELL_PRELUDE}` so earlier failures "
        "are reflected in process_status.</notes>\n"
        "</skill_execution>"
    )
    full_text = (
        f"<skill_content name=\"{doc.name}\" skill_id=\"{rec.skill_id}\">\n"
        f"# Skill: {doc.name}\n\n"
        f"{doc.description}\n\n"
        f"{material}\n\n"
        f"Skill directory: {skill_dir}\n"
        "Relative paths in this skill are relative to the skill directory."
        f"{resources_block}\n"
        f"{resource_groups_block}\n"
        f"{script_usage_block}\n"
        f"{allowed_tools_block}\n"
        f"{tools_block}\n"
        f"{execution_block}\n"
        "</skill_content>"
    )
    fp = hashlib.sha256(full_text.encode("utf-8")).hexdigest()
    return (
        full_text,
        str(skill_dir),
        resources,
        fp,
        resource_groups,
        script_usage,
        script_usage_error,
        allowed_tools_value,
    )


def activate_skill_record(
    rec: RegisteredSkill,
    *,
    bundle_version_by_id: Optional[dict] = None,
) -> Tuple[Message, SkillRef, Dict[str, object]]:
    """Load one full skill body and wrap it for model context or tool results."""
    runner_tools = _runner_tools_for_skill(rec)
    (
        full_text,
        skill_dir,
        resources,
        fp,
        resource_groups,
        script_usage,
        script_usage_error,
        allowed_tools,
    ) = _activated_skill_content(
        rec,
        runner_tools=runner_tools,
    )
    ref = _skill_ref_for_record(rec, bundle_version_by_id=bundle_version_by_id)
    ref.content_fingerprint = fp
    exec_config = _bundle_exec_config_for_skill(rec)
    runtime_capabilities = _runtime_capabilities_from_config(exec_config)
    message = Message(
        role="system",
        content=full_text,
        metadata={
            "skill_id": rec.skill_id,
            "source": rec.source,
            "content_kind": "agent_skill",
            "skill_directory": skill_dir,
        },
    )
    execution_payload: Dict[str, object] = {
        "tool": _WORKSPACE_SHELL_TOOL,
        "lifetime": "workspace",
        "workspace_mode": "workspace",
        "working_directory": "/scratch",
        "skill_directory": f"/scratch/skills/{rec.name}",
        "input_artifacts_param": "input_artifacts",
        "output_paths_param": "output_paths",
        "result_contract": {
            "process_status": "succeeded | failed | timed_out",
            "returncode": "process exit code; 0 means the command succeeded",
            "output_artifacts": "declared output_paths persisted as Artifact Store artifacts, with previews for text-like files",
        },
        "fail_fast_shell_prelude": _FAIL_FAST_SHELL_PRELUDE,
        "guidance": (
            "Run scripts from skill_directory (for example /scratch/skills/<skill>/scripts/name.py), "
            "materialize user files with input_artifacts, "
            "and prefer output_paths for generated files. For multi-step commands, start with "
            f"`{_FAIL_FAST_SHELL_PRELUDE}` so setup or validation failures make process_status fail. "
            "If a script invocation fails, "
            "use stdout/stderr to adjust arguments and retry. Do not run package-manager "
            "installation commands unless workspace_shell_exec reports bootstrap is enabled."
        ),
    }
    if runtime_capabilities:
        execution_payload["runtime_capabilities"] = runtime_capabilities
    base_image_stack = exec_config.get("base_image_stack")
    if isinstance(base_image_stack, str) and base_image_stack.strip():
        execution_payload["base_image_stack"] = base_image_stack.strip()

    payload: Dict[str, object] = {
        "skill_id": rec.skill_id,
        "name": rec.name,
        "description": rec.description,
        "source": rec.source,
        "skill_directory": skill_dir,
        "execution": execution_payload,
        "content": full_text,
        "resources": resources,
        "resource_groups": resource_groups,
        "script_usage": script_usage,
        "tools": runner_tools,
        "content_fingerprint": fp,
    }
    if script_usage_error:
        payload["script_usage_error"] = script_usage_error
    if allowed_tools:
        payload["allowed_tools"] = allowed_tools
        payload["allowed_tools_guidance"] = (
            "Portability guidance from SKILL.md frontmatter only; Motet does not grant tool access from allowed-tools."
        )
    return message, ref, payload


def find_skill_by_name_or_id(
    name: Optional[str] = None,
    skill_id: Optional[str] = None,
    *,
    candidates: Optional[Sequence[RegisteredSkill]] = None,
) -> Optional[RegisteredSkill]:
    """Resolve an exact skill id first, then a visible skill name with source precedence."""
    reg = get_skill_registry()
    sid = str(skill_id or "").strip()
    if sid:
        rec = reg.get(sid)
        if rec:
            return rec

    raw_name = str(name or "").strip().lower()
    if not raw_name:
        return None
    pool = list(candidates) if candidates is not None else reg.list_all()
    matches = [
        rec
        for rec in pool
        if rec.name.lower() == raw_name
        or rec.skill_id.lower() == raw_name
        or rec.skill_id.rsplit(".", 1)[-1].lower() == raw_name
    ]
    if not matches:
        return None
    matches.sort(key=lambda r: (_source_priority(r.source), r.skill_id))
    return matches[0]


def detect_explicit_skill_activations(
    user_text: str,
    candidates: Sequence[RegisteredSkill],
    *,
    max_skills: int = 3,
) -> List[RegisteredSkill]:
    """Detect direct user requests such as `/pdf`, `$pdf`, or `use the pdf skill`."""
    lower = (user_text or "").lower()
    if not lower.strip():
        return []

    matched: List[RegisteredSkill] = []
    seen: Set[str] = set()
    for rec in _catalog_visible_skills(candidates):
        names = {rec.name.lower(), rec.skill_id.lower(), rec.skill_id.rsplit(".", 1)[-1].lower()}
        for name in names:
            mention_patterns = [
                rf"(?<!\w)[/$]{re.escape(name)}(?![\w-])",
                rf"\b(?:use|activate|load|open)\s+(?:the\s+)?{re.escape(name)}\s+skill\b",
                rf"\b{re.escape(name)}\s+skill\b",
            ]
            if any(re.search(pattern, lower) for pattern in mention_patterns):
                if rec.skill_id not in seen:
                    matched.append(rec)
                    seen.add(rec.skill_id)
                break
    return matched[: max(1, int(max_skills or 1))]


def activate_explicit_skills_for_turn(
    user_message: str,
    skill_allowlist: Optional[Sequence[str]],
    *,
    discovery_mode: bool = False,
    max_skills: int = 3,
    bundle_version_by_id: Optional[dict] = None,
) -> Tuple[List[Message], List[SkillRef]]:
    """Load full skill content only when the user explicitly names a skill."""
    candidates = _candidate_skills(skill_allowlist, discovery_mode=discovery_mode)
    selected = detect_explicit_skill_activations(
        user_message,
        candidates,
        max_skills=max_skills,
    )
    messages: List[Message] = []
    refs: List[SkillRef] = []
    for rec in selected:
        try:
            message, ref, _payload = activate_skill_record(
                rec,
                bundle_version_by_id=bundle_version_by_id,
            )
        except Exception as e:
            logger.warning(
                "activate_explicit_skill_failed",
                skill_id=rec.skill_id,
                path=str(rec.skill_md_path),
                error=str(e),
            )
            continue
        messages.append(message)
        refs.append(ref)
    return messages, refs



def assemble_skills_for_turn(
    user_message: str,
    skill_allowlist: Optional[Sequence[str]],
    *,
    discovery_mode: bool = False,
    max_skills: int = 3,
    bundle_version_by_id: Optional[dict] = None,
) -> Tuple[List[Message], List[SkillRef]]:
    """
    Backward-compatible wrapper for explicit skill activation only.

    Args:
        user_message: Latest user text for relevance heuristics.
        skill_allowlist: Explicit skill ids (``bundle.slug``).
        discovery_mode: When True, evaluate all locally-registered skills (optionally
            intersected with skill_allowlist when provided).
        max_skills: Upper bound for explicit skill activations.
        bundle_version_by_id: Optional ``{bundle_id: version_sha}`` for SkillRef enrichment.

    Returns:
        (system_messages, skill_refs) — full skill bodies only for explicit requests.
    """
    return activate_explicit_skills_for_turn(
        user_message,
        skill_allowlist,
        discovery_mode=discovery_mode,
        max_skills=max_skills,
        bundle_version_by_id=bundle_version_by_id,
    )
