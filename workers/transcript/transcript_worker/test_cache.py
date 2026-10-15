import json

import fakeredis
import pytest

from transcript_worker.cache import CacheEntry, MemoryStore, RedisStore, TranscriptCache
from transcript_worker.render import MAX_OUTPUT_BYTES
from transcript_worker.tracks import Track

VID = "dQw4w9WgXcQ"
TRACK = Track("en", "manual")
ENTRY = CacheEntry(
    markdown="# T\n\n```\n[00:00] hi\n```\n",
    title="T",
    transcript={"kind": "manual", "language": "en", "available_languages": ["en", "fr"]},
)


class Boom:
    def get(self, key):
        raise ConnectionError("redis down")

    def set(self, key, value, ttl):
        raise ConnectionError("redis down")


def cache(ttl=60, store=None):
    return TranscriptCache(store if store is not None else MemoryStore(), ttl)


def test_a_stored_transcript_is_found_by_track_and_by_request():
    c = cache()
    c.store(VID, None, TRACK, ENTRY)
    assert c.lookup_track(VID, TRACK) == ENTRY
    assert c.lookup_requested(VID, None) == ENTRY
    assert c.lookup_requested(VID, "fr") is None


def test_the_key_is_video_and_chosen_track_so_different_requests_share_one_entry():
    store = MemoryStore()
    c = cache(store=store)
    c.store(VID, None, TRACK, ENTRY)
    c.store(VID, "en", TRACK, ENTRY)
    c.store(VID, "EN", TRACK, ENTRY)
    blobs = [k for k in store.data if ":track:" in k]
    assert blobs == [f"transcript_cache:v1:track:{VID}:manual:en"]
    assert c.lookup_requested(VID, "en") == c.lookup_requested(VID, "EN") == ENTRY


def test_track_kind_and_key_separate_entries():
    c = cache()
    c.store(VID, "en", Track("en", "manual"), ENTRY)
    assert c.lookup_track(VID, Track("en", "auto")) is None
    assert c.lookup_track(VID, Track("en", "manual", key="en-AbCdEfGhIjK")) is None
    assert c.lookup_track("aaaaaaaaaaa", Track("en", "manual")) is None


def test_zero_ttl_disables_everything():
    store = MemoryStore()
    c = TranscriptCache(store, 0)
    assert not c.enabled
    c.store(VID, None, TRACK, ENTRY)
    assert store.data == {}
    assert c.lookup_track(VID, TRACK) is None and c.lookup_requested(VID, None) is None


def test_entries_expire_with_the_ttl():
    store = MemoryStore()
    c = cache(ttl=86400, store=store)
    c.store(VID, None, TRACK, ENTRY)
    assert set(store.ttls.values()) == {86400}  # pointer and entry both


def test_a_redis_outage_is_a_miss_and_never_an_error():
    c = cache(store=Boom())
    assert c.lookup_requested(VID, None) is None
    assert c.lookup_track(VID, TRACK) is None
    c.store(VID, None, TRACK, ENTRY)  # does not raise


def test_a_pointer_to_a_missing_entry_is_a_miss():
    store = MemoryStore()
    c = cache(store=store)
    c.store(VID, None, TRACK, ENTRY)
    for k in [k for k in store.data if ":track:" in k]:
        del store.data[k]
    assert c.lookup_requested(VID, None) is None


@pytest.mark.parametrize(
    "bad",
    [
        b"not json", b"[]", b"null", b'{"v": 2}',
        json.dumps({**ENTRY.__dict__, "v": 1, "markdown": ""}).encode(),
        json.dumps({**ENTRY.__dict__, "v": 1, "markdown": 5}).encode(),
        json.dumps({**ENTRY.__dict__, "v": 1, "markdown": "x" * (MAX_OUTPUT_BYTES + 1)}).encode(),
        json.dumps({**ENTRY.__dict__, "v": 1, "title": None}).encode(),
        json.dumps({**ENTRY.__dict__, "v": 1, "transcript": {"kind": "weird", "language": "en"}}).encode(),
        json.dumps({**ENTRY.__dict__, "v": 1, "transcript": {"kind": "manual", "language": "-x"}}).encode(),
        json.dumps({**ENTRY.__dict__, "v": 1, "transcript": {"kind": "manual", "language": "en", "available_languages": ["../x"]}}).encode(),
        json.dumps({**ENTRY.__dict__, "v": 1, "transcript": {"kind": "manual", "language": "en", "available_languages": "en"}}).encode(),
        b"\xff\xfe",
    ],
)
def test_a_damaged_or_foreign_entry_is_a_miss(bad):
    store = MemoryStore()
    c = cache(store=store)
    c.store(VID, None, TRACK, ENTRY)
    (key,) = [k for k in store.data if ":track:" in k]
    store.data[key] = bad
    assert c.lookup_requested(VID, None) is None
    assert c.lookup_track(VID, TRACK) is None


def test_a_pointer_cannot_redirect_to_another_key():
    store = MemoryStore()
    c = cache(store=store)
    store.data["job_results:x"] = json.dumps({"v": 1, **ENTRY.__dict__}).encode()
    ptr = f"transcript_cache:v1:req:{VID}:-"
    for target in (b"job_results:x", b"transcript_cache:v1:track:aaaaaaaaaaa:manual:en", b"\xff"):
        store.data[ptr] = target
        assert c.lookup_requested(VID, None) is None


def test_invalid_inputs_are_programming_errors():
    c = cache()
    with pytest.raises(ValueError):
        c.lookup_requested("short", None)
    with pytest.raises(ValueError):
        c.lookup_requested(VID, "../x")
    with pytest.raises(ValueError):
        c.store(VID, None, Track("en", "weird"), ENTRY)


def test_redis_store_round_trip_and_ttl():
    r = fakeredis.FakeRedis()
    c = TranscriptCache(RedisStore(r), 90)
    c.store(VID, "en", TRACK, ENTRY)
    assert c.lookup_requested(VID, "en") == ENTRY
    keys = {k.decode() for k in r.keys("*")}
    assert keys == {f"transcript_cache:v1:track:{VID}:manual:en", f"transcript_cache:v1:req:{VID}:en"}
    assert all(0 < r.ttl(k) <= 90 for k in keys)


def test_link_points_a_request_at_an_entry_without_renewing_it():
    store = MemoryStore()
    c = cache(store=store)
    c.store(VID, None, TRACK, ENTRY)
    store.ttls.clear()
    c.link(VID, "en", TRACK)
    assert c.lookup_requested(VID, "en") == ENTRY
    assert list(store.ttls) == [f"transcript_cache:v1:req:{VID}:en"], "the entry itself keeps its original TTL"


def test_null_available_languages_is_a_miss_not_a_crash():
    raw = json.dumps({"v": 1, "markdown": "# T\n", "title": "T", "transcript": {"kind": "manual", "language": "en", "available_languages": None}})
    assert CacheEntry.from_bytes(raw.encode()) is None
