"""Unit tests for worker identity normalization in worker_utils."""

from __future__ import annotations

from motet.core.workers import worker_utils


def test_get_worker_id_preserves_edge_prefix(monkeypatch) -> None:
    monkeypatch.setenv("MOTET_WORKER_ID", "edge_5a0aaf4d")
    assert worker_utils.get_worker_id() == "edge_5a0aaf4d"


def test_get_worker_id_keeps_cloud_prefix_without_double_prefix(monkeypatch) -> None:
    monkeypatch.setenv("MOTET_WORKER_ID", "cloud_worker1")
    assert worker_utils.get_worker_id() == "cloud_worker1"


def test_is_valid_worker_id_accepts_edge_and_cloud() -> None:
    assert worker_utils.is_valid_worker_id("cloud_worker1") is True
    assert worker_utils.is_valid_worker_id("edge_5a0aaf4d") is True
    assert worker_utils.is_valid_worker_id("worker1") is False

