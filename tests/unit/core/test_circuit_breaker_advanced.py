import asyncio
import pytest
from motet.core.resilience import CircuitBreaker, CircuitState


@pytest.mark.asyncio
async def test_circuit_breaker_transitions():
    br = CircuitBreaker(failure_threshold=2, reset_timeout_seconds=0.1)

    async def fail():
        raise RuntimeError("boom")

    # two failures -> open (use call_async so async failures are observed by breaker)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await br.call_async(fail)
    assert br.state == CircuitState.OPEN

    # immediate call should raise circuit_open
    with pytest.raises(RuntimeError) as ei:
        await br.call_async(fail)
    assert "circuit_open" in str(ei.value)

    # wait for half-open and then succeed; if still open, wait and retry
    async def ok():
        return "ok"
    res = None
    for _ in range(10):
        await asyncio.sleep(0.15)
        try:
            res = await br.call_async(ok)
            break
        except RuntimeError:
            continue
    assert res == "ok"
    assert br.state == CircuitState.CLOSED


