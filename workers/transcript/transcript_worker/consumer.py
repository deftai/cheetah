"""The Redis stream consumer.

What the gateway needs from it (see gateway/queue.go) and how this meets it:

- It keeps showing up in `XINFO CONSUMERS` while jobs run. Jobs run in a thread
  pool; the event loop only ever awaits Redis, so `XREADGROUP` keeps being
  called with a BLOCK of a few seconds. When every slot is busy it still calls
  `XREADGROUP` (reading its own pending history, which takes no new job) so its
  idle time stays low; it never reads a job it cannot start.
- One stable consumer name per replica. Consumers left behind by replicas that
  came and went (a container's hostname changes every recreate) are deleted
  when they are idle for a long time and hold nothing; on a clean shutdown the
  worker deletes its own consumer if it holds nothing.
- At-least-once: an entry is acked only after its result is written. Entries a
  dead worker left pending are claimed after CLAIM_IDLE; one delivered more
  than `max_deliveries` times (it keeps killing its worker) is failed instead.
- Every job ends in a result: a failure is a failed result with an error code,
  never silence. The one case without a result is an entry with no usable job
  id, which has nowhere to be written and is just acked.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable, Mapping
from concurrent.futures import Executor, Future
from typing import Any

import redis.exceptions

from . import errors
from .config import CLAIM_IDLE_SECONDS, GROUP, RESULT_TTL_SECONDS, RESULTS_PREFIX, STREAM, Config
from .handler import JobOutcome
from .results import failed_payload
from .validators import valid_job_id

log = logging.getLogger(__name__)

BLOCK_MS = 2000  # gateway/queue.go: BLOCK must stay at 5 s or less (a consumer idle for 30 s counts as gone)
CLAIM_EVERY = 30.0
SWEEP_EVERY = 60.0
STALE_CONSUMER_IDLE_MS = 10 * 60 * 1000
MAX_DELIVERIES = 3
CLAIM_PAGE = 10
MAX_CLAIM_PAGES = 5
TIMEOUT_GRACE = 15.0  # the asyncio-side limit on a job is the pipeline's own plus this

# An entry id far beyond any real one (a millisecond timestamp in the year 5138). Not the largest possible
# id: on Redis 7.4 a history read from 18446744073709551615-18446744073709551615 delivered a new entry.
_PAST_THE_END = "99999999999999-0"

Handler = Callable[[Mapping[str, str]], JobOutcome]


def _text(v: object) -> str:
    return v.decode("utf-8", "replace") if isinstance(v, (bytes, bytearray)) else str(v)


def _fields(raw: object) -> dict[str, str]:
    return {_text(k): _text(v) for k, v in raw.items()} if isinstance(raw, Mapping) else {}


def _entries(reply: object) -> list[tuple[str, dict[str, str]]]:
    """The entries of an XREADGROUP reply, in RESP2 form ([[stream, [(id, {field: value})]]]) or RESP3 form
    ({stream: [[id, {...}]]})."""
    if not reply:
        return []
    rows: list[Any] = []
    if isinstance(reply, Mapping):
        for entries in reply.values():
            rows.extend(entries)
    elif isinstance(reply, (list, tuple)):
        for stream in reply:
            rows.extend(stream[1])
    return [(_text(r[0]), _fields(r[1])) for r in rows if len(r) == 2 and r[1] is not None]


def _is_nogroup(e: BaseException) -> bool:
    return isinstance(e, redis.exceptions.ResponseError) and "NOGROUP" in str(e)


class Consumer:
    def __init__(
        self,
        redis_client: Any,
        cfg: Config,
        handler: Handler,
        *,
        executor: Executor,
        block_ms: int = BLOCK_MS,
        claim_idle_ms: int = int(CLAIM_IDLE_SECONDS * 1000),
        claim_every: float = CLAIM_EVERY,
        sweep_every: float = SWEEP_EVERY,
        stale_idle_ms: int = STALE_CONSUMER_IDLE_MS,
        max_deliveries: int = MAX_DELIVERIES,
        timeout_grace: float = TIMEOUT_GRACE,
        drain_timeout: float | None = None,
        retry_backoff: float = 0.5,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 0 < block_ms <= 5000:
            raise ValueError("block_ms must be between 1 and 5000")
        self._r = redis_client
        self._cfg = cfg
        self._handler = handler
        self._executor = executor
        self._block_ms = block_ms
        self._claim_idle_ms = claim_idle_ms
        self._claim_every = claim_every
        self._sweep_every = sweep_every
        self._stale_idle_ms = stale_idle_ms
        self._max_deliveries = max_deliveries
        self._timeout_grace = timeout_grace
        self._drain_timeout = cfg.job_timeout + timeout_grace if drain_timeout is None else drain_timeout
        self._retry_backoff = retry_backoff
        self._clock = clock
        self._inflight: dict[str, asyncio.Task[None]] = {}
        # Executor futures of jobs given up on after the worker's own time limit. The thread cannot be
        # stopped, so it keeps its slot until it returns: a new job must not queue behind it.
        self._abandoned: set[Future[JobOutcome]] = set()

    @property
    def inflight_count(self) -> int:
        return len(self._inflight)

    def _free_slots(self) -> int:
        return self._cfg.concurrency - len(self._inflight) - len(self._abandoned)

    # -- Redis plumbing ------------------------------------------------------------------------

    async def _ensure_group(self) -> None:
        try:
            await self._r.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
        except redis.exceptions.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

    async def _read_new(self, count: int) -> list[tuple[str, dict[str, str]]]:
        reply = await self._r.xreadgroup(
            GROUP, self._cfg.consumer_name, {STREAM: ">"}, count=count, block=self._block_ms
        )
        return _entries(reply)

    async def _touch(self) -> None:
        """Calls XREADGROUP without taking a job, so the consumer still counts as seen while every slot is
        busy. It reads the consumer's pending history from an id past every real entry: that never blocks,
        takes nothing new and returns nothing. (Measured on Redis 7.4: reading the history from "0" also
        keeps the idle time low but re-delivers the first pending entry on every call, which raises its
        delivery count and would make a long-running job look like one that keeps killing workers.)"""
        await self._r.xreadgroup(GROUP, self._cfg.consumer_name, {STREAM: _PAST_THE_END}, count=1)

    # -- main loop -----------------------------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        await self._start(stop)
        last_sweep = self._clock()
        last_claim = last_sweep - self._claim_every  # claim whatever a dead worker left, straight away
        failures = 0
        while not stop.is_set():
            try:
                now = self._clock()
                if now - last_claim >= self._claim_every:
                    last_claim = now
                    await self._claim_pending()
                if now - last_sweep >= self._sweep_every:
                    last_sweep = now
                    await self._sweep_stale_consumers()
                free = self._free_slots()
                if free > 0:
                    for entry_id, fields in await self._read_new(free):
                        self._start_job(entry_id, fields)
                else:
                    await self._touch()
                    running = list(self._inflight.values())  # may have emptied during the poll
                    if running:
                        await asyncio.wait(running, timeout=self._block_ms / 1000, return_when=asyncio.FIRST_COMPLETED)
                    else:
                        await asyncio.sleep(self._block_ms / 1000)  # only abandoned threads hold the slots
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                failures += 1
                log.warning("redis error (%s), retrying", type(e).__name__)
                if _is_nogroup(e):
                    await self._guard(self._ensure_group)
                await self._sleep(stop, min(5.0, self._retry_backoff * failures))
        await self._drain()
        await self._leave()

    async def _start(self, stop: asyncio.Event) -> None:
        """Joins the group, retrying while Redis is not reachable yet."""
        failures = 0
        while not stop.is_set():
            try:
                await self._ensure_group()
                log.info("consuming %s as %s in group %s", STREAM, self._cfg.consumer_name, GROUP)
                return
            except Exception as e:
                failures += 1
                log.warning("cannot join the consumer group (%s), retrying", type(e).__name__)
                await self._sleep(stop, min(5.0, self._retry_backoff * failures))

    @staticmethod
    async def _sleep(stop: asyncio.Event, seconds: float) -> None:
        try:
            await asyncio.wait_for(stop.wait(), seconds)
        except TimeoutError:
            pass

    @staticmethod
    async def _guard(fn: Callable[[], Any]) -> None:
        try:
            await fn()
        except Exception as e:
            log.warning("redis error (%s)", type(e).__name__)

    # -- jobs ----------------------------------------------------------------------------------

    def _start_job(self, entry_id: str, fields: dict[str, str]) -> None:
        task = asyncio.get_running_loop().create_task(self._process(entry_id, fields))
        self._inflight[entry_id] = task
        task.add_done_callback(lambda _t: self._inflight.pop(entry_id, None))

    async def _process(self, entry_id: str, fields: dict[str, str]) -> None:
        job_id = fields.get("job_id") if valid_job_id(fields.get("job_id")) else None
        thread_job = self._executor.submit(self._handler, fields)
        try:
            outcome = await asyncio.wait_for(asyncio.wrap_future(thread_job), self._cfg.job_timeout + self._timeout_grace)
        except TimeoutError:
            # The thread cannot be stopped; the caller still gets an answer, and the thread keeps its slot.
            if not thread_job.cancel():
                self._abandoned.add(thread_job)
                thread_job.add_done_callback(self._abandoned.discard)
            err = errors.timeout()
            outcome = JobOutcome(job_id, failed_payload(job_id, err) if job_id else None, f"job={job_id} failed code={err.code} (worker limit)")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("job handler crashed (%s)", type(e).__name__)
            err = errors.internal("unexpected")
            outcome = JobOutcome(job_id, failed_payload(job_id, err) if job_id else None, f"job={job_id} failed code={err.code}")
        try:
            if outcome.payload is not None and outcome.job_id is not None and not await self._would_clobber(outcome):
                await self._r.set(f"{RESULTS_PREFIX}:{outcome.job_id}", json.dumps(outcome.payload), ex=RESULT_TTL_SECONDS)
            await self._r.xack(STREAM, GROUP, entry_id)
        except Exception as e:
            # No ack: the entry stays pending and is claimed later.
            log.error("could not store the result for %s (%s); the entry stays pending", entry_id, type(e).__name__)
            return
        log.info(outcome.summary)

    async def _would_clobber(self, outcome: JobOutcome) -> bool:
        """True when `outcome` is a failure and the job already has a completed result. That happens when a
        run wrote its result but lost the XACK, and the claimed retry then fails (say, rate limited): the
        caller must keep the transcript it was already given."""
        if outcome.payload is None or outcome.payload.get("status") != "failed":
            return False
        raw = await self._r.get(f"{RESULTS_PREFIX}:{outcome.job_id}")
        try:
            return raw is not None and json.loads(raw).get("status") == "completed"
        except (ValueError, AttributeError):
            return False

    # -- recovery and housekeeping -----------------------------------------------------------------

    async def _claim_pending(self) -> None:
        cursor = "0-0"
        for _ in range(MAX_CLAIM_PAGES):
            free = self._free_slots()
            if free <= 0:
                return
            reply = await self._r.xautoclaim(
                STREAM, GROUP, self._cfg.consumer_name,
                min_idle_time=self._claim_idle_ms, start_id=cursor, count=min(free, CLAIM_PAGE),
            )
            cursor, claimed = _text(reply[0]), reply[1]
            for entry_id, fields in _entries([[STREAM, claimed]]):
                if entry_id in self._inflight:
                    continue
                if await self._deliveries(entry_id) > self._max_deliveries:
                    await self._give_up(entry_id, fields)
                else:
                    log.info("claimed pending entry %s", entry_id)
                    self._start_job(entry_id, fields)
            if cursor == "0-0":
                return

    async def _deliveries(self, entry_id: str) -> int:
        rows = await self._r.xpending_range(STREAM, GROUP, min=entry_id, max=entry_id, count=1)
        return int(rows[0]["times_delivered"]) if rows else 0

    async def _give_up(self, entry_id: str, fields: dict[str, str]) -> None:
        """An entry delivered too often keeps killing its worker: fail it instead of running it again."""
        job_id = fields.get("job_id")
        log.error("entry %s was delivered more than %d times; failing it", entry_id, self._max_deliveries)
        if valid_job_id(job_id):
            err = errors.internal("unexpected")
            await self._r.set(f"{RESULTS_PREFIX}:{job_id}", json.dumps(failed_payload(job_id, err)), ex=RESULT_TTL_SECONDS)
        await self._r.xack(STREAM, GROUP, entry_id)

    async def _consumers(self) -> list[dict[str, Any]]:
        return [
            {"name": _text(c["name"]), "pending": int(c["pending"]), "idle": int(c["idle"])}
            for c in await self._r.xinfo_consumers(STREAM, GROUP)
        ]

    async def _sweep_stale_consumers(self) -> None:
        """Deletes consumers that have been idle a long time and hold nothing (replicas that are gone).
        One holding pending entries is left: its entries are claimed first, and it goes on a later sweep."""
        for c in await self._consumers():
            if c["name"] != self._cfg.consumer_name and c["pending"] == 0 and c["idle"] >= self._stale_idle_ms:
                await self._r.xgroup_delconsumer(STREAM, GROUP, c["name"])
                log.info("deleted stale consumer %r", c["name"])  # a Redis-side name: repr keeps it to one line

    async def _drain(self) -> None:
        if self._inflight:
            log.info("shutting down, waiting for %d running job(s)", len(self._inflight))
            _, still = await asyncio.wait(list(self._inflight.values()), timeout=self._drain_timeout)
            for task in still:
                task.cancel()
            await asyncio.gather(*still, return_exceptions=True)

    async def _leave(self) -> None:
        """Deletes the worker's own consumer on the way out, unless it still holds entries (deleting a
        consumer drops its pending entries from the group, which would lose those jobs)."""
        try:
            mine = [c for c in await self._consumers() if c["name"] == self._cfg.consumer_name]
            if mine and mine[0]["pending"] == 0:
                await self._r.xgroup_delconsumer(STREAM, GROUP, self._cfg.consumer_name)
        except Exception as e:
            log.warning("could not remove the consumer on shutdown (%s)", type(e).__name__)
