"""Playlists, favourites and history serializers."""

from django.contrib.auth import get_user_model
from django.db import transaction
from rest_framework import serializers

from music.models import Track
from music.serializers import TrackSummarySerializer

from .models import (
    Favorite,
    PlaybackQueue,
    PlaybackQueueItem,
    PlayHistory,
    Playlist,
    PlaylistTrack,
)

User = get_user_model()


class PlaylistSerializer(serializers.ModelSerializer):
    owner = serializers.SerializerMethodField()
    # Resolved from the `entry_count` / `duration_total` annotations when the
    # queryset carries them, and from the model properties otherwise, so the
    # same serializer works for annotated lists and for bare instances.
    track_count = serializers.SerializerMethodField()
    total_duration_seconds = serializers.SerializerMethodField()
    duration_display = serializers.CharField(read_only=True)
    is_owner = serializers.SerializerMethodField()

    class Meta:
        model = Playlist
        fields = (
            "id", "name", "slug", "owner", "description", "cover_image",
            "visibility", "is_favorite", "total_plays", "track_count",
            "total_duration_seconds", "duration_display", "is_owner",
            "created_at", "updated_at",
        )
        read_only_fields = ("id", "slug", "owner", "total_plays", "created_at", "updated_at")

    def get_owner(self, obj):
        # Email is deliberately withheld: public playlists expose only a display
        # identity, never an address.
        return {
            "id": str(obj.owner_id),
            "display_name": obj.owner.full_name,
            "avatar": obj.owner.avatar.url if obj.owner.avatar else None,
        }

    def get_track_count(self, obj) -> int:
        annotated = getattr(obj, "entry_count", None)
        return obj.track_count if annotated is None else annotated

    def get_total_duration_seconds(self, obj) -> int:
        annotated = getattr(obj, "duration_total", None)
        return obj.total_duration_seconds if annotated is None else annotated

    def get_is_owner(self, obj) -> bool:
        request = self.context.get("request")
        return bool(request and request.user.is_authenticated and obj.owner_id == request.user.pk)


class PlaylistDetailSerializer(PlaylistSerializer):
    tracks = serializers.SerializerMethodField()

    class Meta(PlaylistSerializer.Meta):
        fields = PlaylistSerializer.Meta.fields + ("tracks",)
        read_only_fields = PlaylistSerializer.Meta.read_only_fields

    def get_tracks(self, obj):
        entries = obj.playlist_tracks.select_related("track__artist", "track__album")
        tracks = [entry.track for entry in entries]
        return TrackSummarySerializer(tracks, many=True, context=self.context).data


class PlaylistWriteSerializer(serializers.ModelSerializer):
    # published() so a playlist can never be seeded with unreleased tracks.
    track_ids = serializers.PrimaryKeyRelatedField(
        many=True, queryset=Track.objects.published(), required=False, write_only=True
    )

    class Meta:
        model = Playlist
        fields = ("id", "name", "description", "cover_image", "visibility", "track_ids")
        read_only_fields = ("id",)

    def validate_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Playlist name cannot be blank.")
        return value

    def validate_visibility(self, value):
        if value == Playlist.Visibility.PUBLIC:
            request = self.context.get("request")
            if request and not request.user.is_authenticated:
                raise serializers.ValidationError("Sign in to publish a playlist.")
        return value

    def create(self, validated_data):
        track_ids = validated_data.pop("track_ids", [])
        playlist = Playlist.objects.create(
            owner=self.context["request"].user, **validated_data
        )
        if track_ids:
            PlaylistTrack.objects.bulk_create(
                [
                    PlaylistTrack(playlist=playlist, track=track, position=index,
                                  added_by=self.context["request"].user)
                    for index, track in enumerate(track_ids, start=1)
                ],
                ignore_conflicts=True,
            )
        return playlist

    def update(self, instance, validated_data):
        track_ids = validated_data.pop("track_ids", None)
        for field, value in validated_data.items():
            setattr(instance, field, value)
        instance.save()
        if track_ids is not None:
            instance.playlist_tracks.all().delete()
            PlaylistTrack.objects.bulk_create(
                [
                    PlaylistTrack(playlist=instance, track=track, position=index,
                                  added_by=self.context["request"].user)
                    for index, track in enumerate(track_ids, start=1)
                ]
            )
        return instance


class PlaylistTrackSerializer(serializers.ModelSerializer):
    track = TrackSummarySerializer(read_only=True)
    track_id = serializers.PrimaryKeyRelatedField(
        queryset=Track.objects.published(), source="track", write_only=True
    )

    class Meta:
        model = PlaylistTrack
        fields = ("id", "track", "track_id", "position", "added_at")
        read_only_fields = ("id", "track", "position", "added_at")

    def create(self, validated_data):
        playlist = self.context["playlist"]
        track = validated_data["track"]
        if playlist.playlist_tracks.filter(track=track).exists():
            raise serializers.ValidationError({"track_id": "This track is already in the playlist."})
        last = playlist.playlist_tracks.order_by("-position").first()
        return PlaylistTrack.objects.create(
            playlist=playlist,
            track=track,
            position=last.position + 1 if last else 1,
            added_by=self.context["request"].user,
        )


class ReorderSerializer(serializers.Serializer):
    track_ids = serializers.ListField(
        child=serializers.IntegerField(), allow_empty=True, min_length=0
    )

    def validate_track_ids(self, value):
        if len(value) != len(set(value)):
            raise serializers.ValidationError("track_ids must not contain duplicates.")
        return value


class FavoriteSerializer(serializers.ModelSerializer):
    track = TrackSummarySerializer(read_only=True)
    track_id = serializers.PrimaryKeyRelatedField(
        queryset=Track.objects.published(), source="track", write_only=True
    )

    class Meta:
        model = Favorite
        fields = ("id", "track", "track_id", "created_at")
        read_only_fields = ("id", "track", "created_at")


class BulkFavoriteSerializer(serializers.Serializer):
    track_ids = serializers.ListField(
        child=serializers.IntegerField(), min_length=1, max_length=200
    )

    def validate_track_ids(self, value):
        if len(value) != len(set(value)):
            raise serializers.ValidationError("track_ids must not contain duplicates.")
        return value


class PlayHistorySerializer(serializers.ModelSerializer):
    track = TrackSummarySerializer(read_only=True)

    class Meta:
        model = PlayHistory
        fields = ("id", "track", "play_count", "completed", "last_played_at")
        read_only_fields = fields


class AddTracksSerializer(serializers.Serializer):
    """Bulk-append tracks to a playlist, skipping duplicates."""

    track_ids = serializers.ListField(child=serializers.IntegerField(), min_length=1, max_length=200)

    def validate(self, attrs):
        playlist = self.context["playlist"]
        user = self.context["request"].user
        if playlist.owner_id != user.pk and not playlist.collaborators.filter(pk=user.pk).exists():
            raise serializers.ValidationError("You are not a collaborator on this playlist.")
        if len(attrs["track_ids"]) != len(set(attrs["track_ids"])):
            raise serializers.ValidationError({"track_ids": "Duplicate ids are not allowed."})
        return attrs

    def save(self, **kwargs):
        playlist = self.context["playlist"]
        user = self.context["request"].user
        track_ids = self.validated_data["track_ids"]
        existing = set(playlist.playlist_tracks.values_list("track_id", flat=True))
        start = playlist.playlist_tracks.count() + 1
        added = 0
        with transaction.atomic():
            for offset, track in enumerate(Track.objects.published().filter(pk__in=track_ids)):
                if track.pk in existing:
                    continue
                PlaylistTrack.objects.create(
                    playlist=playlist, track=track, position=start + offset, added_by=user
                )
                added += 1
        return {"added": added, "skipped": len(track_ids) - added, "playlist_id": playlist.pk}


class PlaybackQueueItemSerializer(serializers.ModelSerializer):
    track = TrackSummarySerializer(read_only=True)

    class Meta:
        model = PlaybackQueueItem
        fields = ("id", "track", "position", "added_at")
        read_only_fields = fields


class PlaybackQueueSerializer(serializers.ModelSerializer):
    items = PlaybackQueueItemSerializer(many=True, read_only=True)
    total_duration_seconds = serializers.IntegerField(read_only=True)

    class Meta:
        model = PlaybackQueue
        fields = (
            "id", "user", "current_position", "shuffle", "repeat",
            "total_duration_seconds", "updated_at", "items",
        )
        read_only_fields = ("id", "user", "updated_at")


class PlaybackQueueWriteSerializer(serializers.Serializer):
    """Replace-the-whole-queue payload.

    Replace rather than patch: the client's queue is the source of truth after a
    drag-reorder or a bulk clear, and diffing it server-side would need a
    revision counter to stay consistent under concurrent tabs.
    """

    track_ids = serializers.ListField(
        child=serializers.IntegerField(), min_length=0, max_length=500
    )
    current_position = serializers.IntegerField(min_value=0, default=0)
    shuffle = serializers.BooleanField(default=False)
    repeat = serializers.ChoiceField(choices=PlaybackQueue.Repeat.values, default=PlaybackQueue.Repeat.OFF)

    def validate_track_ids(self, value):
        if len(value) != len(set(value)):
            raise serializers.ValidationError("Duplicate track ids are not allowed.")
        return value

    def validate(self, attrs):
        if attrs["current_position"] >= len(attrs["track_ids"]) and attrs["track_ids"]:
            raise serializers.ValidationError(
                {"current_position": "Position is past the end of the queue."}
            )
        return attrs

    def save(self, **kwargs):
        user = self.context["request"].user
        track_ids = self.validated_data["track_ids"]

        # Drafts and archived tracks are dropped rather than rejected, so a queue
        # saved before a track was unpublished still restores.
        tracks = list(Track.objects.published().filter(pk__in=track_ids))

        # A queue is positional. Track.Meta.ordering is ("-created_at",), so an
        # unordered IN-clause would silently save the queue in the wrong order.
        order = {value: index for index, value in enumerate(track_ids)}
        tracks.sort(key=lambda track: order.get(track.pk, len(order)))

        found = {track.pk for track in tracks}
        dropped = sorted(set(track_ids) - found)

        with transaction.atomic():
            queue, _ = PlaybackQueue.objects.select_for_update().get_or_create(user=user)
            queue.items.all().delete()
            PlaybackQueueItem.objects.bulk_create(
                [
                    PlaybackQueueItem(queue=queue, track=track, position=index)
                    for index, track in enumerate(tracks, start=1)
                ],
                batch_size=500,
            )
            queue.current_position = min(
                self.validated_data["current_position"], max(len(tracks) - 1, 0)
            )
            queue.shuffle = self.validated_data["shuffle"]
            queue.repeat = self.validated_data["repeat"]
            queue.save(
                update_fields=["current_position", "shuffle", "repeat", "updated_at"]
            )
        return {"queue": queue, "dropped_track_ids": dropped}
