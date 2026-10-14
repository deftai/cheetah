"""Core of the YouTube transcript worker: pure logic, no network and no Redis.

The runtime (consumer loop, yt-dlp adapter, cache) is a later slice and sits on
top of these modules. Everything here is testable offline.
"""
