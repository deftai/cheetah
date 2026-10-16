import threading

import pytest

from transcript_worker.ratelimit import MAX_RETRY_AFTER, TokenBucket, backoff_delay, parse_retry_after


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_first_call_is_free_then_calls_are_spaced():
    c = Clock()
    b = TokenBucket(1 / 3, burst=1, clock=c)
    assert b.reserve() == 0.0
    assert b.reserve() == pytest.approx(3.0)
    assert b.reserve() == pytest.approx(6.0), "a second waiter queues behind the first"


def test_tokens_refill_with_time_up_to_the_burst():
    c = Clock()
    b = TokenBucket(1.0, burst=2, clock=c)
    assert [b.reserve(), b.reserve()] == [0.0, 0.0]
    c.now += 100
    assert [b.reserve(), b.reserve(), b.reserve()] == [0.0, 0.0, pytest.approx(1.0)]


def test_max_wait_refuses_without_taking_a_token():
    c = Clock()
    b = TokenBucket(1 / 3, burst=1, clock=c)
    assert b.reserve(max_wait=0) == 0.0
    assert b.reserve(max_wait=1.0) is None
    assert b.reserve(max_wait=1.0) is None, "refused calls do not push the queue out"
    c.now += 3
    assert b.reserve(max_wait=0) == 0.0


def test_a_backwards_clock_does_not_mint_tokens():
    c = Clock()
    b = TokenBucket(1.0, burst=1, clock=c)
    b.reserve()
    c.now -= 50
    assert b.reserve() == pytest.approx(1.0)


@pytest.mark.parametrize("rate", [0, -1, float("inf"), float("nan")])
def test_bad_rate_is_rejected(rate):
    with pytest.raises(ValueError):
        TokenBucket(rate)
    with pytest.raises(ValueError):
        TokenBucket(1.0, burst=0)


def test_concurrent_reservations_never_share_a_slot():
    c = Clock()
    b = TokenBucket(1.0, burst=1, clock=c)
    waits, lock = [], threading.Lock()

    def go():
        w = b.reserve()
        with lock:
            waits.append(w)

    threads = [threading.Thread(target=go) for _ in range(20)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(waits) == pytest.approx([float(i) for i in range(20)])


def test_retry_after_seconds_and_dates():
    assert parse_retry_after("7") == 7.0
    assert parse_retry_after(" 7 ") == 7.0
    assert parse_retry_after("99999") == MAX_RETRY_AFTER
    assert parse_retry_after("1" * 30) == MAX_RETRY_AFTER
    assert parse_retry_after("Wed, 21 Oct 2026 07:28:30 GMT", now_epoch=1792567700.0) == pytest.approx(10.0)
    # Non-ASCII digits satisfy str.isdigit() but int() cannot parse the superscripts and
    # circled digits among them: the isascii() guard keeps them from raising.
    for bad in ["\u00b2", "\u00b3", "\u00b9", "\uff11", "1\u00b2", "\u2460", "\u0663"]:
        assert parse_retry_after(bad, now_epoch=1792567700.0) is None, bad
    for bad in [None, "", "abc", "-5", "0", "1.5", "\u0663", "Wed, 21 Oct 2015 07:28:00 GMT", 5, b"5"]:
        assert parse_retry_after(bad, now_epoch=1792567700.0) is None, bad


def test_backoff_grows_is_jittered_and_capped():
    mid = lambda: 0.5  # noqa: E731
    assert [backoff_delay(i, jitter=mid) for i in range(3)] == [2.0, 4.0, 8.0]
    assert backoff_delay(0, jitter=lambda: 0.0) == 1.0
    assert backoff_delay(0, jitter=lambda: 0.999) == pytest.approx(2.998, abs=1e-3)
    assert backoff_delay(50, jitter=lambda: 0.999) == 60.0
    assert backoff_delay(10**9, jitter=mid) <= 60.0


def test_default_backoff_is_jittered():
    # A constant default jitter would make every worker retry in lockstep.
    assert len({backoff_delay(3) for _ in range(50)}) > 1
    assert all(8.0 <= backoff_delay(3) < 24.0 for _ in range(50))  # step 16, jitter 0.5x to 1.5x


def test_backoff_honours_retry_after():
    assert backoff_delay(0, retry_after=30, jitter=lambda: 0.5) == 30
    assert backoff_delay(0, retry_after=0.1, jitter=lambda: 0.5) == 2.0
    assert backoff_delay(0, retry_after=MAX_RETRY_AFTER, jitter=lambda: 0.5) == MAX_RETRY_AFTER


@pytest.mark.parametrize(
    "bad",
    [
        "Mon, 01 Jan 2026 00:00:00 +99999999999999999999",
        "Mon, 01 Jan 2026 00:00:00 -99999999999999",
        "1 Jan 2026 00:00:00 +9999999999999999999999999",
    ],
)
def test_retry_after_dates_that_overflow_are_unreadable_not_fatal(bad):
    # email.utils raises OverflowError (not ValueError) for an absurd zone offset.
    assert parse_retry_after(bad, now_epoch=1792567700.0) is None
