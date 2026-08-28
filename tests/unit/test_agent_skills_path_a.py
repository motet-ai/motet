"""
Unit tests for ADR-0073 Path A: bundle skills, catalog, registry, assembly, LLMRequest.skill_refs.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

SKILL_MD = """---
name: demo-skill
description: Use this skill when the user asks about bananas or tropical fruit.
---

## Steps
Always mention potassium briefly.
"""

# Slug must be unique vs other tests' global registry entries (discovery lists all skills).
SKILL_MD_DISCOVER_NO_ALLOWLIST = """---
name: discover-no-allowlist-skill
description: Use this skill when the user asks about bananas or tropical fruit.
---

## Steps
Always mention potassium briefly.
"""


def test_parse_skill_markdown_text_roundtrip() -> None:
    from motet.core.skills.parser import parse_skill_markdown_text

    doc = parse_skill_markdown_text(SKILL_MD, source_hint="inline")
    assert doc.name == "demo-skill"
    assert "bananas" in doc.description.lower()
    assert "potassium" in doc.body.lower()


def test_parse_skill_markdown_text_rejects_invalid_name_slug() -> None:
    from motet.core.skills.parser import parse_skill_markdown_text

    bad = """---
name: Demo Skill
description: Valid description text.
---

# Skill
"""
    with pytest.raises(ValueError, match="field 'name' must use lowercase letters"):
        parse_skill_markdown_text(bad, source_hint="inline")


def test_parse_skill_markdown_text_rejects_non_string_metadata() -> None:
    from motet.core.skills.parser import parse_skill_markdown_text

    bad = """---
name: demo-skill
description: Valid description text.
metadata:
  team: core
  retries: 3
---

# Skill
"""
    with pytest.raises(ValueError, match="field 'metadata' must be string-to-string"):
        parse_skill_markdown_text(bad, source_hint="inline")


def test_collect_lint_errors_rejects_skill_name_dir_mismatch() -> None:
    from motet.core.bundles.deploy import _collect_lint_errors

    files = {
        "skills/not-demo/SKILL.md": SKILL_MD.encode("utf-8"),
    }
    errors = _collect_lint_errors(files)
    assert any("must match its parent directory" in e.message for e in errors)


def test_collect_lint_errors_rejects_description_too_long() -> None:
    from motet.core.bundles.deploy import _collect_lint_errors

    long_description = "x" * 1025
    skill_md = f"""---
name: demo-skill
description: {long_description}
---

# Demo
"""
    files = {
        "skills/demo-skill/SKILL.md": skill_md.encode("utf-8"),
    }
    errors = _collect_lint_errors(files)
    assert any("field 'description' exceeds 1024 characters" in e.message for e in errors)


def test_build_skill_catalog_for_turn_discloses_metadata_without_body() -> None:
    from motet.core.skills.registry import RegisteredSkill, get_skill_registry
    from motet.core.skills.assembly import build_skill_catalog_for_turn

    reg = get_skill_registry()
    reg.unregister_bundle("test-bundle")
    with tempfile.TemporaryDirectory() as tmp:
        md = Path(tmp) / "SKILL.md"
        md.write_text(SKILL_MD, encoding="utf-8")
        reg.register(
            RegisteredSkill(
                skill_id="test-bundle.demo-skill",
                bundle_id="test-bundle",
                name="demo-skill",
                description="Use this skill when the user asks about bananas or tropical fruit.",
                skill_md_path=md,
                source="bundle",
                bundle_version="abc123",
            )
        )
        msgs, refs, candidates = build_skill_catalog_for_turn(
            ["test-bundle.demo-skill"],
        )
        assert len(msgs) == 1
        assert msgs[0].role == "system"
        assert "demo-skill" in msgs[0].content
        assert "bananas" in msgs[0].content.lower()
        assert "potassium" not in msgs[0].content.lower()
        assert msgs[0].metadata.get("content_kind") == "agent_skill_catalog"
        assert len(refs) == 1
        assert refs[0].skill_id == "test-bundle.demo-skill"
        assert refs[0].bundle_id == "test-bundle"
        assert refs[0].content_fingerprint is None
        assert [rec.skill_id for rec in candidates] == ["test-bundle.demo-skill"]
    reg.unregister_bundle("test-bundle")


def test_assemble_skills_only_activates_direct_skill_mentions() -> None:
    from motet.core.skills.registry import RegisteredSkill, get_skill_registry
    from motet.core.skills.assembly import assemble_skills_for_turn

    reg = get_skill_registry()
    bundle_id = "rank-bundle"
    reg.unregister_bundle(bundle_id)
    skills = {
        "algorithmic-art": "Use this skill when creating algorithmic art.",
        "brand-guidelines": "Use this skill when applying brand guidelines.",
        "canvas-design": "Use this skill when creating static visual designs.",
        "pdf": "Use this skill whenever the user wants to fill PDF forms or inspect PDF files.",
    }
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for name, description in skills.items():
            md = root / name / "SKILL.md"
            md.parent.mkdir()
            md.write_text(
                f"---\nname: {name}\ndescription: {description}\n---\n\n## Steps\nFollow the {name} workflow.\n",
                encoding="utf-8",
            )
            reg.register(
                RegisteredSkill(
                    skill_id=f"{bundle_id}.{name}",
                    bundle_id=bundle_id,
                    name=name,
                    description=description,
                    skill_md_path=md,
                    source="bundle",
                    bundle_version="rank-sha",
                )
            )

        generic_msgs, generic_refs = assemble_skills_for_turn(
            "Inspect this PDF form and fill it out.",
            [f"{bundle_id}.{name}" for name in skills],
            max_skills=3,
        )
        assert generic_msgs == []
        assert generic_refs == []

        msgs, refs = assemble_skills_for_turn(
            (
                "Use the PDF skill to inspect this PDF form and fill it out. "
                "First check whether it has fillable form fields."
            ),
            [f"{bundle_id}.{name}" for name in skills],
            max_skills=3,
        )

        assert [ref.skill_id for ref in refs] == [f"{bundle_id}.pdf"]
        assert len(msgs) == 1
        assert msgs[0].metadata.get("skill_id") == f"{bundle_id}.pdf"
        assert "pdf workflow" in msgs[0].content.lower()
    reg.unregister_bundle(bundle_id)


def test_assemble_skills_no_allowlist() -> None:
    from motet.core.skills.assembly import assemble_skills_for_turn

    msgs, refs = assemble_skills_for_turn("bananas", None)
    assert msgs == [] and refs == []


def test_assemble_skills_discovery_mode_without_allowlist() -> None:
    from motet.core.skills.assembly import build_skill_catalog_for_turn
    from motet.core.skills.registry import RegisteredSkill, get_skill_registry

    reg = get_skill_registry()
    reg.unregister_bundle("discover-bundle")
    with tempfile.TemporaryDirectory() as tmp:
        md = Path(tmp) / "SKILL.md"
        md.write_text(SKILL_MD_DISCOVER_NO_ALLOWLIST, encoding="utf-8")
        reg.register(
            RegisteredSkill(
                skill_id="discover-bundle.discover-no-allowlist-skill",
                bundle_id="discover-bundle",
                name="discover-no-allowlist-skill",
                description="Use this skill when the user asks about bananas or tropical fruit.",
                skill_md_path=md,
                source="bundle",
                bundle_version="sha-discover",
            )
        )
        msgs, refs, candidates = build_skill_catalog_for_turn(
            None,
            discovery_mode=True,
        )
        our_skill_id = "discover-bundle.discover-no-allowlist-skill"
        assert len(msgs) == 1
        ref_by_id = {r.skill_id: r for r in refs}
        assert our_skill_id in ref_by_id
        assert ref_by_id[our_skill_id].bundle_id == "discover-bundle"
        assert ref_by_id[our_skill_id].bundle_version == "sha-discover"
        assert our_skill_id in msgs[0].content
        candidate_ids = [rec.skill_id for rec in candidates]
        assert our_skill_id in candidate_ids
        # Discovery lists every registered skill; the full suite may preload other bundles.
        assert len(refs) >= 1
        assert len(candidates) >= 1
    reg.unregister_bundle("discover-bundle")


def test_activate_skill_tool_returns_content_and_resources() -> None:
    from motet.core.skills.registry import RegisteredSkill, get_skill_registry
    from motet.core.tools import registry as tool_registry
    from motet.core.tools.builtin.activate_skill import run

    reg = get_skill_registry()
    bundle_id = "activate-bundle"
    runner_tool_name = f"{bundle_id}.pdf.check_fillable_fields"
    reg.unregister_bundle(bundle_id)
    tool_registry.unregister(runner_tool_name)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "pdf"
        root.mkdir()
        md = root / "SKILL.md"
        md.write_text(
            "---\n"
            "name: pdf\n"
            "description: Use this skill for PDFs.\n"
            "allowed-tools: core.workspace_shell_exec\n"
            "---\n\n"
            "## Steps\nUse the PDF workflow.\n",
            encoding="utf-8",
        )
        (root / "scripts").mkdir()
        (root / "scripts" / "inspect.py").write_text("print('ok')\n", encoding="utf-8")
        (root / "scripts" / "__pycache__").mkdir()
        (root / "scripts" / "__pycache__" / "inspect.cpython-312.pyc").write_bytes(b"cache")
        (root / "references").mkdir()
        (root / "references" / "fields.md").write_text("# Fields\n", encoding="utf-8")
        (root / "assets").mkdir()
        (root / "assets" / "sample.txt").write_text("sample\n", encoding="utf-8")
        (root / "scripts.yaml").write_text(
            """
scripts:
  - name: inspect
    path: scripts/inspect.py
    description: Inspect a PDF for fillable fields.
    command: python /scratch/skills/pdf/scripts/inspect.py /scratch/input.pdf /scratch/fields.json
    inputs:
      - name: input_pdf
        type: artifact
        content_types:
          - application/pdf
        recommended_path: /scratch/input.pdf
    outputs:
      - name: field_info
        type: artifact
        content_types:
          - application/json
        recommended_path: /scratch/fields.json
""".lstrip(),
            encoding="utf-8",
        )
        reg.register(
            RegisteredSkill(
                skill_id=f"{bundle_id}.pdf",
                bundle_id=bundle_id,
                name="pdf",
                description="Use this skill for PDFs.",
                skill_md_path=md,
                source="bundle",
                bundle_version="activate-sha",
            )
        )
        tool_registry.register(
            runner_tool_name,
            description="Check whether a PDF has fillable fields.",
            func=lambda _params: {"status": "success", "result": "ok"},
            category="shell",
        )

        result = run({"name": "pdf"})
        assert result["status"] == "success"
        payload = result["result"]
        assert payload["skill_id"] == f"{bundle_id}.pdf"
        assert "Use the PDF workflow" in payload["content"]
        assert set(payload["resources"]) == {
            "assets/sample.txt",
            "references/fields.md",
            "scripts/inspect.py",
            "scripts.yaml",
        }
        assert not any("__pycache__" in resource for resource in payload["resources"])
        assert payload["resource_groups"]["scripts"] == ["scripts/inspect.py"]
        assert payload["resource_groups"]["references"] == ["references/fields.md"]
        assert payload["resource_groups"]["assets"] == ["assets/sample.txt"]
        assert payload["allowed_tools"] == "core.workspace_shell_exec"
        assert "does not grant tool access" in payload["allowed_tools_guidance"]
        assert payload["script_usage"] == [
            {
                "name": "inspect",
                "path": "scripts/inspect.py",
                "description": "Inspect a PDF for fillable fields.",
                "command": "python /scratch/skills/pdf/scripts/inspect.py /scratch/input.pdf /scratch/fields.json",
                "inputs": [
                    {
                        "name": "input_pdf",
                        "type": "artifact",
                        "content_types": ["application/pdf"],
                        "recommended_path": "/scratch/input.pdf",
                    }
                ],
                "outputs": [
                    {
                        "name": "field_info",
                        "type": "artifact",
                        "content_types": ["application/json"],
                        "recommended_path": "/scratch/fields.json",
                    }
                ],
            }
        ]
        assert "<script_usage>" in payload["content"]
        assert payload["execution"]["tool"] == "core.workspace_shell_exec"
        assert payload["execution"]["skill_directory"] == "/scratch/skills/pdf"
        assert payload["execution"]["result_contract"]["process_status"] == "succeeded | failed | timed_out"
        assert payload["execution"]["fail_fast_shell_prelude"] == "set -euo pipefail"
        assert "prefer output_paths" in payload["execution"]["guidance"].lower()
        assert "set -euo pipefail" in payload["execution"]["guidance"]
        assert "set -euo pipefail" in payload["content"]
        assert payload["tools"] == [
            {
                "name": runner_tool_name,
                "description": "Check whether a PDF has fillable fields.",
                "category": "shell",
            }
        ]
        assert runner_tool_name in payload["content"]

        missing = run({"name": "missing"})
        assert missing["status"] == "error"
    reg.unregister_bundle(bundle_id)
    tool_registry.unregister(runner_tool_name)


def test_collect_lint_errors_warns_on_skill_portability_issues() -> None:
    from motet.core.bundles.deploy import _collect_lint_errors

    files = {
        "skills/pdf/SKILL.md": (
            "---\n"
            "name: pdf\n"
            "description: Use this skill for PDFs.\n"
            "allowed-tools: core.workspace_shell_exec\n"
            "---\n\n"
            "Read [missing reference](references/missing.md).\n"
            "Run scripts/missing.py, not /Users/example/local.py.\n"
        ).encode("utf-8"),
        "skills/pdf/scripts/inspect.py": b"import pypdf\nprint('ok')\n",
    }

    errors = _collect_lint_errors(files)
    messages = [err.message for err in errors]

    assert any("allowed-tools" in message and "guidance only" in message for message in messages)
    assert any("host-style absolute path" in message for message in messages)
    assert any("Markdown link target" in message and "not found" in message for message in messages)
    assert any("references script 'scripts/missing.py'" in message for message in messages)
    assert any("possible third-party modules ['pypdf']" in message for message in messages)
    assert all(err.severity == "warning" for err in errors)


def test_collect_lint_errors_validates_script_usage_paths() -> None:
    from motet.core.bundles.deploy import _collect_lint_errors

    files = {
        "skills/pdf/SKILL.md": (
            "---\nname: pdf\ndescription: Use this skill for PDFs.\n---\n\n"
        ).encode("utf-8"),
        "skills/pdf/scripts.yaml": (
            "scripts:\n"
            "  - name: missing\n"
            "    path: scripts/missing.py\n"
            "    description: Missing script.\n"
            "    command: python /scratch/skills/pdf/scripts/missing.py\n"
        ).encode("utf-8"),
    }

    errors = _collect_lint_errors(files)
    assert any("script usage entry 'missing' path" in err.message for err in errors)
    assert any(err.severity == "error" for err in errors)


def test_collect_lint_errors_validates_runtime_capabilities() -> None:
    from motet.core.bundles.deploy import _collect_lint_errors

    files = {
        "config/exec.yaml": (
            "runtime_capabilities:\n"
            "  - libreoffice\n"
            "bootstrap_command: apt-get update\n"
        ).encode("utf-8"),
    }

    errors = _collect_lint_errors(files)
    messages = [err.message for err in errors]

    assert any("No pinned image stack satisfies runtime_capabilities" in message for message in messages)
    assert any("bootstrap_command is dev-only" in message for message in messages)


def test_activate_skill_registered_as_builtin_tool() -> None:
    from motet.core.tools import registry

    assert registry.get("core.activate_skill") is not None
    assert registry.get("core.workspace_shell_exec") is not None


def test_workspace_shell_exec_resolves_image_stack_from_runtime_capabilities(monkeypatch) -> None:
    from motet.core.skills.registry import RegisteredSkill, get_skill_registry
    from motet.core.tools.builtin import workspace_shell_exec

    class _Store:
        pass

    class _Motet:
        tenant_id = "tenant-a"
        principal_id = "user-a"
        motet_id = "motet-a"
        conversation_id = "conv-a"
        command_id = "cmd-a"
        metadata = {"skill_refs": [{"skill_id": "cap-bundle.pdf"}]}
        artifact_store = _Store()

    reg = get_skill_registry()
    bundle_id = "cap-bundle"
    reg.unregister_bundle(bundle_id)
    monkeypatch.setenv(
        "MOTET_IMAGE_STACK_PDF_TOOLS",
        "registry.example.com/motet/pdf-tools@sha256:" + "a" * 64,
    )
    monkeypatch.setenv("MOTET_IMAGE_STACK_PDF_TOOLS_CAPABILITIES", "python,pdf,poppler")
    with tempfile.TemporaryDirectory() as tmp:
        bundle_root = Path(tmp)
        root = bundle_root / "skills" / "pdf"
        (root / "scripts").mkdir(parents=True)
        (bundle_root / "config").mkdir()
        (bundle_root / "config" / "exec.yaml").write_text(
            "runtime_capabilities:\n  - python\n  - poppler\n",
            encoding="utf-8",
        )
        md = root / "SKILL.md"
        md.write_text(
            "---\nname: pdf\ndescription: Use this skill for PDFs.\n---\n\nRun scripts.\n",
            encoding="utf-8",
        )
        (root / "scripts" / "inspect.py").write_text("print('ok')\n", encoding="utf-8")
        reg.register(
            RegisteredSkill(
                skill_id=f"{bundle_id}.pdf",
                bundle_id=bundle_id,
                name="pdf",
                description="Use this skill for PDFs.",
                skill_md_path=md,
                source="bundle",
            )
        )

        captured = {}

        def fake_worker_exec(params):  # noqa: ANN001
            captured.update(params)
            return {"returncode": 0, "stdout": "ok\n", "stderr": "", "timed_out": False}

        monkeypatch.setattr(workspace_shell_exec, "_get_motet_context_optional", lambda: _Motet())
        monkeypatch.setattr("motet.core.tools.builtin.worker_exec.run", fake_worker_exec)

        result = workspace_shell_exec.run({"command": "python3 /scratch/skills/pdf/scripts/inspect.py"})

        assert result["status"] == "success"
        assert captured["workspace_image_stack"] == "pdf-tools"
        assert result["result"]["runtime_resolution"]["source"] == "config/exec.yaml:runtime_capabilities"
        assert result["result"]["runtime_resolution"]["image_stack"] == "pdf-tools"
    reg.unregister_bundle(bundle_id)


def test_workspace_shell_exec_fails_when_runtime_capabilities_unresolved(monkeypatch) -> None:
    from motet.core.skills.registry import RegisteredSkill, get_skill_registry
    from motet.core.tools.builtin import workspace_shell_exec

    class _Store:
        pass

    class _Motet:
        tenant_id = "tenant-a"
        principal_id = "user-a"
        motet_id = "motet-a"
        conversation_id = "conv-a"
        command_id = "cmd-a"
        metadata = {"skill_refs": [{"skill_id": "missing-cap-bundle.pdf"}]}
        artifact_store = _Store()

    reg = get_skill_registry()
    bundle_id = "missing-cap-bundle"
    reg.unregister_bundle(bundle_id)
    with tempfile.TemporaryDirectory() as tmp:
        bundle_root = Path(tmp)
        root = bundle_root / "skills" / "pdf"
        root.mkdir(parents=True)
        (bundle_root / "config").mkdir()
        (bundle_root / "config" / "exec.yaml").write_text(
            "runtime_capabilities:\n  - libreoffice\n",
            encoding="utf-8",
        )
        md = root / "SKILL.md"
        md.write_text(
            "---\nname: pdf\ndescription: Use this skill for PDFs.\n---\n\nRun scripts.\n",
            encoding="utf-8",
        )
        reg.register(
            RegisteredSkill(
                skill_id=f"{bundle_id}.pdf",
                bundle_id=bundle_id,
                name="pdf",
                description="Use this skill for PDFs.",
                skill_md_path=md,
                source="bundle",
            )
        )
        monkeypatch.setattr(workspace_shell_exec, "_get_motet_context_optional", lambda: _Motet())

        result = workspace_shell_exec.run({"command": "true"})

        assert result["status"] == "error"
        assert "No pinned image stack satisfies runtime_capabilities" in result["error"]
    reg.unregister_bundle(bundle_id)


def test_workspace_shell_exec_runs_dev_bootstrap_when_enabled(monkeypatch) -> None:
    from motet.core.skills.registry import RegisteredSkill, get_skill_registry
    from motet.core.tools.builtin import workspace_shell_exec

    class _Store:
        pass

    class _Motet:
        tenant_id = "tenant-a"
        principal_id = "user-a"
        motet_id = "motet-a"
        conversation_id = "conv-a"
        command_id = "cmd-a"
        metadata = {"skill_refs": [{"skill_id": "bootstrap-bundle.pdf"}]}
        artifact_store = _Store()

    reg = get_skill_registry()
    bundle_id = "bootstrap-bundle"
    reg.unregister_bundle(bundle_id)
    monkeypatch.setenv("MOTET_WORKSPACE_SHELL_BOOTSTRAP_ENABLED", "true")
    with tempfile.TemporaryDirectory() as tmp:
        bundle_root = Path(tmp)
        root = bundle_root / "skills" / "pdf"
        root.mkdir(parents=True)
        (bundle_root / "config").mkdir()
        (bundle_root / "config" / "exec.yaml").write_text(
            "base_image_stack: python-minimal\nbootstrap_command: echo bootstrap-ok\n",
            encoding="utf-8",
        )
        md = root / "SKILL.md"
        md.write_text(
            "---\nname: pdf\ndescription: Use this skill for PDFs.\n---\n\nRun scripts.\n",
            encoding="utf-8",
        )
        reg.register(
            RegisteredSkill(
                skill_id=f"{bundle_id}.pdf",
                bundle_id=bundle_id,
                name="pdf",
                description="Use this skill for PDFs.",
                skill_md_path=md,
                source="bundle",
            )
        )

        captured = {}

        def fake_worker_exec(params):  # noqa: ANN001
            captured.update(params)
            return {"returncode": 0, "stdout": "ok\n", "stderr": "", "timed_out": False}

        monkeypatch.setattr(workspace_shell_exec, "_get_motet_context_optional", lambda: _Motet())
        monkeypatch.setattr("motet.core.tools.builtin.worker_exec.run", fake_worker_exec)

        result = workspace_shell_exec.run({"command": "echo done"})

        assert result["status"] == "success"
        shell_command = captured["argv"][2]
        assert "running dev-only bootstrap_command" in shell_command
        assert "( echo bootstrap-ok )" in shell_command
        assert result["result"]["bootstrap"]["enabled"] is True
    reg.unregister_bundle(bundle_id)


def test_workspace_shell_exec_materializes_skill_files_and_artifacts(monkeypatch) -> None:
    from motet.core.skills.registry import RegisteredSkill, get_skill_registry
    from motet.core.tools.builtin import workspace_shell_exec

    class _Meta:
        content_type = "application/pdf"

    class _Store:
        def get_metadata(self, artifact_id: str):  # noqa: ANN001
            assert artifact_id == "art_pdf"
            return _Meta()

        def get(self, artifact_id: str):  # noqa: ANN001
            assert artifact_id == "art_pdf"
            return b"%PDF-1.7"

    class _Motet:
        tenant_id = "tenant-a"
        principal_id = "user-a"
        motet_id = "motet-a"
        conversation_id = "conv-a"
        command_id = "cmd-a"
        metadata = {"skill_refs": [{"skill_id": "shell-bundle.pdf"}]}
        artifact_store = _Store()

    reg = get_skill_registry()
    bundle_id = "shell-bundle"
    reg.unregister_bundle(bundle_id)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "pdf"
        (root / "scripts").mkdir(parents=True)
        md = root / "SKILL.md"
        md.write_text(
            "---\nname: pdf\ndescription: Use this skill for PDFs.\n---\n\nRun scripts.\n",
            encoding="utf-8",
        )
        (root / "scripts" / "inspect.py").write_text("print('ok')\n", encoding="utf-8")
        reg.register(
            RegisteredSkill(
                skill_id=f"{bundle_id}.pdf",
                bundle_id=bundle_id,
                name="pdf",
                description="Use this skill for PDFs.",
                skill_md_path=md,
                source="bundle",
            )
        )

        captured = {}

        def fake_worker_exec(params):  # noqa: ANN001
            captured.update(params)
            return {"returncode": 0, "stdout": "ok\n", "stderr": "", "timed_out": False}

        monkeypatch.setattr(workspace_shell_exec, "_get_motet_context_optional", lambda: _Motet())
        monkeypatch.setattr("motet.core.tools.builtin.worker_exec.run", fake_worker_exec)

        result = workspace_shell_exec.run(
            {
                "command": "python3 skills/pdf/scripts/inspect.py /scratch/form.pdf",
                "input_artifacts": [
                    {"artifact_id": "art_pdf", "filename": "form.pdf"}
                ],
            }
        )

        assert result["status"] == "success"
        payload = result["result"]
        assert payload["skill_id"] == "shell-bundle.pdf"
        assert payload["process_status"] == "succeeded"
        assert payload["skill_directory"] == "/scratch/skills/pdf"
        assert payload["materialized_inputs"][0]["artifact_id"] == "art_pdf"
        assert captured["argv"] == [
            "bash",
            "-lc",
            "python3 skills/pdf/scripts/inspect.py /scratch/form.pdf",
        ]
        assert captured["workspace_bundle_id"] == "shell-bundle"
        assert captured["workspace_skill_name"] == "pdf"
        staged_paths = {item["path"] for item in captured["workspace_materialized_files"]}
        assert "/scratch/skills/pdf/scripts/inspect.py" in staged_paths
        assert "/scratch/form.pdf" in staged_paths
    reg.unregister_bundle(bundle_id)


def test_workspace_shell_exec_installs_bundle_requirements(monkeypatch) -> None:
    from motet.core.skills.registry import RegisteredSkill, get_skill_registry
    from motet.core.tools.builtin import workspace_shell_exec

    class _Store:
        pass

    class _Motet:
        tenant_id = "tenant-a"
        principal_id = "user-a"
        motet_id = "motet-a"
        conversation_id = "conv-a"
        command_id = "cmd-a"
        metadata = {"skill_refs": [{"skill_id": "requirements-bundle.pdf"}]}
        artifact_store = _Store()

    reg = get_skill_registry()
    bundle_id = "requirements-bundle"
    reg.unregister_bundle(bundle_id)
    with tempfile.TemporaryDirectory() as tmp:
        bundle_root = Path(tmp)
        root = bundle_root / "skills" / "pdf"
        (root / "scripts").mkdir(parents=True)
        (bundle_root / "config").mkdir()
        (bundle_root / "exec").mkdir()
        (bundle_root / "config" / "exec.yaml").write_text(
            "base_image_stack: python-minimal\nrequirements_path: exec/requirements.txt\n",
            encoding="utf-8",
        )
        (bundle_root / "exec" / "requirements.txt").write_text("pypdf>=4\n", encoding="utf-8")
        md = root / "SKILL.md"
        md.write_text(
            "---\nname: pdf\ndescription: Use this skill for PDFs.\n---\n\nRun scripts.\n",
            encoding="utf-8",
        )
        (root / "scripts" / "inspect.py").write_text("print('ok')\n", encoding="utf-8")
        reg.register(
            RegisteredSkill(
                skill_id=f"{bundle_id}.pdf",
                bundle_id=bundle_id,
                name="pdf",
                description="Use this skill for PDFs.",
                skill_md_path=md,
                source="bundle",
            )
        )

        captured = {}

        def fake_worker_exec(params):  # noqa: ANN001
            captured.update(params)
            return {"returncode": 0, "stdout": "ok\n", "stderr": "", "timed_out": False}

        monkeypatch.setattr(workspace_shell_exec, "_get_motet_context_optional", lambda: _Motet())
        monkeypatch.setattr("motet.core.tools.builtin.worker_exec.run", fake_worker_exec)

        result = workspace_shell_exec.run({"command": "python3 skills/pdf/scripts/inspect.py"})

        assert result["status"] == "success"
        assert result["result"]["requirements_path"] == "/scratch/exec/requirements.txt"
        assert result["result"]["setup"]["type"] == "python_requirements"
        assert result["result"]["setup"]["log_path"].startswith("/scratch/.motet/requirements-")
        shell_command = captured["argv"][2]
        assert "python3 -m pip install --disable-pip-version-check -r '/scratch/exec/requirements.txt'" in shell_command
        assert "> '/scratch/.motet/requirements-" in shell_command
        assert "pip log follows" in shell_command
        assert shell_command.endswith("&& python3 skills/pdf/scripts/inspect.py")
        staged_paths = {item["path"] for item in captured["workspace_materialized_files"]}
        assert "/scratch/exec/requirements.txt" in staged_paths
    reg.unregister_bundle(bundle_id)


def test_workspace_shell_exec_marks_failed_process_status(monkeypatch) -> None:
    from motet.core.skills.registry import RegisteredSkill, get_skill_registry
    from motet.core.tools.builtin import workspace_shell_exec

    class _Store:
        pass

    class _Motet:
        tenant_id = "tenant-a"
        principal_id = "user-a"
        motet_id = "motet-a"
        conversation_id = "conv-a"
        command_id = "cmd-a"
        metadata = {"skill_refs": [{"skill_id": "failed-shell-bundle.pdf"}]}
        artifact_store = _Store()

    reg = get_skill_registry()
    bundle_id = "failed-shell-bundle"
    reg.unregister_bundle(bundle_id)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "pdf"
        root.mkdir(parents=True)
        md = root / "SKILL.md"
        md.write_text(
            "---\nname: pdf\ndescription: Use this skill for PDFs.\n---\n\nRun scripts.\n",
            encoding="utf-8",
        )
        reg.register(
            RegisteredSkill(
                skill_id=f"{bundle_id}.pdf",
                bundle_id=bundle_id,
                name="pdf",
                description="Use this skill for PDFs.",
                skill_md_path=md,
                source="bundle",
            )
        )

        def fake_worker_exec(_params):  # noqa: ANN001
            return {"returncode": 2, "stdout": "", "stderr": "Usage: inspect.py input\n", "timed_out": False}

        monkeypatch.setattr(workspace_shell_exec, "_get_motet_context_optional", lambda: _Motet())
        monkeypatch.setattr("motet.core.tools.builtin.worker_exec.run", fake_worker_exec)

        result = workspace_shell_exec.run({"command": "python3 skills/pdf/scripts/inspect.py"})

        assert result["status"] == "success"
        assert result["result"]["process_status"] == "failed"
        assert result["result"]["returncode"] == 2
    reg.unregister_bundle(bundle_id)


def test_workspace_shell_exec_output_artifacts_include_text_preview(monkeypatch) -> None:
    from motet.core.skills.registry import RegisteredSkill
    from motet.core.tools.builtin import workspace_shell_exec

    class _Store:
        def put(self, **kwargs):  # noqa: ANN003
            self.kwargs = kwargs
            return "art_output"

    class _Motet:
        tenant_id = "tenant-a"
        conversation_id = "conv-a"
        command_id = "cmd-a"

    rec = RegisteredSkill(
        skill_id="preview-bundle.pdf",
        bundle_id="preview-bundle",
        name="pdf",
        description="Use this skill for PDFs.",
        skill_md_path=Path("/tmp/preview/SKILL.md"),
        source="bundle",
    )

    monkeypatch.setattr(
        workspace_shell_exec,
        "_read_output_from_workspace",
        lambda **_kwargs: (b'{"fields": ["Name"]}\n', None),
    )

    output_artifacts, output_errors = workspace_shell_exec._capture_output_artifacts(
        artifact_store=_Store(),
        output_paths=["/scratch/field_info.json"],
        params_inputs=[],
        rec=rec,
        image_stack="python-minimal",
        motet=_Motet(),
    )

    assert output_errors == []
    assert output_artifacts == [
        {
            "artifact_id": "art_output",
            "path": "/scratch/field_info.json",
            "content_type": "application/json",
            "bytes": 21,
            "preview": '{"fields": ["Name"]}\n',
            "preview_truncated": False,
        }
    ]


def test_workspace_shell_exec_stateful_routes_through_warm_workspace(monkeypatch) -> None:
    from motet.core.skills.registry import RegisteredSkill, get_skill_registry
    from motet.core.tools.builtin import workspace_shell_exec

    class _Meta:
        content_type = "text/plain"

    class _Store:
        def get_metadata(self, artifact_id: str):  # noqa: ANN001
            assert artifact_id == "art_input"
            return _Meta()

        def get(self, artifact_id: str):  # noqa: ANN001
            assert artifact_id == "art_input"
            return "hello"

    class _Motet:
        tenant_id = "tenant-a"
        principal_id = "user-a"
        motet_id = "motet-a"
        conversation_id = "conv-a"
        command_id = "cmd-stateful"
        metadata = {"skill_refs": [{"skill_id": "stateful-shell-bundle.pdf"}]}
        artifact_store = _Store()

    reg = get_skill_registry()
    bundle_id = "stateful-shell-bundle"
    reg.unregister_bundle(bundle_id)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "pdf"
        (root / "scripts").mkdir(parents=True)
        md = root / "SKILL.md"
        md.write_text(
            "---\nname: pdf\ndescription: Use this skill for PDFs.\n---\n\nRun scripts.\n",
            encoding="utf-8",
        )
        (root / "scripts" / "inspect.py").write_text("print('ok')\n", encoding="utf-8")
        reg.register(
            RegisteredSkill(
                skill_id=f"{bundle_id}.pdf",
                bundle_id=bundle_id,
                name="pdf",
                description="Use this skill for PDFs.",
                skill_md_path=md,
                source="bundle",
            )
        )

        captured = {}

        def fake_stateful(**kwargs):  # noqa: ANN003
            captured.update(kwargs)
            return {
                "ok": True,
                "workspace_mode": "stateful",
                "container_id": "container-123",
                "result": {
                    "returncode": 0,
                    "stdout": "ok\n",
                    "stderr": "",
                    "timed_out": False,
                    "stateful_call_count": 1,
                },
            }

        def fake_worker_exec(_params):  # noqa: ANN001
            raise AssertionError("stateful workspace_shell_exec must not call worker_exec")

        monkeypatch.setattr(workspace_shell_exec, "_get_motet_context_optional", lambda: _Motet())
        monkeypatch.setattr(workspace_shell_exec, "run_stateful_in_workspace", fake_stateful)
        monkeypatch.setattr(workspace_shell_exec, "_resolve_workspace_image", lambda _stack: "python:3.11@sha256:test")
        monkeypatch.setattr("motet.core.tools.builtin.worker_exec.run", fake_worker_exec)

        result = workspace_shell_exec.run(
            {
                "command": "python3 skills/pdf/scripts/inspect.py /scratch/inputs/data.txt",
                "lifetime": "stateful",
                "input_artifacts": [
                    {"artifact_id": "art_input", "path": "/scratch/inputs/data.txt"}
                ],
                "timeout_seconds": 12,
            }
        )

        assert result["status"] == "success"
        payload = result["result"]
        assert payload["returncode"] == 0
        assert payload["workspace_mode"] == "stateful"
        assert payload["effective_lifetime"] == "stateful"
        assert payload["backend_ref"] == "container-123"
        assert captured["tenant_id"] == "tenant-a"
        assert captured["conversation_id"] == "conv-a"
        assert captured["bundle_id"] == bundle_id
        assert captured["skill_name"] == "pdf"
        assert captured["timeout_seconds"] == 12
        assert captured["request_id"] == "cmd-stateful"
        assert captured["oci_image_ref"] == "python:3.11@sha256:test"
        assert b"def handle(params):" in captured["script_source"]
        assert captured["script_logical_name"] == "motet_workspace_shell_dispatcher.py"
        params = captured["params"]
        assert params["command"].startswith("python3 skills/pdf/scripts/inspect.py")
        staged_paths = {item["path"] for item in params["workspace_materialized_files"]}
        assert "/scratch/skills/pdf/scripts/inspect.py" in staged_paths
        assert "/scratch/inputs/data.txt" in staged_paths
    reg.unregister_bundle(bundle_id)


def test_refresh_filesystem_skills_loads_project_skills() -> None:
    from motet.core.config import Config
    from motet.core.skills.assembly import build_skill_catalog_for_turn, find_skill_by_name_or_id
    from motet.core.skills.filesystem import refresh_filesystem_skills
    from motet.core.skills.registry import get_skill_registry

    reg = get_skill_registry()
    reg.unregister_source("project")
    reg.unregister_source("user")
    with tempfile.TemporaryDirectory() as tmp:
        project_root = Path(tmp) / "project"
        project_skill = project_root / ".agents" / "skills" / "pdf"
        project_skill.mkdir(parents=True)
        (project_skill / "SKILL.md").write_text(
            "---\nname: pdf\ndescription: Project PDF skill.\n---\n\nProject body.\n",
            encoding="utf-8",
        )

        registered = refresh_filesystem_skills(
            project_root=project_root,
            include_user=False,
            config=Config(enable_filesystem_skills=True, skill_paths=None, project_root=None),
        )

        assert [rec.skill_id for rec in registered] == ["project.pdf"]
        msgs, refs, _candidates = build_skill_catalog_for_turn(None, discovery_mode=True)
        assert any(ref.skill_id == "project.pdf" for ref in refs)
        assert "Project PDF skill" in msgs[0].content
        assert find_skill_by_name_or_id(name="pdf").skill_id == "project.pdf"  # type: ignore[union-attr]
    reg.unregister_source("project")


def test_extract_bundle_catalog_includes_skills() -> None:
    from motet.core.bundles.deploy import _extract_bundle_catalog

    files = {
        "skills/demo-skill/SKILL.md": SKILL_MD.encode("utf-8"),
    }
    catalog = _extract_bundle_catalog("my-bundle", files)
    assert "skills" in catalog
    assert len(catalog["skills"]) == 1
    row = catalog["skills"][0]
    assert row["id"] == "my-bundle.demo-skill"
    assert row.get("dir_matches_name") is True


def test_load_bundle_registers_skills_on_worker() -> None:
    import shutil

    from motet.core.bundles.bundle_reload import _load_bundle
    from motet.core.skills import get_skill_registry

    reg = get_skill_registry()
    bundle_id = "skill-test-bundle"
    reg.unregister_bundle(bundle_id)
    root = Path(tempfile.mkdtemp())
    try:
        bundle_dir = root / bundle_id
        skill_dir = bundle_dir / "skills" / "demo-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
        loaded = _load_bundle(bundle_id, bundle_dir, None, "ver1")
        sid = f"{bundle_id}.demo-skill"
        assert sid in loaded.get("skills", [])
        rec = reg.get(sid)
        assert rec is not None
        assert rec.bundle_version == "ver1"
    finally:
        reg.unregister_bundle(bundle_id)
        shutil.rmtree(root, ignore_errors=True)


def test_llm_request_accepts_skill_refs() -> None:
    from motet.core.types import LLMRequest, Message, SkillRef

    req = LLMRequest(
        messages=[Message(role="user", content="hi")],
        skill_refs=[SkillRef(skill_id="b.s", name="s", source="bundle")],
    )
    dumped = req.model_dump(mode="json", exclude_none=True)
    assert dumped["skill_refs"][0]["skill_id"] == "b.s"
