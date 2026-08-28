"""
Motet - Bundle lint tests for runners.yaml (ADR-0101 Slice B)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-04-25

Description:
    Verifies the runners.yaml lint pass:
      1. Structural errors from the parser surface as lint errors.
      2. Missing scripts referenced from runners.yaml fail bundle-wide lint.
      3. The lint pass is dispatched by both ``_collect_lint_errors``
         (the publish/upload path) and ``_lint_bundle`` (the legacy
         hot-deploy path).
"""

from __future__ import annotations

import textwrap

from motet.core.bundles.deploy import (
    _collect_lint_errors,
    _lint_bundle,
    _lint_python_file,
    _lint_runner_script_paths,
    _lint_runners_file,
)


_VALID_SKILL_MD = textwrap.dedent(
    """
    ---
    name: demo-skill
    description: Demo skill for runner lint tests.
    ---

    body
    """
).encode("utf-8")


def _bundle(runners_yaml: str, *, with_script: bool = True) -> dict[str, bytes]:
    files: dict[str, bytes] = {
        "manifest.yaml": b'format_version: "1"\nname: "demo"\nversion: "0.1.0"\ndescription: x\n',
        "skills/demo-skill/SKILL.md": _VALID_SKILL_MD,
        "skills/demo-skill/runners.yaml": runners_yaml.encode("utf-8"),
    }
    if with_script:
        files["skills/demo-skill/scripts/echo.py"] = b"print('ok')\n"
    return files


def test_valid_runners_yaml_emits_no_fatal_lint_errors() -> None:
    yaml = """
runners:
  - name: echo
    description: hi
    script: scripts/echo.py
"""
    files = _bundle(yaml)
    passed, errors = _lint_bundle(files)
    assert passed is True
    assert all(e.severity != "error" for e in errors)


def test_malformed_runners_yaml_fails_bundle_lint() -> None:
    yaml = "runners:\n  - script: only-script.py\n"  # missing name & description
    passed, errors = _lint_bundle(_bundle(yaml))
    assert passed is False
    assert any(
        "missing required field 'name'" in e.message
        and e.file == "skills/demo-skill/runners.yaml"
        for e in errors
    )


def test_missing_script_path_fails_bundle_lint() -> None:
    yaml = """
runners:
  - name: echo
    description: hi
    script: scripts/missing.py
"""
    files = _bundle(yaml, with_script=False)
    passed, errors = _lint_bundle(files)
    assert passed is False
    assert any(
        "scripts/missing.py" in e.message and e.severity == "error" for e in errors
    )


def test_lifetime_workspace_emits_no_preview_warning() -> None:
    yaml = """
runners:
  - name: echo
    description: hi
    script: scripts/echo.py
    lifetime: workspace
"""
    files = _bundle(yaml)
    passed, errors = _lint_bundle(files)
    assert passed is True
    assert all(e.severity != "warning" for e in errors)


def test_collect_lint_errors_dispatches_runners_lint() -> None:
    yaml = "runners:\n  - description: missing-name\n    script: s.py\n"
    files = _bundle(yaml, with_script=True)
    files["skills/demo-skill/scripts/s.py"] = b"print('ok')\n"
    errors = _collect_lint_errors(files)
    assert any(
        "missing required field 'name'" in e.message
        and e.file == "skills/demo-skill/runners.yaml"
        for e in errors
    )


def test_lint_runners_file_direct_call() -> None:
    errors = _lint_runners_file(
        "skills/demo-skill/runners.yaml",
        "runners:\n  - description: x\n    script: s.py\n",
    )
    assert any(e.severity == "error" for e in errors)


def test_lint_runner_script_paths_skips_unrelated_files() -> None:
    files = {
        "manifest.yaml": b"name: x\n",
        "config/exec.yaml": b"oci_image_ref: nope\n",
    }
    assert _lint_runner_script_paths(files) == []


def test_builtin_compile_is_warning_not_error() -> None:
    errors = _lint_python_file(
        "skills/demo-skill/scripts/dynamic.py",
        "code = compile('1 + 1', '<expr>', 'eval')\n",
    )
    assert any("compile()" in e.message and e.severity == "warning" for e in errors)
    assert all(e.severity != "error" for e in errors)


def test_re_compile_is_not_flagged_as_dynamic_code() -> None:
    errors = _lint_python_file(
        "skills/demo-skill/scripts/regex.py",
        "import re\npattern = re.compile(r'^[a-z]+$')\n",
    )
    assert not any("compile()" in e.message for e in errors)


def test_eval_and_exec_remain_fatal_lint_errors() -> None:
    errors = _lint_python_file(
        "skills/demo-skill/scripts/dynamic.py",
        "eval('1 + 1')\nexec('x = 1')\n",
    )
    assert sum(1 for e in errors if e.severity == "error") == 2
