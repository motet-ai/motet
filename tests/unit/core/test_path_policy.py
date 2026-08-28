"""
Motet - Path policy / file allowlist symlink tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-07-16

Description:
    Unit tests for realpath-based file allowlists (ADR-0122 audit hardening).
"""

from __future__ import annotations

from motet.core.tools.builtin.file_read import run as file_read
from motet.core.tools.builtin.path_policy import (
    allowed_roots_from_csv,
    resolve_allowlisted_path,
)


def test_resolve_allowlisted_path_denies_symlink_escape(tmp_path) -> None:
    allowed = tmp_path / "workspace"
    allowed.mkdir()
    outside = tmp_path / "secret.env"
    outside.write_text("OPENAI_API_KEY=sk-test\n", encoding="utf-8")
    leak = allowed / "leak"
    leak.symlink_to(outside)

    roots = allowed_roots_from_csv(str(allowed))
    resolved, err = resolve_allowlisted_path(str(leak), roots)
    assert resolved is None
    assert err == "path not in allowlist"


def test_file_read_denies_symlink_to_proc_style_escape(tmp_path) -> None:
    allowed = tmp_path / "clone"
    allowed.mkdir()
    outside = tmp_path / "environ.txt"
    outside.write_text("SECRET=1\n", encoding="utf-8")
    link = allowed / ".leak"
    link.symlink_to(outside)

    r = file_read({"path": str(link), "allowlist": str(allowed)})
    assert r.get("error") == "path not in allowlist"


def test_file_read_allows_normal_file(tmp_path) -> None:
    allowed = tmp_path / "clone"
    allowed.mkdir()
    target = allowed / "README.md"
    target.write_text("hello\n", encoding="utf-8")
    r = file_read({"path": str(target), "allowlist": str(allowed)})
    assert "error" not in r, r
    assert r["text"] == "hello\n"
