"""Track / artist / album / genre endpoints."""

from django.conf import settings
from django.core import signing
from django.shortcuts import get_object_or_404
from django.urls import reverse
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .filters import (
    AlbumFilter,
    ArtistFilter,
    GenreFilter,
    TrackFilter,
    TrackSearchFilter,
    annotate_counts,
)
from .models import Album, Artist, Genre, Track, TrackComment
from .permissions import IsAdminStaff
from .serializers import (
    AlbumDetailSerializer,
    AlbumSummarySerializer,
    ArtistDetailSerializer,
    ArtistListSerializer,
    GenreSerializer,
    TrackCommentModerationSerializer,
    TrackCommentSerializer,
    TrackDetailSerializer,
    TrackListSerializer,
    TrackPlaySerializer,
    TrackWriteSerializer,
)


class TrackViewSet(viewsets.ModelViewSet):
    """Catalogue browse, search, upload and play counting.

    Reading is public; any authenticated user may upload a track or register a
    play. ``staff_only_actions`` below marks endpoints reserved for staff.
    """

    queryset = (
        Track.objects.select_related("artist", "album")
        .prefetch_related("genres")
        .all()
    )
    permission_classes = [AllowAny]
    filterset_class = TrackFilter
    search_fields = ("title", "artist__name", "album__title", "isrc")
    ordering_fields = (
        "title", "created_at", "play_count", "like_count",
        "duration_seconds", "release_date", "updated_at",
    )
    ordering = ("-created_at",)
    http_method_names = ["get", "post", "put", "patch", "delete", "head", "options"]
    # Actions that mutate the catalogue and are therefore reserved for staff.
    staff_only_actions = frozenset(
        {"update", "partial_update", "destroy", "manage", "manage_list"}
    )

    def get_permissions(self):
        """Public reads; authenticated uploads; staff-only curation."""
        if self.action in self.staff_only_actions:
            return [IsAuthenticated(), IsAdminStaff()]
        if self.action == "download" and settings.DOWNLOADS_REQUIRE_AUTH:
            return [IsAuthenticated()]
        if self.action == "create":
            return [IsAuthenticated()]
        return [AllowAny()]

    def get_serializer_class(self):
        if self.action in ("create", "update", "partial_update"):
            return TrackWriteSerializer
        if self.action == "retrieve":
            return TrackDetailSerializer
        return TrackListSerializer

    def get_queryset(self):
        queryset = super().get_queryset()
        if not self._is_staff(self.request):
            # Drafts and archived tracks are invisible to the public.
            queryset = queryset.filter(status=Track.Status.PUBLISHED)
        return queryset

    @staticmethod
    def _is_staff(request) -> bool:
        return bool(request.user and request.user.is_authenticated and request.user.is_admin_staff)

    @extend_schema(
        tags=["tracks"],
        request=TrackWriteSerializer,
        responses={201: TrackDetailSerializer},
        summary="Upload a track",
        description="Create a new track, optionally uploading audio and cover art in one multipart request.",
    )
    def create(self, request, *args, **kwargs):
        serializer = TrackWriteSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        track = serializer.save()
        if not self._is_staff(request):
            # Non-staff uploads always land as drafts, never published.
            track.status = Track.Status.DRAFT
            track.save(update_fields=["status"])
        output = TrackDetailSerializer(track, context=self.get_serializer_context())
        return Response(output.data, status=status.HTTP_201_CREATED)

    @extend_schema(
        tags=["tracks"],
        parameters=[
            OpenApiParameter("genre", str, description="Filter by genre slug."),
            OpenApiParameter("q", str, description="Free-text search."),
            OpenApiParameter("page_size", int, description="Items per page (max 100)."),
        ],
    )
    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        serializer = self.get_serializer(page if page is not None else queryset, many=True)
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)

    def get_parser_classes(self):
        if self.action in ("create",):
            return [MultiPartParser, FormParser, JSONParser]
        return super().get_parser_classes()

    @extend_schema(
        tags=["tracks"],
        request=TrackPlaySerializer,
        responses={201: None, 200: None},
        summary="Register a play",
        description=(
            "Increments the denormalised play counter and writes a play event. "
            "Only published tracks are counted; draft/archived tracks are ignored."
        ),
    )
    @action(detail=True, methods=["post"], permission_classes=[AllowAny])
    def play(self, request, pk=None):
        track = self.get_object()
        payload = TrackPlaySerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data

        from analytics.models import PlayEvent, register_play
        from playlists.models import PlayHistory

        source = data.get("source", "web")
        if request.user.is_authenticated and request.user.is_admin_staff and source == "web":
            source = PlayEvent.Source.ADMIN

        event = register_play(
            track,
            user=request.user,
            request=request,
            source=source,
            listened_seconds=data.get("listened_seconds", 0),
            completed=data.get("completed", True),
        )
        if event is None:
            return Response(
                {"detail": "Track is not published; play not recorded."},
                status=status.HTTP_200_OK,
            )

        if request.user.is_authenticated:
            PlayHistory.record(request.user, track, completed=data.get("completed", True))

        track.refresh_from_db()
        return Response(
            {"play_count": track.play_count, "recorded": True},
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(tags=["tracks"], responses={200: TrackDetailSerializer}, summary="Track stream URL")
    @action(detail=True, methods=["get"])
    def stream(self, request, pk=None):
        from .streaming import download_url, stream_url

        track = get_object_or_404(self.get_queryset().select_related("artist", "album"), pk=pk)
        if not track.audio_file:
            return Response({"detail": "No audio file attached."}, status=status.HTTP_404_NOT_FOUND)
        raw_url = request.build_absolute_uri(track.audio_file.url)
        signed_url = stream_url(track, request)
        return Response(
            {
                "track_id": track.id,
                # When streaming is gated, the only advertised source is the
                # signed endpoint; otherwise keep the direct media path.
                "url": signed_url if settings.STREAM_REQUIRE_AUTH else raw_url,
                "stream_url": signed_url,
                "download_url": download_url(track, request),
                "mime_type": getattr(track.audio_file, "content_type", None) or "audio/mpeg",
                "duration_seconds": track.duration_seconds,
                "bitrate_kbps": track.bitrate_kbps,
                "supports_range_requests": True,
            }
        )

    @extend_schema(
        tags=["tracks"],
        responses={200: None, 403: None, 404: None},
        summary="Stream track audio",
        description=(
            "Byte-streams the audio with range support. When "
            "`STREAM_REQUIRE_AUTH` is on, a valid `?token=` from `/stream/` is "
            "required and a missing/forged/expired token is rejected with 403."
        ),
    )
    @action(detail=True, methods=["get"], permission_classes=[AllowAny])
    def audio(self, request, pk=None):
        from .streaming import audio_file_response, verify_stream_token

        if settings.STREAM_REQUIRE_AUTH:
            token = request.query_params.get("token", "")
            try:
                signed_pk = verify_stream_token(token)
            except (signing.SignatureExpired, signing.BadSignature):
                return Response(
                    {"detail": "A valid, unexpired stream token is required."},
                    status=status.HTTP_403_FORBIDDEN,
                )
            if str(signed_pk) != str(pk):
                return Response(
                    {"detail": "Token does not match this track."},
                    status=status.HTTP_403_FORBIDDEN,
                )

        track = get_object_or_404(self.get_queryset().select_related("artist"), pk=pk)
        response = audio_file_response(track)
        if response is None:
            return Response(
                {"detail": "No audio file attached."}, status=status.HTTP_404_NOT_FOUND
            )
        return response

    @extend_schema(
        tags=["tracks"],
        responses={200: None, 403: None, 404: None},
        summary="Download track audio",
        description=(
            "Streams the audio as an attachment and increments `download_count`. "
            "Requires a login unless `DOWNLOADS_REQUIRE_AUTH` is turned off."
        ),
    )
    @action(detail=True, methods=["get"], url_path="download")
    def download(self, request, pk=None):
        from django.db.models import F

        from .streaming import audio_file_response, download_filename

        track = get_object_or_404(self.get_queryset().select_related("artist"), pk=pk)
        response = audio_file_response(
            track, as_attachment=True, filename=download_filename(track)
        )
        if response is None:
            return Response(
                {"detail": "No audio file attached."}, status=status.HTTP_404_NOT_FOUND
            )
        Track.objects.filter(pk=track.pk).update(download_count=F("download_count") + 1)
        return response

    @extend_schema(
        tags=["tracks"],
        responses={200: None},
        summary="HLS playback manifest",
        description=(
            "Reports whether the track has been packaged for adaptive streaming "
            "and, when it has, returns the URL of its master playlist. Sub-playlists "
            "and segments hang off the same `hls/` route."
        ),
    )
    @action(detail=True, methods=["get"])
    def hls(self, request, pk=None):
        from . import hls

        track = get_object_or_404(self.get_queryset().select_related("artist"), pk=pk)
        enabled = hls.hls_enabled()
        ready = enabled and hls.master_exists(track)

        master_url = None
        if ready:
            master_path = reverse(
                "api:track-hls-file", kwargs={"pk": track.pk, "path": hls.MASTER_NAME}
            )
            master_url = request.build_absolute_uri(master_path)
            if settings.STREAM_REQUIRE_AUTH:
                from .streaming import sign_stream_token

                master_url = f"{master_url}?token={sign_stream_token(track)}"

        return Response(
            {
                "track_id": track.id,
                "enabled": enabled,
                "ready": ready,
                "master_url": master_url,
                "variants": hls.parse_variants() if enabled else [],
                "generated_at": track.hls_generated_at,
            }
        )

    @extend_schema(
        tags=["tracks"],
        responses={200: None},
        summary="Track lyrics",
        description=(
            "Returns the raw lyric text plus, when the sheet is LRC-tagged, a "
            "`lines` array of `{time, text}` entries for synced highlighting."
        ),
    )
    @action(detail=True, methods=["get"])
    def lyrics(self, request, pk=None):
        from .lyrics import lyrics_payload

        track = self.get_object()
        return Response(lyrics_payload(track))

    @extend_schema(tags=["tracks"], responses={200: TrackDetailSerializer}, summary="Increment like")
    @action(detail=True, methods=["post"], permission_classes=[AllowAny])
    def like(self, request, pk=None):
        track = get_object_or_404(Track, pk=pk)
        from django.db.models import F

        Track.objects.filter(pk=track.pk).update(like_count=F("like_count") + 1)
        track.refresh_from_db()
        return Response({"like_count": track.like_count})

    @extend_schema(
        tags=["tracks"],
        request=TrackWriteSerializer,
        responses={200: TrackDetailSerializer},
        summary="Staff edit of any track",
    )
    @action(
        detail=True,
        methods=["patch", "put"],
        parser_classes=[MultiPartParser, FormParser, JSONParser],
        permission_classes=[IsAdminStaff],
        url_path="manage",
    )
    def manage(self, request, pk=None):
        track = get_object_or_404(Track, pk=pk)
        serializer = TrackWriteSerializer(
            track, data=request.data, partial=True, context=self.get_serializer_context()
        )
        serializer.is_valid(raise_exception=True)
        updated = serializer.save()
        return Response(TrackDetailSerializer(updated, context=self.get_serializer_context()).data)


    @extend_schema(
        tags=["tracks"],
        request=TrackCommentSerializer,
        responses={200: TrackCommentSerializer(many=True), 201: TrackCommentSerializer},
        summary="List or post comments",
        description=(
            "GET returns top-level comments, newest first; hidden and pending "
            "comments are staff-only. POST publishes a comment or a one-level "
            "reply (set `parent`)."
        ),
    )
    @action(detail=True, methods=["get", "post"])
    def comments(self, request, pk=None):
        track = self.get_object()

        if request.method == "POST":
            if not request.user.is_authenticated:
                return Response(
                    {"detail": "Authentication required."},
                    status=status.HTTP_401_UNAUTHORIZED,
                )
            serializer = TrackCommentSerializer(
                data=request.data, context={"request": request, "track": track}
            )
            serializer.is_valid(raise_exception=True)
            comment = serializer.save(user=request.user, track=track)
            return Response(
                TrackCommentSerializer(comment, context={"request": request}).data,
                status=status.HTTP_201_CREATED,
            )

        queryset = track.comments.select_related("user").prefetch_related("replies")
        if not self._is_staff(request):
            queryset = queryset.filter(status=TrackComment.Status.VISIBLE)
        queryset = queryset.filter(parent__isnull=True)
        page = self.paginate_queryset(queryset)
        serializer = TrackCommentSerializer(
            page if page is not None else queryset,
            many=True,
            context={"request": request, "track": track},
            with_replies=True,
        )
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)

    @extend_schema(
        tags=["tracks"],
        responses={200: TrackListSerializer(many=True)},
        summary="Tracks similar to this one",
    )
    @action(detail=True, methods=["get"])
    def similar(self, request, pk=None):
        from analytics.recommend import similar_tracks

        track = self.get_object()
        try:
            limit = min(max(int(request.query_params.get("limit", 20)), 1), 50)
        except (TypeError, ValueError):
            limit = 20
        tracks = similar_tracks(track, limit=limit)
        return Response(
            TrackListSerializer(tracks, many=True, context={"request": request}).data
        )


class TrackCommentViewSet(viewsets.GenericViewSet):
    """Edit or moderate a single comment.

    Authors may edit the body of their own comment; only staff may change
    ``status``/``is_pinned``/``is_flagged``. Splitting those two powers avoids a
    user being able to un-hide their own comment by editing its text.
    """

    serializer_class = TrackCommentSerializer
    permission_classes = [IsAuthenticated]
    queryset = TrackComment.objects.select_related("user", "track").all()
    http_method_names = ["get", "patch", "delete", "head", "options"]

    def _is_staff(self) -> bool:
        user = self.request.user
        return bool(user and user.is_authenticated and user.is_admin_staff)

    def _get_comment(self):
        # Honour is_hidden so a moderated comment 404s for non-staff instead of
        # confirming it exists.
        queryset = self.get_queryset()
        if not self._is_staff():
            queryset = queryset.filter(status=TrackComment.Status.VISIBLE)
        return get_object_or_404(queryset, pk=self.kwargs["pk"])

    @extend_schema(tags=["tracks"], responses={200: TrackCommentSerializer})
    def retrieve(self, request, *args, **kwargs):
        comment = self._get_comment()
        return Response(
            TrackCommentSerializer(comment, context={"request": request}).data
        )

    @extend_schema(
        tags=["tracks"],
        request=TrackCommentModerationSerializer,
        responses={200: TrackCommentSerializer},
        summary="Edit or moderate a comment",
    )
    def partial_update(self, request, *args, **kwargs):
        comment = self._get_comment()
        is_author = comment.user_id == request.user.pk

        moderation = {k: v for k, v in request.data.items()
                      if k in ("status", "is_pinned", "is_flagged")}
        if moderation and not self._is_staff():
            return Response(
                {"detail": "Only staff can moderate comments."},
                status=status.HTTP_403_FORBIDDEN,
            )

        if "body" in request.data:
            if not is_author and not self._is_staff():
                return Response(
                    {"detail": "You can only edit your own comments."},
                    status=status.HTTP_403_FORBIDDEN,
                )
            serializer = TrackCommentSerializer(
                comment, data=request.data, partial=True,
                context={"request": request, "track": comment.track},
            )
            serializer.is_valid(raise_exception=True)
            comment = serializer.save()
            if moderation:
                comment = self._apply_moderation(comment, moderation)
            return Response(
                TrackCommentSerializer(comment, context={"request": request}).data
            )

        serializer = TrackCommentModerationSerializer(comment, data=moderation or {}, partial=True)
        serializer.is_valid(raise_exception=True)
        comment = self._apply_moderation(comment, serializer.validated_data)
        return Response(
            TrackCommentSerializer(comment, context={"request": request}).data
        )

    @staticmethod
    def _apply_moderation(comment, payload):
        for field, value in payload.items():
            setattr(comment, field, value)
        comment.save(update_fields=list(payload))
        return comment

    @extend_schema(tags=["tracks"], responses={204: None})
    def destroy(self, request, *args, **kwargs):
        comment = self._get_comment()
        if comment.user_id != request.user.pk and not self._is_staff():
            return Response(
                {"detail": "You can only delete your own comments."},
                status=status.HTTP_403_FORBIDDEN,
            )
        comment.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class PopularTrackView(APIView):
    """``GET /api/tracks/popular/`` — quick hook for home surfaces."""

    permission_classes = [AllowAny]

    @extend_schema(
        tags=["tracks"],
        parameters=[OpenApiParameter("limit", int, description="1-50, default 20.")],
        responses={200: TrackListSerializer(many=True)},
    )
    def get(self, request):
        try:
            limit = min(max(int(request.query_params.get("limit", 20)), 1), 50)
        except (TypeError, ValueError):
            limit = 20
        tracks = Track.objects.published().with_listeners().order_by("-play_count")[:limit]
        return Response(TrackListSerializer(tracks, many=True, context={"request": request}).data)


class TrendingTrackView(APIView):
    """``GET /api/tracks/trending/`` — rolling-window popularity."""

    permission_classes = [AllowAny]

    @extend_schema(
        tags=["tracks"],
        parameters=[OpenApiParameter("days", int, description="1-30, default 7.")],
        responses={200: TrackListSerializer(many=True)},
    )
    def get(self, request):
        try:
            days = min(max(int(request.query_params.get("days", 7)), 1), 30)
        except (TypeError, ValueError):
            days = 7
        tracks = list(Track.objects.trending(days=days)[:20])
        return Response(TrackListSerializer(tracks, many=True, context={"request": request}).data)


class GenreViewSet(viewsets.ModelViewSet):
    # `annotate()` suppresses the model's Meta.ordering, so pin a deterministic
    # (and unique) sort: without it pagination can repeat or skip rows.
    queryset = annotate_counts(Genre.objects.all(), "genre").order_by("name")
    serializer_class = GenreSerializer
    permission_classes = [AllowAny]
    filterset_class = GenreFilter
    search_fields = ("name", "description")
    ordering_fields = ("name", "track_count", "created_at")
    ordering = ("name",)
    http_method_names = ["get", "post", "put", "patch", "delete", "head", "options"]

    #: Public ordering key -> annotation alias (see music.filters.annotate_counts).
    ordering_aliases = {"track_count": "agg_track_count"}

    def filter_queryset(self, queryset):
        # Rewrite ?ordering=track_count onto the real alias before DRF's
        # OrderingFilter drops the param, and drop unknown keys so the filter can
        # report them rather than raising FieldError.
        requested = self.request.query_params.get("ordering")
        if requested:
            allowed = set(self.ordering_fields or ())
            remapped = [
                self.ordering_aliases.get(key.strip(), key.strip())
                for key in requested.split(",")
                if key.strip() in allowed
            ]
            if remapped:
                queryset = queryset.order_by(*remapped)
        return super().filter_queryset(queryset)

    def get_permissions(self):
        if self.action in ("create", "update", "partial_update", "destroy"):
            return [IsAdminStaff()]
        return super().get_permissions()


@extend_schema(tags=["artists"])
class ArtistViewSet(viewsets.ModelViewSet):
    queryset = Artist.objects.prefetch_related("genres")
    serializer_class = ArtistListSerializer
    permission_classes = [AllowAny]
    filterset_class = ArtistFilter
    search_fields = ("name", "bio", "country")
    ordering_fields = ("name", "monthly_listeners", "created_at")
    http_method_names = ["get", "post", "put", "patch", "delete", "head", "options"]

    def get_permissions(self):
        """Profile curation is staff-only; following needs a logged-in user."""
        if self.action in ("create", "update", "partial_update", "destroy"):
            return [IsAdminStaff()]
        if self.action == "follow":
            return [IsAuthenticated()]
        return super().get_permissions()

    def get_serializer_class(self):
        return ArtistDetailSerializer if self.action == "retrieve" else ArtistListSerializer

    @extend_schema(tags=["artists"], responses={200: TrackListSerializer(many=True)}, summary="Artist tracks")
    @action(detail=True, methods=["get"])
    def tracks(self, request, pk=None):
        artist = self.get_object()
        queryset = artist.tracks.published().with_listeners()
        page = self.paginate_queryset(queryset)
        serializer = TrackListSerializer(page or queryset, many=True, context={"request": request})
        return self.get_paginated_response(serializer.data) if page is not None else Response(serializer.data)

    @extend_schema(tags=["artists"], responses={200: AlbumSummarySerializer(many=True)}, summary="Artist albums")
    @action(detail=True, methods=["get"])
    def albums(self, request, pk=None):
        artist = self.get_object()
        page = self.paginate_queryset(artist.albums.select_related("artist"))
        serializer = AlbumSummarySerializer(page or [], many=True, context={"request": request})
        return self.get_paginated_response(serializer.data) if page is not None else Response(serializer.data)

    @extend_schema(
        tags=["artists"],
        request=None,
        responses={200: ArtistListSerializer, 201: None},
        summary="Follow / unfollow an artist",
    )
    @action(detail=True, methods=["post", "delete"])
    def follow(self, request, pk=None):
        artist = self.get_object()
        if not request.user.is_authenticated:
            return Response(
                {"detail": "Authentication required."}, status=status.HTTP_401_UNAUTHORIZED
            )
        if request.method == "POST":
            artist.followers.add(request.user)
            followed = True
        else:
            artist.followers.remove(request.user)
            followed = False
        return Response({"is_following": followed, "follower_count": artist.followers.count()},
                        status=status.HTTP_200_OK)

    @extend_schema(
        tags=["artists"],
        methods=["POST"],
        responses={200: ArtistListSerializer},
        summary="Recalculate monthly listeners",
    )
    @action(detail=True, methods=["post"], permission_classes=[IsAdminStaff])
    def refresh_listeners(self, request, pk=None):
        artist = self.get_object()
        artist.recalculate_monthly_listeners()
        artist.refresh_from_db()
        return Response(ArtistListSerializer(artist, context={"request": request}).data)


@extend_schema(tags=["albums"])
class AlbumViewSet(viewsets.ModelViewSet):
    queryset = Album.objects.select_related("artist").prefetch_related("genres")
    serializer_class = AlbumSummarySerializer
    permission_classes = [AllowAny]
    filterset_class = AlbumFilter
    search_fields = ("title", "artist__name", "label", "description")
    ordering_fields = ("title", "release_date", "created_at")
    http_method_names = ["get", "post", "put", "patch", "delete", "head", "options"]

    def get_serializer_class(self):
        return AlbumDetailSerializer if self.action == "retrieve" else AlbumSummarySerializer

    def get_permissions(self):
        if self.action in ("create", "update", "partial_update", "destroy"):
            return [IsAdminStaff()]
        return super().get_permissions()

    @extend_schema(tags=["albums"], responses={200: TrackListSerializer(many=True)}, summary="Album tracks")
    @action(detail=True, methods=["get"])
    def tracks(self, request, pk=None):
        album = self.get_object()
        queryset = album.tracks.with_listeners().order_by("track_number", "title")
        page = self.paginate_queryset(queryset)
        serializer = TrackListSerializer(page or queryset, many=True, context={"request": request})
        return self.get_paginated_response(serializer.data) if page is not None else Response(serializer.data)


class TrackSearchView(APIView):
    """``GET /api/search/tracks/`` — thin alias kept for client compatibility."""

    permission_classes = [AllowAny]

    @extend_schema(tags=["search"], responses={200: TrackListSerializer(many=True)})
    def get(self, request):
        filterset = TrackSearchFilter(
            request.query_params, queryset=Track.objects.published().with_listeners()
        )
        if not filterset.is_valid():
            return Response({"filters": filterset.errors}, status=status.HTTP_400_BAD_REQUEST)
        return Response(
            TrackListSerializer(filterset.qs.distinct()[:50], many=True, context={"request": request}).data
        )


@extend_schema(exclude=True)
def track_hls_file(request, pk, path):
    """Serve one packaged HLS asset (master/variant playlist or a segment).

    Playlists are text, so relative references have the caller's query string
    (and therefore the signed token) stitched onto them before they are sent.
    Every path is confined to the track's own HLS directory.
    """
    from django.http import FileResponse, Http404, HttpResponse

    from . import hls
    from .streaming import verify_stream_token

    queryset = Track.objects.exclude(hls_playlist="")
    if not TrackViewSet._is_staff(request):
        queryset = queryset.filter(status=Track.Status.PUBLISHED)
    track = get_object_or_404(queryset, pk=pk)

    if settings.STREAM_REQUIRE_AUTH and not request.user.is_authenticated:
        try:
            signed_pk = verify_stream_token(request.GET.get("token", ""))
        except (signing.SignatureExpired, signing.BadSignature):
            return HttpResponse(status=status.HTTP_403_FORBIDDEN)
        if str(signed_pk) != str(pk):
            return HttpResponse(status=status.HTTP_403_FORBIDDEN)

    resolved = hls.safe_hls_path(track, path)
    if resolved is None:
        raise Http404("HLS asset not found.")

    content_type = hls.content_type_for(path)
    if content_type == hls.CONTENT_TYPES[".m3u8"]:
        body = resolved.read_text(encoding="utf-8")
        body = hls.rewrite_playlist_uris(body, request.META.get("QUERY_STRING", ""))
        return HttpResponse(body, content_type=content_type)
    return FileResponse(resolved.open("rb"), content_type=content_type)
