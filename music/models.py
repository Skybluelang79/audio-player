import uuid

from django.db import models
from django.db.models import Count, Q
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify

from .storage import (
    AudioUploadPath,
    CoverArtUploadPath,
    delete_file,
    validate_audio_extension,
    validate_image_extension,
    validate_upload_size,
)

TRENDING_HALF_LIFE_DAYS = 7


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class SluggedModel(models.Model):
    """Provides a stable unique slug derived from ``slug_source_field``."""

    slug_source_field = "name"

    slug = models.SlugField(max_length=220, unique=True, blank=True)

    class Meta:
        abstract = True

    def build_slug(self) -> str:
        source = getattr(self, self.slug_source_field, None) or "item"
        base = slugify(str(source))[:200] or "item"
        candidate = base
        suffix = 1
        queryset = self.__class__._default_manager.all()
        if self.pk:
            queryset = queryset.exclude(pk=self.pk)
        while queryset.filter(slug=candidate).exists():
            suffix += 1
            candidate = f"{base}-{suffix}"
        return candidate

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = self.build_slug()
        super().save(*args, **kwargs)


class Genre(SluggedModel):
    name = models.CharField(max_length=80, unique=True)
    slug_source_field = "name"
    description = models.TextField(blank=True, max_length=500)
    color = models.CharField(
        max_length=7, default="#00E5FF", help_text="Hex accent used by the client UI."
    )

    class Meta:
        db_table = "music_genre"
        ordering = ("name",)
        verbose_name_plural = "genres"
        indexes = [models.Index(fields=["name"])]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("music:genre-detail", kwargs={"slug": self.slug})


class Artist(SluggedModel, TimeStampedModel):
    # Artist names are intentionally NOT unique: tribute acts, tribute bands and
    # namesakes all legitimately coexist. ``slug`` stays unique via build_slug().
    name = models.CharField(max_length=180)
    slug_source_field = "name"
    bio = models.TextField(blank=True, max_length=3000)
    image = models.ImageField(
        upload_to=CoverArtUploadPath("artists"),
        blank=True,
        null=True,
        validators=[validate_image_extension, validate_upload_size],
        help_text="Press photo / artist portrait.",
    )
    genres = models.ManyToManyField(Genre, related_name="artists", blank=True)
    followers = models.ManyToManyField(
        "accounts.User", related_name="followed_artists", blank=True
    )
    verified = models.BooleanField(default=False)
    website = models.URLField(blank=True)
    country = models.CharField(max_length=64, blank=True)
    owner = models.ForeignKey(
        "accounts.User",
        related_name="artist_profiles",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        help_text="Account allowed to edit this profile.",
    )
    monthly_listeners = models.PositiveIntegerField(default=0, editable=False)

    class Meta:
        db_table = "music_artist"
        ordering = ("name",)
        indexes = [
            models.Index(fields=["-monthly_listeners"]),
            models.Index(fields=["-created_at"]),
        ]

    def __str__(self):
        return self.name

    @property
    def track_count(self) -> int:
        return self.tracks.published().count()

    @property
    def album_count(self) -> int:
        return self.albums.count()

    @property
    def total_plays(self) -> int:
        return self.tracks.aggregate(total=models.Sum("play_count"))["total"] or 0

    def get_absolute_url(self):
        return reverse("music:artist-detail", kwargs={"slug": self.slug})

    def recalculate_monthly_listeners(self):
        """Distinct users who played any of this artist's tracks in 30 days."""
        from analytics.models import PlayEvent

        since = timezone.now() - timezone.timedelta(days=30)
        value = (
            PlayEvent.objects.filter(track__artist=self, played_at__gte=since)
            .exclude(user__isnull=True)
            .values("user")
            .distinct()
            .count()
        )
        self.monthly_listeners = value
        self.save(update_fields=["monthly_listeners"])
        return value


class Album(SluggedModel, TimeStampedModel):
    class AlbumType(models.TextChoices):
        ALBUM = "album", "Album"
        EP = "ep", "EP"
        SINGLE = "single", "Single"
        COMPILATION = "compilation", "Compilation"
        LIVE = "live", "Live"

    title = models.CharField(max_length=200)
    slug_source_field = "title"
    artist = models.ForeignKey(
        Artist, related_name="albums", on_delete=models.CASCADE, db_index=True
    )
    genres = models.ManyToManyField(Genre, related_name="albums", blank=True)
    cover_image = models.ImageField(
        upload_to=CoverArtUploadPath("albums"),
        blank=True,
        null=True,
        validators=[validate_image_extension, validate_upload_size],
    )
    album_type = models.CharField(
        max_length=16, choices=AlbumType.choices, default=AlbumType.ALBUM, db_index=True
    )
    release_date = models.DateField(null=True, blank=True, db_index=True)
    label = models.CharField(max_length=150, blank=True)
    catalog_number = models.CharField(max_length=64, blank=True)
    description = models.TextField(blank=True, max_length=2000)

    class Meta:
        db_table = "music_album"
        ordering = ("-release_date", "title")
        constraints = [
            models.UniqueConstraint(
                fields=["artist", "title"], name="uniq_album_title_per_artist"
            )
        ]
        indexes = [
            models.Index(fields=["artist", "-release_date"]),
            models.Index(fields=["-release_date"]),
        ]

    def __str__(self):
        return f"{self.title} — {self.artist.name}"

    def get_absolute_url(self):
        return reverse("music:album-detail", kwargs={"slug": self.slug})

    @property
    def track_count(self) -> int:
        return self.tracks.count()

    @property
    def total_duration_seconds(self) -> int:
        return self.tracks.aggregate(total=models.Sum("duration_seconds"))["total"] or 0

    @property
    def duration_display(self) -> str:
        total = int(self.total_duration_seconds)
        hours, minutes = divmod(total // 60, 60)
        return f"{hours} hr {minutes} min" if hours else f"{minutes} min"

    @property
    def total_plays(self) -> int:
        return self.tracks.aggregate(total=models.Sum("play_count"))["total"] or 0


class TrackQuerySet(models.QuerySet):
    def published(self):
        return self.filter(status=Track.Status.PUBLISHED)

    def with_listeners(self):
        return self.select_related("artist", "album").prefetch_related("genres")

    def popular(self):
        return self.published().with_listeners().order_by("-play_count")

    def trending(self, days: int = TRENDING_HALF_LIFE_DAYS):
        """Rank by play volume inside a rolling window (portable across backends)."""
        cutoff = timezone.now() - timezone.timedelta(days=days)
        return (
            self.published()
            .with_listeners()
            .annotate(recent_plays=Count("play_events", filter=Q(play_events__played_at__gte=cutoff)))
            .order_by("-recent_plays", "-play_count")
        )

    def recently_released(self, days: int = 30):
        cutoff = timezone.now() - timezone.timedelta(days=days)
        return self.published().filter(release_date__gte=cutoff.date())


class Track(SluggedModel, TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PROCESSING = "processing", "Processing"
        PUBLISHED = "published", "Published"
        ARCHIVED = "archived", "Archived"

    title = models.CharField(max_length=200)
    slug_source_field = "title"
    artist = models.ForeignKey(
        Artist, related_name="tracks", on_delete=models.CASCADE, db_index=True
    )
    album = models.ForeignKey(
        Album,
        related_name="tracks",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_index=True,
    )
    genres = models.ManyToManyField(Genre, related_name="tracks", blank=True)
    audio_file = models.FileField(
        upload_to=AudioUploadPath("audio"),
        validators=[validate_audio_extension, validate_upload_size],
        help_text="Master audio file.",
    )
    cover_image = models.ImageField(
        upload_to=CoverArtUploadPath("tracks"),
        blank=True,
        null=True,
        validators=[validate_image_extension, validate_upload_size],
    )
    duration_seconds = models.PositiveIntegerField(default=0, db_index=True)
    file_size_bytes = models.BigIntegerField(default=0, editable=False)
    bitrate_kbps = models.PositiveIntegerField(default=0)
    sample_rate_hz = models.PositiveIntegerField(default=0)
    isrc = models.CharField(max_length=24, blank=True, db_index=True)
    track_number = models.PositiveSmallIntegerField(
        null=True, blank=True, help_text="Position within the album."
    )
    explicit = models.BooleanField(default=False)
    lyrics = models.TextField(blank=True)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.DRAFT, db_index=True
    )
    release_date = models.DateField(null=True, blank=True, db_index=True)
    play_count = models.PositiveBigIntegerField(default=0, db_index=True)
    like_count = models.PositiveIntegerField(default=0)
    download_count = models.PositiveIntegerField(default=0)

    objects = TrackQuerySet.as_manager()

    class Meta:
        db_table = "music_track"
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["status", "-play_count"]),
            models.Index(fields=["artist", "status"]),
            models.Index(fields=["album"]),
            models.Index(fields=["-release_date"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["isrc"], condition=~Q(isrc=""), name="uniq_track_isrc_when_set"
            )
        ]

    def __str__(self):
        return f"{self.title} — {self.artist.name}"

    def get_absolute_url(self):
        return reverse("music:track-detail", kwargs={"slug": self.slug})

    def save(self, *args, **kwargs):
        if not self.release_date and self.album_id:
            self.release_date = self.album.release_date
        if self.audio_file and not self.file_size_bytes:
            # A file can disappear from storage independently of the database
            # (manual cleanup, a failed sync). Sizing must never be the reason
            # a save fails, or every later edit to the row -- including a bulk
            # status change -- would blow up with FileNotFoundError.
            try:
                self.file_size_bytes = self.audio_file.size
            except OSError:
                self.file_size_bytes = 0
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        delete_file(self.audio_file)
        delete_file(self.cover_image)
        return super().delete(*args, **kwargs)

    @property
    def is_playable(self) -> bool:
        return bool(self.audio_file) and self.status == self.Status.PUBLISHED

    @property
    def duration_display(self) -> str:
        total = int(self.duration_seconds or 0)
        return f"{total // 60}:{total % 60:02d}"

    @property
    def plays_today(self) -> int:
        from analytics.models import PlayEvent

        today = timezone.localdate()
        return self.play_events.filter(played_at__date=today).count()

    @property
    def trending_score(self) -> float:
        """Exponential decay over 7 days, normalised against the busiest track."""
        recent = self.play_events.filter(
            played_at__gte=timezone.now() - timezone.timedelta(days=7)
        )
        score = 0.0
        now = timezone.now()
        for event in recent.values_list("played_at", flat=True):
            age_days = max((now - event).total_seconds() / 86400, 0.0)
            score += 0.5 ** (age_days / TRENDING_HALF_LIFE_DAYS)
        return score


class TrackComment(TimeStampedModel):
    """Listener commentary on a track, with light moderation.

    ``status`` exists so a comment can be held or hidden without deleting the
    row: deletion is irreversible and there is no way to appeal it afterwards.
    Public reads only ever see ``VISIBLE`` rows; staff see everything.
    """

    class Status(models.TextChoices):
        VISIBLE = "visible", "Visible"
        PENDING = "pending", "Pending review"
        HIDDEN = "hidden", "Hidden"

    MAX_BODY = 2000

    track = models.ForeignKey(Track, on_delete=models.CASCADE, related_name="comments")
    user = models.ForeignKey(
        "accounts.User", on_delete=models.CASCADE, related_name="track_comments"
    )
    body = models.TextField(max_length=MAX_BODY)
    parent = models.ForeignKey(
        "self", on_delete=models.CASCADE, related_name="replies", null=True, blank=True
    )
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.VISIBLE, db_index=True
    )
    is_pinned = models.BooleanField(default=False)
    is_flagged = models.BooleanField(default=False, db_index=True)

    class Meta:
        db_table = "music_track_comment"
        ordering = ("-is_pinned", "-created_at")
        indexes = [
            models.Index(fields=["track", "-created_at"]),
            models.Index(fields=["status", "-created_at"]),
            models.Index(fields=["user", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.user_id} on {self.track_id}: {self.body[:40]}"

    @property
    def is_visible(self) -> bool:
        return self.status == self.Status.VISIBLE


class TrackFeaturedBy(TimeStampedModel):
    """Editorial/algorithmic placement on the home surface."""

    track = models.ForeignKey(Track, on_delete=models.CASCADE, related_name="featured_placements")
    curator = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, related_name="curated_features"
    )
    rank = models.PositiveSmallIntegerField(default=0)

    class Meta:
        db_table = "music_track_featured_by"
        ordering = ("rank",)
        constraints = [
            models.UniqueConstraint(fields=["track", "curator"], name="uniq_feature_per_curator")
        ]


# Module-level aliases for schema generation.
#
# ``SPECTACULAR_SETTINGS["ENUM_NAME_OVERRIDES"]`` values are resolved with
# ``import_string``, which cannot walk into a nested ``TextChoices`` (it splits
# the path at the last dot and looks for a module called "music.models.Track").
# Re-exporting the choice tuples here gives drf-spectacular a path it can
# actually import, which is what keeps the generated enum names stable.
TRACK_STATUS_CHOICES = Track.Status.choices
TRACK_COMMENT_STATUS_CHOICES = TrackComment.Status.choices
