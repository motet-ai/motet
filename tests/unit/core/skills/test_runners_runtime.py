"""
Unit tests for ``motet.core.skills.runtime`` (ADR-0101 Slice B).

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-04-25

Description:
    Verifies that ``register_runners_for_skill`` produces well-formed tool
    registrations for each runner: correct namespaced names, schema
    synthesis, dispatcher composition, and forwarding of params to the
    underlying ``core.worker_exec`` path. Dispatch is exercised against
    a monkey-patched ``worker_exec.run`` so the test does not require a
    Docker daemon, an allowlisted cwd, or even a real bundle on disk.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Dict, List

import pytest

from motet.core.skills.runtime import register_runners_for_skill
from motet.core.tools import registry as tool_registry


@pytest.fixture
def temp_skill_dir(tmp_path: Path) -> Path:
    """Materialize a minimal skill directory with a runners.yaml on disk."""
    skill = tmp_path / "skills" / "demo-skill"
    (skill / "scripts").mkdir(parents=True)
    (skill / "scripts" / "echo.py").write_text("print('hi')\n", encoding="utf-8")
    (skill / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: A demo skill.\n---\n\nbody\n",
        encoding="utf-8",
    )
    (skill / "runners.yaml").write_text(
        """
runners:
  - name: echo
    description: Echo a string.
    script: scripts/echo.py
    interpreter: python3
    timeout_seconds: 5
    args:
      text:
        type: string
        default: "hi"
        description: Text to echo.
      verbose:
        type: boolean
        default: false
        description: Toggle verbose flag.
""",
        encoding="utf-8",
    )
    return skill


@pytest.fixture
def captured_dispatch(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    """Replace worker_exec.run with a capturing stub.

    The stub records the params it was handed and returns a benign result
    so the runner dispatcher's post-processing (the metadata it merges
    in) can be asserted on independently.
    """
    captured: Dict[str, Any] = {}

    def fake_run(params: Dict[str, Any]) -> Dict[str, Any]:
        captured["last"] = dict(params)
        return {"returncode": 0, "stdout": "ok\n", "stderr": "", "backend": "subprocess"}

    monkeypatch.setattr("motet.core.tools.builtin.worker_exec.run", fake_run)
    return captured


@pytest.fixture
def cleanup_runner_tools():
    """Remove any tools registered by these tests so they do not leak between cases."""
    leaked: List[str] = []
    yield leaked
    for name in leaked:
        try:
            tool_registry.unregister(name)
        except Exception:
            pass


def test_register_returns_namespaced_tool_names(
    temp_skill_dir: Path, cleanup_runner_tools: List[str]
) -> None:
    names = register_runners_for_skill(
        bundle_id="demo",
        skill_name="demo-skill",
        skill_dir=temp_skill_dir,
    )
    cleanup_runner_tools.extend(names)
    assert names == ["demo.demo-skill.echo"]
    tool = tool_registry.get(names[0])
    assert tool is not None
    assert tool.name == names[0]


def test_register_synthesizes_per_runner_schema(
    temp_skill_dir: Path, cleanup_runner_tools: List[str]
) -> None:
    names = register_runners_for_skill(
        bundle_id="demo", skill_name="demo-skill", skill_dir=temp_skill_dir
    )
    cleanup_runner_tools.extend(names)
    tool = tool_registry.get(names[0])
    schema = tool.tool_schema.model_json_schema()
    assert schema["properties"]["text"]["type"] == "string"
    assert schema["properties"]["verbose"]["type"] == "boolean"
    assert schema["additionalProperties"] is False


def test_register_no_runners_yaml_returns_empty(tmp_path: Path) -> None:
    skill = tmp_path / "skills" / "x"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: x\ndescription: y\n---\nbody\n")
    assert (
        register_runners_for_skill(bundle_id="b", skill_name="x", skill_dir=skill) == []
    )


def test_register_malformed_runners_raises(tmp_path: Path) -> None:
    skill = tmp_path / "skills" / "x"
    skill.mkdir(parents=True)
    (skill / "runners.yaml").write_text("runners:\n  - script: only-script.py\n")
    with pytest.raises(ValueError, match="missing required field 'name'"):
        register_runners_for_skill(bundle_id="b", skill_name="x", skill_dir=skill)


def test_dispatch_forwards_params_to_worker_exec(
    temp_skill_dir: Path,
    captured_dispatch: Dict[str, Any],
    cleanup_runner_tools: List[str],
) -> None:
    names = register_runners_for_skill(
        bundle_id="demo", skill_name="demo-skill", skill_dir=temp_skill_dir
    )
    cleanup_runner_tools.extend(names)
    tool = tool_registry.get(names[0])
    result = tool.func({"text": "hello world", "verbose": True})

    sent = captured_dispatch["last"]
    assert sent["argv"] == [
        "python3",
        "skills/demo-skill/scripts/echo.py",
        "--text=hello world",
        "--verbose",
    ]
    assert sent["bundle_id"] == "demo"
    assert sent["timeout_seconds"] == 5
    assert result["returncode"] == 0
    assert result["runner"] == "echo"
    assert result["runner_image_stack"] == "python-minimal"
    assert result["runner_lifetime"] == "ephemeral"


def test_dispatch_omits_optional_args_not_supplied(
    temp_skill_dir: Path,
    captured_dispatch: Dict[str, Any],
    cleanup_runner_tools: List[str],
) -> None:
    names = register_runners_for_skill(
        bundle_id="demo", skill_name="demo-skill", skill_dir=temp_skill_dir
    )
    cleanup_runner_tools.extend(names)
    tool = tool_registry.get(names[0])
    tool.func({"text": "x"})

    sent = captured_dispatch["last"]
    assert "--verbose" not in sent["argv"]
    assert sent["argv"][-1] == "--text=x"


def test_dispatch_boolean_false_omitted(
    temp_skill_dir: Path,
    captured_dispatch: Dict[str, Any],
    cleanup_runner_tools: List[str],
) -> None:
    names = register_runners_for_skill(
        bundle_id="demo", skill_name="demo-skill", skill_dir=temp_skill_dir
    )
    cleanup_runner_tools.extend(names)
    tool = tool_registry.get(names[0])
    tool.func({"text": "x", "verbose": False})

    sent = captured_dispatch["last"]
    assert "--verbose" not in sent["argv"]


def test_register_uses_separate_bundle_id_for_staging(
    temp_skill_dir: Path,
    captured_dispatch: Dict[str, Any],
    cleanup_runner_tools: List[str],
) -> None:
    names = register_runners_for_skill(
        bundle_id="demo",
        skill_name="demo-skill",
        skill_dir=temp_skill_dir,
        bundle_id_for_staging="demo-staging-slug",
    )
    cleanup_runner_tools.extend(names)
    tool_registry.get(names[0]).func({"text": "x"})

    sent = captured_dispatch["last"]
    assert sent["bundle_id"] == "demo-staging-slug"


def test_register_is_idempotent_overwrites_existing(
    temp_skill_dir: Path, cleanup_runner_tools: List[str]
) -> None:
    first = register_runners_for_skill(
        bundle_id="demo", skill_name="demo-skill", skill_dir=temp_skill_dir
    )
    cleanup_runner_tools.extend(first)
    second = register_runners_for_skill(
        bundle_id="demo", skill_name="demo-skill", skill_dir=temp_skill_dir
    )
    assert first == second
    assert tool_registry.get(first[0]) is not None


def test_description_contains_runner_metadata(
    temp_skill_dir: Path, cleanup_runner_tools: List[str]
) -> None:
    names = register_runners_for_skill(
        bundle_id="demo", skill_name="demo-skill", skill_dir=temp_skill_dir
    )
    cleanup_runner_tools.extend(names)
    tool = tool_registry.get(names[0])
    assert "image_stack=python-minimal" in tool.description
    assert "bundle:demo skill:demo-skill runner:echo" in tool.description


# ---------------------------------------------------------------------------
# lifetime: workspace — routes through worker_exec.workspace_mode (ADR-0106 Slice A)
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace_skill_dir(tmp_path: Path) -> Path:
    skill = tmp_path / "skills" / "workspace-skill"
    (skill / "scripts").mkdir(parents=True)
    (skill / "scripts" / "do.py").write_text("print('hi')\n", encoding="utf-8")
    (skill / "SKILL.md").write_text(
        "---\nname: workspace-skill\ndescription: w.\n---\nbody\n", encoding="utf-8"
    )
    (skill / "runners.yaml").write_text(
        "runners:\n"
        "  - name: do\n"
        "    description: workspace runner\n"
        "    script: scripts/do.py\n"
        "    interpreter: python3\n"
        "    image_stack: python-office\n"
        "    lifetime: workspace\n",
        encoding="utf-8",
    )
    return skill


def test_workspace_runner_dispatches_with_workspace_mode_workspace(
    workspace_skill_dir: Path,
    captured_dispatch: Dict[str, Any],
    cleanup_runner_tools: List[str],
) -> None:
    """``lifetime: workspace`` runners must set worker_exec workspace fields.

    This is the contract that ties ADR-0101 runners to the
    WorkspaceContainerManager: the runner author writes
    ``lifetime: workspace`` and Motet routes the call through the existing
    persistent-workspace pipeline without any author-side wiring.
    """
    names = register_runners_for_skill(
        bundle_id="demo", skill_name="workspace-skill", skill_dir=workspace_skill_dir
    )
    cleanup_runner_tools.extend(names)
    tool_registry.get(names[0]).func({})
    sent = captured_dispatch["last"]
    assert sent["workspace_mode"] == "workspace"
    assert sent["workspace_image_stack"] == "python-office"
    assert sent["workspace_bundle_id"] == "demo"
    assert sent["workspace_skill_name"] == "workspace-skill"
    assert sent["argv"] == ["python3", "skills/workspace-skill/scripts/do.py"]
    assert len(sent["workspace_materialized_files"]) == 1
    assert (
        sent["workspace_materialized_files"][0]["path"]
        == "/scratch/skills/workspace-skill/scripts/do.py"
    )
    assert (
        base64.b64decode(sent["workspace_materialized_files"][0]["content_b64"])
        == b"print('hi')\n"
    )


# ---------------------------------------------------------------------------
# lifetime: stateful — routes through run_stateful_in_workspace, NOT worker_exec
# ---------------------------------------------------------------------------


@pytest.fixture
def stateful_skill_dir(tmp_path: Path) -> Path:
    skill = tmp_path / "skills" / "stateful-skill"
    (skill / "scripts").mkdir(parents=True)
    (skill / "scripts" / "warmer.py").write_text(
        "def handle(p):\n    return {'echo': p}\n", encoding="utf-8"
    )
    (skill / "SKILL.md").write_text(
        "---\nname: stateful-skill\ndescription: s.\n---\nbody\n", encoding="utf-8"
    )
    (skill / "runners.yaml").write_text(
        "runners:\n"
        "  - name: tick\n"
        "    description: stateful runner\n"
        "    script: scripts/warmer.py\n"
        "    interpreter: python3\n"
        "    image_stack: python-minimal\n"
        "    lifetime: stateful\n"
        "    timeout_seconds: 30\n"
        "    args:\n"
        "      label:\n"
        "        type: string\n"
        "        description: any label\n",
        encoding="utf-8",
    )
    return skill


def test_stateful_runner_routes_through_run_stateful_in_workspace(
    stateful_skill_dir: Path,
    cleanup_runner_tools: List[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stateful runners must call ``run_stateful_in_workspace`` with the staged source.

    Critically: the dispatcher must NOT call ``core.worker_exec.run`` —
    stateful dispatch bypasses argv/staging entirely.
    """
    captured_stateful: Dict[str, Any] = {}
    captured_worker_exec: Dict[str, Any] = {}

    def fake_stateful(**kwargs):
        captured_stateful.update(kwargs)
        return {
            "ok": True,
            "result": {"echo": kwargs["params"]},
            "stdout": "",
            "stderr": "",
            "workspace_mode": "stateful",
            "container_id": "stateful-cid",
        }

    def fake_worker_exec(params: Dict[str, Any]) -> Dict[str, Any]:
        captured_worker_exec["called"] = True
        return {"returncode": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(
        "motet.core.execution.run_stateful_in_workspace", fake_stateful
    )
    monkeypatch.setattr(
        "motet.core.tools.builtin.worker_exec.run", fake_worker_exec
    )

    names = register_runners_for_skill(
        bundle_id="demo", skill_name="stateful-skill", skill_dir=stateful_skill_dir
    )
    cleanup_runner_tools.extend(names)
    tool = tool_registry.get(names[0])

    result = tool.func({"label": "first"})

    assert "called" not in captured_worker_exec, (
        "worker_exec must not be called for stateful runners"
    )
    assert captured_stateful["image_stack"] == "python-minimal"
    assert captured_stateful["bundle_id"] == "demo"
    assert captured_stateful["skill_name"] == "stateful-skill"
    assert captured_stateful["script_logical_name"] == "warmer.py"
    # Source should be the file's actual bytes (it's read fresh on every dispatch)
    assert b"def handle" in captured_stateful["script_source"]
    assert captured_stateful["params"] == {"label": "first"}
    assert captured_stateful["timeout_seconds"] == 30

    # Envelope passes through with runner metadata layered on top.
    assert result["ok"] is True
    assert result["workspace_mode"] == "stateful"
    assert result["runner"] == "tick"
    assert result["runner_lifetime"] == "stateful"
    assert result["bundle_id"] == "demo"
    assert result["skill_name"] == "stateful-skill"


def test_stateful_runner_returns_transport_error_envelope_when_script_unreadable(
    stateful_skill_dir: Path,
    cleanup_runner_tools: List[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the staged script disappears between deploy and dispatch, the
    runner must return a structured ``ok=False`` envelope rather than
    falling back to per-call (which would silently lose stateful semantics).
    """

    def boom(**kwargs):  # pragma: no cover -- should not be reached
        raise AssertionError("run_stateful_in_workspace must not be called when read fails")

    monkeypatch.setattr("motet.core.execution.run_stateful_in_workspace", boom)

    names = register_runners_for_skill(
        bundle_id="demo", skill_name="stateful-skill", skill_dir=stateful_skill_dir
    )
    cleanup_runner_tools.extend(names)

    # Remove the script after registration to simulate the staged file
    # being yanked out from under us (e.g. bundle dir wiped between
    # registration and dispatch).
    (stateful_skill_dir / "scripts" / "warmer.py").unlink()

    tool = tool_registry.get(names[0])
    result = tool.func({"label": "x"})
    assert result["ok"] is False
    assert result["transport_error"] is True
    assert "warmer.py" in result["error"]
    assert result["runner"] == "tick"
    assert result["runner_lifetime"] == "stateful"
