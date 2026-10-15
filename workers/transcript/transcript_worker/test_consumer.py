import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from itertools import pairwise

import fakeredis.aioredis
import pytest
import redis.exceptions

from transcript_worker import errors
from transcript_worker.cache import CacheEntry
from transcript_worker.config import GROUP, STREAM, Config
from transcript_worker.consumer import Consumer
from transcript_worker.handler import JobOutcome
from transcript_worker.results import completed_payload, failed_payload

VID = "dQw4w9WgXcQ"
ENTRY = CacheEntry("# T\n", "T", {"kind": "manual", "language": "en", "available_languages": ["en"]})


def job_id(n):
    return f"0b5c7c1e-3f0a-4d2e-9d57-{n:012d}"


def entry_fields(n):
    return {"job_id": job_id(n), "url": f"https://www.youtube.com/watch?v={VID}", "video_id": VID, "strategy": "youtube_transcript"}


def cfg(**over):
    base = dict(redis_url="redis://x", consumer_name="transcript-test", concurrency=2, job_timeout=30.0)
    base.update(over)
    return Config(**base)


def ok_handler(fields):
    jid = fields["job_id"]
    return JobOutcome(jid, completed_payload(jid, ENTRY), f"job={jid} completed")


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 20))


async def new_redis():
    r = fakeredis.aioredis.FakeRedis()
    await r.flushall()
    real = r.xreadgroup

    async def blocking_xreadgroup(*args, **kw):
        # fakeredis answers a BLOCK read at once and never yields to the event loop; real Redis waits.
        reply = await real(*args, **kw)
        if kw.get("block") and not any(stream[1] for stream in reply):
            await asyncio.sleep(kw["block"] / 1000)
        return reply

    r.xreadgroup = blocking_xreadgroup
    return r


async def add_jobs(r, *ns):
    for n in ns:
        await r.xadd(STREAM, entry_fields(n))


class Running:
    """A consumer running in the background until `stop()`."""

    def __init__(self, r, handler=ok_handler, config=None, **kw):
        self.r = r
        self.stop_event = asyncio.Event()
        self.pool = ThreadPoolExecutor(max_workers=(config or cfg()).concurrency)
        kw.setdefault("block_ms", 20)
        kw.setdefault("claim_idle_ms", 10_000)
        kw.setdefault("retry_backoff", 0.01)
        self.consumer = Consumer(r, config or cfg(), handler, executor=self.pool, **kw)
        self.task = asyncio.create_task(self.consumer.run(self.stop_event))

    async def stop(self):
        self.stop_event.set()
        await self.task
        self.pool.shutdown(wait=True)


async def until(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        got = pred()
        if asyncio.iscoroutine(got):
            got = await got
        if got:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


async def pending(r):
    try:
        return (await r.xpending(STREAM, GROUP))["pending"]
    except redis.exceptions.ResponseError:
        return None  # the consumer has not created the group yet


async def result(r, n):
    raw = await r.get(f"job_results:{job_id(n)}")
    return json.loads(raw) if raw else None


def test_the_group_is_created_and_an_existing_one_is_fine():
    async def main():
        r = await new_redis()
        for _ in range(2):
            w = Running(r)
            await asyncio.sleep(0.05)
            await w.stop()
        assert [g["name"] for g in await r.xinfo_groups(STREAM)] == [GROUP.encode()]

    run(main())


def test_a_job_is_processed_its_result_written_with_a_ttl_then_acked():
    async def main():
        r = await new_redis()
        await add_jobs(r, 1)
        w = Running(r)

        async def done():
            return await pending(r) == 0 and await result(r, 1) is not None

        await until(done)
        await w.stop()
        assert (await result(r, 1)) == completed_payload(job_id(1), ENTRY)
        assert 0 < await r.ttl(f"job_results:{job_id(1)}") <= 3600

    run(main())


def test_the_entry_is_acked_only_after_the_result_is_written():
    async def main():
        r = await new_redis()
        await add_jobs(r, 1)
        order = []
        real_set, real_ack = r.set, r.xack

        async def set_(*a, **k):
            order.append("set")
            return await real_set(*a, **k)

        async def ack(*a, **k):
            order.append("ack")
            return await real_ack(*a, **k)

        r.set, r.xack = set_, ack
        w = Running(r)
        await until(lambda: "ack" in order)
        await w.stop()
        assert order == ["set", "ack"]

    run(main())


def test_a_failed_result_write_leaves_the_entry_pending_for_a_retry():
    async def main():
        r = await new_redis()
        await add_jobs(r, 1)

        async def broken_set(*a, **k):
            raise redis.exceptions.ConnectionError("down")

        r.set = broken_set
        w = Running(r)
        await asyncio.sleep(0.2)
        await w.stop()
        assert await pending(r) == 1, "no ack, so a later claim can redo the job"

    run(main())


def test_a_handler_that_blows_up_still_ends_in_a_failed_result_and_an_ack():
    def bad(fields):
        raise RuntimeError("bug")

    async def main():
        r = await new_redis()
        await add_jobs(r, 1)
        w = Running(r, bad)

        async def done():
            return await pending(r) == 0

        await until(done)
        await w.stop()
        assert (await result(r, 1))["error"] == errors.internal("unexpected").to_job_error()

    run(main())


def test_an_outcome_without_a_payload_is_just_acked():
    async def main():
        r = await new_redis()
        await r.xadd(STREAM, {"job_id": "nope"})
        w = Running(r, lambda f: JobOutcome(None, None, "dropped"))

        async def done():
            return await pending(r) == 0

        await until(done)
        await w.stop()
        assert await r.keys("job_results:*") == []

    run(main())


def test_entry_fields_reach_the_handler_as_text_even_when_not_utf8():
    seen = []

    def handler(fields):
        seen.append(dict(fields))
        return JobOutcome(None, None, "x")

    async def main():
        r = await new_redis()
        await r.xadd(STREAM, {"job_id": b"\xff\xfe", "video_id": "\u00e9".encode()})
        w = Running(r, handler)
        await until(lambda: seen)
        await w.stop()
        assert seen[0]["job_id"] == "\ufffd\ufffd" and seen[0]["video_id"] == "\u00e9"

    run(main())


def test_at_most_concurrency_jobs_run_at_once_and_polling_goes_on_meanwhile():
    release = threading.Event()
    running, peak, lock = [0], [0], threading.Lock()

    def slow(fields):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        release.wait(5)
        with lock:
            running[0] -= 1
        return ok_handler(fields)

    async def main():
        r = await new_redis()
        reads = []
        real = r.xreadgroup

        async def spy(groupname, consumername, streams, **kw):
            reads.append((dict(streams), kw.get("block")))
            return await real(groupname, consumername, streams, **kw)

        r.xreadgroup = spy
        await add_jobs(r, 1, 2, 3)
        w = Running(r, slow)
        await until(lambda: running[0] == 2)
        before = len(reads)
        await asyncio.sleep(0.3)
        # Both slots are busy and a third job waits in the stream, yet the worker kept calling XREADGROUP
        # (without taking the job) so the gateway keeps seeing it as alive.
        assert len(reads) - before >= 3
        assert running[0] == 2 and await r.xlen(STREAM) == 3
        assert await pending(r) == 2, "the third job was not taken while both slots were busy"
        # Polling must not look like a redelivery: that would make a long job look like a poison one.
        rows = await r.xpending_range(STREAM, GROUP, "-", "+", 10)
        assert [row["times_delivered"] for row in rows] == [1, 1]
        release.set()

        async def all_done():
            return await pending(r) == 0 and await result(r, 3) is not None

        await until(all_done)
        await w.stop()
        assert peak[0] == 2

    run(main())


def test_reads_block_for_at_most_five_seconds():
    from transcript_worker import consumer as mod

    assert 0 < mod.BLOCK_MS <= 5000, "the gateway counts a consumer idle for 30 s as gone"
    pool = ThreadPoolExecutor(1)
    try:
        assert Consumer(None, cfg(), ok_handler, executor=pool)._block_ms == mod.BLOCK_MS
        for bad in (0, 5001, 0.0):
            with pytest.raises(ValueError):
                Consumer(None, cfg(), ok_handler, executor=pool, block_ms=bad)
    finally:
        pool.shutdown()


def test_the_blocking_read_really_passes_block_to_redis():
    async def main():
        r = await new_redis()
        blocks = []
        real = r.xreadgroup

        async def spy(groupname, consumername, streams, **kw):
            blocks.append(kw.get("block"))
            return await real(groupname, consumername, streams, **kw)

        r.xreadgroup = spy
        w = Running(r, block_ms=30)
        await until(lambda: blocks)
        await w.stop()
        assert 30 in blocks

    run(main())


def test_the_event_loop_is_not_blocked_by_a_running_job():
    release = threading.Event()

    def slow(fields):
        release.wait(5)
        return ok_handler(fields)

    async def main():
        r = await new_redis()
        await add_jobs(r, 1)
        w = Running(r, slow)
        ticks = []

        async def ticker():
            for _ in range(10):
                ticks.append(time.monotonic())
                await asyncio.sleep(0.02)

        await ticker()
        release.set()
        await w.stop()
        gaps = [b - a for a, b in pairwise(ticks)]
        assert max(gaps) < 0.2

    run(main())


def test_a_job_that_overruns_gets_a_timeout_result_and_an_ack():
    release = threading.Event()

    def hang(fields):
        release.wait(5)
        return ok_handler(fields)

    async def main():
        r = await new_redis()
        await add_jobs(r, 1)
        w = Running(r, hang, config=cfg(job_timeout=0.05), timeout_grace=0.0)

        async def done():
            return await pending(r) == 0 and await result(r, 1) is not None

        await until(done)
        release.set()
        await w.stop()
        assert (await result(r, 1))["error"] == errors.timeout().to_job_error()

    run(main())


def test_entries_a_dead_worker_left_pending_are_claimed_and_processed():
    async def main():
        r = await new_redis()
        await add_jobs(r, 1)
        await r.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
        await r.xreadgroup(GROUP, "dead-worker", {STREAM: ">"}, count=1)  # delivered, never acked
        assert await pending(r) == 1
        w = Running(r, claim_idle_ms=0)

        async def done():
            return await pending(r) == 0 and await result(r, 1) is not None

        await until(done)
        await w.stop()

    run(main())


def test_a_job_that_keeps_killing_workers_is_failed_not_retried_forever():
    called = []

    def handler(fields):
        called.append(1)
        return ok_handler(fields)

    async def main():
        r = await new_redis()
        await add_jobs(r, 1)
        await r.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
        # Delivered, "crashed", claimed away twice: the delivery count climbs with every hand-over.
        await r.xreadgroup(GROUP, "dead-0", {STREAM: ">"}, count=1)
        mid = (await r.xpending_range(STREAM, GROUP, "-", "+", 1))[0]["message_id"]
        for i in (1, 2):
            await r.xclaim(STREAM, GROUP, f"dead-{i}", 0, [mid])
        assert (await r.xpending_range(STREAM, GROUP, "-", "+", 1))[0]["times_delivered"] == 3
        w = Running(r, handler, claim_idle_ms=0, max_deliveries=3)

        async def done():
            return await pending(r) == 0

        await until(done)
        await w.stop()
        assert called == [], "the handler never ran again"
        assert (await result(r, 1))["error"] == errors.internal("unexpected").to_job_error()

    run(main())


def test_the_workers_own_running_job_is_not_claimed_a_second_time():
    release = threading.Event()
    runs = []

    def slow(fields):
        runs.append(fields["job_id"])
        release.wait(5)
        return ok_handler(fields)

    async def main():
        r = await new_redis()
        await add_jobs(r, 1)
        w = Running(r, slow, claim_idle_ms=0, claim_every=0.01)
        await until(lambda: runs)
        await asyncio.sleep(0.3)  # many claim rounds with min-idle 0
        release.set()
        await w.stop()
        assert runs == [job_id(1)]

    run(main())


def test_stale_empty_consumers_are_deleted_but_not_busy_or_live_ones():
    async def main():
        r = await new_redis()
        await r.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
        await r.xgroup_createconsumer(STREAM, GROUP, "old-idle")
        await r.xadd(STREAM, entry_fields(9))
        await r.xreadgroup(GROUP, "old-busy", {STREAM: ">"}, count=1)  # holds a pending entry
        await asyncio.sleep(0.05)
        w = Running(r, stale_idle_ms=10, claim_idle_ms=10_000_000, sweep_every=0.01)

        async def swept():
            names = {c["name"] for c in await r.xinfo_consumers(STREAM, GROUP)}
            return b"old-idle" not in names

        await until(swept)
        names = {c["name"] for c in await r.xinfo_consumers(STREAM, GROUP)}
        assert b"old-busy" in names and b"transcript-test" in names
        await w.stop()

    run(main())


def test_a_clean_shutdown_finishes_the_running_job_and_leaves_no_consumer_behind():
    release = threading.Event()
    started = threading.Event()

    def slow(fields):
        started.set()
        release.wait(5)
        return ok_handler(fields)

    async def main():
        r = await new_redis()
        await add_jobs(r, 1)
        w = Running(r, slow)
        await until(lambda: started.is_set())
        w.stop_event.set()
        await asyncio.sleep(0.1)
        assert not w.task.done(), "shutdown waits for the job"
        release.set()
        await w.task
        w.pool.shutdown()
        assert await pending(r) == 0 and await result(r, 1) is not None
        assert await r.xinfo_consumers(STREAM, GROUP) == [], "its own consumer is deleted on the way out"

    run(main())


def test_shutdown_keeps_the_consumer_when_a_job_was_abandoned():
    release = threading.Event()

    def hang(fields):
        release.wait(5)
        return ok_handler(fields)

    async def main():
        r = await new_redis()
        await add_jobs(r, 1)
        w = Running(r, hang, config=cfg(job_timeout=10.0), drain_timeout=0.05)
        await until(lambda: w.consumer.inflight_count == 1)
        w.stop_event.set()
        await w.task
        release.set()
        w.pool.shutdown()
        assert await pending(r) == 1, "left for another worker to claim"
        assert [c["name"] for c in await r.xinfo_consumers(STREAM, GROUP)] == [b"transcript-test"]

    run(main())


def test_the_group_is_recreated_if_redis_loses_it():
    async def main():
        r = await new_redis()
        w = Running(r)
        await asyncio.sleep(0.1)
        await r.delete(STREAM)  # e.g. a flush or a restart without persistence
        await asyncio.sleep(0.1)
        await add_jobs(r, 1)

        async def done():
            return await result(r, 1) is not None

        await until(done)
        await w.stop()

    run(main())


def test_a_redis_outage_is_survived():
    async def main():
        r = await new_redis()
        w = Running(r)
        real = r.xreadgroup
        fail = {"n": 3}

        async def flaky(*a, **k):
            if fail["n"] > 0:
                fail["n"] -= 1
                raise redis.exceptions.ConnectionError("down")
            return await real(*a, **k)

        r.xreadgroup = flaky
        await add_jobs(r, 1)

        async def done():
            return await result(r, 1) is not None

        await until(done, timeout=10)
        await w.stop()

    run(main())


def test_a_thread_stuck_after_a_timeout_keeps_its_slot_so_later_jobs_are_not_failed_unrun():
    release = threading.Event()
    ran = []

    def handler(fields):
        ran.append(fields["job_id"])
        if fields["job_id"] == job_id(1):
            release.wait(10)
        return ok_handler(fields)

    async def main():
        r = await new_redis()
        await add_jobs(r, 1)
        w = Running(r, handler, config=cfg(concurrency=1, job_timeout=0.05), timeout_grace=0.0)

        async def timed_out():
            return await result(r, 1) is not None

        await until(timed_out)
        await add_jobs(r, 2)
        await asyncio.sleep(0.4)
        # The only thread is still stuck in job 1: job 2 stays in the stream instead of queueing behind it
        # and being failed as a timeout without ever having run.
        assert ran == [job_id(1)] and await result(r, 2) is None
        assert (await r.xinfo_consumers(STREAM, GROUP))[0]["pending"] == 0
        release.set()

        async def second_done():
            return await result(r, 2) is not None

        await until(second_done)
        await w.stop()
        assert (await result(r, 2))["status"] == "completed"

    run(main())


def test_the_loop_survives_the_last_running_job_finishing_during_the_busy_poll(caplog):
    async def main():
        r = await new_redis()
        real = r.xreadgroup

        async def slow_touch(*a, **k):
            reply = await real(*a, **k)
            if k.get("block") is None:  # the busy-slot poll: take long enough for the job to finish meanwhile
                await asyncio.sleep(0.15)
            return reply

        r.xreadgroup = slow_touch
        await add_jobs(r, 1)

        def quick(fields):
            time.sleep(0.05)
            return ok_handler(fields)

        w = Running(r, quick, config=cfg(concurrency=1))

        async def done():
            return await result(r, 1) is not None

        await until(done)
        await asyncio.sleep(0.4)
        await w.stop()

    run(main())
    assert "redis error" not in caplog.text


def failing_handler(fields):
    jid = fields["job_id"]
    return JobOutcome(jid, failed_payload(jid, errors.timeout()), "failed")


def test_a_retry_that_fails_does_not_overwrite_a_result_that_already_completed():
    # The first run wrote its result but its XACK was lost; the claimed retry then fails (say, rate limited).
    async def main():
        r = await new_redis()
        await r.set(f"job_results:{job_id(1)}", json.dumps(completed_payload(job_id(1), ENTRY)), ex=3600)
        await add_jobs(r, 1)
        w = Running(r, failing_handler)

        async def acked():
            return await pending(r) == 0

        await until(acked)
        await w.stop()
        assert (await result(r, 1))["status"] == "completed"

    run(main())


def test_a_failure_still_replaces_a_queued_status_and_a_success_replaces_a_failure():
    async def main():
        r = await new_redis()
        await r.set(f"job_results:{job_id(1)}", json.dumps({"job_id": job_id(1), "status": "queued"}), ex=3600)
        await r.set(f"job_results:{job_id(2)}", json.dumps(failed_payload(job_id(2), errors.blocked())), ex=3600)
        await add_jobs(r, 1)
        w = Running(r, failing_handler)

        async def done():
            return (await result(r, 1) or {}).get("status") == "failed"

        await until(done)
        await w.stop()
        assert (await result(r, 1))["error"]["code"] == "timeout"
        w2 = Running(r, ok_handler)
        await add_jobs(r, 2)

        async def replaced():
            return (await result(r, 2) or {}).get("status") == "completed"

        await until(replaced)
        await w2.stop()

    run(main())
