"""Track recommendations.

Two shapes, both derived from data already in the database rather than a model:

``similar_tracks(track)``
    "More like this" from a seed track: shared genres first, then the same
    artist, then neighbours in the same era.

``recommended_for(user)``
    A personalised feed built from what the listener has favourited and played.
    Falls back to trending when there is not enough personal signal, so a new
    account gets a usable feed instead of an empty list.

Scoring happens in Python over a small candidate set. That is deliberate: the
alternative (annotating a weighted score across several joins) produces row
multiplication that has to be de-duplicated, and the candidate pool here is
bounded by ``CANDIDATE_POOL`` anyway.
"""

from datetime import timedelta

import datetime

from django.db.models import Count, Q
from django.utils import timezone
from django.utils.dateparse import parse_date
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from music.models import Track

DEFAULT_LIMIT = 20
MAX_LIMIT = 50
CANDIDATE_POOL = 200

#: Score awarded per matching signal. Tuned so that a single shared genre
#: outranks a merely similar release date.
WEIGHT_SHARED_GENRE = 3.0
WEIGHT_SAME_ARTIST = 2.0
WEIGHT_SAME_ALBUM = 1.5
WEIGHT_NEIGHBOUR_ERA = 0.5
WEIGHT_SIMILAR_DURATION = 0.5
#: Popularity used to break ties between otherwise equal candidates.
WEIGHT_POPULARITY = 0.001


def _clamp_limit(value, default=DEFAULT_LIMIT) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return min(max(parsed, 1), MAX_LIMIT)


def _as_date(value):
    """Coerce a DateField value to a ``date``.

    ``Track.save()`` copies ``album.release_date`` across without coercing, so a
    track that was just created from an unsaved ``Album(release_date="2026-01-15")``
    still carries the raw string in memory. Reading that straight from an
    in-memory instance would blow up on subtraction.
    """
    if value is None or isinstance(value, datetime.date):
        return value
    if isinstance(value, datetime.datetime):
        return value.date()
    parsed = parse_date(str(value))
    return parsed


def _era_distance_days(left, right):
    """Absolute days between two dates, or ``None`` if either is missing."""
    left, right = _as_date(left), _as_date(right)
    if not left or not right:
        return None
    return abs((left - right).days)


def similar_tracks(track, limit=DEFAULT_LIMIT):
    """Tracks that sit next to ``track`` in the catalogue.

    The seed track and anything sharing its exact artist *and* album are still
    eligible: a single is a legitimate "play this next" suggestion, so only the
    seed itself is excluded here. Callers that want album siblings removed should
    say so at the call site.
    """
    limit = _clamp_limit(limit)

    genre_ids = list(track.genres.values_list("id", flat=True))
    window_start = timezone.localdate() - timedelta(days=365 * 5)

    queryset = (
        Track.objects.published()
        .with_listeners()
        .exclude(pk=track.pk)
        .select_related("artist", "album")
        .prefetch_related("genres")
    )

    # Restrict to a bounded candidate pool *before* scoring, otherwise every
    # published track is loaded and ranked per request.
    if genre_ids:
        queryset = queryset.filter(
            Q(genres__id__in=genre_ids) | Q(artist_id=track.artist_id)
        )
    else:
        queryset = queryset.filter(artist_id=track.artist_id)
    # distinct() is mandatory: the genres join fans a track out into one row per
    # matching genre, which would otherwise return the same track repeatedly.
    queryset = queryset.distinct().order_by("-play_count")[:CANDIDATE_POOL]

    candidates = list(queryset)
    if not candidates:
        return []

    genre_ids = set(genre_ids)
    duration = track.duration_seconds or 0

    scored = []
    for candidate in candidates:
        score = 0.0

        shared = genre_ids & {g.pk for g in candidate.genres.all()}
        if shared:
            score += WEIGHT_SHARED_GENRE * len(shared)

        if candidate.artist_id == track.artist_id:
            score += WEIGHT_SAME_ARTIST

        if track.album_id and candidate.album_id == track.album_id:
            score += WEIGHT_SAME_ALBUM

        gap = _era_distance_days(candidate.release_date, track.release_date)
        if gap is not None and gap <= 730:
            score += WEIGHT_NEIGHBOUR_ERA * (1 - (gap / 730))

        # Compare against a 45s band: listeners notice big runtime differences.
        if duration:
            drift = abs((candidate.duration_seconds or 0) - duration)
            if drift <= 45:
                score += WEIGHT_SIMILAR_DURATION

        score += candidate.play_count * WEIGHT_POPULARITY
        scored.append((score, candidate))

    scored.sort(key=lambda pair: (-pair[0], pair[1].title))
    return [candidate for score, candidate in scored[:limit] if score > 0]


def _taste_seeds(user, *, play_window_days=30, cap=100):
    """Seed tracks describing what the listener likes, strongest signal first.

    Favourites and deduped history are explicit intent, so they come first and
    get the highest weight. Raw play events are appended afterwards: someone
    with no favourites but a real listening history still deserves a
    personalised feed rather than the generic trending shelf.
    """
    from analytics.models import PlayEvent
    from playlists.models import Favorite, PlayHistory

    seeds: list[int] = []

    seeds += list(
        Favorite.objects.filter(user=user).values_list("track_id", flat=True)[:cap]
    )
    seeds += list(
        PlayHistory.objects.filter(user=user).values_list("track_id", flat=True)[:cap]
    )

    cutoff = timezone.now() - timedelta(days=play_window_days)
    played = (
        PlayEvent.objects.filter(user=user, played_at__gte=cutoff)
        .order_by("-played_at")
        .values_list("track_id", flat=True)[:cap]
    )
    seeds += list(played)

    # Preserve order while dropping repeats: weight is positional, so a track
    # appearing twice must not push later signals down.
    seen: set[int] = set()
    unique: list[int] = []
    for track_id in seeds:
        if track_id not in seen:
            seen.add(track_id)
            unique.append(track_id)
    return unique


def recommended_for(user, limit=DEFAULT_LIMIT):
    """A personalised feed from favourites, history and recent listening.

    Anonymous users (or accounts with no signal yet) get the trending shelf so
    the endpoint never returns an empty array for a valid request.
    """
    limit = _clamp_limit(limit)

    if user is None or not getattr(user, "is_authenticated", False):
        return _fallback(limit)

    seed_ids = _taste_seeds(user)
    if not seed_ids:
        return _fallback(limit)

    seeds = list(
        Track.objects.published()
        .with_listeners()
        .filter(pk__in=seed_ids)
        .prefetch_related("genres")
    )
    if not seeds:
        return _fallback(limit)

    # Rank the seeds back into the strength order _taste_seeds produced, so the
    # strongest intent still counts for the most.
    rank = {track_id: index for index, track_id in enumerate(seed_ids)}

    genre_taste: dict[int, float] = {}
    artist_taste: dict[int, float] = {}
    for seed in seeds:
        weight = 1.0 / (rank.get(seed.pk, len(rank)) + 1)
        for genre in seed.genres.all():
            genre_taste[genre.pk] = genre_taste.get(genre.pk, 0.0) + weight
        artist_taste[seed.artist_id] = artist_taste.get(seed.artist_id, 0.0) + weight

    genre_ids = list(genre_taste)
    artist_ids = list(artist_taste)
    pool = (
        Track.objects.published()
        .with_listeners()
        .exclude(pk__in=seed_ids)
        .filter(Q(genres__id__in=genre_ids) | Q(artist_id__in=artist_ids))
        .select_related("artist", "album")
        .prefetch_related("genres")
        .distinct()
        .order_by("-play_count")[:CANDIDATE_POOL]
    )

    # De-duplicate by pk: the genres join fans a track out per matching genre.
    candidates = {candidate.pk: candidate for candidate in pool}
    scored = []
    for candidate in candidates.values():
        score = sum(
            genre_taste.get(genre.pk, 0.0) for genre in candidate.genres.all()
        )
        score += artist_taste.get(candidate.artist_id, 0.0)
        score += candidate.play_count * WEIGHT_POPULARITY
        scored.append((score, candidate))

    if not scored:
        return _fallback(limit)

    scored.sort(key=lambda pair: (-pair[0], pair[1].title))
    return [candidate for score, candidate in scored[:limit]]


def _fallback(limit):
    """Trending, then popular, for users with no personal signal."""
    trending = list(Track.objects.trending()[:limit])
    if len(trending) >= limit:
        return trending
    seen = {track.pk for track in trending}
    filler = Track.objects.published().with_listeners().exclude(pk__in=seen)
    trending.extend(filler.order_by("-play_count")[: limit - len(trending)])
    return trending


def genre_affinity(user, days: int = 90):
    """Top genres by listening events, as ``[{"genre": .., "score": ..}]``.

    Exposed for dashboards, which show a listener profile alongside the feed.
    """
    from analytics.models import PlayEvent

    cutoff = timezone.now() - timedelta(days=days)
    rows = (
        PlayEvent.objects.filter(user=user, played_at__gte=cutoff)
        .values("track__genres__id", "track__genres__name")
        .annotate(plays=Count("id"))
        .order_by("-plays")[:20]
    )
    return [
        {"genre_id": row["track__genres__id"], "name": row["track__genres__name"], "plays": row["plays"]}
        for row in rows
        if row["track__genres__id"]
    ]


class RecommendedTracksView(APIView):
    """``GET /api/recommendations/`` — a personalised feed.

    Anonymous callers get the trending shelf rather than a 401: the endpoint is
    on a public discovery surface, and there is a sensible answer that does not
    need an identity.
    """

    permission_classes = [AllowAny]

    @extend_schema(
        tags=["recommendations"],
        parameters=[OpenApiParameter("limit", int, description="1-50, default 20.")],
        responses={200: None},
        summary="Recommended tracks",
    )
    def get(self, request):
        from music.serializers import TrackListSerializer

        limit = _clamp_limit(request.query_params.get("limit"))
        tracks = recommended_for(
            request.user if request.user.is_authenticated else None, limit=limit
        )
        data = TrackListSerializer(tracks, many=True, context={"request": request}).data

        if request.user.is_authenticated:
            data = {
                "personalised": _has_personal_signal(request.user),
                "tracks": data,
                "genre_affinity": genre_affinity(request.user),
            }
        else:
            data = {"personalised": False, "tracks": data, "genre_affinity": []}
        return Response(data)


def _has_personal_signal(user) -> bool:
    """True when the user has enough signal for a personalised feed.

    Must agree with ``_taste_seeds`` or the payload claims "personalised" while
    serving the generic trending shelf.
    """
    return bool(_taste_seeds(user, cap=1))
