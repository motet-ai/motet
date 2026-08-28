"""
Motet - Local Inference Manager Hoist Tests (ADR-0105)

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-04

Description:
    Unit tests pinning the ADR-0105 hoist of LocalInferenceManager out of the Celery
    worker lifecycle. The architectural contract under test:

      1. Client and manager address Redis Streams by a shared ``manager_id`` (the
         MOTET_LOCAL_INFERENCE_MANAGER_ID routing prefix), NOT a per-worker ``worker_id``.
         This is what lets one hoisted manager serve N workers and survive worker restarts.
      2. ``worker_id`` is observability-only: it rides in the request body but is not part
         of the bus address.
      3. The per-worker subprocess spawn path is deleted — there is no
         ``worker_local_inference_startup`` module and no celery worker_ready handler that
         spawns the manager.

Dependencies:
    - pytest: test runner
    - LocalInferenceClient / LocalInferenceManager: units under test

Usage:
    pytest tests/unit/core/test_local_inference_hoist.py

Notes:
    - A tiny in-memory fake Redis captures stream names without a live Redis, so these are
      pure unit tests (no Docker required).
"""

import importlib
import json

import pytest


class _FakeRedis:
    """Minimal sync Redis stand-in that records stream names and auto-answers responses."""

    def __init__(self):
        self.added_streams = []
        self.deleted_streams = []

    def xadd(self, stream, fields):
        self.added_streams.append(stream)
        return b"1-0"

    def xread(self, streams, count=1, block=100):
        # The client polls its response stream; answer the first poll with success so
        # infer() returns immediately and we can assert which stream it listened on.
        (stream_name, _last_id), = streams.items()
        if ":responses:" in stream_name:
            payload = json.dumps({"success": True, "text": "ok", "finish_reason": "stop"})
            return [[stream_name, [(b"1-0", {b"data": payload.encode("utf-8")})]]]
        return []

    def delete(self, stream):
        self.deleted_streams.append(stream)


@pytest.fixture
def client_module(monkeypatch):
    monkeypatch.setenv("MOTET_LOCAL_INFERENCE_MANAGER_ID", "mgr-test-7")
    monkeypatch.setenv("CELERY_WORKER_ID", "worker-xyz")
    # Reimport not required (env read at __init__), but be explicit about isolation.
    from motet.core.models.local import inference_client
    return inference_client


def test_client_routes_on_manager_id_not_worker_id(client_module):
    """Request + response streams are keyed by manager_id; worker_id is body-only."""
    fake = _FakeRedis()
    client = client_module.LocalInferenceClient(fake)

    assert client.manager_id == "mgr-test-7"
    assert client.worker_id == "worker-xyz"

    client.infer(model_id="phi-4-mini", messages=[{"role": "user", "content": "hi"}], timeout=5.0)

    # The request was published to the manager-keyed request stream.
    assert "local-inference:mgr-test-7:requests" in fake.added_streams
    # No stream was ever keyed on the worker_id.
    assert all("worker-xyz" not in s for s in fake.added_streams)
    # The response stream it listened on (and cleaned up) is manager-keyed.
    assert any(s.startswith("local-inference:mgr-test-7:responses:") for s in fake.deleted_streams)


def test_client_manager_id_defaults_when_env_unset(monkeypatch):
    monkeypatch.delenv("MOTET_LOCAL_INFERENCE_MANAGER_ID", raising=False)
    from motet.core.models.local import inference_client

    client = inference_client.LocalInferenceClient(_FakeRedis())
    assert client.manager_id == "local-inference-default"


def test_manager_listens_on_same_manager_id_as_client(monkeypatch):
    """A manager and client sharing manager_id agree on the request stream name."""
    monkeypatch.setenv("MOTET_LOCAL_INFERENCE_MANAGER_ID", "shared-mgr")
    from motet.core.models.local import inference_client
    from motet.core.models.local.inference_manager import LocalInferenceManager

    client = inference_client.LocalInferenceClient(_FakeRedis())
    manager = LocalInferenceManager(worker_id="bootstrap-worker", manager_id="shared-mgr")

    # manager_id is the addressing key; worker_id stays as bootstrap/observability attribution.
    assert manager.manager_id == "shared-mgr"
    assert manager.worker_id == "bootstrap-worker"
    assert manager.manager_id == client.manager_id

    # The stream the manager listens on matches what the client publishes to.
    expected_request_stream = f"local-inference:{manager.manager_id}:requests"
    assert expected_request_stream == "local-inference:shared-mgr:requests"


def test_manager_id_defaults_independently_of_worker_id(monkeypatch):
    monkeypatch.delenv("MOTET_LOCAL_INFERENCE_MANAGER_ID", raising=False)
    monkeypatch.setenv("CELERY_WORKER_ID", "celery-1")
    from motet.core.models.local.inference_manager import LocalInferenceManager

    manager = LocalInferenceManager()
    assert manager.manager_id == "local-inference-default"
    # worker_id falls back to CELERY_WORKER_ID for status attribution only.
    assert manager.worker_id == "celery-1"


def test_in_worker_spawn_path_is_deleted():
    """ADR-0105 §R0: the per-worker subprocess spawn module no longer exists."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("motet.core.distributed.worker_local_inference_startup")


def test_celery_app_has_no_local_inference_spawn_handler():
    """The worker_ready handler that spawned the manager subprocess is removed."""
    celery_app = importlib.import_module("motet.core.workers.celery_app")
    assert not hasattr(celery_app, "start_local_inference_manager_in_parent_process")


def test_reasoning_submodule_import_does_not_eager_load_manager():
    """LocalAdapter imports reasoning; package __init__ must not pull inference_manager."""
    import sys

    stale = [
        key
        for key in list(sys.modules)
        if key == "motet.core.models.local"
        or key.startswith("motet.core.models.local.")
    ]
    for key in stale:
        del sys.modules[key]

    importlib.import_module("motet.core.models.local.reasoning")

    assert "motet.core.models.local.inference_manager" not in sys.modules
