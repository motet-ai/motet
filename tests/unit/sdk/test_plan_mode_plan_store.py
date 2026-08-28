"""
Unit tests for plan-mode bundle plan_store helpers (#173B).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

PLAN_STORE_PATH = (
    Path(__file__).resolve().parents[3]
    / "motet-sdk"
    / "examples"
    / "bundles"
    / "plan-mode"
    / "tools"
    / "_plan_store.py"
)


def _load_plan_store():
    """Load plan_store.py without requiring the full bundle package on sys.path."""
    name = "plan_mode_plan_store_under_test"
    spec = importlib.util.spec_from_file_location(name, PLAN_STORE_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def store():
    mod = _load_plan_store()
    mod.clear_fallback_store()
    yield mod
    mod.clear_fallback_store()


def test_render_markdown_checkboxes(store):
    PlanDocument = store.PlanDocument
    PlanTodo = store.PlanTodo
    plan = PlanDocument(
        goal="Ship dark mode",
        summary="Add theme toggle.",
        todos=[
            PlanTodo(id="t1", title="Add setting", status="completed"),
            PlanTodo(id="t2", title="Wire CSS", status="in_progress"),
            PlanTodo(id="t3", title="Tests", status="pending"),
            PlanTodo(id="t4", title="Docs", status="cancelled", notes="defer"),
        ],
        files=["ui/theme.ts"],
        acceptance=["Toggle persists"],
        open_questions=[],
        updated_at="2026-08-05T00:00:00+00:00",
    )
    md = store.render_markdown(plan)
    assert "**Goal:** Ship dark mode" in md
    assert "[x] `t1`" in md
    assert "[~] `t2`" in md
    assert "[ ] `t3`" in md
    assert "[-] `t4`" in md
    assert "ui/theme.ts" in md


def test_update_todo_demotes_other_in_progress(store):
    PlanDocument = store.PlanDocument
    PlanTodo = store.PlanTodo
    plan = PlanDocument(
        todos=[
            PlanTodo(id="t1", title="A", status="in_progress"),
            PlanTodo(id="t2", title="B", status="pending"),
        ]
    )
    updated = store.apply_update_todo(plan, "t2", status="in_progress")
    by_id = {t.id: t for t in updated.todos}
    assert by_id["t1"].status == "pending"
    assert by_id["t2"].status == "in_progress"
    assert updated.updated_at


def test_save_load_fallback_without_redis(store):
    PlanDocument = store.PlanDocument
    PlanTodo = store.PlanTodo

    class _Ctx:
        conversation_id = "conv-test-1"
        redis = None

        def resolve_conversation_id(self, explicit_id=None):
            return explicit_id or self.conversation_id

    ctx = _Ctx()
    plan = PlanDocument(
        goal="g",
        summary="s",
        todos=[PlanTodo(id="t1", title="one")],
    )
    key = store.save_plan(ctx, plan)
    assert key.endswith("conv-test-1")
    loaded = store.load_plan(ctx)
    assert loaded is not None
    assert loaded.goal == "g"
    assert loaded.todos[0].title == "one"


def test_apply_update_plan_replaces_todos(store):
    PlanDocument = store.PlanDocument
    PlanTodo = store.PlanTodo
    plan = PlanDocument(todos=[PlanTodo(id="t1", title="old")])
    updated = store.apply_update_plan(
        plan,
        summary="new summary",
        todos=[{"id": "t1", "title": "new", "status": "pending"}],
    )
    assert updated.summary == "new summary"
    assert updated.todos[0].title == "new"


def test_render_markdown_includes_approval(store):
    PlanDocument = store.PlanDocument
    PlanTodo = store.PlanTodo
    plan = PlanDocument(
        goal="g",
        todos=[PlanTodo(id="t1", title="one")],
        approval_status="draft",
    )
    md = store.render_markdown(plan)
    assert "**Approval:** draft — awaiting approval" in md


def test_apply_approval_and_require_gate(store):
    PlanDocument = store.PlanDocument
    PlanTodo = store.PlanTodo
    plan = PlanDocument(
        todos=[PlanTodo(id="t1", title="one")],
        approval_status="draft",
    )
    assert store.require_approved_for_build(plan) is not None

    approved = store.apply_approval(plan, "approved")
    assert approved.approval_status == "approved"
    assert store.require_approved_for_build(approved) is None

    rejected = store.apply_approval(plan, "rejected")
    assert rejected.approval_status == "rejected"
    err = store.require_approved_for_build(rejected)
    assert err is not None
    assert "rejected" in err.lower()


def test_update_plan_resets_approval_to_draft(store):
    PlanDocument = store.PlanDocument
    PlanTodo = store.PlanTodo
    plan = PlanDocument(
        todos=[PlanTodo(id="t1", title="one")],
        approval_status="approved",
    )
    updated = store.apply_update_plan(plan, summary="revised")
    assert updated.summary == "revised"
    assert updated.approval_status == "draft"


def test_load_plan_defaults_missing_approval_status(store):
    class _Ctx:
        conversation_id = "conv-legacy"
        redis = None

        def resolve_conversation_id(self, explicit_id=None):
            return explicit_id or self.conversation_id

    ctx = _Ctx()
    # Simulate a pre-approval payload without approval_status.
    key = store.conversation_key("conv-legacy")
    store._FALLBACK_STORE[key] = (
        '{"version":1,"goal":"g","summary":"s","todos":[{"id":"t1","title":"one"}],'
        '"files":[],"acceptance":[],"open_questions":[],"updated_at":""}'
    )
    loaded = store.load_plan(ctx)
    assert loaded is not None
    assert loaded.approval_status == "draft"
    assert loaded.todos[0].id == "t1"
    assert loaded.latest_artifact_id == ""


def test_snapshot_plan_artifact_via_commands_run(store):
    PlanDocument = store.PlanDocument
    PlanTodo = store.PlanTodo

    class _Commands:
        def run(self, command_type, data=None, **kwargs):
            assert command_type == "core.create_artifact"
            assert data["content_type"] == "text/markdown"
            assert data["kind"] == "tool_artifact"
            assert b"**Goal:**" in data["payload"]
            tags = data["metadata"]["artifact_tags"]
            assert "plan-mode" in tags
            assert "plan-draft" in tags
            assert "plan-snapshot-write" in tags
            return {"artifact_id": "art-from-commands"}

    class _Ctx:
        conversation_id = "conv-snap-1"
        redis = None
        commands = _Commands()
        artifact_store = None

        def resolve_conversation_id(self, explicit_id=None):
            return explicit_id or self.conversation_id

    plan = PlanDocument(
        goal="Ship it",
        summary="Do the thing.",
        todos=[PlanTodo(id="t1", title="one")],
        approval_status="draft",
    )
    aid = store.snapshot_plan_artifact(_Ctx(), plan, reason="write")
    assert aid == "art-from-commands"


def test_snapshot_plan_artifact_falls_back_to_store_put(store):
    PlanDocument = store.PlanDocument
    PlanTodo = store.PlanTodo

    class _Store:
        def put(self, **kwargs):
            assert kwargs["content_type"] == "text/markdown"
            assert kwargs["kind"] == "tool_artifact"
            assert "plan-mode" in kwargs["metadata"]["tags"]
            return "art-from-put"

    class _Ctx:
        conversation_id = "conv-snap-2"
        redis = None
        commands = None
        artifact_store = _Store()

        def resolve_conversation_id(self, explicit_id=None):
            return explicit_id or self.conversation_id

    plan = PlanDocument(
        todos=[PlanTodo(id="t1", title="one")],
        approval_status="approved",
    )
    aid = store.snapshot_plan_artifact(_Ctx(), plan, reason="approve")
    assert aid == "art-from-put"


def test_save_plan_with_artifact_records_latest_id(store):
    PlanDocument = store.PlanDocument
    PlanTodo = store.PlanTodo

    class _Store:
        def put(self, **kwargs):
            return "art-latest-1"

    class _Ctx:
        conversation_id = "conv-snap-3"
        redis = None
        commands = None
        artifact_store = _Store()

        def resolve_conversation_id(self, explicit_id=None):
            return explicit_id or self.conversation_id

    ctx = _Ctx()
    plan = PlanDocument(
        goal="g",
        todos=[PlanTodo(id="t1", title="one")],
        approval_status="draft",
    )
    saved, key, artifact_id = store.save_plan_with_artifact(
        ctx, plan, snapshot_reason="write"
    )
    assert artifact_id == "art-latest-1"
    assert saved.latest_artifact_id == "art-latest-1"
    assert key.endswith("conv-snap-3")
    loaded = store.load_plan(ctx)
    assert loaded is not None
    assert loaded.latest_artifact_id == "art-latest-1"


def test_save_plan_with_artifact_survives_missing_store(store):
    PlanDocument = store.PlanDocument
    PlanTodo = store.PlanTodo

    class _Ctx:
        conversation_id = "conv-snap-4"
        redis = None
        commands = None
        artifact_store = None

        def resolve_conversation_id(self, explicit_id=None):
            return explicit_id or self.conversation_id

    ctx = _Ctx()
    plan = PlanDocument(todos=[PlanTodo(id="t1", title="one")])
    saved, key, artifact_id = store.save_plan_with_artifact(
        ctx, plan, snapshot_reason="write"
    )
    assert artifact_id is None
    assert saved.latest_artifact_id == ""
    assert store.load_plan(ctx) is not None
    assert key.endswith("conv-snap-4")
