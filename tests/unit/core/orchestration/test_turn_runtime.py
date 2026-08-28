"""
Motet - Turn Runtime Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-27

Description:
    ADR-0134 Turn Runtime allowlist and facade-safe helpers: only
    ``runtime/persist.py`` may call store_turn_checkpoint, openai_compat must
    not import checkpoints, hosted_tools dispatches ``core.agent_loop`` (no facade
    ownership loop). Phase 5: ``start`` wraps the loop as TurnResult;
    ``resume_turn`` also returns TurnResult. ``agent_turn`` must not
    ``motet.do(agent_loop)``, import
    ``run_agentic_loop``, or unwrap via ``_runtime_payload``.

Dependencies:
    - ast: call-site scan of motet/ Python sources
    - motet.core.orchestration.turn.runtime: system under test

Usage:
    pytest tests/unit/core/orchestration/test_turn_runtime.py
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from motet.core.orchestration.turn.runtime.result import ResumeHandle
from motet.core.orchestration.turn.runtime import resolve_resume


REPO_ROOT = Path(__file__).resolve().parents[4]
MOTET_ROOT = REPO_ROOT / "motet"
ALLOWED_STORE_CALL_FILES = {
    MOTET_ROOT / "core" / "orchestration" / "turn" / "runtime" / "persist.py",
}
ALLOWED_STORE_DEF_FILES = {
    MOTET_ROOT / "core" / "checkpoints" / "checkpoint.py",
}


def _store_turn_checkpoint_nodes(tree: ast.AST) -> tuple[list[ast.Call], list[ast.FunctionDef]]:
    calls: list[ast.Call] = []
    defs: list[ast.FunctionDef] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "store_turn_checkpoint":
                calls.append(node)
            elif isinstance(func, ast.Attribute) and func.attr == "store_turn_checkpoint":
                calls.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name == "store_turn_checkpoint":
            defs.append(node)
    return calls, defs


def test_store_turn_checkpoint_writer_allowlist() -> None:
    """Only Turn Runtime may call store_turn_checkpoint (ADR-0134 Phase 0)."""
    stray_calls: list[str] = []
    stray_defs: list[str] = []
    for path in MOTET_ROOT.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        calls, defs = _store_turn_checkpoint_nodes(tree)
        if calls and path.resolve() not in {p.resolve() for p in ALLOWED_STORE_CALL_FILES}:
            stray_calls.append(str(path.relative_to(REPO_ROOT)))
        if defs and path.resolve() not in {p.resolve() for p in ALLOWED_STORE_DEF_FILES}:
            stray_defs.append(str(path.relative_to(REPO_ROOT)))
    assert stray_calls == [], f"store_turn_checkpoint call sites outside runtime: {stray_calls}"
    assert stray_defs == [], f"store_turn_checkpoint defined outside checkpoints: {stray_defs}"


def test_openai_compat_execution_does_not_import_checkpoints() -> None:
    from motet.interfaces.api.openai_compat import execution

    source = inspect.getsource(execution)
    assert "motet.core.checkpoints" not in source
    assert "from ....core.checkpoints" not in source


def test_agentic_loop_does_not_import_checkpoints() -> None:
    import motet.core.reasoning.react.agentic_loop as agentic_loop_module

    source = inspect.getsource(agentic_loop_module)
    assert "motet.core.checkpoints" not in source


def test_agent_turn_does_not_import_run_agentic_loop() -> None:
    source = (
        MOTET_ROOT / "core" / "orchestration" / "turn" / "agent_turn.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert "run_agentic_loop" not in names
    do_targets = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "do":
            continue
        value = node.func.value
        if isinstance(value, ast.Name) and value.id == "motet" and node.args:
            arg0 = node.args[0]
            if isinstance(arg0, ast.Name):
                do_targets.append(arg0.id)
    assert "agent" not in do_targets
    assert "agent_loop" not in do_targets
    assert "adaptive_reasoning" not in do_targets
    assert "run_adaptive_reasoning" not in names
    assert "run_agent" in names
    assert "_runtime_payload" not in source
    assert "TurnResultKind" in source


def test_start_wraps_loop_payload(monkeypatch) -> None:
    from motet.core.orchestration.turn.runtime import start
    from motet.core.orchestration.turn.runtime.result import TurnResult, TurnResultKind

    payload = {"final_response": "ok", "stop_reason": "stop"}
    monkeypatch.setattr(
        "motet.core.reasoning.react.loop_driver.run_agentic_loop",
        lambda _motet, _data: payload,
    )
    result = start(object(), object())
    assert isinstance(result, TurnResult)
    assert result.kind is TurnResultKind.COMPLETE
    assert result.payload["final_response"] == "ok"


def test_continue_after_budget_returns_none_on_miss(monkeypatch) -> None:
    from motet.core.orchestration.turn.runtime import continue_after_budget

    monkeypatch.setattr(
        "motet.core.orchestration.turn.budget_continue.try_build_budget_continue_loop_data",
        lambda *_args, **_kwargs: None,
    )
    assert (
        continue_after_budget(
            object(),
            history=[],
            stream_key="task:t:response",
            max_iterations=20,
        )
        is None
    )


def test_continue_after_budget_starts_when_snapshot_exists(monkeypatch) -> None:
    from types import SimpleNamespace

    from motet.core.orchestration.turn.runtime import continue_after_budget
    from motet.core.orchestration.turn.runtime.result import TurnResult, TurnResultKind

    history = [{"role": "user", "content": "keep going"}]
    loop_data = SimpleNamespace(
        conversation_history=history,
        model_provider=None,
        model_name=None,
        model_profile_name=None,
        enable_thinking=None,
        reasoning_effort=None,
        tool_filter_metadata=None,
    )
    monkeypatch.setattr(
        "motet.core.orchestration.turn.budget_continue.try_build_budget_continue_loop_data",
        lambda *_args, **_kwargs: loop_data,
    )
    monkeypatch.setattr(
        "motet.core.reasoning.react.loop_driver.run_agentic_loop",
        lambda _motet, _data: {"final_response": "ok", "stop_reason": "stop"},
    )
    result = continue_after_budget(
        object(),
        history=[],
        stream_key="task:t:response",
        max_iterations=20,
        model_provider="openai",
        model_name="gpt-4o-mini",
    )
    assert isinstance(result, TurnResult)
    assert result.kind is TurnResultKind.COMPLETE
    assert result.conversation_history == history
    assert loop_data.model_provider == "openai"
    assert loop_data.model_name == "gpt-4o-mini"


def test_resume_turn_returns_turn_result() -> None:
    from typing import get_type_hints

    from motet.core.orchestration.turn.runtime import resume_turn
    from motet.core.orchestration.turn.runtime.result import TurnResult

    assert get_type_hints(resume_turn)["return"] is TurnResult
    source = (
        MOTET_ROOT / "core" / "orchestration" / "turn" / "resume_agent_turn.py"
    ).read_text(encoding="utf-8")
    assert "TurnResultKind" in source
    assert "coerce_turn_result" in source
    facade = (
        MOTET_ROOT / "interfaces" / "api" / "openai_compat" / "execution.py"
    ).read_text(encoding="utf-8")
    assert "TurnResultKind" in facade
    assert "turn.kind is TurnResultKind.SUSPENDED" in facade


def test_hosted_tools_dispatches_agent_not_facade_loop() -> None:
    """Phase 4: hosted_tools has no second ownership loop (ADR-0134)."""
    from motet.interfaces.api.openai_compat import execution

    source = inspect.getsource(execution)
    assert "thin_turn" not in source
    assert "from motet.core.reasoning.react.agent import agent_loop" in source
    assert "persist_facade_handback" not in source
    assert "hosted_turn_action" not in source
    assert "_persist_hosted_handback" not in source
    assert "_hosted_turn_decision" not in source
    assert "_append_hosted_observations" not in source


def test_hosted_tools_skips_motet_system_prompt() -> None:
    """An allowlist turn gets the client's prompt, not Motet's fallback.

    ``inject_meta_tools`` gates Motet's fallback system prompt.
    """
    from motet.core.reasoning.react.agentic_loop import _is_motet_owned_turn
    from motet.core.reasoning.react.agentic_loop_data import AgenticLoopData

    hosted = AgenticLoopData(input="x", inject_meta_tools=False)
    chat = AgenticLoopData(agent_id="cursor.backend", input="x")
    assert _is_motet_owned_turn(hosted) is False
    assert _is_motet_owned_turn(chat) is True

    source = (
        MOTET_ROOT / "core" / "reasoning" / "react" / "agentic_loop.py"
    ).read_text(encoding="utf-8")
    assert "if not has_system_prompt and _is_motet_owned_turn(data):" in source
    # The injection this flag was named for must not come back. The name still
    # appears in docstrings recording the deletion, so match the code shape.
    assert "_ensure_meta_tools_in_schemas" not in source
    assert 'name="escalate_reasoning"' not in source
    assert "data.tools = _ensure_meta_tools" not in source


def test_hosted_tools_builder_does_not_stamp_owning_agent() -> None:
    """hosted_tools is not cursor.backend (or any registry agent)."""
    from types import SimpleNamespace

    from motet.core.reasoning.react.agent import build_agent_loop_data
    from motet.core.reasoning.react.agent_data import AgentData
    from motet.interfaces.api.openai_compat.execution import HOSTED_TOOLS_LOOP_ID

    loop = build_agent_loop_data(
        SimpleNamespace(task_id="t1"),
        AgentData(
            agent_id=HOSTED_TOOLS_LOOP_ID,
            input="list tools",
            inject_meta_tools=False,
            tools=[],
        ),
    )
    assert loop.agent_id is None
    assert loop.parent_agent_id is None
    assert loop.inject_meta_tools is False
    assert loop.tools == []


def test_build_agent_loop_data_forwards_parent_agent_id() -> None:
    """Nested identity is a field, not a substring of agent_id."""
    from types import SimpleNamespace

    from motet.core.reasoning.react.agent import build_agent_loop_data
    from motet.core.reasoning.react.agent_data import AgentData

    loop = build_agent_loop_data(
        SimpleNamespace(task_id="t1"),
        AgentData(
            agent_id="researcher",
            parent_agent_id="core.default",
            input="price Aurora",
        ),
    )
    assert loop.agent_id == "researcher"
    assert loop.parent_agent_id == "core.default"


def test_build_agent_loop_data_use_task_stream_keeps_parent_key() -> None:
    from types import SimpleNamespace

    from motet.core.reasoning.react.agent import build_agent_loop_data
    from motet.core.reasoning.react.agent_data import AgentData

    loop = build_agent_loop_data(
        SimpleNamespace(task_id="t1"),
        AgentData(
            agent_id="core.default.spawn-1",
            use_task_stream=True,
            base_stream_key="task:t1:response",
            input="price Aurora",
        ),
    )
    assert loop.stream_key == "task:t1:response"


def test_build_agent_loop_data_without_task_stream_scopes_the_key() -> None:
    from types import SimpleNamespace

    from motet.core.reasoning.react.agent import build_agent_loop_data
    from motet.core.reasoning.react.agent_data import AgentData

    loop = build_agent_loop_data(
        SimpleNamespace(task_id="t1"),
        AgentData(
            agent_id="core.default.spawn-1",
            use_task_stream=False,
            base_stream_key="task:t1:response",
            input="price Aurora",
        ),
    )
    assert loop.stream_key == "task:t1:response:core.default.spawn-1"


def test_stamp_stream_agent_identity_copies_parent_metadata() -> None:
    from types import SimpleNamespace

    from motet.core.reasoning.react.agent import _stamp_stream_agent_identity
    from motet.core.reasoning.react.agent_data import AgentData

    parent_meta = {"agent_id": "core.default", "model_name": "gpt-4.1"}
    distributed = SimpleNamespace(metadata=parent_meta)
    motet_ctx = SimpleNamespace(
        metadata=parent_meta,
        _command=SimpleNamespace(distributed_context=distributed),
    )
    _stamp_stream_agent_identity(
        motet_ctx,
        AgentData(
            agent_id="core.default.spawn-1",
            parent_agent_id="core.default",
            input="price Aurora",
        ),
    )
    assert distributed.metadata["agent_id"] == "core.default.spawn-1"
    assert distributed.metadata["parent_agent_id"] == "core.default"
    assert distributed.metadata["model_name"] == "gpt-4.1"
    assert parent_meta["agent_id"] == "core.default"
    assert distributed.metadata is not parent_meta


def test_stamp_stream_agent_identity_skips_hosted_tools() -> None:
    from types import SimpleNamespace

    from motet.core.reasoning.react.agent import _stamp_stream_agent_identity
    from motet.core.reasoning.react.agent_data import AgentData

    parent_meta = {"agent_id": "core.default"}
    distributed = SimpleNamespace(metadata=parent_meta)
    motet_ctx = SimpleNamespace(
        metadata=parent_meta,
        _command=SimpleNamespace(distributed_context=distributed),
    )
    _stamp_stream_agent_identity(
        motet_ctx,
        AgentData(
            agent_id="hosted_tools",
            inject_meta_tools=False,
            input="hello",
        ),
    )
    assert distributed.metadata is parent_meta
    assert parent_meta["agent_id"] == "core.default"


def test_resolve_resume_returns_handle_not_checkpoint(monkeypatch) -> None:
    class _Checkpoint:
        checkpoint_id = "suspend-abc"
        conversation_id = "conv-1"
        handed_back_tool_calls = [{"tool_call_id": "c1"}]

    monkeypatch.setattr(
        "motet.core.checkpoints.resolve_resume_checkpoint",
        lambda **_kwargs: _Checkpoint(),
    )
    handle = resolve_resume(tenant_id="t", motet_id="m", tool_call_ids=["c1"])
    assert isinstance(handle, ResumeHandle)
    assert handle.checkpoint_id == "suspend-abc"
    assert handle.conversation_id == "conv-1"
    assert not hasattr(handle, "handed_back_tool_calls")
