"""Playlists, favourites and per-user listening history."""

import uuid

from django.conf import settings
from django.db import models
from django.db.models import Count, F, Q
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify

from music.models import SluggedModel, TimeStampedModel
from music.storage import CoverArtUploadPath, delete_file, validate_image_extension, validate_upload_size


class Playlist(SluggedModel, TimeStampedModel):
    class Visibility(models.TextChoices):
        PUBLIC = "public", "Public"
        UNLISTED = "unlisted", "Unlisted"
        PRIVATE = "private", "Private"

    name = models.CharField(max_length=150)
    slug_source_field = "name"
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, related_name="playlists", on_delete=models.CASCADE, db_index=True
    )
    description = models.TextField(blank=True, max_length=2000)
    cover_image = models.ImageField(
        upload_to=CoverArtUploadPath("playlists"),
        blank=True,
        null=True,
        validators=[validate_image_extension, validate_upload_size],
    )
    visibility = models.CharField(
        max_length=12, choices=Visibility.choices, default=Visibility.PRIVATE, db_index=True
    )
    tracks = models.ManyToManyField(
        "music.Track", through="PlaylistTrack", related_name="playlists", blank=True
    )
    collaborators = models.ManyToManyField(
        settings.AUTH_USER_MODEL, related_name="collaborating_playlists", blank=True
    )
    is_favorite = models.BooleanField(default=False)
    total_plays = models.PositiveBigIntegerField(default=0, editable=False)

    class Meta:
        db_table = "playlists_playlist"
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["owner", "-created_at"]),
            models.Index(fields=["visibility", "-total_plays"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.owner.email})"

    def get_absolute_url(self):
        # reverse() rather than a literal path so renaming the route breaks the
        # model loudly instead of producing a dead link.
        return reverse("playlists:detail", kwargs={"slug": self.slug})

    def delete(self, *args, **kwargs):
        delete_file(self.cover_image)
        return super().delete(*args, **kwargs)

    def is_visible_to(self, user) -> bool:
        if self.visibility == self.Visibility.PUBLIC:
            return True
        if not user or not user.is_authenticated:
            return False
        return user.pk == self.owner_id or user in self.collaborators.all()

    @property
    def track_count(self) -> int:
        return self.tracks.count()

    @property
    def total_duration_seconds(self) -> int:
        from django.db.models import Sum

        return self.tracks.aggregate(total=Sum("duration_seconds"))["total"] or 0

    @property
    def duration_display(self) -> str:
        total = int(self.total_duration_seconds)
        hours, remainder = divmod(total // 60, 60)
        if hours:
            return f"{hours} hr {remainder} min"
        return f"{total // 60} min"

    def reorder(self, ordered_ids):
        """Persist a drag-and-drop ordering.

        Uses a two-phase write because ``(playlist, position)`` is unique and a
        naive in-place update would collide on swaps. Phase 1 parks every row at
        ``target + offset``; phase 2 drops each row onto its real target.
        """
        positions = {int(track_id): index for index, track_id in enumerate(ordered_ids, start=1)}
        entries = list(self.playlist_tracks.filter(track_id__in=positions))
        if not entries:
            return self.track_count
        offset = self.playlist_tracks.count() + 1000

        for entry in entries:
            entry.position = positions[entry.track_id] + offset
        PlaylistTrack.objects.bulk_update(entries, ["position"])

        for entry in entries:
            entry.position = positions[entry.track_id]
        PlaylistTrack.objects.bulk_update(entries, ["position"])

        return self.track_count


class PlaylistTrack(models.Model):
    playlist = models.ForeignKey(Playlist, related_name="playlist_tracks", on_delete=models.CASCADE)
    track = models.ForeignKey("music.Track", related_name="playlist_entries", on_delete=models.CASCADE)
    position = models.PositiveIntegerField(default=1)
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="added_playlist_tracks",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "playlists_playlist_track"
        ordering = ("position", "added_at")
        constraints = [
            models.UniqueConstraint(fields=["playlist", "track"], name="uniq_track_per_playlist"),
            models.UniqueConstraint(
                fields=["playlist", "position"], name="uniq_position_per_playlist"
            ),
        ]
        indexes = [models.Index(fields=["playlist", "position"])]

    def __str__(self):
        return f"{self.track_id} @ {self.position} in {self.playlist_id}"


class Favorite(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, related_name="favorites", on_delete=models.CASCADE
    )
    track = models.ForeignKey(
        "music.Track", related_name="favorited_by", on_delete=models.CASCADE
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "playlists_favorite"
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(fields=["user", "track"], name="uniq_favorite_per_user")
        ]
        indexes = [models.Index(fields=["user", "-created_at"]), models.Index(fields=["track"])]

    def __str__(self):
        return f"{self.user.email} ♥ {self.track_id}"


class PlayHistory(models.Model):
    """Deduplicated per-user listening record, latest entry per track per day."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, related_name="history", on_delete=models.CASCADE
    )
    track = models.ForeignKey("music.Track", related_name="in_history", on_delete=models.CASCADE)
    last_played_at = models.DateTimeField(default=timezone.now, db_index=True)
    play_count = models.PositiveIntegerField(default=0)
    completed = models.BooleanField(default=True)

    class Meta:
        db_table = "playlists_play_history"
        ordering = ("-last_played_at",)
        constraints = [
            models.UniqueConstraint(fields=["user", "track"], name="uniq_history_per_user_track")
        ]
        indexes = [models.Index(fields=["user", "-last_played_at"])]

    def __str__(self):
        return f"{self.user.email} → {self.track_id} ({self.play_count}x)"

    @classmethod
    def record(cls, user, track, *, completed=True):
        """Upsert the history row for a (user, track) pair."""
        entry, _created = cls.objects.update_or_create(
            user=user,
            track=track,
            defaults={"last_played_at": timezone.now(), "completed": completed},
        )
        cls.objects.filter(pk=entry.pk).update(play_count=F("play_count") + 1)
        entry.refresh_from_db()
        return entry


class PlaybackQueue(models.Model):
    """A listener's saved play queue, so playback survives a reload or a new tab.

    One queue per user (``OneToOne``) rather than a named list: the player has a
    single now-playing/next-up list, and a one-to-one keeps the write path a
    single upsert instead of a "which queue did you mean?" lookup.
    """

    class Repeat(models.TextChoices):
        OFF = "off", "Off"
        ALL = "all", "Repeat all"
        ONE = "one", "Repeat one"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, related_name="playback_queue", on_delete=models.CASCADE
    )
    current_position = models.PositiveIntegerField(
        default=0, help_text="Index into the queue of the currently loaded track."
    )
    shuffle = models.BooleanField(default=False)
    repeat = models.CharField(
        max_length=8, choices=Repeat.choices, default=Repeat.OFF
    )
    updated_at = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        db_table = "playlists_playback_queue"
        verbose_name = "playback queue"

    def __str__(self):
        return f"queue for {self.user_id} ({self.items.count()} tracks)"

    @property
    def total_duration_seconds(self) -> int:
        return sum(item.track.duration_seconds for item in self.items.select_related("track"))


class PlaybackQueueItem(models.Model):
    queue = models.ForeignKey(PlaybackQueue, related_name="items", on_delete=models.CASCADE)
    track = models.ForeignKey("music.Track", related_name="queue_entries", on_delete=models.CASCADE)
    position = models.PositiveIntegerField()
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "playlists_playback_queue_item"
        ordering = ("position", "added_at")
        constraints = [
            models.UniqueConstraint(fields=["queue", "track"], name="uniq_queue_track"),
            models.UniqueConstraint(fields=["queue", "position"], name="uniq_queue_position"),
        ]
        indexes = [models.Index(fields=["queue", "position"])]

    def __str__(self):
        return f"{self.track_id} @ {self.position} in {self.queue_id}"


class PlaylistFollow(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, related_name="followed_playlists", on_delete=models.CASCADE
    )
    playlist = models.ForeignKey(Playlist, related_name="followers", on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "playlists_playlist_follow"
        constraints = [
            models.UniqueConstraint(fields=["user", "playlist"], name="uniq_playlist_follow")
        ]

    def __str__(self):
        return f"{self.user_id} follows {self.playlist_id}"


def trending_playlists(days: int = 7, limit: int = 10):
    """Playlists gaining followers/plays inside a rolling window.

    Only playlists with at least one in-window play are returned; otherwise the
    tail of a short result set fills up with arbitrary inactive playlists.
    ``distinct=True`` matters here because joining tracks, their play events and
    followers in one query multiplies the rows and would inflate the counts.
    """
    from analytics.models import PlayEvent  # noqa: F401  (registers the relation)

    cutoff = timezone.now() - timezone.timedelta(days=days)
    return (
        Playlist.objects.filter(visibility=Playlist.Visibility.PUBLIC)
        .annotate(
            window_plays=Count(
                "tracks__play_events",
                filter=Q(tracks__play_events__played_at__gte=cutoff),
                distinct=True,
            ),
            followers_total=Count("followers", distinct=True),
        )
        .filter(window_plays__gt=0)
        .order_by("-window_plays", "-followers_total")[:limit]
    )


# Module-level alias for schema generation; see the note in music/models.py.
PLAYBACK_QUEUE_REPEAT_CHOICES = PlaybackQueue.Repeat.choices
