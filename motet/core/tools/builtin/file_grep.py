"""
Motet - File Grep Tool

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Regex content search under an allowlisted root. Prefers ripgrep (``rg --json``)
    when available and falls back to a pure-Python walk + ``re`` search. Caps
    per-file and total matches, skips binary / oversized files, and returns a
    compact match list sized for LLM consumption.

Dependencies:
    - os, re, shutil, subprocess, fnmatch, pathlib, typing, pydantic
    - Tool registry and protocol (err)

Usage:
    result = run({
        "root": "/mnt/motet/read/0",
        "pattern": "def register\\(",
        "glob": "*.py",
        "max_results": 50,
    })

Notes:
    - Routed only to edge workers (EDGE_EXECUTION + EDGE_FILE_SEARCH), like
      core.file_search.
    - Uses MOTET_FILE_READ_ALLOWLIST (or allowlist param) for root policy.
    - Does not follow symlinks during the Python fallback walk.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from ..protocol import err
from ..registry import ToolRegistry
from .path_policy import allowed_roots_from_csv, is_under_allowlist, resolve_allowlisted_path


class Params(BaseModel):
    root: str = Field(
        ...,
        description="Directory to search (must fall under allowlist when allowlist is set)",
    )
    pattern: str = Field(..., description="Regular expression to search for in file contents")
    glob: Optional[str] = Field(
        default=None,
        description=(
            "Optional filename/path glob filter (same semantics as core.file_search): "
            "without '/' or '**' matches basename; otherwise relative path"
        ),
    )
    context_lines: int = Field(
        default=0,
        ge=0,
        le=20,
        description="Lines of context before and after each match",
    )
    max_matches_per_file: int = Field(
        default=20,
        ge=1,
        description="Cap on matches returned from a single file",
    )
    max_results: int = Field(
        default=200,
        ge=1,
        description="Cap on total matches returned",
    )
    max_file_bytes: int = Field(
        default=2_000_000,
        ge=1,
        description="Skip files larger than this many bytes",
    )
    case_insensitive: bool = Field(
        default=False,
        description="If true, match case-insensitively",
    )
    allowlist: Optional[str] = Field(
        default=None,
        description="Override comma-separated allowed roots (else MOTET_FILE_READ_ALLOWLIST)",
    )


def _allowed_roots(allowlist: Optional[str]) -> List[str]:
    return allowed_roots_from_csv(allowlist or os.getenv("MOTET_FILE_READ_ALLOWLIST"))


def _is_under_allowlist(abs_path: str, roots: List[str]) -> bool:
    return is_under_allowlist(abs_path, roots)


def _root_must_be_allowlisted(root_abs: str, roots: List[str]) -> Optional[Dict[str, Any]]:
    if not roots:
        return None
    resolved, deny = resolve_allowlisted_path(root_abs, roots)
    if deny or resolved is None:
        return err("root not in allowlist")
    return None


def _glob_matches(rel_file: Path, pattern: Optional[str]) -> bool:
    if not pattern:
        return True
    pat = pattern.strip()
    if not pat:
        return True
    if "/" in pat or "**" in pat:
        return PurePosixPath(rel_file.as_posix()).match(pat)
    return fnmatch.fnmatch(rel_file.name, pat)


def _is_binary(path: str, sample_size: int = 8192) -> bool:
    try:
        with open(path, "rb") as f:
            chunk = f.read(sample_size)
        return b"\x00" in chunk
    except OSError:
        return True


def _match_entry(
    path: str,
    line_no: int,
    text: str,
    context_before: List[str],
    context_after: List[str],
) -> Dict[str, Any]:
    return {
        "path": path,
        "line": line_no,
        "text": text.rstrip("\n"),
        "context_before": [c.rstrip("\n") for c in context_before],
        "context_after": [c.rstrip("\n") for c in context_after],
    }


def _python_grep(
    root_abs: str,
    pattern: str,
    glob: Optional[str],
    context_lines: int,
    max_matches_per_file: int,
    max_results: int,
    max_file_bytes: int,
    case_insensitive: bool,
) -> Tuple[List[Dict[str, Any]], bool, str]:
    flags = re.IGNORECASE if case_insensitive else 0
    try:
        regex = re.compile(pattern, flags)
    except re.error as exc:
        raise ValueError(f"invalid regex: {exc}") from exc

    matches: List[Dict[str, Any]] = []
    truncated = False
    files_with_hits: set[str] = set()

    for dirpath, dirnames, filenames in os.walk(root_abs, topdown=True, followlinks=False):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name.startswith("."):
                continue
            full = os.path.join(dirpath, name)
            if not os.path.isfile(full):
                continue
            try:
                rel = Path(os.path.relpath(full, root_abs))
            except ValueError:
                continue
            if not _glob_matches(rel, glob):
                continue
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            if size > max_file_bytes:
                continue
            if _is_binary(full):
                continue
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
            except OSError:
                continue

            per_file = 0
            for idx, line in enumerate(lines):
                if not regex.search(line):
                    continue
                before = lines[max(0, idx - context_lines) : idx]
                after = lines[idx + 1 : idx + 1 + context_lines]
                matches.append(
                    _match_entry(
                        os.path.abspath(full),
                        idx + 1,
                        line,
                        before,
                        after,
                    )
                )
                files_with_hits.add(full)
                per_file += 1
                if len(matches) >= max_results:
                    truncated = True
                    return matches, truncated, "python"
                if per_file >= max_matches_per_file:
                    break

    return matches, truncated, "python"


def _ripgrep_available() -> bool:
    return shutil.which("rg") is not None


def _ripgrep_grep(
    root_abs: str,
    pattern: str,
    glob: Optional[str],
    context_lines: int,
    max_matches_per_file: int,
    max_results: int,
    max_file_bytes: int,
    case_insensitive: bool,
) -> Tuple[List[Dict[str, Any]], bool, str]:
    cmd = [
        "rg",
        "--json",
        "--line-number",
        "--no-heading",
        "--color",
        "never",
        "--max-filesize",
        str(max_file_bytes),
        "--max-count",
        str(max_matches_per_file),
    ]
    if case_insensitive:
        cmd.append("-i")
    if context_lines > 0:
        cmd.extend(["-C", str(context_lines)])
    if glob:
        cmd.extend(["--glob", glob])
    cmd.extend(["--", pattern, root_abs])

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except FileNotFoundError:
        raise RuntimeError("rg not found")
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"rg timed out: {exc}") from exc

    # rg exit: 0 = matches, 1 = no matches, 2 = error
    if proc.returncode not in (0, 1):
        stderr = (proc.stderr or "").strip()
        raise RuntimeError(stderr or f"rg failed with exit {proc.returncode}")

    matches: List[Dict[str, Any]] = []
    truncated = False
    # Buffer pending context for match lines when -C is used.
    # rg --json emits type=match and type=context events.
    pending_before: Dict[str, List[str]] = {}
    last_match_key: Optional[Tuple[str, int]] = None
    after_needed: Dict[Tuple[str, int], int] = {}

    for raw_line in (proc.stdout or "").splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        etype = event.get("type")
        data = event.get("data") or {}
        path_text = ((data.get("path") or {}).get("text")) or ""
        if not path_text:
            continue
        abs_path = os.path.abspath(path_text)
        line_text = ((data.get("lines") or {}).get("text")) or ""
        line_no = data.get("line_number")

        if etype == "context":
            if last_match_key and after_needed.get(last_match_key, 0) > 0:
                # Attach as after-context to the last match
                for m in reversed(matches):
                    if m["path"] == last_match_key[0] and m["line"] == last_match_key[1]:
                        m["context_after"].append(line_text.rstrip("\n"))
                        after_needed[last_match_key] -= 1
                        break
            else:
                pending_before.setdefault(abs_path, []).append(line_text)
                if len(pending_before[abs_path]) > context_lines:
                    pending_before[abs_path] = pending_before[abs_path][-context_lines:]
            continue

        if etype != "match":
            continue
        if line_no is None:
            continue

        before = pending_before.pop(abs_path, [])[-context_lines:] if context_lines else []
        entry = _match_entry(abs_path, int(line_no), line_text, before, [])
        matches.append(entry)
        key = (abs_path, int(line_no))
        last_match_key = key
        if context_lines > 0:
            after_needed[key] = context_lines

        if len(matches) >= max_results:
            truncated = True
            break

    return matches, truncated, "ripgrep"


def run(params: Dict[str, Any]) -> Dict[str, Any]:
    """Synchronous content grep (gevent/eventlet compatible)."""
    root = params.get("root")
    pattern = params.get("pattern")
    if not root or not str(root).strip():
        return err("root is required")
    if pattern is None or not str(pattern).strip():
        return err("pattern is required")

    glob = params.get("glob")
    if glob is not None:
        glob = str(glob).strip() or None

    try:
        context_lines = int(params.get("context_lines", 0) or 0)
        max_matches_per_file = int(params.get("max_matches_per_file", 20) or 20)
        max_results = int(params.get("max_results", 200) or 200)
        max_file_bytes = int(params.get("max_file_bytes", 2_000_000) or 2_000_000)
    except (TypeError, ValueError):
        return err("numeric parameters must be integers")

    if context_lines < 0 or max_matches_per_file < 1 or max_results < 1 or max_file_bytes < 1:
        return err("invalid numeric parameter")

    case_insensitive = bool(params.get("case_insensitive", False))
    allowlist = params.get("allowlist")
    roots = _allowed_roots(str(allowlist) if allowlist else None)

    try:
        root_abs = os.path.abspath(str(root).strip())
        if not os.path.isdir(root_abs):
            return err("root is not a directory or does not exist")
        deny = _root_must_be_allowlisted(root_abs, roots)
        if deny is not None:
            return deny

        engine = "python"
        matches: List[Dict[str, Any]]
        truncated: bool
        if _ripgrep_available():
            try:
                matches, truncated, engine = _ripgrep_grep(
                    root_abs,
                    str(pattern),
                    glob,
                    context_lines,
                    max_matches_per_file,
                    max_results,
                    max_file_bytes,
                    case_insensitive,
                )
            except (RuntimeError, ValueError):
                matches, truncated, engine = _python_grep(
                    root_abs,
                    str(pattern),
                    glob,
                    context_lines,
                    max_matches_per_file,
                    max_results,
                    max_file_bytes,
                    case_insensitive,
                )
        else:
            matches, truncated, engine = _python_grep(
                root_abs,
                str(pattern),
                glob,
                context_lines,
                max_matches_per_file,
                max_results,
                max_file_bytes,
                case_insensitive,
            )

        files = sorted({m["path"] for m in matches})
        return {
            "root": root_abs,
            "pattern": str(pattern),
            "matches": matches,
            "match_count": len(matches),
            "file_count": len(files),
            "truncated": truncated,
            "engine": engine,
        }
    except ValueError as exc:
        return err(str(exc))
    except Exception as exc:
        return err(str(exc))


def _fmt(res: Dict[str, Any]) -> str:
    if "error" in res:
        return f"file_grep(error={res['error']})"
    return (
        f"file_grep(ok, files={res.get('file_count')}, "
        f"matches={res.get('match_count')}, truncated={res.get('truncated')})"
    )


def register(registry: ToolRegistry) -> None:
    registry.register(
        name="core.file_grep",
        description=(
            "Search file contents under a directory with a regular expression "
            "(local device worker only). Respects MOTET_FILE_READ_ALLOWLIST "
            "(or allowlist param). Prefer this over reading whole files when "
            "locating symbols or strings. Optional glob filters by filename/path."
        ),
        func=run,
        tool_schema=Params,
        triggers=[],
        priority=5,
        estimate_tokens=lambda _: 40,
        parse_params=None,
        observation_formatter=_fmt,
        category="filesystem",
        required_capabilities=[
            "TOOL_EXECUTION",
            "EDGE_EXECUTION",
            "EDGE_FILE_SEARCH",
        ],
    )


__all__ = ["register", "run"]
