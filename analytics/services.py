"""Aggregation services powering the dashboard and the analytics API.

Everything here is read-optimised and index-backed; nothing runs per-row in
Python except the decay-weighted trending score, which is bounded to a small
top-N window.
"""

from datetime import timedelta

from django.db.models import Avg, Count, F, Q, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

from analytics.models import PlayEvent
from music.models import Album, Artist, Genre, Track


def catalogue_overview() -> dict:
    """Headline counters for the dashboard tiles."""
    from playlists.models import Favorite, Playlist

    total_plays = Track.objects.aggregate(total=Sum("play_count"))["total"] or 0
    total_duration = Track.objects.aggregate(total=Sum("duration_seconds"))["total"] or 0
    return {
        "total_tracks": Track.objects.count(),
        "published_tracks": Track.objects.published().count(),
        "draft_tracks": Track.objects.filter(status=Track.Status.DRAFT).count(),
        "total_artists": Artist.objects.count(),
        "total_albums": Album.objects.count(),
        "total_genres": Genre.objects.count(),
        "total_playlists": Playlist.objects.count(),
        "public_playlists": Playlist.objects.filter(visibility="public").count(),
        "total_favorites": Favorite.objects.count(),
        "total_listeners": (
            PlayEvent.objects.exclude(user__isnull=True).values("user").distinct().count()
        ),
        "total_plays": total_plays,
        "total_duration_seconds": total_duration,
        "total_duration_hours": round(total_duration / 3600, 1),
    }


def top_tracks(limit: int = 10, days: int | None = None):
    """Most-played tracks, optionally restricted to a rolling window."""
    queryset = Track.objects.published().with_listeners()
    if days:
        cutoff = timezone.now() - timedelta(days=days)
        queryset = queryset.annotate(
            window_plays=Count("play_events", filter=Q(play_events__played_at__gte=cutoff))
        ).order_by("-window_plays", "-play_count")
    else:
        queryset = queryset.order_by("-play_count")
    return queryset[:limit]


def trending_tracks(limit: int = 10, days: int = 7):
    """Decay-weighted trending, computed over a bounded candidate set."""
    candidate_limit = max(limit * 20, 100)
    candidates = list(
        Track.objects.published()
        .with_listeners()
        .annotate(
            window_plays=Count(
                "play_events", filter=Q(play_events__played_at__gte=timezone.now() - timedelta(days=days))
            )
        )
        .order_by("-window_plays")[:candidate_limit]
    )
    scored = [(track.trending_score, track) for track in candidates]
    scored.sort(key=lambda pair: (pair[0], pair[1].play_count), reverse=True)
    return [track for _score, track in scored[:limit]]


def play_distribution(limit: int = 8):
    """Share of total plays per track — the basis for the popularity chart."""
    total = Track.objects.aggregate(total=Sum("play_count"))["total"] or 0
    rows = (
        Track.objects.published()
        .values("id", "title", "play_count")
        .order_by("-play_count")[:limit]
    )
    return [
        {
            "id": row["id"],
            "title": row["title"],
            "plays": row["play_count"],
            "share": round((row["play_count"] / total) * 100, 2) if total else 0.0,
        }
        for row in rows
    ]


def genre_distribution(limit: int = 8):
    total = (
        Track.objects.published()
        .aggregate(plays=Sum("play_count"))
    )["plays"] or 0
    rows = (
        Track.objects.published()
        .values("genres__id", "genres__name", "genres__color")
        .annotate(plays=Sum("play_count"), tracks=Count("id", distinct=True))
        .exclude(genres__isnull=True)
        .order_by("-plays")[:limit]
    )
    return [
        {
            "genre": row["genres__name"],
            "color": row["genres__color"],
            "plays": row["plays"] or 0,
            "tracks": row["tracks"],
            "share": round(((row["plays"] or 0) / total) * 100, 2) if total else 0.0,
        }
        for row in rows
    ]


def plays_timeseries(days: int = 14) -> list[dict]:
    """Zero-filled daily play counts, oldest first, ready for charting."""
    cutoff = (timezone.now() - timedelta(days=days - 1)).date()
    rows = (
        PlayEvent.objects.filter(played_at__date__gte=cutoff)
        .annotate(day=TruncDate("played_at"))
        .values("day")
        .annotate(plays=Count("id"), listeners=Count("user", filter=Q(user__isnull=False), distinct=True))
        .order_by("day")
    )
    by_day = {row["day"]: row for row in rows}
    series = []
    for offset in range(days - 1, -1, -1):
        day = (timezone.now() - timedelta(days=offset)).date()
        row = by_day.get(day)
        series.append(
            {
                "date": day,
                "plays": row["plays"] if row else 0,
                "listeners": row["listeners"] if row else 0,
            }
        )
    return series


def engagement_stats(days: int = 7) -> dict:
    """Listener-facing health metrics: uniques, completion, favourites."""
    from playlists.models import Favorite

    cutoff = timezone.now() - timedelta(days=days)
    recent = PlayEvent.objects.filter(played_at__gte=cutoff)
    return {
        "window_days": days,
        "plays": recent.count(),
        "unique_listeners": recent.exclude(user__isnull=True).values("user").distinct().count(),
        "unique_tracks": recent.values("track").distinct().count(),
        "completion_rate": round(
            (recent.filter(completed=True).count() / recent.count()) * 100, 1
        ) if recent.exists() else 0.0,
        "avg_listened_seconds": round(
            recent.aggregate(avg=Avg("listened_seconds"))["avg"] or 0, 1
        ),
        "favorites": Favorite.objects.count(),
        "plays_per_day": round(recent.count() / days, 1),
    }


def growth_series(days: int = 30) -> dict:
    """Compare the current window against the previous one."""
    now = timezone.now()
    current = PlayEvent.objects.filter(played_at__gte=now - timedelta(days=days))
    previous = PlayEvent.objects.filter(
        played_at__gte=now - timedelta(days=days * 2), played_at__lt=now - timedelta(days=days)
    )
    current_count = current.count()
    previous_count = previous.count()
    if previous_count == 0:
        change = 100.0 if current_count else 0.0
    else:
        change = round(((current_count - previous_count) / previous_count) * 100, 1)
    return {
        "current": current_count,
        "previous": previous_count,
        "change_percent": change,
        "window_days": days,
    }


def leaderboard_artists(limit: int = 10, days: int = 30):
    cutoff = timezone.now() - timedelta(days=days)
    return (
        Artist.objects.annotate(
            window_plays=Count(
                "tracks__play_events", filter=Q(tracks__play_events__played_at__gte=cutoff)
            ),
            total_plays_sum=Sum("tracks__play_count"),
        )
        .order_by("-window_plays", "-total_plays_sum")[:limit]
    )


def new_content_series(days: int = 14) -> list[dict]:
    """Uploads per day, for the activity chart."""
    now = timezone.now()
    rows = (
        Track.objects.filter(created_at__date__gte=(now - timedelta(days=days - 1)).date())
        .annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(count=Count("id"))
        .order_by("day")
    )
    by_day = {row["day"]: row["count"] for row in rows}
    return [
        {"date": (now - timedelta(days=offset)).date(), "tracks": by_day.get((now - timedelta(days=offset)).date(), 0)}
        for offset in range(days - 1, -1, -1)
    ]
