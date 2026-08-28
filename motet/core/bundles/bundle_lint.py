"""
Motet - Bundle Lint Helpers

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    AST / YAML / skills / exec lint helpers for the bundle deployment pipeline
    (GitHub issue #158). Deploy and publish commands remain in
    deploy.py and import this module.

Dependencies:
    - ast / re / hashlib: Syntax and content linting
    - pydantic: LintError model

Usage:
    from motet.core.bundles.bundle_lint import (
        LintError, _lint_bundle, _collect_lint_errors, _fatal_lint_errors,
    )

Notes:
    - Publish digest-pinning helpers stay in deploy.py (publish-time gate).
    - ``_lint_reserved_bundle_name`` lazy-imports manifest helpers from deploy
      to avoid an import cycle.
"""

from __future__ import annotations

import ast
import hashlib
import os
import re
import sys
from typing import Any, Dict, List, Optional, Set, Tuple

import structlog
from pydantic import BaseModel, Field

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Lint helpers
# ---------------------------------------------------------------------------

DANGEROUS_IMPORTS = frozenset(["subprocess", "ctypes", "pty", "socket"])
THREADING_ANTIPATTERNS = [
    (r"\bthreading\.Lock\b", "Use WorkerLock from motet.core.workers.concurrency_primitives"),
    (r"\bthreading\.RLock\b", "Use WorkerRLock from motet.core.workers.concurrency_primitives"),
    (r"\bthreading\.Event\b", "Use WorkerEvent from motet.core.workers.concurrency_primitives"),
    (r"\bthreading\.Semaphore\b", "Use WorkerSemaphore from motet.core.workers.concurrency_primitives"),
    (r"\btime\.sleep\b", "Use worker_sleep from motet.core.workers.concurrency_primitives"),
    (r"\bThreadPoolExecutor\b", "Use WorkerExecutor from motet.core.workers.concurrency_primitives"),
    (r"\bthreading\.Thread\b", "Use WorkerThread from motet.core.workers.concurrency_primitives"),
]

_THREADING_RE = [(re.compile(pat), msg) for pat, msg in THREADING_ANTIPATTERNS]

# Regex for hardcoded system principal strings in bundle code (ADR-0090).
_SYSTEM_PRINCIPAL_RE = re.compile(r"""["']system:[a-z]""")

# Ad-hoc stack identity access patterns that should use resolve_current_identity.
_ADHOC_IDENTITY_RE = re.compile(
    r"""stack\._(?:principal_id|tenant_id|motet_id)\b"""
    r"""|getattr\(\s*stack\s*,\s*["']_?(?:principal_id|tenant_id|motet_id)["']"""
)


def _get_decorator_qualified_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> List[str]:
    """Return decorator names for a function node.

    Handles bare names (``@distributed_command``), attribute access
    (``@motet.tool``), and call forms (``@motet.tool(...)``,
    ``@motet.command(...)``).  Returns strings like ``"distributed_command"``,
    ``"motet.tool"``, ``"motet.command"``.
    """
    names: List[str] = []
    for dec in node.decorator_list:
        if isinstance(dec, ast.Name):
            names.append(dec.id)
        elif isinstance(dec, ast.Attribute):
            if isinstance(dec.value, ast.Name):
                names.append(f"{dec.value.id}.{dec.attr}")
            else:
                names.append(dec.attr)
        elif isinstance(dec, ast.Call):
            func = dec.func
            if isinstance(func, ast.Name):
                names.append(func.id)
            elif isinstance(func, ast.Attribute):
                if isinstance(func.value, ast.Name):
                    names.append(f"{func.value.id}.{func.attr}")
                else:
                    names.append(func.attr)
    return names


def _is_motet_tool(decorator_names: List[str]) -> bool:
    """Return True if decorator list includes @motet.tool."""
    return "motet.tool" in decorator_names


class LintError(BaseModel):
    """A single lint finding."""
    file: str
    line: int
    message: str
    severity: str = "error"  # "error" | "warning"


def _lint_python_file(file_path: str, content: str) -> List[LintError]:
    """
    Run syntax, safety, description, and concurrency-primitive checks on a single Python file.
    Returns a list of LintError objects.
    """
    errors: List[LintError] = []

    # --- Syntax check ---
    try:
        tree = ast.parse(content, filename=file_path)
    except SyntaxError as e:
        errors.append(LintError(
            file=file_path,
            line=e.lineno or 0,
            message=f"SyntaxError: {e.msg}",
            severity="error",
        ))
        return errors  # can't analyse further without a valid AST

    # --- Safety checks via AST ---
    for node in ast.walk(tree):
        # Block eval/exec with dynamic code. Builtin compile() remains visible
        # as a warning so deployers can review it without rejecting common
        # vendored helpers such as regex/schema validators.
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                if func.id in ("eval", "exec"):
                    errors.append(LintError(
                        file=file_path,
                        line=getattr(node, "lineno", 0),
                        message=f"Unsafe call: {func.id}() is not allowed in bundle code",
                        severity="error",
                    ))
                elif func.id == "compile":
                    errors.append(LintError(
                        file=file_path,
                        line=getattr(node, "lineno", 0),
                        message="Dynamic code compilation via compile(): review before deploying bundle code",
                        severity="warning",
                    ))

        # Warn on dangerous imports
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in DANGEROUS_IMPORTS:
                    errors.append(LintError(
                        file=file_path,
                        line=getattr(node, "lineno", 0),
                        message=f"Dangerous import '{alias.name}': use with caution in bundle code",
                        severity="warning",
                    ))
        if isinstance(node, ast.ImportFrom):
            if node.module in DANGEROUS_IMPORTS:
                errors.append(LintError(
                    file=file_path,
                    line=getattr(node, "lineno", 0),
                    message=f"Dangerous import from '{node.module}': use with caution in bundle code",
                    severity="warning",
                ))

    # --- Description checks: commands and tools must have docstrings ---
    _COMMAND_DECORATORS = {"distributed_command", "motet.command"}
    _TOOL_DECORATORS = {"motet.tool"}

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            dec_names = set(_get_decorator_qualified_names(node))
            has_docstring = (
                node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
            )
            if dec_names & _COMMAND_DECORATORS and not has_docstring:
                errors.append(LintError(
                    file=file_path,
                    line=node.lineno,
                    message=f"Command function '{node.name}' is missing a docstring (required for AI discovery)",
                    severity="error",
                ))
            elif dec_names & _TOOL_DECORATORS and not has_docstring:
                errors.append(LintError(
                    file=file_path,
                    line=node.lineno,
                    message=f"Tool function '{node.name}' is missing a docstring (required for AI discovery)",
                    severity="error",
                ))

        # Pydantic BaseModel subclasses (command data) must have docstrings
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                base_name = ""
                if isinstance(base, ast.Name):
                    base_name = base.id
                elif isinstance(base, ast.Attribute):
                    base_name = base.attr
                if base_name == "BaseModel":
                    if not (node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant)):
                        errors.append(LintError(
                            file=file_path,
                            line=node.lineno,
                            message=f"Data class '{node.name}' is missing a docstring (required for AI discovery)",
                            severity="error",
                        ))

    # --- Concurrency primitive warnings (line-by-line regex) ---
    for line_num, line_content in enumerate(content.splitlines(), start=1):
        for pattern, suggestion in _THREADING_RE:
            if pattern.search(line_content):
                errors.append(LintError(
                    file=file_path,
                    line=line_num,
                    message=f"Pool-incompatible primitive detected. {suggestion}",
                    severity="warning",
                ))
                break  # one warning per line

    # --- Identity hygiene checks (ADR-0090) ---

    # Rule: @motet.tool functions must not accept a MotetContext parameter.
    # Tool functions receive (params: dict); MotetContext is for commands only.
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        dec_names = _get_decorator_qualified_names(node)
        if not _is_motet_tool(dec_names):
            continue

        for arg in node.args.args:
            ann = arg.annotation
            ann_name = ""
            if isinstance(ann, ast.Name):
                ann_name = ann.id
            elif isinstance(ann, ast.Attribute):
                ann_name = ann.attr
            if ann_name == "MotetContext":
                errors.append(LintError(
                    file=file_path,
                    line=arg.lineno if hasattr(arg, "lineno") else node.lineno,
                    message=(
                        f"@motet.tool function '{node.name}' has a MotetContext parameter. "
                        "Tool functions receive (params: dict), not MotetContext. "
                        "Use resolve_current_identity() for identity access."
                    ),
                    severity="error",
                ))

    # Rule: no hardcoded "system:*" principal strings in bundle code.
    for line_num, line_content in enumerate(content.splitlines(), start=1):
        if _SYSTEM_PRINCIPAL_RE.search(line_content):
            stripped = line_content.lstrip()
            if stripped.startswith("#"):
                continue
            errors.append(LintError(
                file=file_path,
                line=line_num,
                message=(
                    'Hardcoded "system:*" principal string detected. '
                    "Use IdentityContext with named constants instead (ADR-0090)."
                ),
                severity="warning",
            ))

    # Rule: ad-hoc stack identity access should use resolve_current_identity.
    if file_path.startswith("tools/") or "/tools/" in file_path:
        for line_num, line_content in enumerate(content.splitlines(), start=1):
            if _ADHOC_IDENTITY_RE.search(line_content):
                stripped = line_content.lstrip()
                if stripped.startswith("#"):
                    continue
                errors.append(LintError(
                    file=file_path,
                    line=line_num,
                    message=(
                        "Ad-hoc stack identity access detected. "
                        "Use resolve_current_identity() from motet_sdk instead (ADR-0090)."
                    ),
                    severity="warning",
                ))

    return errors


def _lint_yaml_file(file_path: str, content: str) -> List[LintError]:
    """Basic YAML validation."""
    errors: List[LintError] = []
    try:
        import yaml  # type: ignore[import]
        yaml.safe_load(content)
    except Exception as e:
        errors.append(LintError(file=file_path, line=0, message=f"YAML parse error: {e}", severity="error"))
    return errors


def _is_skill_runners_file(file_path: str) -> bool:
    """Match ``skills/<dir>/runners.yaml`` (or .yml) for ADR-0101 Slice B lint."""
    return (
        file_path.startswith("skills/")
        and (file_path.endswith("/runners.yaml") or file_path.endswith("/runners.yml"))
    )


def _is_skill_script_usage_file(file_path: str) -> bool:
    """Match optional ``skills/<dir>/scripts.yaml`` or ``usage.yaml`` metadata."""
    try:
        from motet.core.skills.script_usage import is_script_usage_manifest_path

        return is_script_usage_manifest_path(file_path)
    except Exception:
        return False


def _lint_script_usage_file(file_path: str, content: str) -> List[LintError]:
    """Validate optional per-skill script usage metadata."""
    try:
        from motet.core.skills.script_usage import parse_script_usage_yaml_text

        parse_script_usage_yaml_text(content, source_hint=file_path)
    except Exception as exc:
        return [
            LintError(
                file=file_path,
                line=0,
                message=str(exc),
                severity="error",
            )
        ]
    return []


def _lint_runners_file(file_path: str, content: str) -> List[LintError]:
    """Validate skills/<name>/runners.yaml (ADR-0101 Slice B).

    Emits one ``error`` per structural violation. The script-existence
    check uses ``_lint_exec_bundle_paths``-style cross-file logic and
    therefore lives in :func:`_lint_runner_script_paths` (called from
    bundle-wide lint passes), not here.
    """
    errors: List[LintError] = []
    try:
        from motet.core.skills.runners import parse_runners_yaml_text

        doc = parse_runners_yaml_text(content, source_hint=file_path)
    except Exception as exc:
        return [
            LintError(
                file=file_path,
                line=0,
                message=str(exc),
                severity="error",
            )
        ]

    parts = file_path.split("/")
    skill_dir = parts[1] if len(parts) >= 3 else ""

    for runner in doc.runners:
        # Light cross-check: tool name length cap so qualified-name aware
        # systems (the semantic index, ScopedRegistry) don't trip on a
        # surprise long entry.
        composed = f"{skill_dir}.{runner.name}" if skill_dir else runner.name
        if len(composed) > 192:
            errors.append(
                LintError(
                    file=file_path,
                    line=0,
                    message=(
                        f"composed tool name '{composed}' exceeds 192 characters; "
                        "shorten the runner or skill directory name"
                    ),
                    severity="error",
                )
            )
    return errors


def _lint_script_usage_paths(bundle_files: Dict[str, bytes]) -> List[LintError]:
    """Bundle-wide check: every script usage path exists under the skill dir."""
    errors: List[LintError] = []
    try:
        from motet.core.skills.script_usage import parse_script_usage_yaml_text
    except Exception:
        return errors

    for file_path, raw in bundle_files.items():
        if not _is_skill_script_usage_file(file_path):
            continue
        parts = file_path.split("/")
        if len(parts) < 3:
            continue
        skill_dir = parts[1]
        try:
            doc = parse_script_usage_yaml_text(
                raw.decode("utf-8", errors="replace"), source_hint=file_path
            )
        except Exception:
            continue
        for script in doc.scripts:
            target = f"skills/{skill_dir}/{script.path.lstrip('/')}"
            if target not in bundle_files:
                errors.append(
                    LintError(
                        file=file_path,
                        line=0,
                        message=(
                            f"script usage entry '{script.name}' path {script.path!r} "
                            f"not found in bundle (looked for {target!r})"
                        ),
                        severity="error",
                    )
                )
    return errors


def _lint_runner_script_paths(bundle_files: Dict[str, bytes]) -> List[LintError]:
    """Bundle-wide check: every runner.script must exist in the bundle.

    Mirrors ``_lint_exec_bundle_paths`` for runners. Done bundle-wide
    rather than per-file so the lint catches the cross-file relationship
    that runners.yaml on its own can't validate.
    """
    errors: List[LintError] = []
    try:
        from motet.core.skills.runners import parse_runners_yaml_text
    except Exception:
        return errors

    for file_path, raw in bundle_files.items():
        if not _is_skill_runners_file(file_path):
            continue
        parts = file_path.split("/")
        if len(parts) < 3:
            continue
        skill_dir = parts[1]
        try:
            doc = parse_runners_yaml_text(
                raw.decode("utf-8", errors="replace"), source_hint=file_path
            )
        except Exception:
            # Structural error already surfaced by _lint_runners_file.
            continue
        for runner in doc.runners:
            target = f"skills/{skill_dir}/{runner.script.lstrip('/')}"
            if target not in bundle_files:
                errors.append(
                    LintError(
                        file=file_path,
                        line=0,
                        message=(
                            f"runner '{runner.name}' script {runner.script!r} not "
                            f"found in bundle (looked for {target!r})"
                        ),
                        severity="error",
                    )
                )
    return errors


_MARKDOWN_LINK_RE = re.compile(r"!?\[[^\]]*]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_SCRIPT_REF_RE = re.compile(r"(?<![\w/.-])(scripts/[A-Za-z0-9_./-]+\.(?:py|sh|js|mjs|ts))(?![\w/.-])")
_HOST_ABSOLUTE_PATH_RE = re.compile(r"(?<![\w])(/(?:Users|home|tmp|var/folders|work|app)/[^\s)`'\"]+)")
_STD_LIB_MODULES = set(getattr(sys, "stdlib_module_names", set()))
_COMMON_LOCAL_IMPORTS = {"motet", "motet_sdk"}


def _line_for_offset(text: str, offset: int) -> int:
    return text.count("\n", 0, max(offset, 0)) + 1


def _is_external_markdown_link(target: str) -> bool:
    lowered = target.strip().lower()
    return (
        not lowered
        or lowered.startswith("#")
        or lowered.startswith(("http://", "https://", "mailto:", "tel:"))
    )


def _safe_skill_relative_path(path: str) -> Optional[str]:
    normalized = path.strip().replace("\\", "/")
    if not normalized or normalized.startswith("/"):
        return None
    parts = [part for part in normalized.split("/") if part]
    if any(part == ".." for part in parts):
        return None
    return "/".join(parts)


def _third_party_imports(content: str) -> List[str]:
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return []
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                imports.add(root)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            root = (node.module or "").split(".", 1)[0]
            if root:
                imports.add(root)
    return sorted(
        name
        for name in imports
        if name not in _STD_LIB_MODULES and name not in _COMMON_LOCAL_IMPORTS
    )


def _lint_skill_portability(bundle_files: Dict[str, bytes]) -> List[LintError]:
    """Warn on Agent Skill portability issues that can break workspace execution."""
    errors: List[LintError] = []
    has_requirements = any(path in bundle_files for path in ("config/exec.yaml", "config/exec.yml"))

    for file_path, raw in bundle_files.items():
        if not (file_path.startswith("skills/") and file_path.endswith("/SKILL.md")):
            continue
        parts = file_path.split("/")
        if len(parts) < 3:
            continue
        skill_root = "/".join(parts[:-1])
        content = raw.decode("utf-8", errors="replace")

        try:
            from motet.core.skills.parser import parse_skill_markdown_text

            doc = parse_skill_markdown_text(content, source_hint=file_path)
        except Exception:
            # Structural SKILL.md errors are reported by _lint_skill_markdown_file.
            doc = None

        if doc is not None and doc.raw_frontmatter.get("allowed-tools"):
            errors.append(
                LintError(
                    file=file_path,
                    line=0,
                    message=(
                        "SKILL frontmatter 'allowed-tools' is treated as portability guidance only; "
                        "Motet does not grant tool access from it unless mapped explicitly."
                    ),
                    severity="warning",
                )
            )

        for match in _HOST_ABSOLUTE_PATH_RE.finditer(content):
            errors.append(
                LintError(
                    file=file_path,
                    line=_line_for_offset(content, match.start()),
                    message=(
                        f"SKILL.md references host-style absolute path {match.group(1)!r}; "
                        "use skill-relative paths or /scratch workspace paths instead."
                    ),
                    severity="warning",
                )
            )

        for match in _MARKDOWN_LINK_RE.finditer(content):
            raw_target = match.group(1).strip()
            target = raw_target.split("#", 1)[0]
            if _is_external_markdown_link(raw_target) or not target:
                continue
            safe = _safe_skill_relative_path(target)
            if not safe:
                errors.append(
                    LintError(
                        file=file_path,
                        line=_line_for_offset(content, match.start()),
                        message=(
                            f"Markdown link target {raw_target!r} is not a safe skill-relative path."
                        ),
                        severity="warning",
                    )
                )
                continue
            bundle_target = f"{skill_root}/{safe}"
            if bundle_target not in bundle_files:
                errors.append(
                    LintError(
                        file=file_path,
                        line=_line_for_offset(content, match.start()),
                        message=(
                            f"Markdown link target {raw_target!r} was not found in the skill directory."
                        ),
                        severity="warning",
                    )
                )

        for match in _SCRIPT_REF_RE.finditer(content):
            target = f"{skill_root}/{match.group(1)}"
            if target not in bundle_files:
                errors.append(
                    LintError(
                        file=file_path,
                        line=_line_for_offset(content, match.start()),
                        message=(
                            f"SKILL.md references script {match.group(1)!r}, but it was not found "
                            "under the skill directory."
                        ),
                        severity="warning",
                    )
                )

        if has_requirements:
            continue
        for script_path, script_raw in bundle_files.items():
            if not (
                script_path.startswith(f"{skill_root}/scripts/")
                and script_path.endswith(".py")
            ):
                continue
            imports = _third_party_imports(script_raw.decode("utf-8", errors="replace"))
            if imports:
                errors.append(
                    LintError(
                        file=script_path,
                        line=0,
                        message=(
                            "Python skill script imports possible third-party modules "
                            f"{imports}; declare config/exec.yaml requirements_path if these "
                            "are not provided by the selected image stack."
                        ),
                        severity="warning",
                    )
                )
    return errors


def _lint_skill_markdown_file(file_path: str, content: str) -> List[LintError]:
    """
    Validate skills/<name>/SKILL.md against public Agent Skills constraints.

    This catches malformed frontmatter early during bundle validation.
    """
    errors: List[LintError] = []
    try:
        from motet.core.skills.parser import (
            parse_skill_markdown_text,
            skill_dir_matches_name,
        )

        doc = parse_skill_markdown_text(content, source_hint=file_path)
        parts = file_path.split("/")
        skill_dir = parts[-2] if len(parts) >= 2 else ""
        if not skill_dir_matches_name(skill_dir, doc.name):
            errors.append(
                LintError(
                    file=file_path,
                    line=0,
                    message=(
                        "SKILL frontmatter 'name' must match its parent directory "
                        f"under skills/ (dir={skill_dir!r}, name={doc.name!r})"
                    ),
                    severity="error",
                )
            )
    except Exception as e:
        errors.append(
            LintError(
                file=file_path,
                line=0,
                message=str(e),
                severity="error",
            )
        )
    return errors


_EXEC_CONFIG_ALLOWED_KEYS = frozenset(
    {
        "oci_image_ref",
        "exec_artifact_digest",
        "base_image_stack",
        "requirements_path",
        "runtime_capabilities",
        "bootstrap_command",
    }
)
_RUNTIME_CAPABILITY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$|^[a-z0-9]$")


def _safe_exec_requirements_relative(path: str) -> Optional[str]:
    """Return normalized bundle-relative path or None if unsafe (absolute, .., empty)."""
    p = path.strip().replace("\\", "/")
    if not p or p.startswith("/"):
        return None
    parts = p.split("/")
    if any(part == ".." for part in parts):
        return None
    return p


def _normalize_runtime_capabilities(value: Any) -> List[str]:
    """Normalize config/exec runtime capabilities to lowercase dash names."""
    if not isinstance(value, list):
        return []
    out: List[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        normalized = item.strip().lower().replace("_", "-")
        if not normalized or not _RUNTIME_CAPABILITY_RE.match(normalized):
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return out


def _normalize_exec_config_block(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Keep only known string fields for catalog ``exec`` (Phase 3)."""
    out: Dict[str, Any] = {}
    for k in _EXEC_CONFIG_ALLOWED_KEYS:
        v = raw.get(k)
        if k == "runtime_capabilities":
            caps = _normalize_runtime_capabilities(v)
            if caps:
                out[k] = caps
        elif isinstance(v, str) and v.strip():
            out[k] = v.strip()
    return out


# ADR-0100 §rule 2: production refs MUST use ``image@sha256:...``. The catalog
# UI surfaces a "mutable tag" warning today; gate-based enforcement is opt-in
# via ``MOTET_REQUIRE_DIGEST_PINNED_PUBLISH=true`` so non-prod installs stay
# friction-free while prod-shaped deploys can hard-fail at publish.
_ENV_REQUIRE_DIGEST_PINNED_PUBLISH = "MOTET_REQUIRE_DIGEST_PINNED_PUBLISH"


def _lint_exec_config_file(file_path: str, content: str) -> List[LintError]:
    """Semantic lint for config/exec.yaml (catalog + worker_exec image resolution)."""
    errors: List[LintError] = []
    try:
        import yaml  # type: ignore[import]
        data = yaml.safe_load(content)
    except Exception as e:
        return [LintError(file=file_path, line=0, message=f"YAML parse error: {e}", severity="error")]
    if data is None:
        return errors
    if not isinstance(data, dict):
        return [
            LintError(
                file=file_path,
                line=0,
                message="config/exec must be a YAML mapping at the top level",
                severity="error",
            )
        ]
    for k in data.keys():
        if k not in _EXEC_CONFIG_ALLOWED_KEYS:
            errors.append(
                LintError(
                    file=file_path,
                    line=0,
                    message=(
                        f"Unknown key {k!r} in config/exec "
                        f"(allowed: {', '.join(sorted(_EXEC_CONFIG_ALLOWED_KEYS))})"
                    ),
                    severity="warning",
                )
            )
    for key in _EXEC_CONFIG_ALLOWED_KEYS:
        val = data.get(key)
        if val is None:
            continue
        if key == "runtime_capabilities":
            if not isinstance(val, list):
                errors.append(
                    LintError(
                        file=file_path,
                        line=0,
                        message="config/exec field 'runtime_capabilities' must be a list of strings",
                        severity="error",
                    )
                )
                continue
            normalized_caps = _normalize_runtime_capabilities(val)
            if len(normalized_caps) != len(val):
                errors.append(
                    LintError(
                        file=file_path,
                        line=0,
                        message=(
                            "config/exec runtime_capabilities must contain only non-empty "
                            "lowercase/dash string capability names"
                        ),
                        severity="error",
                    )
                )
                continue
            try:
                from motet.core.execution.image_stacks import resolve_image_stack_for_capabilities

                resolution = resolve_image_stack_for_capabilities(normalized_caps)
                if not resolution.matched:
                    errors.append(
                        LintError(
                            file=file_path,
                            line=0,
                            message=(
                                "No pinned image stack satisfies runtime_capabilities "
                                f"{normalized_caps}; configure MOTET_IMAGE_STACK_<NAME> "
                                "and MOTET_IMAGE_STACK_<NAME>_CAPABILITIES, or set base_image_stack explicitly."
                            ),
                            severity="warning",
                        )
                    )
            except Exception:
                pass
            continue
        if not isinstance(val, str):
            errors.append(
                LintError(
                    file=file_path,
                    line=0,
                    message=f"config/exec field {key!r} must be a string",
                    severity="error",
                )
            )
            continue
        if key == "bootstrap_command" and val.strip():
            errors.append(
                LintError(
                    file=file_path,
                    line=0,
                    message=(
                        "config/exec bootstrap_command is dev-only and ignored unless "
                        "MOTET_WORKSPACE_SHELL_BOOTSTRAP_ENABLED=true."
                    ),
                    severity="warning",
                )
            )
        if key == "oci_image_ref" and not val.strip():
            errors.append(
                LintError(
                    file=file_path,
                    line=0,
                    message="config/exec oci_image_ref must not be empty when set",
                    severity="error",
                )
            )
        if key == "requirements_path" and not val.strip():
            errors.append(
                LintError(
                    file=file_path,
                    line=0,
                    message="config/exec requirements_path must not be empty when set",
                    severity="error",
                )
            )
        if key == "base_image_stack" and val.strip():
            # ADR-0101 §"Platform-managed image stacks": warn (not error) when
            # the bundle targets a stack the platform does not know about. We
            # warn rather than error because operators may legitimately roll
            # out a new stack and a bundle that targets it before the env var
            # lands on every API node — failing publish in that window is
            # worse than a visible warning. The deployer build path will
            # ignore an unknown stack and fall back to the in-repo Dockerfile
            # default, which keeps publishes producing a usable image while
            # making the misconfiguration visible.
            try:
                from motet.core.execution.image_stacks import is_known_stack

                if not is_known_stack(val.strip()):
                    errors.append(
                        LintError(
                            file=file_path,
                            line=0,
                            message=(
                                f"base_image_stack {val!r} is not a known stack; "
                                "register it via MOTET_IMAGE_STACK_<NAME> or pick a "
                                "builtin (python-minimal, python-office, python-browser)."
                            ),
                            severity="warning",
                        )
                    )
            except Exception:
                pass
    return errors


def _lint_exec_bundle_paths(bundle_files: Dict[str, bytes]) -> List[LintError]:
    """Ensure config/exec requirements_path resolves inside the bundle archive."""
    errors: List[LintError] = []
    for exec_name in ("config/exec.yaml", "config/exec.yml"):
        if exec_name not in bundle_files:
            continue
        content = bundle_files[exec_name].decode("utf-8", errors="replace")
        try:
            import yaml  # type: ignore[import]

            data = yaml.safe_load(content)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        req_path = data.get("requirements_path")
        if req_path is None:
            continue
        if not isinstance(req_path, str) or not req_path.strip():
            errors.append(
                LintError(
                    file=exec_name,
                    line=0,
                    message="requirements_path must be a non-empty string when set",
                    severity="error",
                )
            )
            continue
        safe = _safe_exec_requirements_relative(req_path)
        if not safe:
            errors.append(
                LintError(
                    file=exec_name,
                    line=0,
                    message="requirements_path must be a relative bundle path without '..' or absolute segments",
                    severity="error",
                )
            )
            continue
        if safe not in bundle_files:
            errors.append(
                LintError(
                    file=exec_name,
                    line=0,
                    message=f"requirements_path {safe!r} does not exist in the bundle",
                    severity="error",
                )
            )
    return errors


def _enrich_exec_meta_requirements_sha(
    exec_meta: Dict[str, Any],
    bundle_files: Dict[str, bytes],
) -> Dict[str, Any]:
    """Add requirements_sha256 to catalog exec when requirements_path is set."""
    meta = dict(exec_meta)
    rp = meta.get("requirements_path")
    if not rp:
        return meta
    safe = _safe_exec_requirements_relative(rp)
    if not safe:
        return meta
    blob = bundle_files.get(safe)
    if blob is None:
        return meta
    meta["requirements_sha256"] = hashlib.sha256(blob).hexdigest()
    return meta


def _lint_bundle(bundle_files: Dict[str, bytes]) -> Tuple[bool, List[LintError]]:
    """
    Run the full bundle lint gate over all files.
    Returns (passed: bool, all_errors: List[LintError]).
    Warnings do not fail the gate; only severity=='error' items do.
    """
    all_errors: List[LintError] = []
    all_errors.extend(_lint_reserved_bundle_name(bundle_files))
    for file_path, content_bytes in bundle_files.items():
        try:
            content = content_bytes.decode("utf-8", errors="replace")
        except Exception:
            content = ""

        if file_path.endswith(".py"):
            all_errors.extend(_lint_python_file(file_path, content))
        elif file_path.endswith((".yaml", ".yml")):
            all_errors.extend(_lint_yaml_file(file_path, content))
            if _is_skill_runners_file(file_path):
                all_errors.extend(_lint_runners_file(file_path, content))
            if _is_skill_script_usage_file(file_path):
                all_errors.extend(_lint_script_usage_file(file_path, content))
        elif file_path.startswith("skills/") and file_path.endswith("/SKILL.md"):
            all_errors.extend(_lint_skill_markdown_file(file_path, content))

    all_errors.extend(_lint_exec_bundle_paths(bundle_files))
    all_errors.extend(_lint_runner_script_paths(bundle_files))
    all_errors.extend(_lint_script_usage_paths(bundle_files))
    all_errors.extend(_lint_skill_portability(bundle_files))
    fatal = [e for e in all_errors if e.severity == "error"]
    return (len(fatal) == 0), all_errors


def _manifest_file_name(bundle_files: Dict[str, bytes]) -> str:
    """Return the manifest filename present in bundle files (best effort)."""
    for name in ("manifest.yaml", "manifest.yml", "bundle.json"):
        if name in bundle_files:
            return name
    return "manifest.yaml"


def _lint_reserved_bundle_name(bundle_files: Dict[str, bytes]) -> List[LintError]:
    """
    Emit an explicit lint error when the manifest name is reserved.

    This complements _validate_bundle_name() so validate-only flows can surface
    reserved-name failures as lint_error/lint_complete SSE events.
    """
    # Lazy import avoids deploy ↔ bundle_lint cycle at module load (issue #158).
    from motet.core.bundles.deploy import RESERVED_BUNDLE_NAMES, _parse_manifest

    try:
        manifest = _parse_manifest(bundle_files)
    except ValueError:
        return []

    bundle_name = str(manifest.get("name", "") or "")
    if bundle_name in RESERVED_BUNDLE_NAMES:
        return [
            LintError(
                file=_manifest_file_name(bundle_files),
                line=0,
                message=(
                    f"Reserved bundle name '{bundle_name}' is not allowed. "
                    "Choose a different manifest name."
                ),
                severity="error",
            )
        ]
    return []


def _collect_lint_errors(
    bundle_files: Dict[str, bytes],
    *,
    motet: Optional[Any] = None,
) -> List[LintError]:
    """
    Run per-file lint checks and optionally stream lint_file/lint_error events.

    Reserved-name lint is intentionally handled in manifest validation and in
    _lint_bundle() to preserve current call-site behavior.
    """
    all_errors: List[LintError] = []
    for file_path in sorted(bundle_files.keys()):
        if motet is not None:
            motet.stream_event("lint_file", file=file_path)
        if file_path.endswith((".py", ".yaml", ".yml")):
            content = bundle_files[file_path].decode("utf-8", errors="replace")
            errs = _lint_python_file(file_path, content) if file_path.endswith(".py") else _lint_yaml_file(file_path, content)
            if file_path in ("config/exec.yaml", "config/exec.yml"):
                errs = errs + _lint_exec_config_file(file_path, content)
            if _is_skill_runners_file(file_path):
                errs = errs + _lint_runners_file(file_path, content)
            if _is_skill_script_usage_file(file_path):
                errs = errs + _lint_script_usage_file(file_path, content)
        elif file_path.startswith("skills/") and file_path.endswith("/SKILL.md"):
            content = bundle_files[file_path].decode("utf-8", errors="replace")
            errs = _lint_skill_markdown_file(file_path, content)
        else:
            errs = []
        for err in errs:
            if motet is not None:
                motet.stream_event(
                    "lint_error",
                    file=err.file,
                    line=err.line,
                    message=err.message,
                    severity=err.severity,
                )
            all_errors.append(err)
    all_errors.extend(_lint_exec_bundle_paths(bundle_files))
    runner_path_errs = _lint_runner_script_paths(bundle_files)
    if motet is not None:
        for err in runner_path_errs:
            motet.stream_event(
                "lint_error",
                file=err.file,
                line=err.line,
                message=err.message,
                severity=err.severity,
            )
    all_errors.extend(runner_path_errs)
    for cross_file_err in _lint_script_usage_paths(bundle_files) + _lint_skill_portability(bundle_files):
        if motet is not None:
            motet.stream_event(
                "lint_error",
                file=cross_file_err.file,
                line=cross_file_err.line,
                message=cross_file_err.message,
                severity=cross_file_err.severity,
            )
        all_errors.append(cross_file_err)
    return all_errors


def _fatal_lint_errors(all_errors: List[LintError]) -> List[LintError]:
    """Return only severity=error lint findings."""
    return [err for err in all_errors if err.severity == "error"]


def _emit_lint_failure_events(
    motet: Any,
    bundle_id: str,
    errors: List[LintError],
) -> None:
    """Emit lint_error + lint_complete(passed=False) for a failed lint phase."""
    for err in errors:
        motet.stream_event(
            "lint_error",
            file=err.file,
            line=err.line,
            message=err.message,
            severity=err.severity,
        )
    motet.stream_event(
        "lint_complete",
        passed=False,
        bundle_id=bundle_id,
        errors=[err.model_dump() for err in errors],
    )

__all__ = [
    "DANGEROUS_IMPORTS",
    "LintError",
    "THREADING_ANTIPATTERNS",
    "_ADHOC_IDENTITY_RE",
    "_COMMON_LOCAL_IMPORTS",
    "_ENV_REQUIRE_DIGEST_PINNED_PUBLISH",
    "_EXEC_CONFIG_ALLOWED_KEYS",
    "_HOST_ABSOLUTE_PATH_RE",
    "_MARKDOWN_LINK_RE",
    "_RUNTIME_CAPABILITY_RE",
    "_SCRIPT_REF_RE",
    "_STD_LIB_MODULES",
    "_SYSTEM_PRINCIPAL_RE",
    "_THREADING_RE",
    "_collect_lint_errors",
    "_emit_lint_failure_events",
    "_enrich_exec_meta_requirements_sha",
    "_fatal_lint_errors",
    "_get_decorator_qualified_names",
    "_is_external_markdown_link",
    "_is_motet_tool",
    "_is_skill_runners_file",
    "_is_skill_script_usage_file",
    "_line_for_offset",
    "_lint_bundle",
    "_lint_exec_bundle_paths",
    "_lint_exec_config_file",
    "_lint_python_file",
    "_lint_reserved_bundle_name",
    "_lint_runner_script_paths",
    "_lint_runners_file",
    "_lint_script_usage_file",
    "_lint_script_usage_paths",
    "_lint_skill_markdown_file",
    "_lint_skill_portability",
    "_lint_yaml_file",
    "_manifest_file_name",
    "_normalize_exec_config_block",
    "_normalize_runtime_capabilities",
    "_safe_exec_requirements_relative",
    "_safe_skill_relative_path",
    "_third_party_imports",
]
