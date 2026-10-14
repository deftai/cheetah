"""Caption track listing and selection.

Policy (from the slice 6 spike, see the plan): a manual track in the requested
language, then a native auto track (`<lang>-orig` when present), then a
machine-translated track as best effort. YouTube refused translated tracks
(HTTP 429) while the video's native `-orig` track worked, so a translated track
is never preferred over a native one.

`tracks_from_info` reads the info dict yt-dlp returns (`subtitles` and
`automatic_captions`); `select_track` is pure, so both are tested offline.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from . import errors
from .validators import clean_languages, valid_language_tag

MANUAL = "manual"
AUTO = "auto"  # YouTube's speech recognition in the video's original language
TRANSLATED = "translated"  # a machine translation of another track


@dataclass(frozen=True)
class Track:
    language: str  # validated caption tag, e.g. "en", "pt-BR", "ko-orig"
    kind: str  # MANUAL, AUTO or TRANSLATED; manual tracks live in info["subtitles"], the others in "automatic_captions"


@dataclass(frozen=True)
class TrackListing:
    tracks: tuple[Track, ...]
    spoken_language: str | None = None  # the video's original spoken language

    def available_languages(self) -> list[str]:
        """Languages worth telling a caller about: creator-made and native
        tracks. Translated tracks are left out (a video can offer 160 of them)
        unless they are all there is."""
        native = [t.language for t in self.tracks if t.kind != TRANSLATED]
        return clean_languages(native or [t.language for t in self.tracks])

    def original_language_hint(self) -> str | None:
        """What to ask for instead of a refused translated track."""
        for t in sorted(self.tracks, key=lambda t: t.language.casefold()):
            if t.kind == AUTO and _is_orig(t.language):
                return t.language
        return self.spoken_language


@dataclass(frozen=True)
class Selection:
    track: Track
    available_languages: tuple[str, ...]

    def transcript_meta(self) -> dict[str, object]:
        """The `TranscriptMeta` the gateway relays: only validated strings."""
        return {
            "kind": self.track.kind,
            "language": self.track.language,
            "available_languages": list(self.available_languages),
        }


def _is_orig(lang: str) -> bool:
    return lang.casefold().endswith("-orig")


def _primary(lang: str) -> str:
    return lang.casefold().split("-")[0]


def _is_translated(formats: object) -> bool:
    # A machine-translated track's URL carries `tlang=`; a native one does not.
    if not isinstance(formats, list):
        return False
    return any(isinstance(f, dict) and "tlang=" in str(f.get("url") or "") for f in formats)


def _collect(group: object, source: str) -> list[Track]:
    out: list[Track] = []
    if not isinstance(group, Mapping):
        return out
    for lang, formats in group.items():
        # `live_chat` and any odd key fail validation and are dropped.
        if not valid_language_tag(lang) or not isinstance(formats, list) or not formats:
            continue
        kind = MANUAL if source == MANUAL else (TRANSLATED if _is_translated(formats) else AUTO)
        out.append(Track(language=lang, kind=kind))
    return out


def tracks_from_info(info: Mapping[str, object]) -> TrackListing:
    """Builds the listing from a yt-dlp info dict. Anything unexpected in it is
    dropped, never raised: the dict is third-party data."""
    tracks = _collect(info.get("subtitles"), MANUAL) + _collect(info.get("automatic_captions"), AUTO)
    spoken = info.get("language")
    spoken_language = spoken if isinstance(spoken, str) and valid_language_tag(spoken) else None
    if spoken_language is None:
        for t in sorted(tracks, key=lambda t: t.language.casefold()):
            if t.kind == AUTO and _is_orig(t.language):
                spoken_language = t.language[: -len("-orig")]
                break
    return TrackListing(tracks=tuple(tracks), spoken_language=spoken_language)


def _match(candidates: list[Track], want: str) -> Track | None:
    """Exact language first; then, for a bare language like "en", any regional
    variant of it (en-US, en-GB), picked deterministically."""
    w = want.casefold()
    exact = [t for t in candidates if t.language.casefold() == w]
    if exact:
        return exact[0]
    if "-" not in w:
        near = sorted((t for t in candidates if _primary(t.language) == w), key=lambda t: t.language.casefold())
        if near:
            return near[0]
    return None


def select_track(listing: TrackListing, requested: str | None) -> Selection:
    """Picks the caption track to fetch, or raises TranscriptError
    (`no_captions`, `no_track_in_language`)."""
    if not listing.tracks:
        raise errors.no_captions()
    available = listing.available_languages()

    want = requested or listing.spoken_language
    if want is None:
        # No language given and no known original language: only guess when the
        # video offers a single language.
        primaries = {_primary(lang) for lang in available}
        if len(primaries) == 1:
            want = primaries.pop()
        else:
            raise errors.no_track_in_language(None, available)

    manual = [t for t in listing.tracks if t.kind == MANUAL]
    native = [t for t in listing.tracks if t.kind == AUTO]
    translated = [t for t in listing.tracks if t.kind == TRANSLATED]

    found: Track | None
    if _is_orig(want):
        # An explicit original-speech request: only that native track will do.
        found = _match(native, want)
    else:
        # A language we worked out ourselves may carry a region ("en-US") that the
        # track tags lack ("en-orig"), so fall back to its primary subtag. A language
        # the caller asked for is never widened that way.
        wants = [want] if requested or "-" not in want else [want, _primary(want)]
        found = next(
            (t for w in wants if (t := _match(manual, w) or _match(native, w + "-orig") or _match(native, w))), None
        ) or next((t for w in wants if (t := _match(translated, w))), None)
    if found is None:
        raise errors.no_track_in_language(want, available)
    return Selection(track=found, available_languages=tuple(available))
