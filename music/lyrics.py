"""Lyrics formatting helpers.

``Track.lyrics`` is free text stored verbatim. Two shapes are supported:

* plain text -- one line per lyric, rendered as-is;
* LRC -- lines prefixed with ``[mm:ss.xx]`` timestamps, which lets a client
  highlight the active line during playback.

Parsing is deliberately tolerant: a malformed timestamp is skipped rather than
raising, because a partially wrong lyric sheet should still render the lines it
does understand instead of taking the whole page down.
"""

import re

#: ``[mm:ss]`` or ``[mm:ss.xx]`` / ``[mm:ss.xxx]``. Also accepts ``:`` before
#: the fraction because some taggers write ``[01:02:50]``.
_TIMESTAMP = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")

#: ID tags (``[ar:Artist]``, ``[offset:-500]``) are metadata, not lyrics.
_METADATA = re.compile(r"^\[([a-zA-Z]+):(.*)\]$")


def is_synced(lyrics: str) -> bool:
    """True when the text carries at least one LRC timestamp."""
    return bool(lyrics) and bool(_TIMESTAMP.search(lyrics))


def parse_lrc(lyrics: str):
    """Return ``[{"time": seconds, "text": line}]`` sorted by timestamp.

    A single line may carry several timestamps (a repeated chorus, for
    example); each becomes its own entry pointing at the same text.
    """
    entries = []
    for raw in (lyrics or "").splitlines():
        matches = list(_TIMESTAMP.finditer(raw))
        if not matches:
            continue
        text = _TIMESTAMP.sub("", raw).strip()
        if not text:
            continue
        for match in matches:
            minutes = int(match.group(1))
            seconds = int(match.group(2))
            fraction = match.group(3) or ""
            sub = int(fraction) / (10 ** len(fraction)) if fraction else 0.0
            entries.append({"time": minutes * 60 + seconds + sub, "text": text})
    entries.sort(key=lambda entry: entry["time"])
    return entries


def plain_lines(lyrics: str):
    """Strip ID tags/timestamps and return the remaining non-empty lines."""
    lines = []
    for raw in (lyrics or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        metadata = _METADATA.match(line)
        if metadata and metadata.group(1).lower() in {
            "ar", "ti", "al", "by", "offset", "re", "ve", "length",
        }:
            continue
        lines.append(_TIMESTAMP.sub("", line).strip())
    return [line for line in lines if line]


def lyrics_payload(track) -> dict:
    """API shape for a track's lyrics, used by ``TrackViewSet.lyrics``."""
    text = track.lyrics or ""
    synced = is_synced(text)
    return {
        "track_id": str(track.pk),
        "title": track.title,
        "artist": track.artist.name,
        "has_lyrics": bool(text.strip()),
        "synced": synced,
        "lyrics": text,
        "lines": parse_lrc(text) if synced else [],
    }
