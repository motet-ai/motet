"""Unit tests for core.file_edit builtin (ADR-0122)."""

from __future__ import annotations

import os

from motet.core.tools.builtin.file_edit import run


def test_file_edit_unique_replacement(tmp_path) -> None:
    path = tmp_path / "mod.py"
    path.write_text("def foo():\n    return 1\n\ndef bar():\n    return 2\n", encoding="utf-8")

    r = run(
        {
            "path": str(path),
            "old_string": "def foo():\n    return 1",
            "new_string": "def foo():\n    return 42",
            "allowlist": str(tmp_path),
        }
    )
    assert "error" not in r, r
    assert r["replacements"] == 1
    assert r["bytes_written"] > 0
    assert "return 42" in path.read_text(encoding="utf-8")
    assert "def bar():" in path.read_text(encoding="utf-8")


def test_file_edit_not_found(tmp_path) -> None:
    path = tmp_path / "a.txt"
    path.write_text("hello world\n", encoding="utf-8")
    r = run(
        {
            "path": str(path),
            "old_string": "missing",
            "new_string": "x",
            "allowlist": str(tmp_path),
        }
    )
    assert r.get("error") == "old_string not found"


def test_file_edit_not_unique_without_replace_all(tmp_path) -> None:
    path = tmp_path / "a.txt"
    path.write_text("aaa\nbbb\naaa\n", encoding="utf-8")
    r = run(
        {
            "path": str(path),
            "old_string": "aaa",
            "new_string": "zzz",
            "allowlist": str(tmp_path),
        }
    )
    assert "error" in r
    assert "not unique" in r["error"]
    assert "2 matches" in r["error"]
    assert path.read_text(encoding="utf-8") == "aaa\nbbb\naaa\n"


def test_file_edit_replace_all(tmp_path) -> None:
    path = tmp_path / "a.txt"
    path.write_text("aaa\nbbb\naaa\n", encoding="utf-8")
    r = run(
        {
            "path": str(path),
            "old_string": "aaa",
            "new_string": "zzz",
            "replace_all": True,
            "allowlist": str(tmp_path),
        }
    )
    assert "error" not in r, r
    assert r["replacements"] == 2
    assert path.read_text(encoding="utf-8") == "zzz\nbbb\nzzz\n"


def test_file_edit_identical_strings(tmp_path) -> None:
    path = tmp_path / "a.txt"
    path.write_text("same\n", encoding="utf-8")
    r = run(
        {
            "path": str(path),
            "old_string": "same",
            "new_string": "same",
            "allowlist": str(tmp_path),
        }
    )
    assert "error" in r
    assert "identical" in r["error"]


def test_file_edit_allowlist_denies(tmp_path) -> None:
    path = tmp_path / "a.txt"
    path.write_text("x\n", encoding="utf-8")
    r = run(
        {
            "path": str(path),
            "old_string": "x",
            "new_string": "y",
            "allowlist": "/nonexistent/allowlist_root_only",
        }
    )
    assert r.get("error") == "path not in allowlist"


def test_file_edit_missing_file(tmp_path) -> None:
    r = run(
        {
            "path": str(tmp_path / "nope.txt"),
            "old_string": "a",
            "new_string": "b",
            "allowlist": str(tmp_path),
        }
    )
    assert r.get("error") == "file does not exist"


def test_file_edit_empty_old_string(tmp_path) -> None:
    path = tmp_path / "a.txt"
    path.write_text("x\n", encoding="utf-8")
    r = run(
        {
            "path": str(path),
            "old_string": "",
            "new_string": "y",
            "allowlist": str(tmp_path),
        }
    )
    assert "error" in r
    assert "non-empty" in r["error"]
