"""
Motet - Distributed Performance Smoke Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-26

Description:
    Lane C checks that chat, streaming, memory list, and a cheap builtin
    tool stay healthy on a small Celery fleet (mock-small). These are
    smoke bars, not published benchmarks: no fictional local baselines
    and no required RPS scaling on two workers.

Dependencies:
    - tests.integration.conftest: native_chat_client, ready workers, Redis reset
    - performance_tracker fixture for elapsed-ms prints
    - psutil: optional host CPU/memory sample when a full stack URL is set

Usage:
    docker compose -f tests/docker-compose.test.yml --profile workers up -d
    docker compose -f tests/docker-compose.test.yml run --rm test-runner \\
        python -m pytest tests/performance/test_performance.py -v -m distributed

Notes:
    - mock-small echoes ``You said: <prompt>``
    - Isolated async Redis prevents empty SSE bodies from a closed event loop
    - Tool execute uses ``core.note`` (the registered name)
"""

from __future__ import annotations

import asyncio
import statistics
import time
from typing import Any, List

import psutil
import pytest

from tests.integration.conftest import (
    isolated_async_redis,
    native_chat_app,
    native_chat_client,
    ready_celery_workers,
    sse_assembled_text,
    sse_event_names,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.distributed,
    pytest.mark.performance,
    pytest.mark.requires_external,
]

CHAT_LATENCY_MS_MAX = 15_000.0
STREAM_LATENCY_S_MAX = 15.0


@pytest.fixture(autouse=True)
def _async_redis(isolated_async_redis):
    """Stream and chat reads need Redis clients bound to this test loop."""


@pytest.fixture(autouse=True)
def _workers(ready_celery_workers):
    """Skip unless compose --profile workers has a ready Celery worker."""


async def _chat(client, prompt: str, *, stream: bool = False):
    return await client.post(
        "/api/v1/chat",
        json={
            "messages": [{"role": "user", "content": prompt}],
            "stream": stream,
        },
    )


def _ok(response: Any) -> bool:
    return not isinstance(response, Exception) and getattr(response, "status_code", 0) == 200


class TestDistributedPerformance:
    """Smoke latency, modest concurrency, and streaming on ready workers."""

    async def test_latency_comparison(self, native_chat_client, performance_tracker):
        """Chat returns 200 with the mock echo under a generous ceiling."""
        latencies: List[float] = []
        for iteration in range(3):
            prompt = f"latency ping {iteration}"
            name = f"chat_iter_{iteration}"
            performance_tracker.start_timer(name)
            response = await _chat(native_chat_client, prompt)
            performance_tracker.end_timer(name)
            assert response.status_code == 200, response.text
            assert prompt in (response.json().get("content") or ""), response.text
            latencies.append(performance_tracker.get_duration(name))

        avg_latency = statistics.mean(latencies)
        print(f"\nCHAT latency: avg={avg_latency:.1f}ms all={latencies}")
        assert max(latencies) < CHAT_LATENCY_MS_MAX, (
            f"chat slower than {CHAT_LATENCY_MS_MAX:.0f}ms: {latencies}"
        )

    async def test_throughput_scaling(self, native_chat_client):
        """Modest concurrency still succeeds. Do not require RPS to scale."""
        for concurrent in (1, 2, 4):
            print(f"\nThroughput with {concurrent} concurrent chats...")
            start = time.time()
            responses = await asyncio.gather(
                *[
                    _chat(native_chat_client, f"throughput {concurrent}-{i}")
                    for i in range(concurrent)
                ],
                return_exceptions=True,
            )
            elapsed = time.time() - start
            successful = sum(1 for r in responses if _ok(r))
            print(f"  Successful: {successful}/{concurrent} in {elapsed:.2f}s")
            assert successful == concurrent, (
                f"{successful}/{concurrent} chats succeeded at concurrency {concurrent}"
            )

    async def test_resource_utilization(self, native_chat_client, distributed_stack):
        """Host CPU/memory stay sane while a handful of chats run."""
        baseline_cpu = psutil.cpu_percent(interval=1)
        baseline_memory = psutil.virtual_memory().percent
        print(f"\nBaseline CPU={baseline_cpu:.1f}% mem={baseline_memory:.1f}%")

        start_time = time.time()
        cpu_samples: List[float] = []
        memory_samples: List[float] = []

        async def monitor_resources() -> None:
            while time.time() - start_time < 15:
                cpu_samples.append(psutil.cpu_percent())
                memory_samples.append(psutil.virtual_memory().percent)
                await asyncio.sleep(0.5)

        monitor_task = asyncio.create_task(monitor_resources())
        responses = await asyncio.gather(
            *[_chat(native_chat_client, f"resource test {i}") for i in range(4)]
        )
        monitor_task.cancel()

        if cpu_samples and memory_samples:
            print(
                f"\nUnder load CPU max={max(cpu_samples):.1f}% "
                f"mem max={max(memory_samples):.1f}%"
            )
            assert max(cpu_samples) < 95, f"CPU usage too high: {max(cpu_samples):.1f}%"
            assert max(memory_samples) < 95, (
                f"Memory usage too high: {max(memory_samples):.1f}%"
            )

        successful = sum(1 for r in responses if r.status_code == 200)
        assert successful >= 3, f"Too many failed chats: {4 - successful}/4"

    async def test_streaming_performance_detailed(self, native_chat_client):
        """Streamed mock-small echo produces token or end frames with text."""
        prompt = "stream ping"
        start = time.time()
        response = await _chat(native_chat_client, prompt, stream=True)
        elapsed = time.time() - start

        assert response.status_code == 200, response.text
        assert "text/event-stream" in response.headers.get("content-type", "")
        events = sse_event_names(response.text)
        assembled = sse_assembled_text(response.text)
        print(f"\nSTREAM events={events} elapsed={elapsed:.2f}s text={assembled!r}")

        assert "error" not in events, response.text
        assert "token" in events or "end" in events, response.text
        assert prompt in assembled, assembled
        assert elapsed < STREAM_LATENCY_S_MAX, f"stream too slow: {elapsed:.2f}s"


@pytest.mark.stress
class TestDistributedStress:
    """Bounded concurrency and mixed real endpoints — not a 100-client soak."""

    async def test_high_concurrency_stress(self, native_chat_client):
        """Eight concurrent chats mostly succeed on a two-worker mock fleet."""
        concurrent = 8
        responses = await asyncio.gather(
            *[_chat(native_chat_client, f"stress {i}") for i in range(concurrent)],
            return_exceptions=True,
        )
        successful = sum(1 for r in responses if _ok(r))
        success_rate = successful / concurrent
        print(f"\nSTRESS {successful}/{concurrent} ({success_rate:.1%})")
        assert success_rate >= 0.75, (
            f"Poor success rate at {concurrent} chats: {success_rate:.1%}"
        )

    async def test_memory_stress(self, native_chat_client):
        """Memory list stays healthy under a burst of reads after one chat."""
        chat = await _chat(native_chat_client, "memory stress seed")
        assert chat.status_code == 200, chat.text

        responses = await asyncio.gather(
            *[
                native_chat_client.get("/api/v1/memories", params={"limit": 5})
                for _ in range(12)
            ],
            return_exceptions=True,
        )
        successful = sum(1 for r in responses if _ok(r))
        success_rate = successful / len(responses)
        print(f"\nMEMORY list {successful}/{len(responses)} ({success_rate:.1%})")
        assert success_rate >= 0.8, (
            f"Memory list success rate too low: {success_rate:.1%}"
        )

    async def test_mixed_workload_performance(
        self, native_chat_client, performance_tracker
    ):
        """Chat, memory list, tool list, and core.note all return 200."""
        calls = [
            ("chat", _chat(native_chat_client, "mixed chat 0")),
            ("chat", _chat(native_chat_client, "mixed chat 1")),
            ("chat", _chat(native_chat_client, "mixed chat 2")),
            ("memory", native_chat_client.get("/api/v1/memories", params={"limit": 5})),
            ("memory", native_chat_client.get("/api/v1/memories", params={"limit": 5})),
            ("tools", native_chat_client.get("/api/v1/tools")),
            (
                "note",
                native_chat_client.post(
                    "/api/v1/tools/execute",
                    json={"name": "core.note", "params": {"text": "mixed note"}},
                ),
            ),
            (
                "note",
                native_chat_client.post(
                    "/api/v1/tools/execute",
                    json={"name": "core.note", "params": {"text": "mixed note 2"}},
                ),
            ),
        ]

        performance_tracker.start_timer("mixed_workload")
        responses = await asyncio.gather(
            *[coro for _, coro in calls], return_exceptions=True
        )
        performance_tracker.end_timer("mixed_workload")

        by_type: dict[str, dict[str, int]] = {}
        for (kind, _), response in zip(calls, responses):
            stats = by_type.setdefault(kind, {"successful": 0, "total": 0})
            stats["total"] += 1
            if _ok(response):
                stats["successful"] += 1

        elapsed_ms = performance_tracker.get_duration("mixed_workload")
        successful = sum(s["successful"] for s in by_type.values())
        total = len(calls)
        rate = successful / total
        print(f"\nMIXED {successful}/{total} ({rate:.1%}) in {elapsed_ms:.1f}ms")
        for kind, stats in by_type.items():
            print(f"  {kind}: {stats['successful']}/{stats['total']}")

        assert rate >= 0.85, f"Mixed workload success rate too low: {rate:.1%}"
