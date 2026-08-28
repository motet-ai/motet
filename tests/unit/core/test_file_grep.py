"""Unit tests for core.file_grep builtin (ADR-0122)."""

from __future__ import annotations

import os
from unittest.mock import patch

from motet.core.tools.builtin import file_grep
from motet.core.tools.builtin.file_grep import run


def _write_tree(tmp_path) -> None:
    (tmp_path / "a.py").write_text("def register():\n    pass\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("no match here\n", encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "c.py").write_text(
        "class ToolRegistry:\n    def register(self):\n        return 1\n",
        encoding="utf-8",
    )


def test_file_grep_python_fallback_finds_matches(tmp_path) -> None:
    _write_tree(tmp_path)
    with patch.object(file_grep, "_ripgrep_available", return_value=False):
        r = run(
            {
                "root": str(tmp_path),
                "pattern": r"def register",
                "allowlist": str(tmp_path),
            }
        )
    assert "error" not in r, r
    assert r["engine"] == "python"
    assert r["match_count"] >= 2
    assert r["file_count"] >= 2
    paths = {m["path"] for m in r["matches"]}
    assert any(p.endswith("a.py") for p in paths)
    assert any(p.endswith(os.path.join("sub", "c.py")) for p in paths)


def test_file_grep_glob_filters(tmp_path) -> None:
    _write_tree(tmp_path)
    with patch.object(file_grep, "_ripgrep_available", return_value=False):
        r = run(
            {
                "root": str(tmp_path),
                "pattern": "register",
                "glob": "*.txt",
                "allowlist": str(tmp_path),
            }
        )
    assert "error" not in r, r
    assert r["match_count"] == 0


def test_file_grep_case_insensitive(tmp_path) -> None:
    (tmp_path / "x.py").write_text("Hello World\n", encoding="utf-8")
    with patch.object(file_grep, "_ripgrep_available", return_value=False):
        r = run(
            {
                "root": str(tmp_path),
                "pattern": "hello",
                "case_insensitive": True,
                "allowlist": str(tmp_path),
            }
        )
    assert r["match_count"] == 1
    assert r["matches"][0]["line"] == 1


def test_file_grep_context_lines(tmp_path) -> None:
    (tmp_path / "x.py").write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
    with patch.object(file_grep, "_ripgrep_available", return_value=False):
        r = run(
            {
                "root": str(tmp_path),
                "pattern": "three",
                "context_lines": 1,
                "allowlist": str(tmp_path),
            }
        )
    assert r["match_count"] == 1
    m = r["matches"][0]
    assert m["context_before"] == ["two"]
    assert m["context_after"] == ["four"]


def test_file_grep_max_results_truncates(tmp_path) -> None:
    (tmp_path / "x.py").write_text("hit\nhit\nhit\nhit\n", encoding="utf-8")
    with patch.object(file_grep, "_ripgrep_available", return_value=False):
        r = run(
            {
                "root": str(tmp_path),
                "pattern": "hit",
                "max_results": 2,
                "allowlist": str(tmp_path),
            }
        )
    assert r["truncated"] is True
    assert r["match_count"] == 2


def test_file_grep_allowlist_denies(tmp_path) -> None:
    r = run(
        {
            "root": str(tmp_path),
            "pattern": "x",
            "allowlist": "/nonexistent/allowlist_root_only",
        }
    )
    assert "error" in r


def test_file_grep_invalid_regex(tmp_path) -> None:
    (tmp_path / "x.py").write_text("x\n", encoding="utf-8")
    with patch.object(file_grep, "_ripgrep_available", return_value=False):
        r = run(
            {
                "root": str(tmp_path),
                "pattern": "(",
                "allowlist": str(tmp_path),
            }
        )
    assert "error" in r
    assert "invalid regex" in r["error"]


def test_file_grep_skips_binary(tmp_path) -> None:
    (tmp_path / "bin.dat").write_bytes(b"abc\x00def\nregister\n")
    (tmp_path / "ok.py").write_text("register()\n", encoding="utf-8")
    with patch.object(file_grep, "_ripgrep_available", return_value=False):
        r = run(
            {
                "root": str(tmp_path),
                "pattern": "register",
                "allowlist": str(tmp_path),
            }
        )
    assert r["match_count"] == 1
    assert r["matches"][0]["path"].endswith("ok.py")


def test_file_grep_uses_ripgrep_when_available(tmp_path) -> None:
    """When rg is present, engine should be ripgrep (integration-ish)."""
    if not file_grep._ripgrep_available():
        # Skip assertion on machines without rg; python path is covered above.
        return
    _write_tree(tmp_path)
    r = run(
        {
            "root": str(tmp_path),
            "pattern": r"def register",
            "allowlist": str(tmp_path),
        }
    )
    assert "error" not in r, r
    assert r["engine"] == "ripgrep"
    assert r["match_count"] >= 2
