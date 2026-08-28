"""
Motet - Bundle Loading Unit Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-09

Description:
    Unit tests for bundle_reload.py covering:
    - _load_bundle_workflows: YAML workflow loading and namespacing (ADR-0071)
    - _unregister_bundle_workflows: cleanup on unload
    - _load_bundle: full load order including commands, tools, and workflows sections
    - _extract_bundle_catalog: catalog extraction including workflow entries (deploy.py)
    - bad-lint bundle: syntax error produces no command registrations

    Uses committed bundle sources from tests/bundles/ and motet-sdk/examples/bundles/.
    All tests are pure unit tests — no Docker, Redis, or network required.

Dependencies:
    - pytest: test framework
    - pathlib: bundle directory paths
    - motet.core.bundles.bundle_reload: system under test
    - motet.core.bundles.deploy: _extract_bundle_catalog

Usage:
    pytest tests/unit/test_bundle_loading.py -v

Notes:
    - Bundle sources live in tests/bundles/{calculator,bad-lint,sdk-demo,agent-configured}
      and motet-sdk/examples/bundles/{hello-world,celebs}; minimal skill bundles are built in-memory.
    - Each test that calls _load_bundle() copies the source into a fresh temp dir
      to avoid polluting sys.modules and actual plugin directories.
    - The isolate_registries fixture snapshots and restores registries around
      every test to prevent state bleed between tests, and also restores the
      motet_sdk runtime bridge (issue #116) for all tests in this module — not
      only TestMotetSdkBridge.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from types import SimpleNamespace
from pathlib import Path
from typing import Any, Callable, Dict, cast

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent.parent
BUNDLES_DIR = REPO_ROOT / "tests" / "bundles"
SDK_EXAMPLES_BUNDLES_DIR = REPO_ROOT / "motet-sdk" / "examples" / "bundles"

HELLO_WORLD_DIR = SDK_EXAMPLES_BUNDLES_DIR / "hello-world"
CALCULATOR_DIR = BUNDLES_DIR / "calculator"
AGENT_CONFIGURED_DIR = BUNDLES_DIR / "agent-configured"
BAD_LINT_DIR = BUNDLES_DIR / "bad-lint"
SDK_DEMO_DIR = BUNDLES_DIR / "sdk-demo"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_temp_bundle_dir(bundle_id: str, bundle_src: Path) -> Path:
    """
    Copy bundle sources into a fresh temp directory.

    Returns bundle_dir (the directory that contains manifest.yaml, commands/, etc.).
    Caller is responsible for cleanup.
    """
    tmp = Path(tempfile.mkdtemp(prefix=f"imf_test_{bundle_id}_"))
    bundle_dir = tmp / bundle_id
    shutil.copytree(bundle_src, bundle_dir)
    return bundle_dir


def _cleanup_sys_modules(bundle_id: str) -> None:
    """Remove bundle.{bundle_id}.* entries from sys.modules after a _load_bundle call."""
    prefix = f"bundle.{bundle_id}."
    stale = [k for k in list(sys.modules) if k.startswith(prefix)]
    for k in stale:
        del sys.modules[k]


def _bundle_files(bundle_dir: Path) -> Dict[str, bytes]:
    """
    Read all files from a bundle directory as {relative_path: bytes}.
    Excludes __pycache__ entries — mirrors what deploy.py receives from the tar archive.
    """
    files: Dict[str, bytes] = {}
    for f in bundle_dir.rglob("*"):
        if f.is_file() and "__pycache__" not in f.parts:
            rel = str(f.relative_to(bundle_dir))
            files[rel] = f.read_bytes()
    return files


def _make_minimal_bundle_with_skill(bundle_id: str) -> Path:
    """
    Create a tiny bundle tree with ``skills/<name>/SKILL.md`` and ``agents/agents.yaml``.

    Parent directory is a temp root; caller should ``shutil.rmtree(bundle_dir.parent)``.
    """
    tmp_root = Path(tempfile.mkdtemp(prefix=f"imf_skill_bundle_{bundle_id}_"))
    bundle_dir = tmp_root / bundle_id
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "manifest.yaml").write_text(
        "\n".join(
            [
                'format_version: "1"',
                f'name: "{bundle_id}"',
                'version: "0.0.1"',
                'description: "unit test bundle with one skill"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    greet = bundle_dir / "skills" / "greet"
    greet.mkdir(parents=True)
    (greet / "SKILL.md").write_text(
        "\n".join(
            [
                "---",
                "name: greet",
                "description: When the user says hello or wants a greeting.",
                "---",
                "",
                "## Guidance",
                "Respond warmly.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    agents_dir = bundle_dir / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "agents.yaml").write_text(
        "\n".join(
            [
                "agents:",
                '  - agent_id: "demo"',
                '    aliases: ["d"]',
                '    display_name: "Demo"',
                '    description: "Agent with skill allowlist"',
                '    allowed_roles: ["*"]',
                '    system_prompt: "You help users."',
                "    tool_filter:",
                '      mode: "discovery"',
                "    skill_ids:",
                f'      - "{bundle_id}.greet"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return bundle_dir


def _write_agents_config(bundle_dir: Path, *, agent_id: str = "assistant") -> None:
    """Write a minimal agents/agents.yaml for bundle agent registration tests."""
    agents_dir = bundle_dir / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "agents.yaml").write_text(
        "\n".join(
            [
                "agents:",
                f'  - agent_id: "{agent_id}"',
                '    aliases: ["helper"]',
                '    display_name: "Bundle Assistant"',
                '    description: "Test agent from bundle config"',
                '    system_prompt: "You are a bundle assistant."',
                "    tool_filter:",
                '      mode: "discovery"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolate_registries():
    """
    Snapshot and restore command_type_registry, tool_registry, WorkflowRegistry,
    AgentConfigRegistry, SkillRegistry, and motet_sdk bridge modules before/after
    each test so tests don't bleed (including issue #116 bridge inject).
    """
    from motet.core.bundles.bundle_reload import (
        restore_motet_sdk_runtime_bridge,
        snapshot_motet_sdk_runtime_bridge,
    )
    from motet.core.commands.command_type_registry import (
        command_type_registry,
    )
    from motet.core.workflow import WorkflowRegistry
    from motet.core.tools import registry as tool_registry
    from motet.core.agents import get_agent_registry
    from motet.core.skills import get_skill_registry

    sdk_bridge_snapshot = snapshot_motet_sdk_runtime_bridge()

    _ctr = cast(Any, command_type_registry)
    # CommandTypeRegistry is a ScopedRegistry (#61): snapshot _entries (+ domain maps).
    with _ctr._lock:
        orig_command_entries = dict(_ctr._entries)
        orig_command_versions = {k: dict(v) for k, v in _ctr._versions.items()}
        orig_command_stats = dict(_ctr._stats)
    orig_tool_entries = list(tool_registry.list_entries())
    orig_workflow_entries = list(WorkflowRegistry._registry.list_entries())
    agent_registry = get_agent_registry()
    orig_agent_entries = list(agent_registry.list_entries())
    orig_aliases = dict(agent_registry._aliases)
    orig_aliases_by_qid = {k: set(v) for k, v in agent_registry._aliases_by_qid.items()}

    skill_registry = get_skill_registry()
    with skill_registry._lock:
        orig_skill_by_id = dict(skill_registry._by_id)

    try:
        yield
    finally:
        with _ctr._lock:
            _ctr._entries.clear()
            _ctr._entries.update(orig_command_entries)
            _ctr._versions.clear()
            _ctr._versions.update(orig_command_versions)
            _ctr._stats.clear()
            _ctr._stats.update(orig_command_stats)
        for tool_name in list(tool_registry.list_items().keys()):
            tool_registry.unregister(tool_name)
        for entry in orig_tool_entries:
            rt = entry.item
            rt_data = rt.model_dump()
            name = rt_data.pop("name")
            description = rt_data.pop("description")
            func = rt_data.pop("func")
            tool_registry.register(
                name=name,
                description=description,
                func=func,
                scope=entry.scope,
                **rt_data,
            )
        for workflow in list(WorkflowRegistry.list_all()):
            WorkflowRegistry.unregister(workflow.workflow_id)
        for entry in orig_workflow_entries:
            WorkflowRegistry.register(entry.item, scope=entry.scope)
        agent_registry._entries.clear()
        for entry in orig_agent_entries:
            agent_registry._entries[entry.key] = entry
        agent_registry._aliases.clear()
        agent_registry._aliases.update(orig_aliases)
        agent_registry._aliases_by_qid.clear()
        agent_registry._aliases_by_qid.update(orig_aliases_by_qid)

        with skill_registry._lock:
            skill_registry._by_id.clear()
            skill_registry._by_id.update(orig_skill_by_id)

        restore_motet_sdk_runtime_bridge(sdk_bridge_snapshot)


# ---------------------------------------------------------------------------
# Tests: _load_bundle_workflows
# ---------------------------------------------------------------------------


class TestLoadBundleWorkflows:
    def test_calculator_workflow_registered(self):
        """calculator bundle: multi_step_calc.yaml registers as calculator.multi_step_calc."""
        from motet.core.bundles.bundle_reload import (
            _load_bundle_workflows,
        )
        from motet.core.workflow import WorkflowRegistry

        workflows_dir = CALCULATOR_DIR / "workflows"
        registered = _load_bundle_workflows("calculator", workflows_dir)

        assert "calculator.multi_step_calc" in registered
        wf = WorkflowRegistry.get("calculator.multi_step_calc")
        assert wf is not None
        assert wf.workflow_id == "calculator.multi_step_calc"

    def test_workflow_steps_loaded(self):
        """calculator.multi_step_calc has add_step and multiply_step."""
        from motet.core.bundles.bundle_reload import (
            _load_bundle_workflows,
        )
        from motet.core.workflow import WorkflowRegistry

        _load_bundle_workflows("calculator", CALCULATOR_DIR / "workflows")
        wf = WorkflowRegistry.get("calculator.multi_step_calc")
        assert wf is not None
        assert "add_step" in wf.steps
        assert "multiply_step" in wf.steps

    def test_workflow_step_multiply_depends_on_add(self):
        """multiply_step depends on add_step."""
        from motet.core.bundles.bundle_reload import (
            _load_bundle_workflows,
        )
        from motet.core.workflow import WorkflowRegistry

        _load_bundle_workflows("calculator", CALCULATOR_DIR / "workflows")
        wf = WorkflowRegistry.get("calculator.multi_step_calc")
        assert wf is not None
        multiply = wf.steps["multiply_step"]
        assert "add_step" in (multiply.dependencies or [])

    def test_hello_world_no_workflows_dir_returns_empty(self):
        """hello-world bundle has no workflows/ dir — returns empty list without raising."""
        from motet.core.bundles.bundle_reload import (
            _load_bundle_workflows,
        )

        # hello-world has no workflows/ directory
        workflows_dir = HELLO_WORLD_DIR / "workflows"
        registered = _load_bundle_workflows("hello-world", workflows_dir)
        assert registered == []

    def test_workflow_id_namespaced_not_bare(self):
        """Workflows register under 'bundle_id.workflow_id', NOT the bare YAML workflow_id."""
        from motet.core.bundles.bundle_reload import (
            _load_bundle_workflows,
        )
        from motet.core.workflow import WorkflowRegistry

        _load_bundle_workflows("calculator", CALCULATOR_DIR / "workflows")

        assert WorkflowRegistry.get("multi_step_calc") is None
        assert WorkflowRegistry.get("calculator.multi_step_calc") is not None

    def test_nonexistent_dir_returns_empty(self):
        """Completely nonexistent workflows dir returns empty list without raising."""
        from motet.core.bundles.bundle_reload import (
            _load_bundle_workflows,
        )

        result = _load_bundle_workflows(
            "nonexistent", Path("/tmp/__does_not_exist__/workflows")
        )
        assert result == []


# ---------------------------------------------------------------------------
# Tests: _unregister_bundle_workflows
# ---------------------------------------------------------------------------


class TestUnregisterBundleWorkflows:
    def test_unregister_removes_workflow(self):
        """Workflow registered by _load_bundle_workflows is removed by _unregister."""
        from motet.core.bundles.bundle_reload import (
            _load_bundle_workflows,
            _unregister_bundle_workflows,
        )
        from motet.core.workflow import WorkflowRegistry

        _load_bundle_workflows("calculator", CALCULATOR_DIR / "workflows")
        assert WorkflowRegistry.get("calculator.multi_step_calc") is not None

        _unregister_bundle_workflows("calculator")
        assert WorkflowRegistry.get("calculator.multi_step_calc") is None

    def test_unregister_noop_for_unknown_bundle(self):
        """Unregistering a bundle that never loaded is a no-op (no exception raised)."""
        from motet.core.bundles.bundle_reload import (
            _unregister_bundle_workflows,
        )

        _unregister_bundle_workflows("no-such-bundle")  # must not raise

    def test_unregister_leaves_other_workflows_untouched(self):
        """Unregistering 'calculator' does not remove workflows from other bundles."""
        from motet.core.bundles.bundle_reload import (
            _load_bundle_workflows,
            _unregister_bundle_workflows,
        )
        from motet.core.workflow import Workflow, WorkflowRegistry

        # Register a standalone workflow from a different bundle
        dummy = Workflow(
            workflow_id="other-bundle.my_wf",
            name="Dummy",
            description="Dummy workflow for test isolation",
        )
        WorkflowRegistry.register(dummy)

        _load_bundle_workflows("calculator", CALCULATOR_DIR / "workflows")
        _unregister_bundle_workflows("calculator")

        # The other bundle's workflow must survive
        assert WorkflowRegistry.get("other-bundle.my_wf") is not None
        assert WorkflowRegistry.get("calculator.multi_step_calc") is None


class TestUnregisterBundleAgents:
    def test_unregister_removes_bundle_agents_only(self):
        """_unregister_bundle_agents removes bundle-scoped agents without touching core agents."""
        from motet.core.agents import AgentConfig, get_agent_registry
        from motet.core.bundles.bundle_reload import _unregister_bundle_agents

        registry = get_agent_registry()
        registry.register_agent(
            AgentConfig(
                agent_id="assistant",
                display_name="Bundle Assistant",
                description="test",
                system_prompt="test",
                bundle_id="hello-world",
            )
        )
        assert registry.get("hello-world.assistant") is not None
        assert registry.get("core.default") is not None

        _unregister_bundle_agents("hello-world")

        assert registry.get("hello-world.assistant") is None
        assert registry.get("core.default") is not None


# ---------------------------------------------------------------------------
# Tests: _load_bundle (full load order)
# ---------------------------------------------------------------------------


class TestLoadBundle:
    def test_calculator_result_includes_workflow_key(self):
        """_load_bundle returns a 'workflows' key in result for the calculator bundle."""
        from motet.core.bundles.bundle_reload import _load_bundle

        bundle_dir = _make_temp_bundle_dir("calculator", CALCULATOR_DIR)
        try:
            result = _load_bundle("calculator", bundle_dir, None)
            assert "workflows" in result
            assert "calculator.multi_step_calc" in result["workflows"]
        finally:
            _cleanup_sys_modules("calculator")
            shutil.rmtree(bundle_dir.parent, ignore_errors=True)

    def test_hello_world_workflows_empty(self):
        """_load_bundle on hello-world bundle returns empty workflows list."""
        from motet.core.bundles.bundle_reload import _load_bundle

        bundle_dir = _make_temp_bundle_dir("hello-world", HELLO_WORLD_DIR)
        try:
            result = _load_bundle("hello-world", bundle_dir, None)
            assert result.get("workflows", []) == []
        finally:
            _cleanup_sys_modules("hello-world")
            shutil.rmtree(bundle_dir.parent, ignore_errors=True)

    def test_hello_world_command_registered(self):
        """_load_bundle registers hello-world.hello_world command."""
        from motet.core.bundles.bundle_reload import _load_bundle
        from motet.core.commands.command_type_registry import (
            command_type_registry,
        )

        bundle_dir = _make_temp_bundle_dir("hello-world", HELLO_WORLD_DIR)
        try:
            result = _load_bundle("hello-world", bundle_dir, None)
            assert "hello-world.hello_world" in result.get("commands", [])
            assert command_type_registry.is_registered("hello-world.hello_world")
        finally:
            _cleanup_sys_modules("hello-world")
            shutil.rmtree(bundle_dir.parent, ignore_errors=True)

    def test_hello_world_tool_registered(self):
        """_load_bundle registers hello-world.hello_world_tool tool."""
        from motet.core.bundles.bundle_reload import _load_bundle
        from motet.core.tools import registry as tool_registry

        bundle_dir = _make_temp_bundle_dir("hello-world", HELLO_WORLD_DIR)
        try:
            _load_bundle("hello-world", bundle_dir, None)
            assert "hello-world.hello_world_tool" in tool_registry.get_all_tool_names()
        finally:
            _cleanup_sys_modules("hello-world")
            shutil.rmtree(bundle_dir.parent, ignore_errors=True)

    def test_calculator_command_registered(self):
        """_load_bundle registers calculator.calculate command."""
        from motet.core.bundles.bundle_reload import _load_bundle
        from motet.core.commands.command_type_registry import (
            command_type_registry,
        )

        bundle_dir = _make_temp_bundle_dir("calculator", CALCULATOR_DIR)
        try:
            result = _load_bundle("calculator", bundle_dir, None)
            assert "calculator.calculate" in result.get("commands", [])
            assert command_type_registry.is_registered("calculator.calculate")
        finally:
            _cleanup_sys_modules("calculator")
            shutil.rmtree(bundle_dir.parent, ignore_errors=True)

    def test_sdk_demo_bundle_registers_echo_command(self):
        """Bundle that uses 'from motet_sdk import ...' registers sdk-demo.echo (ADR-0080 bridge)."""
        from motet.core.bundles.bundle_reload import _load_bundle
        from motet.core.commands.command_type_registry import (
            command_type_registry,
        )

        bundle_dir = _make_temp_bundle_dir("sdk-demo", SDK_DEMO_DIR)
        try:
            result = _load_bundle("sdk-demo", bundle_dir, None)
            assert "sdk-demo.echo" in result.get("commands", [])
            assert command_type_registry.is_registered("sdk-demo.echo")
        finally:
            _cleanup_sys_modules("sdk-demo")
            shutil.rmtree(bundle_dir.parent, ignore_errors=True)

    def test_calculator_tool_registered(self):
        """_load_bundle registers calculator.math_tool tool."""
        from motet.core.bundles.bundle_reload import _load_bundle
        from motet.core.tools import registry as tool_registry

        bundle_dir = _make_temp_bundle_dir("calculator", CALCULATOR_DIR)
        try:
            _load_bundle("calculator", bundle_dir, None)
            assert "calculator.math_tool" in tool_registry.get_all_tool_names()
        finally:
            _cleanup_sys_modules("calculator")
            shutil.rmtree(bundle_dir.parent, ignore_errors=True)

    def test_calculator_tool_reported_on_redeploy(self):
        """
        Redeploy should still report unchanged tools in loaded['tools'].

        Regression coverage for ADR-0089 tool decorator path where previously
        only newly-added tools were reported, causing downstream prune/index
        steps to miss unchanged bundle tools.
        """
        from motet.core.bundles.bundle_reload import _load_bundle
        from motet.core.tools import registry as tool_registry

        bundle_dir = _make_temp_bundle_dir("calculator", CALCULATOR_DIR)
        try:
            first = _load_bundle("calculator", bundle_dir, None)
            assert "calculator.math_tool" in first.get("tools", [])
            assert "calculator.math_tool" in tool_registry.get_all_tool_names()

            # Simulate redeploy with unchanged code
            second = _load_bundle("calculator", bundle_dir, None)
            assert "calculator.math_tool" in second.get("tools", [])
            assert "calculator.math_tool" in tool_registry.get_all_tool_names()
        finally:
            _cleanup_sys_modules("calculator")
            shutil.rmtree(bundle_dir.parent, ignore_errors=True)

    def test_calculator_workflow_in_registry_after_load(self):
        """After _load_bundle, calculator.multi_step_calc is in WorkflowRegistry."""
        from motet.core.bundles.bundle_reload import _load_bundle
        from motet.core.workflow import WorkflowRegistry

        bundle_dir = _make_temp_bundle_dir("calculator", CALCULATOR_DIR)
        try:
            _load_bundle("calculator", bundle_dir, None)
            assert WorkflowRegistry.get("calculator.multi_step_calc") is not None
        finally:
            _cleanup_sys_modules("calculator")
            shutil.rmtree(bundle_dir.parent, ignore_errors=True)

    def test_result_has_all_expected_keys(self):
        """_load_bundle result always contains commands/tools/workflows/agents keys."""
        from motet.core.bundles.bundle_reload import _load_bundle

        bundle_dir = _make_temp_bundle_dir("calculator", CALCULATOR_DIR)
        try:
            result = _load_bundle("calculator", bundle_dir, None)
            for key in ("commands", "tools", "workflows", "agents", "skills"):
                assert key in result, f"Missing key in result: {key}"
        finally:
            _cleanup_sys_modules("calculator")
            shutil.rmtree(bundle_dir.parent, ignore_errors=True)

    def test_load_bundle_registers_agents_from_config(self):
        """agents/agents.yaml entries are registered in AgentConfigRegistry under bundle namespace."""
        from motet.core.agents import get_agent_registry
        from motet.core.bundles.bundle_reload import _load_bundle

        bundle_dir = _make_temp_bundle_dir("agent-configured", AGENT_CONFIGURED_DIR)
        try:
            result = _load_bundle("agent-configured", bundle_dir, None)

            assert "agent-configured.support" in result.get("agents", [])
            cfg = get_agent_registry().get("agent-configured.support")
            assert cfg is not None
            assert cfg.bundle_id == "agent-configured"
        finally:
            _cleanup_sys_modules("agent-configured")
            shutil.rmtree(bundle_dir.parent, ignore_errors=True)

    def test_load_bundle_registers_skills_and_agent_skill_ids(self):
        """ADR-0073: skills/ + agents.yaml skill_ids load into registries with bundle_version."""
        from motet.core.agents import get_agent_registry
        from motet.core.bundles.bundle_reload import _load_bundle
        from motet.core.skills import get_skill_registry

        bundle_id = "skill-load-mini"
        bundle_dir = _make_minimal_bundle_with_skill(bundle_id)
        try:
            result = _load_bundle(bundle_id, bundle_dir, None, bundle_version="vers-sha")
            assert f"{bundle_id}.greet" in result.get("skills", [])

            rec = get_skill_registry().get(f"{bundle_id}.greet")
            assert rec is not None
            assert rec.bundle_version == "vers-sha"
            assert rec.skill_md_path.is_file()

            cfg = get_agent_registry().get(f"{bundle_id}.demo")
            assert cfg is not None
            assert cfg.skill_ids == [f"{bundle_id}.greet"]
        finally:
            shutil.rmtree(bundle_dir.parent, ignore_errors=True)


# ---------------------------------------------------------------------------
# Tests: _extract_bundle_catalog (deploy.py)
# ---------------------------------------------------------------------------


class TestExtractBundleCatalog:
    def test_hello_world_catalog_has_command(self):
        """hello-world catalog contains hello-world.hello_world command."""
        from motet.core.bundles.deploy import _extract_bundle_catalog

        catalog = _extract_bundle_catalog("hello-world", _bundle_files(HELLO_WORLD_DIR))
        assert "hello-world.hello_world" in catalog["commands"]

    def test_hello_world_catalog_has_tool(self):
        """hello-world catalog contains hello-world.hello_world_tool."""
        from motet.core.bundles.deploy import _extract_bundle_catalog

        catalog = _extract_bundle_catalog("hello-world", _bundle_files(HELLO_WORLD_DIR))
        assert "hello-world.hello_world_tool" in catalog["tools"]

    def test_hello_world_catalog_has_no_workflows(self):
        """hello-world catalog has an empty workflows list (no workflows/ dir)."""
        from motet.core.bundles.deploy import _extract_bundle_catalog

        catalog = _extract_bundle_catalog("hello-world", _bundle_files(HELLO_WORLD_DIR))
        assert catalog.get("workflows", []) == []

    def test_calculator_catalog_has_command(self):
        """calculator catalog contains calculator.calculate command."""
        from motet.core.bundles.deploy import _extract_bundle_catalog

        catalog = _extract_bundle_catalog("calculator", _bundle_files(CALCULATOR_DIR))
        assert "calculator.calculate" in catalog["commands"]

    def test_calculator_catalog_has_tool(self):
        """calculator catalog contains calculator.math_tool."""
        from motet.core.bundles.deploy import _extract_bundle_catalog

        catalog = _extract_bundle_catalog("calculator", _bundle_files(CALCULATOR_DIR))
        assert "calculator.math_tool" in catalog["tools"]

    def test_calculator_catalog_has_workflow(self):
        """calculator catalog contains calculator.multi_step_calc workflow."""
        from motet.core.bundles.deploy import _extract_bundle_catalog

        catalog = _extract_bundle_catalog("calculator", _bundle_files(CALCULATOR_DIR))
        assert "calculator.multi_step_calc" in catalog.get("workflows", [])

    def test_bad_lint_catalog_has_no_commands(self):
        """bad-lint catalog: syntax error in commands/*.py produces no command entries."""
        from motet.core.bundles.deploy import _extract_bundle_catalog

        catalog = _extract_bundle_catalog("bad-lint", _bundle_files(BAD_LINT_DIR))
        assert catalog["commands"] == []

    def test_catalog_bundle_id_field(self):
        """Catalog always carries the correct bundle_id field."""
        from motet.core.bundles.deploy import _extract_bundle_catalog

        catalog = _extract_bundle_catalog("hello-world", _bundle_files(HELLO_WORLD_DIR))
        assert catalog["bundle_id"] == "hello-world"

    def test_catalog_has_all_required_keys(self):
        """Catalog always has commands, tools, workflows, agents, mcp_servers, model_ids, bundle_id."""
        from motet.core.bundles.deploy import _extract_bundle_catalog

        catalog = _extract_bundle_catalog("hello-world", _bundle_files(HELLO_WORLD_DIR))
        for key in (
            "commands",
            "command_capabilities",
            "command_descriptions",
            "command_schemas",
            "tools",
            "workflows",
            "agents",
            "mcp_servers",
            "model_ids",
            "bundle_id",
            "skills",
        ):
            assert key in catalog, f"Missing key: {key}"
        assert catalog["command_schemas"] == {}

    def test_hello_world_catalog_has_command_description(self):
        """hello-world command description comes from the function docstring."""
        from motet.core.bundles.deploy import _extract_bundle_catalog

        catalog = _extract_bundle_catalog("hello-world", _bundle_files(HELLO_WORLD_DIR))
        descs = catalog.get("command_descriptions") or {}
        assert "hello-world.hello_world" in descs
        assert "greeting" in descs["hello-world.hello_world"].lower()

    def test_catalog_extracts_command_capabilities(self):
        """@motet.command(required_capabilities=...) is stored under command_capabilities."""
        from motet.core.bundles.deploy import _extract_bundle_catalog

        bundle_dir = _make_temp_bundle_dir("cap-demo", HELLO_WORLD_DIR)
        try:
            cmd = bundle_dir / "commands" / "edge_cmd.py"
            cmd.write_text(
                "from motet import motet\n"
                "from motet.core.commands.capabilities import WorkerCapability\n"
                "from motet.core.orchestration.motet_context import MotetContext\n"
                "from pydantic import BaseModel\n"
                "\n"
                "class D(BaseModel):\n"
                "    x: str = 'ok'\n"
                "\n"
                "@motet.command(\n"
                "    timeout_seconds=60,\n"
                "    required_capabilities=[\n"
                "        WorkerCapability.TOOL_EXECUTION,\n"
                "        WorkerCapability.EDGE_EXECUTION,\n"
                "        WorkerCapability.EDGE_FILE_READ,\n"
                "    ],\n"
                ")\n"
                "def edge_cmd(data: D, motet: MotetContext):\n"
                "    return {'ok': True}\n",
                encoding="utf-8",
            )
            catalog = _extract_bundle_catalog("cap-demo", _bundle_files(bundle_dir))
            assert "cap-demo.edge_cmd" in catalog["commands"]
            assert catalog["command_capabilities"]["cap-demo.edge_cmd"] == [
                "tool_execution",
                "edge_execution",
                "edge_file_read",
            ]
        finally:
            import shutil

            shutil.rmtree(bundle_dir, ignore_errors=True)

    def test_catalog_prefers_explicit_decorator_description(self):
        """Explicit description= on @motet.command wins over the docstring."""
        from motet.core.bundles.deploy import _extract_bundle_catalog

        bundle_dir = _make_temp_bundle_dir("desc-demo", HELLO_WORLD_DIR)
        try:
            cmd = bundle_dir / "commands" / "named_desc.py"
            cmd.write_text(
                "from motet import motet\n"
                "from pydantic import BaseModel\n"
                "\n"
                "class D(BaseModel):\n"
                "    x: str = 'ok'\n"
                "\n"
                "@motet.command(\n"
                "    timeout_seconds=30,\n"
                "    description='Search-friendly explicit command description.',\n"
                ")\n"
                "def named_desc(data: D, motet):\n"
                "    '''Docstring that should be ignored when description= is set.'''\n"
                "    return {'ok': True}\n",
                encoding="utf-8",
            )
            catalog = _extract_bundle_catalog("desc-demo", _bundle_files(bundle_dir))
            assert (
                catalog["command_descriptions"]["desc-demo.named_desc"]
                == "Search-friendly explicit command description."
            )
        finally:
            import shutil

            shutil.rmtree(bundle_dir, ignore_errors=True)

    def test_catalog_honors_motet_tool_name_override(self):
        """ADR-0089: @motet.tool(name='x') appears as bundle_id.x in catalog (not function name)."""
        from motet.core.bundles.deploy import _extract_bundle_catalog

        bundle_dir = _make_temp_bundle_dir("hello-world", HELLO_WORLD_DIR)
        try:
            tools_dir = bundle_dir / "tools"
            tools_dir.mkdir(parents=True, exist_ok=True)
            (tools_dir / "named_tool.py").write_text(
                "\n".join(
                    [
                        "from motet_sdk import motet",
                        "",
                        '@motet.tool(description="Named tool", name="custom_tool_name")',
                        "def internal_function_name(params):",
                        '    return {"ok": True}',
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            catalog = _extract_bundle_catalog("hello-world", _bundle_files(bundle_dir))
            assert "hello-world.custom_tool_name" in catalog["tools"]
            assert "hello-world.internal_function_name" not in catalog["tools"]
        finally:
            _cleanup_sys_modules("hello-world")
            shutil.rmtree(bundle_dir.parent, ignore_errors=True)

    def test_catalog_includes_agents_from_config(self):
        """Catalog includes namespaced agent IDs extracted from agents/agents.yaml."""
        from motet.core.bundles.deploy import _extract_bundle_catalog

        bundle_dir = _make_temp_bundle_dir("agent-configured", AGENT_CONFIGURED_DIR)
        try:
            catalog = _extract_bundle_catalog("agent-configured", _bundle_files(bundle_dir))
            assert "agent-configured.support" in catalog.get("agents", [])
        finally:
            _cleanup_sys_modules("agent-configured")
            shutil.rmtree(bundle_dir.parent, ignore_errors=True)

    def test_agent_config_validation_fails_for_invalid_agents_yaml(self):
        """Strict agent extraction raises on invalid agents/agents.yaml shape."""
        from motet.core.bundles.deploy import _extract_bundle_agent_ids

        bundle_dir = _make_temp_bundle_dir("hello-world", HELLO_WORLD_DIR)
        try:
            (bundle_dir / "agents").mkdir(parents=True, exist_ok=True)
            (bundle_dir / "agents" / "agents.yaml").write_text("agents: bad\n", encoding="utf-8")
            with pytest.raises(ValueError, match="Invalid bundle agent config"):
                _extract_bundle_agent_ids("hello-world", _bundle_files(bundle_dir), strict=True)
        finally:
            _cleanup_sys_modules("hello-world")
            shutil.rmtree(bundle_dir.parent, ignore_errors=True)


# ---------------------------------------------------------------------------
# Tests: bad-lint bundle
# ---------------------------------------------------------------------------


class TestBadLintBundle:
    def test_bad_lint_commands_syntax_error_raises(self):
        """bad-lint commands/ has a syntax error — _load_bundle_commands raises RuntimeError."""
        from motet.core.bundles.bundle_reload import (
            _load_bundle_commands,
        )
        from motet.core.commands.command_type_registry import (
            command_type_registry,
        )

        with pytest.raises(RuntimeError, match="Failed to import command file"):
            _load_bundle_commands("bad-lint", BAD_LINT_DIR / "commands", None)

        # Even though it raised, no command should have been registered
        assert not command_type_registry.is_registered("bad-lint.broken")

    def test_bad_lint_full_load_raises_runtime_error(self):
        """_load_bundle on bad-lint propagates RuntimeError from the syntax error."""
        from motet.core.bundles.bundle_reload import _load_bundle

        bundle_dir = _make_temp_bundle_dir("bad-lint", BAD_LINT_DIR)
        try:
            with pytest.raises(RuntimeError, match="Failed to import command file"):
                _load_bundle("bad-lint", bundle_dir, None)
        finally:
            _cleanup_sys_modules("bad-lint")
            shutil.rmtree(bundle_dir.parent, ignore_errors=True)

    def test_bad_lint_catalog_extraction_is_safe(self):
        """_extract_bundle_catalog (AST-based, no execution) handles syntax error safely."""
        from motet.core.bundles.deploy import _extract_bundle_catalog

        # Catalog extraction uses AST.parse which catches SyntaxError — no RuntimeError
        catalog = _extract_bundle_catalog("bad-lint", _bundle_files(BAD_LINT_DIR))
        assert catalog["commands"] == []


class TestMotetSdkBridge:
    def test_bridge_registers_motet_namespace_submodule(self):
        """ADR-0089: runtime bridge must expose motet_sdk.motet_namespace.motet."""
        from motet.core.bundles.bundle_reload import (
            motet_sdk_runtime_bridge,
        )
        from motet.core.commands.motet_namespace import motet as runtime_motet

        with motet_sdk_runtime_bridge():
            assert "motet_sdk.motet_namespace" in sys.modules
            assert getattr(sys.modules["motet_sdk.motet_namespace"], "motet", None) is runtime_motet
            assert getattr(sys.modules["motet_sdk"], "motet", None) is runtime_motet

    def test_bridge_exposes_base_command_data_from_motet_sdk(self):
        """ADR-0080: bundle imports `from motet_sdk import BaseCommandData` must work."""
        from motet.core.bundles.bundle_reload import (
            motet_sdk_runtime_bridge,
        )

        with motet_sdk_runtime_bridge():
            import motet_sdk  # type: ignore[import]
            from motet_sdk import BaseCommandData  # type: ignore[import]
            from motet_sdk.models import BaseCommandData as ModelsBaseCommandData  # type: ignore[import]

            assert hasattr(motet_sdk, "BaseCommandData")
            assert BaseCommandData is getattr(motet_sdk, "BaseCommandData")
            assert ModelsBaseCommandData is BaseCommandData


# ---------------------------------------------------------------------------
# Tests: _refresh_search_index incremental bundle sync
# ---------------------------------------------------------------------------


class TestRefreshSearchIndexIncremental:
    def test_prefers_bundle_incremental_sync(self, monkeypatch):
        """When bundle context is provided, _refresh_search_index uses bundle-scoped sync first."""
        from motet.core.bundles import bundle_reload

        class FakeStore:
            def __init__(self):
                self.sync_called = False
                self.full_called = False
                self.sync_args = {}

            def sync_bundle_entries(self, **kwargs):
                self.sync_called = True
                self.sync_args = kwargs
                return {"removed": 1, "added": 3}

            def index_tools_and_workflows(self, *args, **kwargs):
                self.full_called = True

        class FakeMotet:
            def __init__(self, store):
                self.function_discovery_store = store
                self.tools = object()

        store = FakeStore()
        monkeypatch.setattr(
            bundle_reload,
            "get_motet_context",
            lambda: FakeMotet(store),
        )

        loaded = {
            "commands": ["calculator.calculate"],
            "tools": ["calculator.math_tool"],
            "workflows": ["calculator.multi_step_calc"],
        }
        bundle_reload._refresh_search_index(bundle_id="calculator", loaded=loaded)

        assert store.sync_called is True
        assert store.full_called is False
        assert store.sync_args["bundle_id"] == "calculator"
        assert store.sync_args["tool_names"] == ["calculator.math_tool"]
        assert store.sync_args["workflow_ids"] == ["calculator.multi_step_calc"]
        assert store.sync_args["command_types"] == ["calculator.calculate"]

    def test_falls_back_to_full_reindex_when_incremental_fails(self, monkeypatch):
        """Incremental failures fall back to the existing full reindex path."""
        from motet.core.bundles import bundle_reload

        class FakeStore:
            def __init__(self):
                self.full_called = False
                self.force_reindex = None
                self.include_commands = None

            def sync_bundle_entries(self, **kwargs):
                raise RuntimeError("incremental failed")

            def index_tools_and_workflows(self, *args, **kwargs):
                self.full_called = True
                self.force_reindex = kwargs.get("force_reindex")
                self.include_commands = kwargs.get("include_commands")

        class FakeMotet:
            def __init__(self, store):
                self.function_discovery_store = store
                self.tools = object()

        store = FakeStore()
        monkeypatch.setattr(
            bundle_reload,
            "get_motet_context",
            lambda: FakeMotet(store),
        )

        loaded = {
            "commands": ["calculator.calculate"],
            "tools": ["calculator.math_tool"],
            "workflows": ["calculator.multi_step_calc"],
        }
        bundle_reload._refresh_search_index(bundle_id="calculator", loaded=loaded)

        assert store.full_called is True
        assert store.force_reindex is True
        assert store.include_commands is True

    def test_full_reindex_fallback_takes_the_writer_lock(self, monkeypatch):
        """
        The fallback rebuild is destructive, so it must be serialized (#156).

        It drops the shared index and repopulates it from this worker's
        registry; landing that on top of another worker's rebuild is how whole
        tool families got evicted.
        """
        from motet.core.bundles import bundle_reload

        class FakeStore:
            def __init__(self):
                self.kwargs = None
                self.lock_acquired = False

            def sync_bundle_entries(self, **kwargs):
                raise RuntimeError("incremental failed")

            def ensure_shared_index(self, *args, **kwargs):
                # Exercise the injected factory, then record success last:
                # _refresh_search_index swallows exceptions, so anything set
                # before a failure would still look like a pass.
                lock = kwargs["lock_factory"]()
                lock.release_sync()
                self.lock_acquired = True
                self.kwargs = kwargs
                return "rebuilt"

        class FakeMotet:
            def __init__(self, store):
                self.function_discovery_store = store
                self.tools = object()

        store = FakeStore()
        acquired = {"n": 0}

        def _fake_acquire(client_id, lock_key, ttl_seconds=90):
            acquired["n"] += 1
            assert lock_key == "motet:function_discovery:index_writer"
            return SimpleNamespace(release_sync=lambda: None)

        monkeypatch.setattr(bundle_reload, "get_motet_context", lambda: FakeMotet(store))
        monkeypatch.setattr(
            "motet.core.distributed.redis_manager.acquire_distributed_lock_sync",
            _fake_acquire,
        )

        bundle_reload._refresh_search_index(
            bundle_id="calculator",
            loaded={"commands": [], "tools": [], "workflows": []},
        )

        assert store.lock_acquired is True
        assert acquired["n"] == 1
        assert store.kwargs is not None
        assert store.kwargs["force_reindex"] is True
        assert store.kwargs["include_commands"] is True


class TestPruneStaleBundleRegistrations:
    def test_prunes_stale_renamed_symbols(self):
        """After load, stale bundle symbols are removed when not present in loaded lists."""
        from motet.core.bundles.bundle_reload import (
            _prune_stale_bundle_registrations,
        )
        from motet.core.agents import AgentConfig, get_agent_registry
        from motet.core.commands.command_type_registry import (
            command_type_registry,
            CommandImplementationType,
        )
        from motet.core.workflow import Workflow, WorkflowRegistry
        from motet.core.tools import registry as tool_registry

        # Register stale + current command
        stale_cmd = "calculator.old_command"
        current_cmd = "calculator.new_command"
        command_type_registry.register_command(
            command_type=stale_cmd,
            implementation=lambda *_args, **_kwargs: {},
            implementation_type=CommandImplementationType.DECORATOR_BASED,
            overwrite=True,
            bundle_id="calculator",
            hot_loadable=True,
        )
        command_type_registry.register_command(
            command_type=current_cmd,
            implementation=lambda *_args, **_kwargs: {},
            implementation_type=CommandImplementationType.DECORATOR_BASED,
            overwrite=True,
            bundle_id="calculator",
            hot_loadable=True,
        )

        # Register stale + current tool
        tool_registry.register(
            name="calculator.old_tool",
            func=lambda _params: {},
            description="old tool",
            schema=None,
        )
        tool_registry.register(
            name="calculator.new_tool",
            func=lambda _params: {},
            description="new tool",
            schema=None,
        )

        # Register stale + current workflow
        stale_wf = Workflow(
            workflow_id="calculator.old_workflow",
            name="Old",
            description="Old wf",
        )
        current_wf = Workflow(
            workflow_id="calculator.new_workflow",
            name="New",
            description="New wf",
        )
        WorkflowRegistry.register(stale_wf)
        WorkflowRegistry.register(current_wf)

        # Register stale + current agent
        agent_registry = get_agent_registry()
        agent_registry.register_agent(
            AgentConfig(
                agent_id="old_agent",
                display_name="Old Agent",
                description="old",
                system_prompt="old",
                bundle_id="calculator",
            )
        )
        agent_registry.register_agent(
            AgentConfig(
                agent_id="new_agent",
                display_name="New Agent",
                description="new",
                system_prompt="new",
                bundle_id="calculator",
            )
        )

        loaded = {
            "commands": [current_cmd],
            "tools": ["calculator.new_tool"],
            "workflows": ["calculator.new_workflow"],
            "agents": ["calculator.new_agent"],
        }
        _prune_stale_bundle_registrations("calculator", loaded)

        assert command_type_registry.is_registered(current_cmd)
        assert not command_type_registry.is_registered(stale_cmd)
        names = (
            set(tool_registry.get_all_tool_names())
            if hasattr(tool_registry, "get_all_tool_names")
            else set(getattr(tool_registry, "_tools", {}).keys())
        )
        assert "calculator.new_tool" in names
        assert "calculator.old_tool" not in names
        assert WorkflowRegistry.get("calculator.new_workflow") is not None
        assert WorkflowRegistry.get("calculator.old_workflow") is None
        assert agent_registry.get("calculator.new_agent") is not None
        assert agent_registry.get("calculator.old_agent") is None


class TestBundleReloadErrors:
    def test_reload_bundle_missing_artifact_raises_structured_command_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Missing artifact path raises CommandExecutionError with required ADR-0052 fields."""
        from motet.core.bundles import bundle_reload
        from motet.core.bundles.bundle_reload import ReloadBundleData
        from motet.core.commands.response_models import CommandExecutionError
        from motet.core.bundles import deploy as deploy_mod

        class _FakeMotet:
            def __init__(self) -> None:
                self.redis = object()
                self.command_id = "cmd-reload-test"

        monkeypatch.setattr(bundle_reload, "PLUGIN_ROOT", tmp_path)
        monkeypatch.setattr(bundle_reload, "get_motet_context", lambda: _FakeMotet())
        monkeypatch.setattr(deploy_mod, "_fetch_artifact", lambda *_args, **_kwargs: None)

        _reload_inner = cast(
            Callable[[ReloadBundleData], Any],
            getattr(bundle_reload.reload_bundle, "__wrapped__"),
        )
        with pytest.raises(CommandExecutionError) as exc_info:
            _reload_inner(
                ReloadBundleData(
                    bundle_id="calculator",
                    bundle_version="deadbeef",
                    targeting=None,
                    target_worker_id=None,
                )
            )

        exc = exc_info.value
        assert exc.error_type == "ArtifactNotFound"
        assert exc.command_type == "core.reload_bundle"
        assert exc.command_id == "cmd-reload-test"
        assert exc.recoverable is False


# ---------------------------------------------------------------------------
# Tests: shared underscore module freshness across reloads (#169)
# ---------------------------------------------------------------------------


def _write_helper_bundle(
    bundle_dir: Path,
    subpackage: str,
    *,
    marker: str,
    pass_extra: bool,
) -> None:
    """Write a bundle whose module in ``subpackage`` calls a shared ``_shared`` helper.

    ``marker`` identifies the helper revision. When ``pass_extra`` is set, both the
    helper signature and the call site use a second keyword — so loading a fresh
    caller against a stale helper raises ``TypeError``, which is exactly how #169
    presented in production.
    """
    (bundle_dir).mkdir(parents=True, exist_ok=True)
    (bundle_dir / "manifest.yaml").write_text(
        'format_version: "1"\nname: "helper-demo"\nversion: "0.0.1"\n'
        'description: "shared helper reload fixture"\n',
        encoding="utf-8",
    )

    sub = bundle_dir / subpackage
    sub.mkdir(parents=True, exist_ok=True)

    extra_param = ", b=None" if pass_extra else ""
    extra_body = ', "b": b' if pass_extra else ""
    (sub / "_shared.py").write_text(
        f'MARKER = "{marker}"\n\n\ndef describe(*, a{extra_param}):\n'
        f'    return {{"a": a, "marker": MARKER{extra_body}}}\n',
        encoding="utf-8",
    )

    call_kwargs = "a=1, b=2" if pass_extra else "a=1"
    (sub / "entry.py").write_text(
        "from . import _shared as h\n\n\ndef run():\n"
        f"    return h.describe({call_kwargs})\n",
        encoding="utf-8",
    )


def _loaded_entry(bundle_id: str, subpackage: str) -> Any:
    return sys.modules[f"bundle.{bundle_id}.{subpackage}.entry"]


class TestSharedHelperReloadFreshness:
    """A redeploy must execute fresh code for ``_``-prefixed shared modules.

    Regression guard for #169: a reload loaded new command modules while the
    worker kept the previous helper, surfacing as a ``TypeError`` about an
    argument the new caller passed and the stale helper did not accept.
    """

    @pytest.mark.parametrize("subpackage", ["commands", "tools", "routing"])
    def test_shared_helper_refreshes_on_reload(self, subpackage: str, tmp_path: Path) -> None:
        from motet.core.bundles.bundle_reload import _load_bundle

        bundle_id = "helper-demo"
        bundle_dir = tmp_path / bundle_id
        try:
            _write_helper_bundle(bundle_dir, subpackage, marker="v1", pass_extra=False)
            _load_bundle(bundle_id, bundle_dir, None)
            assert _loaded_entry(bundle_id, subpackage).run() == {"a": 1, "marker": "v1"}

            # Edit the shared helper's signature and the call site, then reload.
            _write_helper_bundle(bundle_dir, subpackage, marker="v2", pass_extra=True)
            _load_bundle(bundle_id, bundle_dir, None)

            # Stale helper here raises TypeError on the new keyword.
            assert _loaded_entry(bundle_id, subpackage).run() == {
                "a": 1,
                "b": 2,
                "marker": "v2",
            }
        finally:
            _cleanup_sys_modules(bundle_id)

    def test_reload_from_a_different_directory_uses_the_new_tree(self, tmp_path: Path) -> None:
        """``__path__`` is authoritative, not append-only.

        Two directories for one bundle id used to leave both on ``__path__``, so
        relative imports resolved against the older tree while module files
        loaded by absolute path from the newer one.
        """
        from motet.core.bundles.bundle_reload import _load_bundle

        bundle_id = "helper-demo"
        first = tmp_path / "first" / bundle_id
        second = tmp_path / "second" / bundle_id
        try:
            _write_helper_bundle(first, "commands", marker="old", pass_extra=False)
            _load_bundle(bundle_id, first, None)
            assert _loaded_entry(bundle_id, "commands").run()["marker"] == "old"

            _write_helper_bundle(second, "commands", marker="new", pass_extra=True)
            _load_bundle(bundle_id, second, None)

            pkg = sys.modules[f"bundle.{bundle_id}.commands"]
            assert list(pkg.__path__) == [str(second / "commands")]
            assert _loaded_entry(bundle_id, "commands").run()["marker"] == "new"
        finally:
            _cleanup_sys_modules(bundle_id)

    def test_purge_clears_every_subpackage_but_keeps_the_shared_root(self) -> None:
        from motet.core.bundles.bundle_reload import _purge_bundle_modules

        bundle_id = "purge-demo"
        keys = [
            f"bundle.{bundle_id}",
            f"bundle.{bundle_id}.commands",
            f"bundle.{bundle_id}.commands._shared",
            f"bundle.{bundle_id}.tools._shared",
            f"bundle.{bundle_id}.routing._shared",
        ]
        survivors = ["bundle", "bundle.other-demo.commands._shared"]
        for key in keys + survivors:
            sys.modules.setdefault(key, SimpleNamespace())  # type: ignore[arg-type]

        try:
            _purge_bundle_modules(bundle_id)
            assert [k for k in keys if k in sys.modules] == []
            for key in survivors:
                assert key in sys.modules
        finally:
            for key in keys + survivors:
                if key != "bundle":
                    sys.modules.pop(key, None)
