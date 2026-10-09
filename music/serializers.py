from django.contrib.auth import get_user_model
from rest_framework import serializers

from .models import Album, Artist, Genre, Track, TrackComment
from .storage import human_size

User = get_user_model()


def counter(obj, name: str) -> int:
    """Resolve a counter field, preferring a batched ``agg_<name>`` annotation.

    ``track_count``/``album_count`` are ``property`` objects on Artist and Album,
    and a property is a data descriptor: it always wins over an instance
    attribute, so a same-named annotation is invisible. Prefer the explicit
    ``agg_`` alias when the queryset supplied one, else use the property.
    """
    value = getattr(obj, f"agg_{name}", None)
    if value is None:
        value = getattr(obj, name, 0)
    return value or 0


class GenreSerializer(serializers.ModelSerializer):
    track_count = serializers.SerializerMethodField()

    class Meta:
        model = Genre
        fields = ("id", "name", "slug", "description", "color", "track_count")
        read_only_fields = ("id", "slug", "track_count")

    def get_track_count(self, obj) -> int:
        return counter(obj, "track_count")


class ArtistSummarySerializer(serializers.ModelSerializer):
    """Nested shape used inside track/album payloads."""

    class Meta:
        model = Artist
        fields = ("id", "name", "slug", "image", "verified")
        read_only_fields = fields


class ArtistListSerializer(serializers.ModelSerializer):
    track_count = serializers.SerializerMethodField()
    album_count = serializers.SerializerMethodField()
    is_following = serializers.SerializerMethodField()

    class Meta:
        model = Artist
        fields = (
            "id", "name", "slug", "image", "bio", "verified", "country",
            "track_count", "album_count", "monthly_listeners", "is_following",
        )
        read_only_fields = ("id", "slug", "monthly_listeners")

    def get_track_count(self, obj) -> int:
        return counter(obj, "track_count")

    def get_album_count(self, obj) -> int:
        return counter(obj, "album_count")

    def get_is_following(self, obj) -> bool:
        request = self.context.get("request")
        if not request or not request.user.is_authenticated:
            return False
        return obj.followers.filter(pk=request.user.pk).exists()


class ArtistDetailSerializer(ArtistListSerializer):
    genres = GenreSerializer(many=True, read_only=True)
    top_tracks = serializers.SerializerMethodField()
    albums = serializers.SerializerMethodField()

    class Meta(ArtistListSerializer.Meta):
        fields = ArtistListSerializer.Meta.fields + (
            "genres", "website", "owner", "created_at", "updated_at",
            "total_plays", "top_tracks", "albums",
        )
        read_only_fields = ("id", "slug", "monthly_listeners", "created_at", "updated_at", "total_plays")

    def get_top_tracks(self, obj):
        tracks = obj.tracks.published().with_listeners()[:5]
        return TrackSummarySerializer(tracks, many=True, context=self.context).data

    def get_albums(self, obj):
        albums = obj.albums.select_related("artist")[:20]
        return AlbumSummarySerializer(albums, many=True, context=self.context).data


class AlbumSummarySerializer(serializers.ModelSerializer):
    artist = ArtistSummarySerializer(read_only=True)
    track_count = serializers.SerializerMethodField()

    class Meta:
        model = Album
        fields = ("id", "title", "slug", "artist", "cover_image", "release_date",
                  "album_type", "track_count")
        read_only_fields = fields

    def get_track_count(self, obj) -> int:
        return counter(obj, "track_count")


class AlbumDetailSerializer(serializers.ModelSerializer):
    artist = ArtistSummarySerializer(read_only=True)
    genres = GenreSerializer(many=True, read_only=True)
    tracks = serializers.SerializerMethodField()
    total_duration_seconds = serializers.IntegerField(read_only=True)
    total_plays = serializers.IntegerField(read_only=True)

    class Meta:
        model = Album
        fields = (
            "id", "title", "slug", "artist", "description", "cover_image",
            "album_type", "release_date", "label", "catalog_number", "genres",
            "track_count", "total_duration_seconds", "total_plays",
            "created_at", "updated_at", "tracks",
        )
        read_only_fields = ("id", "slug", "created_at", "updated_at")

    def get_tracks(self, obj):
        tracks = obj.tracks.with_listeners().order_by("track_number", "title")
        return TrackSummarySerializer(tracks, many=True, context=self.context).data


class TrackSummarySerializer(serializers.ModelSerializer):
    artist = ArtistSummarySerializer(read_only=True)
    duration_display = serializers.CharField(read_only=True)
    is_favorite = serializers.SerializerMethodField()

    class Meta:
        model = Track
        fields = (
            "id", "title", "slug", "artist", "cover_image", "duration_seconds",
            "duration_display", "explicit", "status", "play_count", "is_favorite",
        )
        read_only_fields = fields

    def get_is_favorite(self, obj) -> bool:
        request = self.context.get("request")
        if not request or not request.user.is_authenticated:
            return False
        from playlists.models import Favorite

        return Favorite.objects.filter(user=request.user, track=obj).exists()


class TrackListSerializer(TrackSummarySerializer):
    album = AlbumSummarySerializer(read_only=True)
    genres = GenreSerializer(many=True, read_only=True)

    class Meta(TrackSummarySerializer.Meta):
        fields = TrackSummarySerializer.Meta.fields + (
            "album", "genres", "release_date", "like_count", "created_at",
        )
        read_only_fields = fields


class TrackDetailSerializer(TrackListSerializer):
    audio_url = serializers.SerializerMethodField()
    file_size_display = serializers.SerializerMethodField()
    is_playable = serializers.BooleanField(read_only=True)
    plays_today = serializers.IntegerField(read_only=True)
    has_lyrics = serializers.SerializerMethodField()
    download_url = serializers.SerializerMethodField()

    class Meta(TrackListSerializer.Meta):
        fields = TrackListSerializer.Meta.fields + (
            "audio_url", "download_url", "lyrics", "has_lyrics", "isrc",
            "bitrate_kbps", "sample_rate_hz", "file_size_bytes",
            "file_size_display", "is_playable", "plays_today", "updated_at",
        )
        read_only_fields = fields

    def get_audio_url(self, obj):
        if not obj.audio_file:
            return None
        request = self.context.get("request")
        url = obj.audio_file.url
        return request.build_absolute_uri(url) if request else url

    def get_file_size_display(self, obj):
        return human_size(obj.file_size_bytes)

    def get_has_lyrics(self, obj) -> bool:
        return bool((obj.lyrics or "").strip())

    def get_download_url(self, obj):
        if not obj.audio_file:
            return None
        from .streaming import download_url

        return download_url(obj, self.context.get("request"))


class TrackCommentAuthorSerializer(serializers.ModelSerializer):
    display_name = serializers.SerializerMethodField()

    class Meta:
        model = get_user_model()
        fields = ("id", "display_name", "role")
        read_only_fields = fields

    def get_display_name(self, obj) -> str:
        # Fall back to the local part of the email rather than leaking the
        # whole address on a public comment thread.
        return obj.display_name or obj.email.split("@")[0]


class TrackCommentSerializer(serializers.ModelSerializer):
    author = TrackCommentAuthorSerializer(source="user", read_only=True)
    replies = serializers.SerializerMethodField()

    class Meta:
        model = TrackComment
        fields = (
            "id", "track", "author", "body", "parent", "status",
            "is_pinned", "is_flagged", "created_at", "updated_at", "replies",
        )
        read_only_fields = (
            "id", "track", "status", "is_pinned", "is_flagged", "created_at", "updated_at",
        )

    def __init__(self, *args, **kwargs):
        self._with_replies = kwargs.pop("with_replies", False)
        super().__init__(*args, **kwargs)

    def get_replies(self, obj):
        if not self._with_replies:
            return None
        visible = [r for r in obj.replies.all() if r.is_visible]
        return TrackCommentSerializer(
            visible, many=True, context=self.context, with_replies=False
        ).data

    def validate_body(self, value):
        cleaned = value.strip()
        if not cleaned:
            raise serializers.ValidationError("Comment cannot be blank.")
        return cleaned

    def validate_parent(self, value):
        """A reply must belong to the same track as the comment it answers."""
        if value is None:
            return value
        track = self.context.get("track")
        if track is not None and value.track_id != track.pk:
            raise serializers.ValidationError("Parent comment belongs to a different track.")
        if value.parent_id is not None:
            raise serializers.ValidationError("Replies cannot be nested more than one level.")
        return value


class TrackCommentModerationSerializer(serializers.ModelSerializer):
    """Staff-only patch payload for a comment."""

    class Meta:
        model = TrackComment
        fields = ("status", "is_pinned", "is_flagged")

    def validate_status(self, value):
        if value not in (
            TrackComment.Status.VISIBLE,
            TrackComment.Status.PENDING,
            TrackComment.Status.HIDDEN,
        ):
            raise serializers.ValidationError("Unknown comment status.")
        return value


class TrackWriteSerializer(serializers.ModelSerializer):
    """Upload/create/update payload for a track."""

    genres = serializers.PrimaryKeyRelatedField(
        many=True, queryset=Genre.objects.all(), required=False
    )

    class Meta:
        model = Track
        fields = (
            "id", "title", "artist", "album", "genres", "audio_file",
            "cover_image", "duration_seconds", "bitrate_kbps", "sample_rate_hz",
            "isrc", "explicit", "lyrics", "status", "release_date",
        )
        extra_kwargs = {
            "audio_file": {"write_only": True, "required": True},
            "artist": {"required": True},
            "duration_seconds": {"required": False},
        }

    def validate_isrc(self, value):
        if value and len(value) > 24:
            raise serializers.ValidationError("ISRC must be 24 characters or fewer.")
        return value.strip().upper()

    def validate(self, attrs):
        album = attrs.get("album") or getattr(self.instance, "album", None)
        artist = attrs.get("artist") or getattr(self.instance, "artist", None)
        if album and artist and album.artist_id != artist.pk:
            raise serializers.ValidationError(
                {"album": "The selected album belongs to a different artist."}
            )
        if not attrs.get("release_date") and album and album.release_date:
            attrs["release_date"] = album.release_date
        return attrs

    def create(self, validated_data):
        genres = validated_data.pop("genres", [])
        track = Track.objects.create(**validated_data)
        if genres:
            track.genres.set(genres)
        return track

    def update(self, instance, validated_data):
        genres = validated_data.pop("genres", None)
        for field, value in validated_data.items():
            setattr(instance, field, value)
        instance.save()
        if genres is not None:
            instance.genres.set(genres)
        return instance


class TrackPlaySerializer(serializers.Serializer):
    """Payload accepted by the play-counting endpoint."""

    listened_seconds = serializers.IntegerField(min_value=0, max_value=24 * 3600, required=False)
    source = serializers.ChoiceField(
        choices=["web", "mobile", "desktop", "api", "admin"], default="web", required=False
    )
    completed = serializers.BooleanField(default=True, required=False)
