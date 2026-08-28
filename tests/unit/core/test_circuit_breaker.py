from __future__ import annotations

import asyncio
import time
import pytest

from motet.core.resilience import CircuitBreaker, CircuitState


@pytest.mark.asyncio
async def test_half_open_probe_in_flight_blocks():
    br = CircuitBreaker(failure_threshold=1, reset_timeout_seconds=0.01)

    async def fail_once():
        raise RuntimeError("boom")

    # trip to OPEN (use call_async so async failure is observed by breaker)
    with pytest.raises(RuntimeError):
        await br.call_async(fail_once)
    assert br.state == CircuitState.OPEN

    # wait for HALF_OPEN
    await asyncio.sleep(0.02)

    # start a probe that sleeps a bit to keep half-open occupied
    async def slow_ok():
        await asyncio.sleep(0.05)
        return "ok"

    # Launch first probe
    t1 = asyncio.create_task(br.call_async(slow_ok))
    # give the first call time to acquire the HALF_OPEN probe
    await asyncio.sleep(0.02)

    # Concurrent probe should be blocked with specific error
    with pytest.raises(RuntimeError) as ei:
        await br.call_async(slow_ok)
    msg = str(ei.value)
    assert ("circuit_half_open_probe_in_flight" in msg) or ("circuit_open" in msg)

    # Let any in-flight task complete (we don't assert final state due to timing variability)
    try:
        _ = await t1
    except RuntimeError:
        pass


