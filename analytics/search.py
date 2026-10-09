"""Cross-entity search.

One endpoint fans out across tracks, artists, albums, genres and playlists,
returning typed groups so a client can render a unified "search everything"
surface from a single round trip.
"""

from django.db.models import Count, Q
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from music.models import Album, Artist, Genre, Track
from music.serializers import (
    AlbumSummarySerializer,
    ArtistListSerializer,
    GenreSerializer,
    TrackListSerializer,
)
from playlists.models import Playlist
from playlists.serializers import PlaylistSerializer

MIN_QUERY_LENGTH = 2
DEFAULT_LIMIT = 10


class GlobalSearchView(APIView):
    """``GET /api/search/?q=...&types=tracks,artists&limit=10``"""

    permission_classes = [AllowAny]

    @extend_schema(
        tags=["search"],
        parameters=[
            OpenApiParameter("q", str, required=True, description="Search term (min 2 characters)."),
            OpenApiParameter(
                "types", str, description="Comma-separated subset of: tracks,artists,albums,genres,playlists."
            ),
            OpenApiParameter("limit", int, description=f"Results per group (1-50, default {DEFAULT_LIMIT})."),
        ],
        responses={200: None},
        summary="Search across all entities",
    )
    def get(self, request):
        query = (request.query_params.get("q") or request.query_params.get("search") or "").strip()
        limit = _clamp_int(request.query_params.get("limit"), DEFAULT_LIMIT, 1, 50)
        requested = _parse_types(request.query_params.get("types"))

        if len(query) < MIN_QUERY_LENGTH:
            return Response(
                {
                    "query": query,
                    "results": {},
                    "total": 0,
                    "detail": f"Provide at least {MIN_QUERY_LENGTH} characters to search.",
                }
            )

        context = {"request": request}
        results = {}

        if "tracks" in requested:
            tracks = (
                Track.objects.published()
                .with_listeners()
                .filter(track_query(query))
                .distinct()[:limit]
            )
            results["tracks"] = TrackListSerializer(tracks, many=True, context=context).data

        if "artists" in requested:
            artists = (
                Artist.objects.filter(Q(name__icontains=query) | Q(bio__icontains=query))
                .annotate(
                    agg_track_count=Count("tracks", distinct=True),
                    agg_album_count=Count("albums", distinct=True),
                )
                .distinct()[:limit]
            )
            results["artists"] = ArtistListSerializer(artists, many=True, context=context).data

        if "albums" in requested:
            albums = (
                Album.objects.filter(Q(title__icontains=query) | Q(artist__name__icontains=query))
                .select_related("artist")
                .annotate(agg_track_count=Count("tracks", distinct=True))
                .distinct()[:limit]
            )
            results["albums"] = AlbumSummarySerializer(albums, many=True, context=context).data

        if "genres" in requested:
            genres = (
                Genre.objects.filter(Q(name__icontains=query) | Q(description__icontains=query))
                .annotate(agg_track_count=Count("tracks", distinct=True))[:limit]
            )
            results["genres"] = GenreSerializer(genres, many=True, context=context).data

        if "playlists" in requested:
            user = request.user
            visibility = Q(visibility=Playlist.Visibility.PUBLIC)
            if user.is_authenticated and not user.is_admin_staff:
                visibility = visibility | Q(owner=user)
            playlists = (
                Playlist.objects.filter(visibility)
                .filter(Q(name__icontains=query) | Q(description__icontains=query))
                .select_related("owner")
                .annotate(entry_count=Count("tracks", distinct=True))
                .distinct()[:limit]
            )
            results["playlists"] = PlaylistSerializer(playlists, many=True, context=context).data

        return Response(
            {
                "query": query,
                "results": results,
                "total": sum(len(items) for items in results.values()),
            }
        )


class SearchSuggestionsView(GlobalSearchView):
    """``GET /api/search/suggest/?q=`` — lightweight typeahead (names only)."""

    @extend_schema(
        tags=["search"],
        parameters=[OpenApiParameter("q", str, required=True)],
        responses={200: None},
        summary="Typeahead suggestions",
    )
    def get(self, request):
        response = super().get(request)
        payload = response.data
        suggestions = []
        for track in payload.get("results", {}).get("tracks", []):
            suggestions.append({"type": "track", "label": track["title"], "id": track["id"], "slug": track["slug"]})
        for artist in payload.get("results", {}).get("artists", []):
            suggestions.append({"type": "artist", "label": artist["name"], "id": artist["id"], "slug": artist["slug"]})
        for album in payload.get("results", {}).get("albums", []):
            suggestions.append({"type": "album", "label": album["title"], "id": album["id"], "slug": album["slug"]})
        for genre in payload.get("results", {}).get("genres", []):
            suggestions.append({"type": "genre", "label": genre["name"], "id": genre["id"], "slug": genre["slug"]})
        return Response({"query": payload["query"], "suggestions": suggestions})


def track_query(query):
    """Match a free-text term against tracks.

    Public because the HTML search page (``music.site_views.SearchView``) reuses
    it so both surfaces rank identically.
    """
    return (
        Q(title__icontains=query)
        | Q(artist__name__icontains=query)
        | Q(album__title__icontains=query)
        | Q(genres__name__icontains=query)
    )


ALL_TYPES = ("tracks", "artists", "albums", "genres", "playlists")


def _parse_types(raw):
    if not raw:
        return ALL_TYPES
    wanted = {part.strip().lower() for part in raw.split(",") if part.strip()}
    allowed = {name for name in wanted if name in ALL_TYPES}
    return allowed or ALL_TYPES


def _clamp_int(raw, default, low, high):
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))
