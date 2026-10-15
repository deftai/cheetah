import dataclasses

import pytest

from transcript_worker.source import CaptionSource, VideoInfo
from transcript_worker.tracks import TrackListing
from transcript_worker.ytdlp_source import YtDlpSource


def test_the_yt_dlp_adapter_fills_the_seam():
    assert isinstance(YtDlpSource(), CaptionSource)


def test_something_missing_a_method_does_not():
    class OnlyLists:
        def list_tracks(self, video_id):
            return None

    assert not isinstance(OnlyLists(), CaptionSource)


def test_video_info_is_immutable_and_the_handle_is_optional():
    info = VideoInfo(title=None, listing=TrackListing(tracks=()))
    assert info.handle is None and info.title is None
    with pytest.raises(dataclasses.FrozenInstanceError):
        info.title = "x"  # type: ignore[misc]
