import asyncio
import json
import time

import fakeredis
import fakeredis.aioredis

from transcript_worker import main
from transcript_worker.config import Config
from transcript_worker.source import VideoInfo
from transcript_worker.tracks import Track, TrackListing

VID = "dQw4w9WgXcQ"
JOB_ID = "0b5c7c1e-3f0a-4d2e-9d57-1c6d9b8a1f20"


def test_a_bad_setting_stops_the_worker_before_it_touches_redis(monkeypatch, caplog):
    monkeypatch.setenv("TRANSCRIPT_CONCURRENCY", "0")
    monkeypatch.setenv("REDIS_URL", "redis://:s3cret@localhost:6379")
    assert main.main() == 2
    assert "TRANSCRIPT_CONCURRENCY" in caplog.text and "s3cret" not in caplog.text


def test_an_old_python_is_refused(monkeypatch, caplog):
    monkeypatch.setattr(main.sys, "version_info", (3, 11, 9, "final", 0))
    assert main.main() == 2
    assert "3.12.4" in caplog.text


def test_the_minimum_python_is_pinned_where_the_project_says_so():
    import tomllib
    from pathlib import Path

    pyproject = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["requires-python"] == ">=" + ".".join(map(str, main.MIN_PYTHON))


class Source:
    def list_tracks(self, video_id):
        return VideoInfo(title="Me at the zoo", listing=TrackListing(tracks=(Track("en", "manual"),)), handle=None)

    def fetch_captions(self, info, track):
        return b"WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nhello\n"


def test_wired_end_to_end_a_gateway_shaped_job_becomes_a_result_the_gateway_can_read():
    async def go():
        server = fakeredis.FakeServer()
        aio = fakeredis.aioredis.FakeRedis(server=server)
        sync = fakeredis.FakeRedis(server=server)
        real = aio.xreadgroup

        async def blocking(*a, **k):  # fakeredis does not block; real Redis does
            reply = await real(*a, **k)
            if k.get("block") and not any(s[1] for s in reply):
                await asyncio.sleep(k["block"] / 1000)
            return reply

        aio.xreadgroup = blocking
        cfg = Config(redis_url="redis://x", consumer_name="transcript-e2e", rate_interval=0.1, cache_ttl=60)
        stop = asyncio.Event()
        worker = asyncio.create_task(main.run(cfg, source=Source(), aio_client=aio, cache_client=sync, stop=stop))
        # What gateway/queue.go writes: the stream entry and the queued status.
        await aio.xadd(
            "transcript_jobs",
            {
                "job_id": JOB_ID,
                "url": f"https://www.youtube.com/watch?v={VID}",
                "format": "markdown",
                "resolved_ip": "142.250.80.46",
                "strategy": "youtube_transcript",
                "payload": "{}",
                "video_id": VID,
                "language": "en",
            },
        )
        await aio.set(f"job_results:{JOB_ID}", json.dumps({"job_id": JOB_ID, "status": "queued"}), ex=3600)
        end = time.monotonic() + 15
        status = {}
        while time.monotonic() < end and status.get("status") != "completed":
            await asyncio.sleep(0.05)
            status = json.loads(await aio.get(f"job_results:{JOB_ID}"))
        stop.set()
        await asyncio.wait_for(worker, 15)
        return status, await aio.xpending("transcript_jobs", "transcript-workers"), sorted(k.decode() for k in await aio.keys("transcript_cache:*"))

    status, pend, cache_keys = asyncio.run(go())
    assert status["status"] == "completed"
    data = status["results"][0]["data"]
    assert "[00:01] hello" in data["markdown"] and data["metadata"]["strategy"] == "youtube_transcript"
    assert data["metadata"]["transcript"] == {"kind": "manual", "language": "en", "available_languages": ["en"]}
    assert pend["pending"] == 0
    assert len(cache_keys) == 2, "the transcript was cached (entry and request pointer)"
