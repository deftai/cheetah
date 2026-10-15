"""Runs against a real Redis. Opt in:

    docker run --rm -d --name tw-redis -p 6390:6379 redis:7-alpine
    TRANSCRIPT_TEST_REDIS_URL=redis://127.0.0.1:6390 python -m pytest -m redis -v

fakeredis cannot show what the gateway depends on: the real `idle` value of a consumer that is busy,
the real XAUTOCLAIM and the real DELCONSUMER. Use a throwaway Redis: the worker's stream name is fixed
(transcript_jobs), so the test refuses to run when that key already exists, and removes what it made.
"""

import asyncio
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
import redis.asyncio
import redis.exceptions

from transcript_worker.cache import CacheEntry
from transcript_worker.config import GROUP, STREAM, Config
from transcript_worker.consumer import Consumer
from transcript_worker.handler import JobOutcome
from transcript_worker.results import completed_payload

URL = os.environ.get("TRANSCRIPT_TEST_REDIS_URL")
pytestmark = [pytest.mark.redis, pytest.mark.skipif(not URL, reason="set TRANSCRIPT_TEST_REDIS_URL to a throwaway Redis")]

VID = "dQw4w9WgXcQ"
ENTRY = CacheEntry("# T\n", "T", {"kind": "manual", "language": "en", "available_languages": ["en"]})
GATEWAY_MAX_IDLE_MS = 30_000  # transcriptWorkerMaxIdle in gateway/queue.go


def job_id(n):
    return f"0b5c7c1e-3f0a-4d2e-9d57-{n:012d}"


def ok(fields):
    return JobOutcome(fields["job_id"], completed_payload(fields["job_id"], ENTRY), "done")


async def add_jobs(r, *ns):
    for n in ns:
        await r.xadd(STREAM, {"job_id": job_id(n), "video_id": VID, "strategy": "youtube_transcript"})


def with_redis(test):
    """Runs `test(r)` on a fresh client, after checking the stream is not someone's real queue, and cleans up."""

    def wrapper():
        async def main():
            r = redis.asyncio.Redis.from_url(URL, protocol=2)
            try:
                if await r.exists(STREAM):
                    # Nothing below may touch this Redis: the stream is someone's real queue.
                    pytest.fail(f"refusing to run: {STREAM} already exists in this Redis (use a throwaway one)")
                try:
                    await test(r)
                finally:
                    await r.delete(STREAM, *[f"job_results:{job_id(n)}" for n in range(1, 6)])
            finally:
                await r.aclose()

        asyncio.run(asyncio.wait_for(main(), 60))

    wrapper.__name__ = test.__name__
    return wrapper


class Worker:
    def __init__(self, r, handler=ok, name="transcript-itest", concurrency=2, **kw):
        self.r, self.stop = r, asyncio.Event()
        self.pool = ThreadPoolExecutor(concurrency)
        cfg = Config(redis_url=URL or "", consumer_name=name, concurrency=concurrency, job_timeout=30.0)
        self.task = asyncio.create_task(Consumer(r, cfg, handler, executor=self.pool, **kw).run(self.stop))

    async def close(self):
        self.stop.set()
        await self.task
        self.pool.shutdown(wait=True)


async def consumers(r):
    return {c["name"].decode(): c for c in await r.xinfo_consumers(STREAM, GROUP)}


async def pending(r):
    try:
        return (await r.xpending(STREAM, GROUP))["pending"]
    except redis.exceptions.ResponseError:
        return None  # the worker has not created the group yet


async def eventually(pred, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if await pred():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not reached")


@with_redis
async def test_a_job_is_processed_acked_and_its_result_expires(r):
    await add_jobs(r, 1)
    w = Worker(r)

    async def done():
        return await r.exists(f"job_results:{job_id(1)}") and await pending(r) == 0

    await eventually(done)
    await w.close()
    assert json.loads(await r.get(f"job_results:{job_id(1)}"))["status"] == "completed"
    assert 0 < await r.ttl(f"job_results:{job_id(1)}") <= 3600


@with_redis
async def test_a_consumer_with_every_slot_busy_still_looks_alive_to_the_gateway(r):
    release = threading.Event()

    def slow(fields):
        release.wait(30)
        return ok(fields)

    await add_jobs(r, 1, 2, 3)
    w = Worker(r, slow)
    await eventually(lambda: _both_running(r))
    worst = 0
    for _ in range(14):  # about 7 s with both slots busy and a third job waiting
        await asyncio.sleep(0.5)
        worst = max(worst, (await consumers(r))["transcript-itest"]["idle"])
    # Polling while busy must not look like a redelivery (a long job would then look like a poison one).
    assert [p["times_delivered"] for p in await r.xpending_range(STREAM, GROUP, "-", "+", 10)] == [1, 1]
    release.set()
    await w.close()
    assert worst < 5000, f"idle reached {worst} ms; the gateway's limit is {GATEWAY_MAX_IDLE_MS} ms"


async def _both_running(r):
    return await pending(r) == 2


@with_redis
async def test_the_third_job_waits_in_the_stream_while_both_slots_are_busy(r):
    release = threading.Event()

    def slow(fields):
        release.wait(30)
        return ok(fields)

    await add_jobs(r, 1, 2, 3)
    w = Worker(r, slow)
    await eventually(lambda: _both_running(r))
    await asyncio.sleep(2.5)
    assert await pending(r) == 2
    assert not await r.exists(f"job_results:{job_id(3)}")
    release.set()

    async def all_done():
        return await r.exists(f"job_results:{job_id(3)}") and await pending(r) == 0

    await eventually(all_done)
    await w.close()


@with_redis
async def test_entries_a_dead_worker_left_are_claimed_after_the_idle_time(r):
    await add_jobs(r, 1)
    await r.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
    await r.xreadgroup(GROUP, "dead-worker", {STREAM: ">"}, count=1)
    w = Worker(r, claim_idle_ms=500, claim_every=0.2)

    async def done():
        return await r.exists(f"job_results:{job_id(1)}") and await pending(r) == 0

    await eventually(done)
    await w.close()


@with_redis
async def test_a_job_that_keeps_being_delivered_is_failed(r):
    await add_jobs(r, 1)
    await r.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
    await r.xreadgroup(GROUP, "dead-0", {STREAM: ">"}, count=1)
    entry_id = (await r.xpending_range(STREAM, GROUP, "-", "+", 1))[0]["message_id"]
    for name in ("dead-1", "dead-2"):
        await r.xclaim(STREAM, GROUP, name, 0, [entry_id])
    called = []

    def handler(fields):
        called.append(1)
        return ok(fields)

    w = Worker(r, handler, claim_idle_ms=0, claim_every=0.2, max_deliveries=3)

    async def done():
        return await r.exists(f"job_results:{job_id(1)}") and await pending(r) == 0

    await eventually(done)
    await w.close()
    assert called == [] and json.loads(await r.get(f"job_results:{job_id(1)}"))["status"] == "failed"


@with_redis
async def test_stale_consumers_are_removed_and_busy_ones_kept(r):
    await add_jobs(r, 1)
    await r.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
    await r.xgroup_createconsumer(STREAM, GROUP, "gone-replica")
    await r.xreadgroup(GROUP, "busy-replica", {STREAM: ">"}, count=1)
    await asyncio.sleep(0.5)
    w = Worker(r, stale_idle_ms=300, sweep_every=0.2, claim_idle_ms=10**9)

    async def swept():
        return "gone-replica" not in await consumers(r)

    await eventually(swept)
    names = await consumers(r)
    assert "busy-replica" in names and "transcript-itest" in names
    await w.close()


@with_redis
async def test_a_clean_shutdown_removes_the_workers_own_consumer(r):
    await add_jobs(r, 1)
    w = Worker(r)

    async def done():
        return await r.exists(f"job_results:{job_id(1)}") and await pending(r) == 0

    await eventually(done)
    await w.close()
    assert "transcript-itest" not in await consumers(r)
