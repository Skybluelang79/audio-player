"""Bulk catalogue operations for the staff dashboard.

Two related jobs live here:

* :func:`apply_bulk_action` performs one staff decision across many tracks.
* :func:`tracks_csv` renders the catalogue (or a filtered slice of it) for
  download.

Both take an explicit queryset/selection rather than reaching for global state,
so the same code backs the UI and can be tested without HTTP.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from io import StringIO

from django.core.files.storage import FileSystemStorage, default_storage
from django.db import transaction
from django.utils.text import slugify

from music.models import Album, Artist, Genre, Track

# Fields staff can set in bulk. Deliberately excludes counters and slugs:
# counters are derived from real events, and slugs are load-bearing URLs.
BULK_EDITABLE_FIELDS = (
    "status",
    "album_id",
    "artist_id",
    "explicit",
    "bitrate_kbps",
    "sample_rate_hz",
    "release_date",
    "track_number",
)

# Counters a bulk reset is allowed to zero.
COUNTER_CHOICES = (
    ("play_count", "Play count"),
    ("like_count", "Like count"),
    ("download_count", "Download count"),
)


@dataclass
class BulkResult:
    """Outcome of a bulk action, including anything the caller must be told."""

    updated: int = 0
    skipped: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        parts = []
        if self.updated:
            parts.append(f"{self.updated} track(s) updated")
        if self.skipped:
            parts.append(f"{self.skipped} skipped")
        return ", ".join(parts) or "Nothing to do"


class BulkActionError(Exception):
    """A requested bulk action is not valid; ``str(exc)`` is user-facing."""


def missing_audio_ids() -> list[int]:
    """Primary keys of tracks whose audio is blank or gone from storage.

    ``Track.audio_file`` is non-blank at the schema level, so an empty value
    only shows up in hand-edited rows. The far more common failure is a file
    that was removed behind Django's back, which needs a storage probe.

    Only ``FileSystemStorage`` is probed. On a remote backend this would cost
    one network round-trip per track, which is not worth it for a staff filter,
    so it degrades to the cheap blank-field check instead.
    """
    blank = list(Track.objects.filter(audio_file="").values_list("pk", flat=True))
    if not isinstance(default_storage, FileSystemStorage):
        return blank

    gone = [
        pk
        for pk, name in Track.objects.exclude(audio_file="").values_list("pk", "audio_file")
        if not default_storage.exists(name)
    ]
    return blank + gone


# --- bulk actions -----------------------------------------------------------


def set_status(track_ids: list[int], status: str) -> BulkResult:
    """Move tracks to a new lifecycle state.

    Publishing a track with no playable audio would put a dead row on the public
    site, so those are skipped and reported rather than published.
    """
    valid = {choice for choice, _ in Track.Status.choices}
    if status not in valid:
        raise BulkActionError(f"“{status}” is not a valid track status.")

    tracks = list(Track.objects.filter(pk__in=track_ids))
    result = BulkResult()

    blocked = []
    if status == Track.Status.PUBLISHED:
        # Publishing a track whose audio has gone would put a dead row on the
        # public site, so skip and report rather than break playback for users.
        missing = set(missing_audio_ids())
        blocked = [t for t in tracks if t.pk in missing]

    publishable = [t for t in tracks if t not in blocked]
    if publishable:
        result.updated = Track.objects.filter(
            pk__in=[t.pk for t in publishable]
        ).update(status=status)

    result.skipped = len(blocked)
    if blocked:
        titles = ", ".join(t.title for t in blocked[:3])
        more = f" (+{len(blocked) - 3} more)" if len(blocked) > 3 else ""
        result.notes.append(f"Missing audio file, not published: {titles}{more}.")
    return result


def set_album(track_ids: list[int], album_id: int | None) -> BulkResult:
    """Re-file tracks onto one album, or clear the album with ``None``."""
    album = None
    if album_id is not None:
        try:
            album = Album.objects.get(pk=album_id)
        except Album.DoesNotExist:
            raise BulkActionError("That album no longer exists.") from None

    result = BulkResult()
    result.updated = Track.objects.filter(pk__in=track_ids).update(
        album=album,
        track_number=None,
    )
    if album is not None:
        result.notes.append(f"Re-filed to {album.title} with track numbers cleared.")
    return result


def set_artist(track_ids: list[int], artist_id: int) -> BulkResult:
    """Re-assign tracks to one artist.

    Albums are unique per artist, so a track whose album belongs to somebody
    else has to be detached rather than left pointing at another artist's
    record. Albums already owned by the target artist are left alone.
    """
    try:
        artist = Artist.objects.get(pk=artist_id)
    except Artist.DoesNotExist:
        raise BulkActionError("That artist no longer exists.") from None

    tracks = Track.objects.filter(pk__in=track_ids)
    orphaned = tracks.exclude(album__isnull=True).exclude(album__artist=artist)

    with transaction.atomic():
        # Detach only the albums that no longer belong to the target artist.
        result = BulkResult()
        detached = orphaned.update(album=None)
        result.updated = tracks.update(artist=artist)

    result.notes.append(f"Re-assigned to {artist.name}.")
    if detached:
        result.notes.append(
            f"{detached} track(s) had an album from another artist; album cleared."
        )
    return result


def set_genres(track_ids: list[int], genre_ids: list[int], mode: str) -> BulkResult:
    """Add to or replace each track's genre tags.

    M2M edits cannot go through ``QuerySet.update``, so this loops. The loop is
    bounded by the page size of the UI rather than by the catalogue.
    """
    if mode not in ("add", "replace"):
        raise BulkActionError("Genre updates must be either “add” or “replace”.")

    genres = list(Genre.objects.filter(pk__in=genre_ids))
    tracks = list(Track.objects.filter(pk__in=track_ids))
    result = BulkResult()

    for track in tracks:
        if mode == "replace":
            track.genres.set(genres)
        else:
            track.genres.add(*genres)
        result.updated += 1

    if genres:
        label = ", ".join(g.name for g in genres[:3])
        more = f" (+{len(genres) - 3} more)" if len(genres) > 3 else ""
        verb = "Set to" if mode == "replace" else "Tagged with"
        result.notes.append(f"{verb} {label}{more}.")
    return result


def set_flag(track_ids: list[int], field_name: str, value: bool) -> BulkResult:
    """Toggle a plain boolean column (``explicit``)."""
    if field_name not in ("explicit",):
        raise BulkActionError(f"“{field_name}” is not an editable flag.")
    result = BulkResult()
    result.updated = Track.objects.filter(pk__in=track_ids).update(**{field_name: value})
    return result


def reset_counters(track_ids: list[int], counters: list[str]) -> BulkResult:
    """Zero out engagement counters, e.g. after bad traffic inflated them.

    Refuses to wipe them unless the caller names them explicitly, because the
    daily rollup tables and the cached ``play_count`` would otherwise disagree.
    """
    allowed = {"play_count", "like_count", "download_count"}
    unknown = set(counters) - allowed
    if unknown:
        raise BulkActionError(
            "Cannot reset " + ", ".join(sorted(unknown)) + "."
        )
    if not counters:
        raise BulkActionError("Choose at least one counter to reset.")

    result = BulkResult()
    result.updated = Track.objects.filter(pk__in=track_ids).update(
        **{name: 0 for name in counters}
    )
    result.notes.append(
        "Counters were zeroed on the tracks. Historical play events were left intact; "
        "rebuild the rollups to make the aggregates agree again."
    )
    return result


def delete_tracks(track_ids: list[int]) -> BulkResult:
    """Delete tracks outright, including their play events and comments."""
    result = BulkResult()
    result.updated = Track.objects.filter(pk__in=track_ids).delete()[0]
    result.notes.append("Deleted permanently.")
    return result


# --- CSV export -------------------------------------------------------------

TRACK_CSV_COLUMNS = (
    "id",
    "title",
    "slug",
    "artist",
    "album",
    "track_number",
    "status",
    "duration_seconds",
    "explicit",
    "release_date",
    "isrc",
    "bitrate_kbps",
    "sample_rate_hz",
    "genres",
    "play_count",
    "like_count",
    "download_count",
    "audio_file",
    "created_at",
    "updated_at",
)


def filter_tracks(queryset, *, status: str = "", artist_id: str = "", album_id: str = "",
                  genre_id: str = "", search: str = "", has_audio: str = ""):
    """Apply the dashboard's catalogue filters to a track queryset."""
    if status:
        queryset = queryset.filter(status=status)
    if artist_id:
        queryset = queryset.filter(artist_id=artist_id)
    if album_id:
        queryset = queryset.filter(album_id=album_id)
    if genre_id:
        queryset = queryset.filter(genres__id=genre_id)
    if search:
        from django.db.models import Q

        queryset = queryset.filter(
            Q(title__icontains=search)
            | Q(isrc__icontains=search)
            | Q(artist__name__icontains=search)
            | Q(album__title__icontains=search)
        )
    if has_audio == "yes":
        queryset = queryset.exclude(pk__in=missing_audio_ids())
    elif has_audio == "no":
        queryset = queryset.filter(pk__in=missing_audio_ids())

    return queryset


def track_row(track) -> dict:
    """One CSV record for a track."""
    return {
        "id": track.pk,
        "title": track.title,
        "slug": track.slug,
        "artist": track.artist.name,
        "album": track.album.title if track.album_id else "",
        "track_number": track.track_number if track.track_number is not None else "",
        "status": track.status,
        "duration_seconds": track.duration_seconds,
        "explicit": "yes" if track.explicit else "no",
        "release_date": track.release_date.isoformat() if track.release_date else "",
        "isrc": track.isrc,
        "bitrate_kbps": track.bitrate_kbps,
        "sample_rate_hz": track.sample_rate_hz,
        "genres": "; ".join(track.genres.order_by("name").values_list("name", flat=True)),
        "play_count": track.play_count,
        "like_count": track.like_count,
        "download_count": track.download_count,
        "audio_file": track.audio_file.name if track.audio_file else "",
        "created_at": track.created_at.isoformat() if track.created_at else "",
        "updated_at": track.updated_at.isoformat() if track.updated_at else "",
    }


def tracks_csv(queryset) -> str:
    """Render a track queryset as CSV text.

    Returns text rather than a streaming response so callers can unit test the
    bytes; the view wraps it in a ``TextResponse``. ``\\r\\n`` and a leading BOM
    are deliberate: Excel on Windows mis-reads plain ``\\n`` CSV and needs the
    BOM to detect UTF-8.
    """
    buffer = StringIO()
    writer = csv.DictWriter(
        buffer, fieldnames=TRACK_CSV_COLUMNS, lineterminator="\r\n", extrasaction="ignore"
    )
    writer.writeheader()
    for track in queryset:
        writer.writerow(track_row(track))
    return "﻿" + buffer.getvalue()


def catalogue_export_filename(prefix: str = "tracks") -> str:
    from django.utils import timezone

    return f"{slugify(prefix)}-{timezone.now().date().isoformat()}.csv"
