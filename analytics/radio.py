"""Radio stations: endless, seed-driven playback queues.

A station blends three signals, in order of relevance:

1. tracks acoustically near the seed (``recommend.similar_tracks``);
2. the seed's own back catalogue (artist) or library (genre);
3. a popularity tail, so a station whose seed has few neighbours still fills.

The public entry point is :func:`build_station`, which dispatches on
``seed_type``. It returns ``None`` for an unknown or missing seed so the view
can answer 404 rather than guessing.
"""

from music.models import Artist, Genre, Track

from .recommend import similar_tracks

DEFAULT_LIMIT = 30
MAX_LIMIT = 100


def _clamp(limit) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return min(max(value, 1), MAX_LIMIT)


def _dedupe(*groups, limit):
    """Flatten iterables of tracks, dropping repeats, capped at ``limit``."""
    seen = set()
    out = []
    for group in groups:
        for track in group:
            if track.pk in seen:
                continue
            seen.add(track.pk)
            out.append(track)
            if len(out) >= limit:
                return out
    return out


def _popularity_tail(limit, exclude_ids):
    if limit <= 0:
        return []
    queryset = Track.objects.published().with_listeners()
    if exclude_ids:
        queryset = queryset.exclude(pk__in=exclude_ids)
    return queryset.order_by("-play_count")[:limit]


def _fill(tracks, limit):
    """Top up ``tracks`` from the popularity tail, preserving seed order."""
    if len(tracks) >= limit:
        return tracks[:limit]
    tail = _popularity_tail(limit - len(tracks), {track.pk for track in tracks})
    return _dedupe(tracks, tail, limit=limit)


def _find(model, pk):
    if pk in (None, ""):
        return None
    try:
        return model.objects.filter(pk=pk).first()
    except (ValueError, TypeError):
        return None


def station_for_track(track, limit=DEFAULT_LIMIT):
    return _fill(_dedupe([track], similar_tracks(track, limit=limit), limit=limit), limit)


def station_for_artist(artist, limit=DEFAULT_LIMIT):
    own = artist.tracks.published().with_listeners().order_by("-play_count")
    ordered = _dedupe(own, limit=limit)
    if ordered:
        ordered = _dedupe(ordered, similar_tracks(ordered[0], limit=limit), limit=limit)
    return _fill(ordered, limit)


def station_for_genre(genre, limit=DEFAULT_LIMIT):
    tracks = (
        genre.tracks.published().with_listeners().order_by("-play_count")
    )
    return _fill(_dedupe(tracks, limit=limit), limit)


def station_for_playlist(playlist, limit=DEFAULT_LIMIT):
    tracks = [
        entry.track
        for entry in playlist.playlist_tracks.select_related("track__artist", "track__album")
        if entry.track.status == Track.Status.PUBLISHED
    ]
    tracks = _dedupe(tracks, limit=limit)
    if tracks:
        tracks = _dedupe(tracks, similar_tracks(tracks[0], limit=limit), limit=limit)
    return _fill(tracks, limit)


def station_for_user(user, limit=DEFAULT_LIMIT):
    """A personal mix: the listener's favourites, widened by recommendation."""
    from playlists.models import Favorite

    favourite_ids = list(
        Favorite.objects.filter(user=user).values_list("track_id", flat=True)[:limit]
    )
    seeds = list(
        Track.objects.published()
        .with_listeners()
        .filter(pk__in=favourite_ids)
    )
    # Preserve favourite order (newest first): IN-clauses give no ordering.
    order = {track_id: index for index, track_id in enumerate(favourite_ids)}
    seeds.sort(key=lambda track: order.get(track.pk, len(order)))

    widened = []
    for seed in seeds[:5]:
        widened.extend(similar_tracks(seed, limit=5))
    mixed = _dedupe(seeds, widened, recommended_for_safe(user, limit), limit=limit)
    return _fill(mixed, limit)


def recommended_for_safe(user, limit):
    """``recommended_for`` that never raises when the catalogue is empty."""
    try:
        from .recommend import recommended_for

        return recommended_for(user, limit=limit)
    except Exception:  # pragma: no cover - defensive: never break a station
        return []


def build_station(seed_type, seed_id, *, user=None, limit=DEFAULT_LIMIT):
    """Return ``{"seed_type", "seed_label", "tracks"}`` or ``None``."""
    limit = _clamp(limit)
    seed_type = (seed_type or "track").lower()

    if seed_type == "track":
        seed = _find(Track, seed_id)
        if seed is None or seed.status != Track.Status.PUBLISHED:
            return None
        return _payload(seed_type, f"{seed.title} · {seed.artist.name}", station_for_track(seed, limit))

    if seed_type == "artist":
        seed = _find(Artist, seed_id)
        if seed is None:
            return None
        return _payload(seed_type, seed.name, station_for_artist(seed, limit))

    if seed_type == "genre":
        seed = _find(Genre, seed_id)
        if seed is None:
            return None
        return _payload(seed_type, seed.name, station_for_genre(seed, limit))

    if seed_type == "playlist":
        from playlists.models import Playlist

        seed = _find(Playlist, seed_id)
        if seed is None or not seed.is_visible_to(user):
            return None
        return _payload(seed_type, seed.name, station_for_playlist(seed, limit))

    if seed_type in {"user", "my_mix", "favorites"}:
        if user is None or not getattr(user, "is_authenticated", False):
            return None
        return _payload("my_mix", "Your mix", station_for_user(user, limit))

    return None


def _payload(seed_type, label, tracks):
    return {"seed_type": seed_type, "seed_label": label, "tracks": tracks}
