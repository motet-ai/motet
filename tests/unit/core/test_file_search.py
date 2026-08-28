"""Unit tests for core.file_search builtin."""

from __future__ import annotations

import os

from motet.core.tools.builtin.file_search import run


def test_file_search_basename_pattern(tmp_path) -> None:
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "b.py").write_text("b", encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "c.py").write_text("c", encoding="utf-8")

    r = run(
        {
            "root": str(tmp_path),
            "pattern": "*.py",
            "allowlist": str(tmp_path),
        }
    )
    assert "error" not in r
    paths = sorted(r["paths"])
    assert len(paths) == 2
    assert paths[0].endswith("b.py")
    assert paths[1].endswith(os.path.join("sub", "c.py"))


def test_file_search_allowlist_denies(tmp_path) -> None:
    r = run(
        {
            "root": str(tmp_path),
            "pattern": "*",
            "allowlist": "/nonexistent/allowlist_root_only",
        }
    )
    assert "error" in r


def test_file_search_max_depth(tmp_path) -> None:
    (tmp_path / "root.txt").write_text("x", encoding="utf-8")
    sub = tmp_path / "d"
    sub.mkdir()
    (sub / "inner.txt").write_text("y", encoding="utf-8")

    r = run(
        {
            "root": str(tmp_path),
            "pattern": "*.txt",
            "allowlist": str(tmp_path),
            "max_depth": 1,
        }
    )
    assert "error" not in r
    assert len(r["paths"]) == 1
    assert r["paths"][0].endswith("root.txt")


def test_file_search_truncated(tmp_path) -> None:
    for i in range(5):
        (tmp_path / f"f{i}.txt").write_text("x", encoding="utf-8")
    r = run(
        {
            "root": str(tmp_path),
            "pattern": "*.txt",
            "allowlist": str(tmp_path),
            "max_results": 3,
        }
    )
    assert r["truncated"] is True
    assert r["count"] == 3
