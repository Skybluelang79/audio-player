"""Playlist, favourite and play-history endpoints."""

from django.db import IntegrityError
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from music.models import Track
from music.permissions import IsPlaylistOwnerOrCollaborator

from .filters import FavoriteFilter, PlaylistFilter, with_playlist_counters
from .models import (
    Favorite,
    PlaybackQueue,
    PlayHistory,
    Playlist,
    PlaylistFollow,
    PlaylistTrack,
)
from .serializers import (
    AddTracksSerializer,
    BulkFavoriteSerializer,
    FavoriteSerializer,
    PlaybackQueueSerializer,
    PlaybackQueueWriteSerializer,
    PlayHistorySerializer,
    PlaylistDetailSerializer,
    PlaylistSerializer,
    PlaylistTrackSerializer,
    PlaylistWriteSerializer,
    ReorderSerializer,
)


def _int_param(request, name, *, default, minimum, maximum):
    """Clamp an integer query parameter, ignoring junk values.

    A malformed value (``?limit=abc``) must not become a 500, so fall back to
    the default instead of letting int() raise.
    """
    try:
        value = int(request.query_params.get(name, default))
    except (TypeError, ValueError):
        return default
    return min(max(value, minimum), maximum)


class PlaylistViewSet(viewsets.ModelViewSet):
    """CRUD for playlists with ordered membership.

    Public playlists are world-readable; unlisted ones are reachable by
    collaborators only; private ones are owner-only.
    """

    serializer_class = PlaylistSerializer
    permission_classes = [IsAuthenticated]
    filterset_class = PlaylistFilter
    ordering_fields = ("name", "created_at", "total_plays", "track_count")
    http_method_names = ["get", "post", "put", "patch", "delete", "head", "options"]

    #: ``with_playlist_counters`` aggregates across a multi-valued relation, and
    #: Django silently drops ``Playlist.Meta.ordering`` for any queryset with an
    #: aggregate annotation (it would be invalid SQL next to the GROUP BY).
    #: Without an explicit default the list endpoint returns rows in arbitrary
    #: order, which makes page boundaries non-deterministic. State it here so
    #: DRF's OrderingFilter applies a real ORDER BY.
    ordering = ("-created_at",)

    #: Public ordering key -> annotation alias (see with_playlist_counters).
    ordering_aliases = {"track_count": "entry_count"}

    def filter_queryset(self, queryset):
        # Translate the public alias to the real annotation before DRF's
        # OrderingFilter strips the param. Unknown keys are dropped here so
        # OrderingFilter can report them instead of us raising FieldError.
        requested = self.request.query_params.get("ordering")
        if requested:
            allowed = set(self.ordering_fields or ())
            remapped = [
                self.ordering_aliases.get(k.strip(), k.strip())
                for k in requested.split(",")
                if k.strip() in allowed
            ]
            if remapped:
                queryset = queryset.order_by(*remapped)
        return super().filter_queryset(queryset)

    #: Every action that writes to a playlist. Each of these requires
    #: object-level ownership/collaborator rights, not just a logged-in user.
    owner_actions = frozenset(
        {
            "update",
            "partial_update",
            "destroy",
            "add_track",
            "add_tracks",
            "remove_track",
            "reorder",
        }
    )

    def get_permissions(self):
        if self.action in ("list", "retrieve", "public", "trending"):
            return [AllowAny()]
        if self.action in self.owner_actions:
            return [IsAuthenticated(), IsPlaylistOwnerOrCollaborator()]
        return [IsAuthenticated()]

    def get_serializer_class(self):
        if self.action in ("create", "update", "partial_update"):
            return PlaylistWriteSerializer
        if self.action == "retrieve":
            return PlaylistDetailSerializer
        return PlaylistSerializer

    def get_queryset(self):
        queryset = with_playlist_counters(
            Playlist.objects.select_related("owner").prefetch_related(
                "playlist_tracks__track__artist"
            )
        )
        user = self.request.user
        is_staff = bool(user.is_authenticated and user.is_admin_staff)
        if not is_staff and self.action in ("list", "retrieve"):
            if user.is_authenticated:
                queryset = queryset.filter(_visible_to(user))
            else:
                queryset = queryset.filter(visibility=Playlist.Visibility.PUBLIC)
        elif not is_staff and self.action == "follow":
            # Following is a public, social action: never expose private or
            # unlisted playlists to it, so IDs cannot be enumerated this way.
            queryset = queryset.filter(visibility=Playlist.Visibility.PUBLIC)
        return queryset

    def get_object(self):
        playlist = super().get_object()
        self.check_object_permissions(self.request, playlist)
        return playlist

    def perform_create(self, serializer):
        serializer.save()

    def create(self, request, *args, **kwargs):
        """Return the read serializer so the owner/track_count fields come back.

        PlaylistWriteSerializer intentionally omits computed fields, which would
        otherwise leave the client unable to tell who owns what it just created.
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        playlist = serializer.save()
        output = PlaylistDetailSerializer(playlist, context=self.get_serializer_context())
        return Response(output.data, status=status.HTTP_201_CREATED)

    def destroy(self, request, *args, **kwargs):
        """Deletion is owner-only; collaborators may edit but not destroy."""
        playlist = self.get_object()
        if playlist.owner_id != request.user.pk and not request.user.is_superuser:
            return Response(
                {"detail": "Only the playlist owner can delete it."},
                status=status.HTTP_403_FORBIDDEN,
            )
        playlist.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(
        tags=["playlists"],
        request=PlaylistTrackSerializer,
        responses={201: PlaylistTrackSerializer},
        summary="Add a track to the playlist",
    )
    @action(detail=True, methods=["post"])
    def add_track(self, request, pk=None):
        playlist = self.get_object()
        serializer = PlaylistTrackSerializer(
            data=request.data, context={**self.get_serializer_context(), "playlist": playlist}
        )
        serializer.is_valid(raise_exception=True)
        entry = serializer.save()
        return Response(
            PlaylistTrackSerializer(entry, context=self.get_serializer_context()).data,
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(
        tags=["playlists"],
        request=AddTracksSerializer,
        responses={200: None},
        summary="Add many tracks to the playlist",
    )
    @action(detail=True, methods=["post"], url_path="add-tracks")
    def add_tracks(self, request, pk=None):
        playlist = self.get_object()
        serializer = AddTracksSerializer(data=request.data, context={"request": request, "playlist": playlist})
        serializer.is_valid(raise_exception=True)
        return Response(serializer.save())

    @extend_schema(
        tags=["playlists"],
        request=None,
        responses={204: None, 404: None},
        summary="Remove a track from the playlist",
    )
    @action(detail=True, methods=["delete"], url_path=r"tracks/(?P<track_id>[^/.]+)")
    def remove_track(self, request, pk=None, track_id=None):
        playlist = self.get_object()
        deleted, _ = playlist.playlist_tracks.filter(track_id=track_id).delete()
        if not deleted:
            return Response({"detail": "Track is not in this playlist."}, status=status.HTTP_404_NOT_FOUND)
        playlist.reorder(list(playlist.playlist_tracks.order_by("position").values_list("track_id", flat=True)))
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(
        tags=["playlists"],
        request=ReorderSerializer,
        responses={200: PlaylistDetailSerializer},
        summary="Reorder tracks",
    )
    @action(detail=True, methods=["post"])
    def reorder(self, request, pk=None):
        playlist = self.get_object()
        serializer = ReorderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        playlist.reorder(serializer.validated_data["track_ids"])
        playlist.refresh_from_db()
        return Response(PlaylistDetailSerializer(playlist, context=self.get_serializer_context()).data)

    @extend_schema(tags=["playlists"], request=None, responses={200: PlaylistSerializer, 201: None},
                   summary="Follow / unfollow a playlist")
    @action(detail=True, methods=["post", "delete"], permission_classes=[IsAuthenticated])
    def follow(self, request, pk=None):
        playlist = self.get_object()
        from .models import PlaylistFollow

        if request.method == "POST":
            PlaylistFollow.objects.get_or_create(user=request.user, playlist=playlist)
        else:
            PlaylistFollow.objects.filter(user=request.user, playlist=playlist).delete()
        return Response({"is_following": request.method == "POST",
                         "follower_count": playlist.followers.count()})

    @extend_schema(tags=["playlists"], parameters=[OpenApiParameter("limit", int)],
                   responses={200: PlaylistSerializer(many=True)}, summary="Public playlists")
    @action(detail=False, methods=["get"])
    def public(self, request):
        limit = _int_param(request, "limit", default=20, minimum=1, maximum=50)
        playlists = with_playlist_counters(
            Playlist.objects.filter(visibility=Playlist.Visibility.PUBLIC)
            .select_related("owner")
            .order_by("-total_plays")
        )[:limit]
        return Response(PlaylistSerializer(playlists, many=True, context={"request": request}).data)

    @extend_schema(tags=["playlists"], parameters=[OpenApiParameter("days", int), OpenApiParameter("limit", int)],
                   responses={200: PlaylistSerializer(many=True)}, summary="Trending playlists")
    @action(detail=False, methods=["get"])
    def trending(self, request):
        from .models import trending_playlists

        limit = _int_param(request, "limit", default=10, minimum=1, maximum=50)
        days = _int_param(request, "days", default=7, minimum=1, maximum=90)
        playlists = with_playlist_counters(
            trending_playlists(days=days, limit=limit).select_related("owner")
        )
        return Response(PlaylistSerializer(playlists, many=True, context={"request": request}).data)


def _visible_to(user):
    """Q object: public playlists plus anything owned by or shared with ``user``."""
    return Q(visibility=Playlist.Visibility.PUBLIC) | Q(owner=user) | Q(collaborators=user)


class FavoriteViewSet(viewsets.ModelViewSet):
    """Idempotent favourite/unfavourite. Always scoped to the current user."""

    serializer_class = FavoriteSerializer
    permission_classes = [IsAuthenticated]
    filterset_class = FavoriteFilter
    http_method_names = ["get", "post", "delete", "head", "options"]

    def get_queryset(self):
        return (
            Favorite.objects.filter(user=self.request.user)
            .select_related("track__artist", "track__album")
            .order_by("-created_at")
        )

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            favorite = serializer.save(user=request.user)
        except IntegrityError:
            return Response({"detail": "Already in favourites."}, status=status.HTTP_200_OK)
        return Response(
            FavoriteSerializer(favorite, context=self.get_serializer_context()).data,
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(tags=["favorites"], request=None, responses={200: FavoriteSerializer, 404: None},
                   summary="Toggle favourite state for a track")
    @action(detail=False, methods=["post", "delete"], permission_classes=[IsAuthenticated],
            url_path=r"tracks/(?P<track_id>[^/.]+)")
    def toggle(self, request, track_id=None):
        # published() only: never expose drafts through the favourites API.
        track = get_object_or_404(Track.objects.published(), pk=track_id)
        existing = Favorite.objects.filter(user=request.user, track=track).first()
        if request.method == "POST" and existing is None:
            existing = Favorite.objects.create(user=request.user, track=track)
        elif request.method == "DELETE" and existing is not None:
            existing.delete()
        # Identical body for both verbs so clients can branch on one shape.
        return Response({
            "is_favorite": request.method == "POST",
            "favorite_count": track.favorited_by.count(),
        })

    @extend_schema(
        tags=["favorites"],
        request=BulkFavoriteSerializer,
        responses={200: None},
        summary="Add many favourites at once",
    )
    @action(detail=False, methods=["post"])
    def bulk_add(self, request):
        serializer = BulkFavoriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        track_ids = serializer.validated_data["track_ids"]
        tracks = list(Track.objects.published().filter(pk__in=track_ids))
        existing = set(
            Favorite.objects.filter(user=request.user, track__in=tracks).values_list(
                "track_id", flat=True
            )
        )
        to_create = [
            Favorite(user=request.user, track=track) for track in tracks if track.pk not in existing
        ]
        Favorite.objects.bulk_create(to_create, ignore_conflicts=True)
        return Response({
            "added": len(to_create),
            "already_present": len(existing),
            "requested": len(track_ids),
        })


class PlayHistoryViewSet(viewsets.ReadOnlyModelViewSet):
    """Per-user listening history, newest first."""

    serializer_class = PlayHistorySerializer
    permission_classes = [IsAuthenticated]
    ordering = ("-last_played_at",)
    ordering_fields = ("last_played_at", "play_count")
    # `clear` and `remove` are DELETE actions, so "delete" must be allowed here.
    http_method_names = ["get", "delete", "head", "options"]

    def get_queryset(self):
        return (
            PlayHistory.objects.filter(user=self.request.user)
            .select_related("track__artist", "track__album")
            .order_by("-last_played_at")
        )

    @extend_schema(tags=["history"], request=None, responses={200: None}, summary="Clear history")
    @action(detail=False, methods=["delete"], permission_classes=[IsAuthenticated])
    def clear(self, request):
        deleted, _ = self.get_queryset().delete()
        return Response({"deleted": deleted})

    @extend_schema(tags=["history"], request=None, responses={200: None}, summary="Remove one history entry")
    @action(detail=False, methods=["delete"], permission_classes=[IsAuthenticated],
            url_path=r"tracks/(?P<track_id>[^/.]+)")
    def remove(self, request, track_id=None):
        deleted, _ = self.get_queryset().filter(track_id=track_id).delete()
        if not deleted:
            return Response({"detail": "No history entry for that track."}, status=status.HTTP_404_NOT_FOUND)
        return Response(status=status.HTTP_204_NO_CONTENT)


class PlaybackQueueView(APIView):
    """``/api/queue/`` — the caller's saved play queue.

    ``GET`` returns an empty queue shape rather than 404 for someone who has
    never saved one, so a client booting cold needs no special case. ``PUT``
    replaces the whole queue; ``DELETE`` clears it.
    """

    permission_classes = [IsAuthenticated]

    def _queue(self):
        return (
            PlaybackQueue.objects.filter(user=self.request.user)
            .prefetch_related("items__track__artist", "items__track__album")
            .first()
        )

    @extend_schema(tags=["queue"], responses={200: PlaybackQueueSerializer}, summary="Fetch saved queue")
    def get(self, request):
        queue = self._queue()
        if queue is None:
            return Response(
                {
                    "id": None,
                    "current_position": 0,
                    "shuffle": False,
                    "repeat": "off",
                    "total_duration_seconds": 0,
                    "items": [],
                }
            )
        return Response(
            PlaybackQueueSerializer(queue, context={"request": request}).data
        )

    @extend_schema(
        tags=["queue"],
        request=PlaybackQueueWriteSerializer,
        responses={200: PlaybackQueueSerializer},
        summary="Replace the saved queue",
        description=(
            "The payload replaces the queue wholesale. Draft or archived tracks "
            "are dropped rather than rejected, and their ids come back in "
            "`dropped_track_ids` so a client can reconcile."
        ),
    )
    def put(self, request):
        serializer = PlaybackQueueWriteSerializer(
            data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        result = serializer.save()

        queue = PlaybackQueue.objects.prefetch_related(
            "items__track__artist", "items__track__album"
        ).get(pk=result["queue"].pk)
        data = PlaybackQueueSerializer(queue, context={"request": request}).data
        data["dropped_track_ids"] = result["dropped_track_ids"]
        return Response(data)

    @extend_schema(tags=["queue"], responses={204: None}, summary="Clear the saved queue")
    def delete(self, request):
        PlaybackQueue.objects.filter(user=request.user).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class ListenStatsView(APIView):
    """``GET /api/history/stats/summary`` — aggregate counts for one listener."""

    permission_classes = [IsAuthenticated]

    @extend_schema(tags=["history"], responses={200: None}, summary="Listening summary")
    def get(self, request):
        user = request.user
        history = PlayHistory.objects.filter(user=user)
        totals = history.aggregate(
            total_plays=Sum("play_count"),
            distinct_tracks=Count("track_id"),
        )
        window = history.filter(
            last_played_at__gte=timezone.now() - timezone.timedelta(days=7)
        ).count()
        top = (
            history.order_by("-play_count")
            .select_related("track__artist")[:5]
        )
        return Response(
            {
                "total_plays": totals["total_plays"] or 0,
                "distinct_tracks": totals["distinct_tracks"] or 0,
                "plays_last_7_days": window,
                "favorites": Favorite.objects.filter(user=user).count(),
                "playlists": Playlist.objects.filter(owner=user).count(),
                "listening_minutes": round(
                    sum(
                        entry.track.duration_seconds * entry.play_count
                        for entry in history.select_related("track")
                    )
                    / 60
                ),
                "top_tracks": [
                    {"track_id": str(entry.track_id), "title": entry.track.title,
                     "artist": entry.track.artist.name, "play_count": entry.play_count}
                    for entry in top
                ],
            }
        )
