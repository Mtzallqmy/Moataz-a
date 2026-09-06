import asyncio

import pytest

from app.queue import InlineJobManager


@pytest.mark.asyncio
async def test_inline_queue_enforces_per_user_and_survives_runner_exception():
    active = 0
    peak = 0
    done = asyncio.Event()
    seen = []

    async def lookup(job_id):
        return 1 if job_id in {1, 2} else 2

    async def runner(job_id):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        seen.append(job_id)
        try:
            await asyncio.sleep(0.03)
            if job_id == 1:
                raise RuntimeError("background failure")
        finally:
            active -= 1
            if len(seen) >= 3 and active == 0:
                done.set()

    manager = InlineJobManager(runner=runner, user_lookup=lookup, global_limit=2, per_user_limit=1)
    await manager.start()
    assert await manager.enqueue(1)
    assert await manager.enqueue(2)
    assert await manager.enqueue(3)
    await asyncio.wait_for(done.wait(), timeout=2)
    assert peak <= 2
    assert seen.index(3) < seen.index(2)
    await manager.shutdown()


@pytest.mark.asyncio
async def test_inline_queue_duplicate_protection():
    gate = asyncio.Event()

    async def runner(job_id):  # noqa: ARG001
        await gate.wait()

    async def lookup(job_id):  # noqa: ARG001
        return 1

    manager = InlineJobManager(runner=runner, user_lookup=lookup, global_limit=1, per_user_limit=1)
    await manager.start()
    assert await manager.enqueue(10) is True
    assert await manager.enqueue(10) is False
    gate.set()
    await asyncio.sleep(0.05)
    await manager.shutdown()


@pytest.mark.asyncio
async def test_saturated_high_priority_user_does_not_starve_eligible_lower_priority_job():
    release_first = asyncio.Event()
    lower_started = asyncio.Event()
    order = []

    async def lookup(job_id):
        return 1 if job_id in {1, 2} else 2

    async def runner(job_id):
        order.append(job_id)
        if job_id == 1:
            await release_first.wait()
        elif job_id == 3:
            lower_started.set()

    manager = InlineJobManager(runner=runner, user_lookup=lookup, global_limit=2, per_user_limit=1)
    await manager.start()
    assert await manager.enqueue(1, priority=100)
    await asyncio.sleep(0.02)
    assert await manager.enqueue(2, priority=100)
    assert await manager.enqueue(3, priority=0)
    await asyncio.wait_for(lower_started.wait(), timeout=1)
    assert order[:2] == [1, 3]
    release_first.set()
    await asyncio.sleep(0.05)
    await manager.shutdown()
