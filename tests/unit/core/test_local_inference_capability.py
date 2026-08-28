"""
Motet - Local Inference Capability Advertisement Tests

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-03

Description:
    Unit tests for presence-aware ``local_inference`` capability advertisement
    (ADR-0104 Open Q10). Verifies that a worker only advertises ``local_inference``
    when a local model is truly usable, and that both cloud and edge workers
    advertise it when one is.

Dependencies:
    - pytest: Test framework
    - motet.core.workers.tasks: capability detection under test

Usage:
    pytest tests/unit/core/test_local_inference_capability.py

Notes:
    - File-presence is exercised via MOTET_LOCAL_MODEL_PATHS pointing at a tmp file,
      so the test does not depend on real (gitignored) GGUF weights.
"""

import json

import pytest

from motet.core.commands.capabilities import WorkerCapability
from motet.core.workers import tasks
from motet.core.workers.tasks import (
    _available_local_models,
    _detect_worker_capabilities,
    _should_advertise_local_inference,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in (
        "MOTET_LOCAL_INFERENCE_ENABLED",
        "MOTET_GPU_ENABLED",
        "MOTET_LOCAL_MODEL_PATHS",
        "MOTET_EDGE_WORKER_ID",
        "MOTET_WORKER_ID",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MOTET_WORKER_ID", "cloud_worker")
    yield


def test_available_local_models_only_returns_existing(monkeypatch, tmp_path):
    present = tmp_path / "phi-4-mini.gguf"
    present.write_bytes(b"gguf")
    monkeypatch.setenv(
        "MOTET_LOCAL_MODEL_PATHS",
        json.dumps({
            "phi-4-mini": str(present),
            "ghost-model": str(tmp_path / "does-not-exist.gguf"),
        }),
    )

    available = _available_local_models()

    assert available == ["phi-4-mini"]


def test_not_advertised_when_disabled():
    assert _should_advertise_local_inference() is False


def test_not_advertised_when_enabled_but_no_models_and_no_gpu(monkeypatch):
    monkeypatch.setenv("MOTET_LOCAL_INFERENCE_ENABLED", "true")
    monkeypatch.setattr(tasks, "_available_local_models", lambda: [])
    monkeypatch.setattr("motet.core.workers.hardware_detection.has_gpu", lambda: False)

    assert _should_advertise_local_inference() is False


def test_advertised_when_enabled_and_model_present(monkeypatch):
    monkeypatch.setenv("MOTET_LOCAL_INFERENCE_ENABLED", "true")
    monkeypatch.setattr(tasks, "_available_local_models", lambda: ["phi-4-mini"])

    assert _should_advertise_local_inference() is True


def test_advertised_when_enabled_and_gpu_present_without_files(monkeypatch):
    monkeypatch.setenv("MOTET_GPU_ENABLED", "true")
    monkeypatch.setattr(tasks, "_available_local_models", lambda: [])
    monkeypatch.setattr("motet.core.workers.hardware_detection.has_gpu", lambda: True)

    assert _should_advertise_local_inference() is True


def test_cloud_worker_advertises_local_inference_when_available(monkeypatch):
    monkeypatch.setattr(tasks, "_should_advertise_local_inference", lambda: True)

    caps = _detect_worker_capabilities(stack=object(), available_tools=[])

    assert "local_inference" in caps
    # Advertised value is the canonical WorkerCapability enum member (not a bare magic string).
    assert WorkerCapability.LOCAL_INFERENCE.value == "local_inference"
    assert WorkerCapability.LOCAL_INFERENCE in caps


def test_cloud_worker_omits_local_inference_when_unavailable(monkeypatch):
    monkeypatch.setattr(tasks, "_should_advertise_local_inference", lambda: False)

    caps = _detect_worker_capabilities(stack=object(), available_tools=[])

    assert "local_inference" not in caps


def test_edge_worker_advertises_local_inference_when_available(monkeypatch):
    monkeypatch.setenv("MOTET_EDGE_WORKER_ID", "edge_test_worker")
    monkeypatch.setattr(tasks, "_should_advertise_local_inference", lambda: True)

    caps = _detect_worker_capabilities(stack=object(), available_tools=[])

    assert "edge_execution" in caps
    assert "local_inference" in caps


def test_edge_worker_omits_local_inference_when_unavailable(monkeypatch):
    monkeypatch.setenv("MOTET_EDGE_WORKER_ID", "edge_test_worker")
    monkeypatch.setattr(tasks, "_should_advertise_local_inference", lambda: False)

    caps = _detect_worker_capabilities(stack=object(), available_tools=[])

    assert "edge_execution" in caps
    assert "local_inference" not in caps
