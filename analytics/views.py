"""Analytics endpoints: play statistics, leaderboards and platform dashboards."""

from django.db.models import Count
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers, status, viewsets
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from music.models import Track
from music.permissions import IsAdminStaff
from music.serializers import TrackSummarySerializer

from . import services
from .models import DailyPlatformStat, DailyTrackStat, PlayEvent
class PlayEventSerializer(serializers.ModelSerializer):
    track_title = serializers.CharField(source="track.title", read_only=True)
    artist_name = serializers.CharField(source="track.artist.name", read_only=True)

    class Meta:
        model = PlayEvent
        fields = (
            "id", "track", "track_title", "artist_name", "user", "source",
            "played_at", "listened_seconds", "completed",
        )
        read_only_fields = fields


class DailyTrackStatSerializer(serializers.ModelSerializer):
    track_title = serializers.CharField(source="track.title", read_only=True)

    class Meta:
        model = DailyTrackStat
        fields = ("track", "track_title", "date", "plays", "unique_listeners", "total_listened_seconds")
        read_only_fields = fields


class DailyPlatformStatSerializer(serializers.ModelSerializer):
    class Meta:
        model = DailyPlatformStat
        fields = ("date", "total_plays", "unique_listeners", "unique_tracks", "by_source")
        read_only_fields = fields


class PlatformStatsView(APIView):
    """``GET /api/analytics/summary/`` — everything the dashboard needs, JSON."""

    permission_classes = [IsAdminStaff]

    @extend_schema(
        tags=["analytics"],
        parameters=[OpenApiParameter("days", int, description="Window size, 1-90. Default 30.")],
        responses={200: None},
        summary="Platform-wide statistics",
    )
    def get(self, request):
        days = _bounded_int(request.query_params.get("days"), default=30, low=1, high=90)
        return Response(
            {
                "overview": services.catalogue_overview(),
                "engagement": services.engagement_stats(days=min(days, 30)),
                "growth": services.growth_series(days=min(days, 90)),
                "timeseries": services.plays_timeseries(days=min(days, 30)),
                "top_tracks": _serialize_tracks(services.top_tracks(limit=10, days=days)),
                "trending_tracks": _serialize_tracks(services.trending_tracks(limit=10)),
                "play_distribution": services.play_distribution(),
                "genre_distribution": services.genre_distribution(),
                "uploads": services.new_content_series(days=14),
            }
        )


class TrackAnalyticsView(APIView):
    """Per-track analytics for a single track (staff or public read)."""

    permission_classes = [AllowAny]

    @extend_schema(tags=["analytics"], responses={200: None}, summary="Track analytics")
    def get(self, request, pk):
        track = Track.objects.filter(pk=pk).select_related("artist").first()
        if not track:
            return Response({"detail": "Track not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(
            {
                "track_id": track.id,
                "title": track.title,
                "artist": track.artist.name,
                "play_count": track.play_count,
                "like_count": track.like_count,
                "download_count": track.download_count,
                "plays_today": track.plays_today,
                "favorites": track.favorited_by.count(),
                "playlist_appearances": track.playlist_entries.count(),
                "daily": list(
                    DailyTrackStat.objects.filter(track=track)
                    .order_by("date")
                    .values("date", "plays", "unique_listeners")
                ),
                "sources": list(
                    PlayEvent.objects.filter(track=track)
                    .values("source")
                    .annotate(plays=Count("id"))
                    .order_by("-plays")
                ),
            }
        )


class TopTracksView(APIView):
    """``GET /api/analytics/top-tracks/``"""

    permission_classes = [AllowAny]

    @extend_schema(
        tags=["analytics"],
        parameters=[OpenApiParameter("limit", int), OpenApiParameter("days", int)],
        responses={200: TrackSummarySerializer(many=True)},
        summary="Most-played tracks",
    )
    def get(self, request):
        limit = _bounded_int(request.query_params.get("limit"), default=10, low=1, high=50)
        days = request.query_params.get("days")
        days = _bounded_int(days, default=0, low=0, high=90) or None
        tracks = services.top_tracks(limit=limit, days=days)
        return Response(_serialize_tracks(tracks, request))


class TrendingTracksAnalyticsView(APIView):
    """``GET /api/analytics/trending/``"""

    permission_classes = [AllowAny]

    @extend_schema(
        tags=["analytics"],
        parameters=[OpenApiParameter("limit", int), OpenApiParameter("days", int)],
        responses={200: TrackSummarySerializer(many=True)},
        summary="Decay-weighted trending tracks",
    )
    def get(self, request):
        limit = _bounded_int(request.query_params.get("limit"), default=10, low=1, high=50)
        days = _bounded_int(request.query_params.get("days"), default=7, low=1, high=30)
        tracks = services.trending_tracks(limit=limit, days=days)
        return Response(_serialize_tracks(tracks, request))


class DailyStatsViewSet(viewsets.ReadOnlyModelViewSet):
    """Rollup rows produced by ``manage.py aggregate_plays``."""

    queryset = DailyTrackStat.objects.select_related("track")
    serializer_class = DailyTrackStatSerializer
    permission_classes = [IsAdminStaff]
    filterset_fields = ["date", "track"]
    ordering = ("-date",)
    http_method_names = ["get", "head", "options"]


class PlayEventAdminViewSet(viewsets.ReadOnlyModelViewSet):
    """Raw event log for support and debugging."""

    queryset = PlayEvent.objects.select_related("track", "user")
    serializer_class = PlayEventSerializer
    permission_classes = [IsAdminStaff]
    filterset_fields = ["track", "user", "source", "completed"]
    ordering = ("-played_at",)
    http_method_names = ["get", "head", "options"]


class ArtistLeaderboardView(APIView):
    """``GET /api/analytics/artists/``"""

    permission_classes = [AllowAny]

    @extend_schema(
        tags=["analytics"],
        parameters=[OpenApiParameter("limit", int), OpenApiParameter("days", int)],
        responses={200: None},
        summary="Artist leaderboard",
    )
    def get(self, request):
        limit = _bounded_int(request.query_params.get("limit"), default=10, low=1, high=50)
        days = _bounded_int(request.query_params.get("days"), default=30, low=1, high=365)
        artists = services.leaderboard_artists(limit=limit, days=days)
        return Response(
            [
                {
                    "id": artist.id,
                    "name": artist.name,
                    "image": artist.image.url if artist.image else None,
                    "monthly_listeners": artist.monthly_listeners,
                    "window_plays": artist.window_plays,
                    "total_plays": artist.total_plays_sum or 0,
                }
                for artist in artists
            ]
        )


class RebuildStatsView(APIView):
    """Trigger the rollup on demand (staff only)."""

    permission_classes = [IsAdminStaff]

    @extend_schema(tags=["analytics"], request=None, responses={200: None}, summary="Rebuild rollups")
    def post(self, request):
        from django.core.management import call_command

        days = _bounded_int(request.data.get("days"), default=7, low=1, high=90)
        call_command("aggregate_plays", days=days, verbosity=0)
        return Response({"status": "ok", "days": days})


class RadioStationView(APIView):
    """``GET /api/radio/`` — an endless queue built from a seed.

    Seeds are identified by ``seed_type`` (``track``, ``artist``, ``genre``,
    ``playlist`` or ``my_mix``) plus a ``seed_id``. ``my_mix`` ignores the id
    and uses the caller's own favourites, so it needs a login.
    """

    permission_classes = [AllowAny]

    @extend_schema(
        tags=["recommendations"],
        parameters=[
            OpenApiParameter("seed_type", str, description="track|artist|genre|playlist|my_mix"),
            OpenApiParameter("seed_id", str, description="Seed entity id (ignored for my_mix)."),
            OpenApiParameter("limit", int, description="1-100, default 30."),
        ],
        responses={200: None, 404: None},
        summary="Radio station",
    )
    def get(self, request):
        from music.serializers import TrackListSerializer

        from .radio import build_station

        seed_type = request.query_params.get("seed_type", "track")
        seed_id = request.query_params.get("seed_id")
        limit = _bounded_int(request.query_params.get("limit"), default=30, low=1, high=100)
        user = request.user if request.user.is_authenticated else None

        station = build_station(seed_type, seed_id, user=user, limit=limit)
        if station is None:
            return Response(
                {"detail": "Unknown seed type, or the seed does not exist."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(
            {
                "seed_type": station["seed_type"],
                "seed_label": station["seed_label"],
                "count": len(station["tracks"]),
                "tracks": TrackListSerializer(
                    station["tracks"], many=True, context={"request": request}
                ).data,
            }
        )


def _serialize_tracks(tracks, request=None):
    return TrackSummarySerializer(tracks, many=True, context={"request": request} if request else {}).data


def _bounded_int(raw, default, low, high):
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))
