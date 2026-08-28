"""
Motet - unit tests for readiness empty-capability recovery (#151)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-03

Description:
    Verifies that workers are not marked ready with empty/fallback capabilities,
    and that a successful worker-context rebuild rewrites Redis readiness.

Dependencies:
    - pytest
    - unittest.mock
"""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import MagicMock, patch


def _fallback_context() -> Dict[str, Any]:
    return {
        "worker_id": "cloud_worker1",
        "pool_type": "gevent",
        "capabilities": [],
        "tool_count": 0,
        "tool_registry": None,
        "error": "Timeout reading from socket",
    }


def _good_context(*, tool_count: int = 188) -> Dict[str, Any]:
    registry = MagicMock()
    registry.list_items.return_value = {
        "core.agent_list": MagicMock(),
        "mcp.google_workspace.list_docs_in_folder": MagicMock(),
    }
    return {
        "worker_id": "cloud_worker1",
        "pool_type": "gevent",
        "capabilities": [
            "distributed_command_processing",
            "reasoning",
            "tool_execution",
        ],
        "tool_count": tool_count,
        "tool_registry": registry,
        "local_inference_client": None,
    }


class TestUsableWorkerContext:
    def test_fallback_context_not_usable(self) -> None:
        from motet.core.workers.parent_coordinator import _is_usable_worker_context

        assert _is_usable_worker_context(_fallback_context()) is False

    def test_empty_capabilities_not_usable(self) -> None:
        from motet.core.workers.parent_coordinator import _is_usable_worker_context

        assert _is_usable_worker_context({"capabilities": []}) is False

    def test_good_context_usable(self) -> None:
        from motet.core.workers.parent_coordinator import _is_usable_worker_context

        assert _is_usable_worker_context(_good_context()) is True


class TestPublishWorkerReadinessFromContext:
    def test_fallback_registers_but_does_not_mark_ready(self) -> None:
        from motet.core.workers.parent_coordinator import (
            publish_worker_readiness_from_context,
        )

        readiness = MagicMock()
        with patch(
            "motet.core.distributed.worker_readiness.get_readiness_service",
            return_value=readiness,
        ), patch(
            "motet.core.workers.parent_coordinator.get_celery_concurrency_from_args",
            return_value=4,
        ):
            result = publish_worker_readiness_from_context(
                "cloud_worker1", _fallback_context()
            )

        assert result["registered"] is True
        assert result["marked_ready"] is False
        readiness.register_worker.assert_called_once()
        assert readiness.register_worker.call_args.kwargs["capabilities"] == []
        readiness.mark_worker_ready.assert_not_called()

    def test_good_context_registers_and_marks_ready(self) -> None:
        from motet.core.workers.parent_coordinator import (
            publish_worker_readiness_from_context,
        )

        readiness = MagicMock()
        context = _good_context()
        with patch(
            "motet.core.distributed.worker_readiness.get_readiness_service",
            return_value=readiness,
        ), patch(
            "motet.core.workers.parent_coordinator.get_celery_concurrency_from_args",
            return_value=4,
        ):
            result = publish_worker_readiness_from_context("cloud_worker1", context)

        assert result["marked_ready"] is True
        assert "tool_execution" in result["capabilities"]
        readiness.register_worker.assert_called_once()
        readiness.mark_worker_ready.assert_called_once()
        assert readiness.mark_worker_ready.call_args.kwargs["tool_count"] == 188
        assert readiness.mark_worker_ready.call_args.kwargs["mcp_tool_count"] == 1


class TestInitializeParentCoordinationReadyGuard:
    def test_fallback_context_does_not_mark_ready(self) -> None:
        from motet.core.workers import parent_coordinator

        parent_coordinator._parent_coordination_initialized = False
        readiness = MagicMock()

        with patch.object(
            parent_coordinator, "is_celery_parent_process", return_value=True
        ), patch(
            "motet.core.workers.tasks._create_worker_context",
            return_value=_fallback_context(),
        ), patch(
            "motet.core.distributed.worker_readiness.get_readiness_service",
            return_value=readiness,
        ), patch.object(
            parent_coordinator, "_start_parent_heartbeat"
        ), patch.object(
            parent_coordinator, "_start_parent_health_check"
        ), patch.object(
            parent_coordinator, "_start_parent_cleanup"
        ), patch.object(
            parent_coordinator, "_start_thread_health_monitor"
        ), patch.object(
            parent_coordinator, "get_celery_concurrency_from_args", return_value=4
        ):
            result = parent_coordinator.initialize_parent_coordination("cloud_worker1")

        try:
            assert result["status"] == "initialized"
            assert result["marked_ready"] is False
            readiness.mark_worker_ready.assert_not_called()
            readiness.register_worker.assert_called_once()
        finally:
            parent_coordinator._parent_coordination_initialized = False


class TestInitializeWorkerContextReadinessRewrite:
    def test_successful_init_rewrites_redis_capabilities(self) -> None:
        from motet.core.workers import worker_initialization

        good = _good_context()
        publish = MagicMock(
            return_value={
                "registered": True,
                "marked_ready": True,
                "capabilities": good["capabilities"],
                "tool_count": good["tool_count"],
                "mcp_tool_count": 1,
            }
        )

        with patch(
            "motet.core.workers.tasks._clear_worker_context_cache"
        ), patch(
            "motet.core.workers.tasks._create_worker_context",
            return_value=good,
        ), patch(
            "motet.core.bundles.bundle_reload.load_bundles_on_startup",
            return_value=0,
        ), patch(
            "motet.core.workers.worker_initialization._start_event_observer_consumer"
        ), patch(
            "motet.core.workers.worker_utils.get_worker_id",
            return_value="cloud_worker1",
        ), patch(
            "motet.core.workers.parent_coordinator.publish_worker_readiness_from_context",
            publish,
        ):
            result = worker_initialization.initialize_worker_context("threads")

        assert result["status"] == "success"
        assert result["readiness_marked_ready"] is True
        publish.assert_called_once_with("cloud_worker1", good)

    def test_publish_failure_does_not_fail_context_init(self) -> None:
        from motet.core.workers import worker_initialization

        good = _good_context()
        with patch(
            "motet.core.workers.tasks._clear_worker_context_cache"
        ), patch(
            "motet.core.workers.tasks._create_worker_context",
            return_value=good,
        ), patch(
            "motet.core.bundles.bundle_reload.load_bundles_on_startup",
            return_value=0,
        ), patch(
            "motet.core.workers.worker_initialization._start_event_observer_consumer"
        ), patch(
            "motet.core.workers.worker_utils.get_worker_id",
            return_value="cloud_worker1",
        ), patch(
            "motet.core.workers.parent_coordinator.publish_worker_readiness_from_context",
            side_effect=RuntimeError("redis down"),
        ):
            result = worker_initialization.initialize_worker_context("threads")

        assert result["status"] == "success"
        assert result["context"] is good


class TestReRegisterWorkerReadyGuard:
    def test_re_register_returns_false_when_only_fallback_available(self) -> None:
        from motet.core.workers import parent_coordinator

        readiness = MagicMock()
        with patch(
            "motet.core.workers.tasks._create_worker_context",
            return_value=_fallback_context(),
        ), patch(
            "motet.core.workers.tasks._clear_worker_context_cache"
        ) as clear_cache, patch(
            "motet.core.distributed.worker_readiness.get_readiness_service",
            return_value=readiness,
        ), patch.object(
            parent_coordinator, "get_celery_concurrency_from_args", return_value=4
        ):
            ok = parent_coordinator._re_register_worker("cloud_worker1")

        assert ok is False
        clear_cache.assert_called_once()
        readiness.mark_worker_ready.assert_not_called()
