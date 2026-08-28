"""
Motet - Reasoning Regression Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-22

Description:
    Regression tests for reasoning-path failures observed in production traces:
    - Dropped tool_call_ids when a single assistant turn emits multiple tool calls.
    - Skill exec argument normalization.

Dependencies:
    - pytest: test framework
    - agentic_loop helper internals: tool-call filtering logic
    - agentic_loop helper internals: tool-call filtering logic

Usage:
    pytest tests/unit/core/test_reasoning_regressions.py

Notes:
    - These tests target pure helper behavior and avoid distributed runtime setup.
"""

from motet.core.types import tool_schema_name
from motet.core.reasoning.react.loop_discovery import (
    normalize_exec_and_catalog_parameters,
)
from motet.core.reasoning.react.loop_skills import (
    expose_activated_skill_runner_tools,
)
from motet.core.reasoning.react.loop_execution import (
    build_unique_tool_calls,
)
from motet.core.reasoning.react.agentic_loop_data import AgenticLoopData
from motet.core.types import Message
from motet.core.types import SkillRef


class _FakeMotet:
    """Minimal context object for helper-level tests."""

    tools = None
    function_discovery_store = None

    def stream_event(self, *_args, **_kwargs) -> None:  # noqa: ANN002, ANN003
        return


def test_agentic_loop_keeps_all_tool_calls_in_same_turn():
    """All tool calls from one assistant message must be preserved and executed."""
    data = AgenticLoopData(
        input="test",
        conversation_history=[Message(role="user", content="test")],
        max_tools=1,
        remaining_iterations=3,
        max_iterations=3,
        stream_key="task:test:response",
    )
    motet = _FakeMotet()
    tool_calls = [
        {
            "call_id": "core__web_search:0",
            "tool_name": "core.web_search",
            "arguments": {"query": "first"},
        },
        {
            "call_id": "core__web_search:1",
            "tool_name": "core.web_search",
            "arguments": {"query": "second"},
        },
    ]

    result = build_unique_tool_calls(
        tool_calls=tool_calls,
        data=data,
        motet=motet,
        current_iteration=1,
    )

    assert len(result.unique_tool_calls) == 2
    assert {c["tool_call_id"] for c in result.unique_tool_calls} == {
        "core__web_search:0",
        "core__web_search:1",
    }


# A parser regression test lived here: internal error strings like
# "LLM inference failed: ... (BadRequestError)" were parsed out of freeform
# model output and dispatched as fan-out work items. ADR-0138 removed the
# parser rather than the bug — core.spawn_agents takes a typed ``tasks`` list
# straight from the tool call, so there is no prose to misread.


def test_exec_normalization_maps_skill_id_bundle_and_script_path(monkeypatch):
    """Exec tool calls should resolve skill_id to bundle_id and deployed script path."""
    monkeypatch.setenv("MOTET_PLUGIN_ROOT", "/tmp/imf_bundles")
    data = AgenticLoopData(
        input="run the script",
        conversation_history=[Message(role="user", content="run the script")],
        skill_refs=[
            SkillRef(
                skill_id="basic-skill-example.basic-script-skill",
                bundle_id="basic-skill-example",
                name="basic-script-skill",
                source="bundle",
            )
        ],
    )
    calls = [
        {
            "tool_name": "core.worker_exec",
            "parameters": {
                "bundle_id": "basic-skill-example.basic-script-skill",
                "argv": [
                    "python",
                    "/work/skills/basic-script-skill/scripts/echo_payload.py",
                    "--text",
                    "bundle-skill-smoke",
                ],
            },
        }
    ]

    normalize_exec_and_catalog_parameters(calls, data)

    params = calls[0]["parameters"]
    assert params["bundle_id"] == "basic-skill-example"
    assert params["argv"][1] == "skills/basic-script-skill/scripts/echo_payload.py"


def test_catalog_normalization_maps_skill_id_to_bundle_id():
    """Bundle catalog lookup should translate skill_id input to bundle_id."""
    data = AgenticLoopData(
        input="show catalog",
        conversation_history=[Message(role="user", content="show catalog")],
        skill_refs=[
            SkillRef(
                skill_id="basic-skill-example.basic-script-skill",
                bundle_id="basic-skill-example",
                name="basic-script-skill",
                source="bundle",
            )
        ],
    )
    calls = [
        {
            "tool_name": "motet_admin.get_bundle_catalog",
            "parameters": {"bundle_id": "basic-skill-example.basic-script-skill"},
        }
    ]

    normalize_exec_and_catalog_parameters(calls, data)
    assert calls[0]["parameters"]["bundle_id"] == "basic-skill-example"


def test_activate_skill_result_exposes_runner_tools_for_next_iteration():
    """Runner tools returned by core.activate_skill are pinned and added to schemas."""
    from motet.core.tools import registry as tool_registry

    runner_tool_name = "activate-loop-bundle.pdf.check_fillable_fields"
    tool_registry.unregister(runner_tool_name)
    try:
        tool_registry.register(
            runner_tool_name,
            description="Check whether a PDF has fillable fields.",
            func=lambda _params: {"status": "success", "result": "ok"},
            category="shell",
        )
        data = AgenticLoopData(
            input="Use the PDF skill.",
            conversation_history=[Message(role="user", content="Use the PDF skill.")],
            tools=[],
        )
        motet = _FakeMotet()
        motet.tools = tool_registry

        exposed = expose_activated_skill_runner_tools(
            [
                {
                    "tool_call_id": "call_activate",
                    "tool_name": "core.activate_skill",
                    "parameters": {"name": "pdf"},
                }
            ],
            [
                {
                    "tool_call_id": "call_activate",
                    "tool_name": "core.activate_skill",
                    "status": "success",
                    "result": {
                        "status": "success",
                        "result": {
                            "skill_id": "activate-loop-bundle.pdf",
                            "tools": [{"name": runner_tool_name}],
                            "execution": {"tool": "core.workspace_shell_exec"},
                        },
                    },
                }
            ],
            data,
            motet,
        )

        assert exposed == [runner_tool_name, "core.workspace_shell_exec"]
        assert data.tool_filter_metadata is not None
        assert runner_tool_name in data.tool_filter_metadata["required_tools"]
        assert "core.workspace_shell_exec" in data.tool_filter_metadata["required_tools"]
        schema_names = {tool_schema_name(schema) for schema in data.tools or []}
        assert runner_tool_name in schema_names
        assert "core.workspace_shell_exec" in schema_names
    finally:
        tool_registry.unregister(runner_tool_name)


def test_exec_normalization_rewrites_non_python_script_path(monkeypatch):
    """Non-python exec calls should also rewrite bundle-local script paths."""
    monkeypatch.setenv("MOTET_PLUGIN_ROOT", "/tmp/imf_bundles")
    data = AgenticLoopData(
        input="run node script",
        conversation_history=[Message(role="user", content="run node script")],
        skill_refs=[
            SkillRef(
                skill_id="basic-skill-example.basic-script-skill",
                bundle_id="basic-skill-example",
                name="basic-script-skill",
                source="bundle",
            )
        ],
    )
    calls = [
        {
            "tool_name": "core.worker_exec",
            "parameters": {
                "bundle_id": "basic-skill-example.basic-script-skill",
                "argv": ["node", "skills/basic-script-skill/scripts/echo_payload.py"],
            },
        }
    ]

    normalize_exec_and_catalog_parameters(calls, data)
    params = calls[0]["parameters"]
    assert params["bundle_id"] == "basic-skill-example"
    assert params["argv"][1] == "skills/basic-script-skill/scripts/echo_payload.py"


def test_exec_normalization_drops_worker_plugin_root_cwd(monkeypatch):
    """Worker exec should normalize absolute plugin-root script paths to relative."""
    monkeypatch.setenv("MOTET_PLUGIN_ROOT", "/tmp/imf_bundles")
    data = AgenticLoopData(
        input="run script",
        conversation_history=[Message(role="user", content="run script")],
        skill_refs=[
            SkillRef(
                skill_id="basic-skill-example.basic-script-skill",
                bundle_id="basic-skill-example",
                name="basic-script-skill",
                source="bundle",
            )
        ],
    )
    calls = [
        {
            "tool_name": "core.worker_exec",
            "parameters": {
                "bundle_id": "basic-skill-example",
                "argv": [
                    "python",
                    "/tmp/imf_bundles/basic-skill-example/skills/basic-script-skill/scripts/echo_payload.py",
                ],
            },
        }
    ]

    normalize_exec_and_catalog_parameters(calls, data)
    params = calls[0]["parameters"]
    assert params["argv"][1] == "skills/basic-script-skill/scripts/echo_payload.py"


def test_exec_normalization_ignores_leftover_wire_format_exec_tool_name(monkeypatch):
    """Issue #225: leftover core__worker_exec is not an exec-tool alias."""
    monkeypatch.setenv("MOTET_PLUGIN_ROOT", "/tmp/imf_bundles")
    data = AgenticLoopData(
        input="run script",
        conversation_history=[Message(role="user", content="run script")],
        skill_refs=[
            SkillRef(
                skill_id="basic-skill-example.basic-script-skill",
                bundle_id="basic-skill-example",
                name="basic-script-skill",
                source="bundle",
            )
        ],
    )
    calls = [
        {
            "tool_name": "core__worker_exec",
            "parameters": {
                "argv": [
                    "python",
                    "/tmp/imf_bundles/basic-skill-example/skills/basic-script-skill/scripts/echo_payload.py",
                    "--text",
                    "bundle-skill-smoke",
                ],
            },
        }
    ]

    normalize_exec_and_catalog_parameters(calls, data)
    params = calls[0]["parameters"]
    assert "bundle_id" not in params
    assert params["argv"][1] == (
        "/tmp/imf_bundles/basic-skill-example/skills/basic-script-skill/scripts/echo_payload.py"
    )


def test_exec_normalization_coerces_skill_id_bundle_without_skill_refs(monkeypatch):
    """When skill_refs are empty, bundle_id shaped like skill_id still maps to bundle slug."""
    monkeypatch.setenv("MOTET_PLUGIN_ROOT", "/tmp/imf_bundles")
    data = AgenticLoopData(
        input="run",
        conversation_history=[Message(role="user", content="run")],
        skill_refs=[],
    )
    calls = [
        {
            "tool_name": "core.worker_exec",
            "parameters": {
                "bundle_id": "basic-skill-example.basic-script-skill",
                "argv": ["python", "skills/basic-script-skill/scripts/echo_payload.py"],
            },
        }
    ]
    normalize_exec_and_catalog_parameters(calls, data)
    params = calls[0]["parameters"]
    assert params["bundle_id"] == "basic-skill-example"


def test_catalog_normalization_coerces_skill_id_bundle_without_skill_refs():
    """Bundle catalog calls should coerce skill-shaped bundle_id even without skill_refs."""
    data = AgenticLoopData(
        input="catalog",
        conversation_history=[Message(role="user", content="catalog")],
        skill_refs=[],
    )
    calls = [
        {
            "tool_name": "motet_admin.get_bundle_catalog",
            "parameters": {"bundle_id": "basic-skill-example.basic-script-skill"},
        }
    ]
    normalize_exec_and_catalog_parameters(calls, data)
    assert calls[0]["parameters"]["bundle_id"] == "basic-skill-example"


def test_exec_normalization_fixes_plugin_path_missing_skills_segment(monkeypatch):
    """Deployed absolute paths must not drop the ``skills/`` prefix under the bundle root."""
    monkeypatch.setenv("MOTET_PLUGIN_ROOT", "/tmp/imf_bundles")
    data = AgenticLoopData(
        input="run",
        conversation_history=[Message(role="user", content="run")],
        skill_refs=[
            SkillRef(
                skill_id="basic-skill-example.basic-script-skill",
                bundle_id="basic-skill-example",
                name="basic-script-skill",
                source="bundle",
            )
        ],
    )
    calls = [
        {
            "tool_name": "core.worker_exec",
            "parameters": {
                "bundle_id": "basic-skill-example",
                "argv": [
                    "python",
                    "/tmp/imf_bundles/basic-skill-example/basic-script-skill/scripts/echo_payload.py",
                ],
            },
        }
    ]
    normalize_exec_and_catalog_parameters(calls, data)
    assert calls[0]["parameters"]["argv"][1] == (
        "skills/basic-script-skill/scripts/echo_payload.py"
    )


def test_exec_normalization_prefers_bundle_slug_inferred_from_argv(monkeypatch):
    """Argv paths under plugin root override a wrong bundle_id (e.g. skill name as slug)."""
    monkeypatch.setenv("MOTET_PLUGIN_ROOT", "/tmp/imf_bundles")
    data = AgenticLoopData(
        input="run",
        conversation_history=[Message(role="user", content="run")],
        skill_refs=[
            SkillRef(
                skill_id="basic-skill-example.basic-script-skill",
                bundle_id="basic-skill-example",
                name="basic-script-skill",
                source="bundle",
            )
        ],
    )
    calls = [
        {
            "tool_name": "core.worker_exec",
            "parameters": {
                "bundle_id": "basic-script-skill",
                "argv": [
                    "python",
                    "/tmp/imf_bundles/basic-skill-example/skills/basic-script-skill/scripts/echo_payload.py",
                ],
            },
        }
    ]
    normalize_exec_and_catalog_parameters(calls, data)
    params = calls[0]["parameters"]
    assert params["bundle_id"] == "basic-skill-example"
    assert params["argv"][1] == "skills/basic-script-skill/scripts/echo_payload.py"
