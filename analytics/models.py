"""Listening telemetry: raw play events plus rollup tables for dashboards."""

import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Count, F
from django.db.models.functions import TruncDate
from django.utils import timezone


class PlayEvent(models.Model):
    """One completed (or started) playback of a track.

    Anonymous playback is allowed, so ``user`` is nullable and the row is
    attributed to a device/session instead.
    """

    class Source(models.TextChoices):
        WEB = "web", "Web player"
        MOBILE = "mobile", "Mobile app"
        DESKTOP = "desktop", "Desktop app"
        API = "api", "API"
        ADMIN = "admin", "Admin preview"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    track = models.ForeignKey("music.Track", related_name="play_events", on_delete=models.CASCADE)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="play_events",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_index=True,
    )
    session_key = models.CharField(max_length=64, blank=True, db_index=True)
    source = models.CharField(max_length=16, choices=Source.choices, default=Source.WEB, db_index=True)
    played_at = models.DateTimeField(default=timezone.now, db_index=True)
    listened_seconds = models.PositiveIntegerField(default=0)
    completed = models.BooleanField(default=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=256, blank=True)

    class Meta:
        db_table = "analytics_play_event"
        ordering = ("-played_at",)
        indexes = [
            models.Index(fields=["track", "-played_at"]),
            models.Index(fields=["user", "-played_at"]),
            models.Index(fields=["-played_at", "source"]),
        ]
        verbose_name = "play event"
        verbose_name_plural = "play events"

    def __str__(self):
        who = self.user.email if self.user else f"anon:{self.session_key[:8] or '-'}"
        return f"{self.track_id} by {who} at {self.played_at:%Y-%m-%d %H:%M}"


class DailyTrackStat(models.Model):
    """Per-track, per-day rollup. Written by ``aggregate_plays``."""

    track = models.ForeignKey("music.Track", related_name="daily_stats", on_delete=models.CASCADE)
    date = models.DateField(db_index=True)
    plays = models.PositiveIntegerField(default=0)
    unique_listeners = models.PositiveIntegerField(default=0)
    total_listened_seconds = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "analytics_daily_track_stat"
        ordering = ("-date",)
        constraints = [models.UniqueConstraint(fields=["track", "date"], name="uniq_track_stat_day")]
        indexes = [models.Index(fields=["-date", "-plays"])]

    def __str__(self):
        return f"{self.track_id} on {self.date}: {self.plays}"


class DailyPlatformStat(models.Model):
    """Platform-wide rollup for one calendar day."""

    date = models.DateField(unique=True, db_index=True)
    total_plays = models.PositiveIntegerField(default=0)
    unique_listeners = models.PositiveIntegerField(default=0)
    unique_tracks = models.PositiveIntegerField(default=0)
    by_source = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "analytics_daily_platform_stat"
        ordering = ("-date",)
        verbose_name = "daily platform stat"
        verbose_name_plural = "daily platform stats"

    def __str__(self):
        return f"{self.date}: {self.total_plays} plays"


class ArtistListenerSnapshot(models.Model):
    """Materialised monthly-listener counts so the artist page stays cheap."""

    artist = models.ForeignKey("music.Artist", related_name="listener_snapshots", on_delete=models.CASCADE)
    month = models.DateField(help_text="First day of the month.")
    monthly_listeners = models.PositiveIntegerField(default=0)
    total_plays = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "analytics_artist_listener_snapshot"
        ordering = ("-month",)
        constraints = [models.UniqueConstraint(fields=["artist", "month"], name="uniq_artist_month")]
        verbose_name = "artist listener snapshot"
        verbose_name_plural = "artist listener snapshots"

    def __str__(self):
        return f"{self.artist_id} {self.month:%Y-%m}: {self.monthly_listeners}"


class ArtistPayout(models.Model):
    """One artist's earnings for one period, frozen when generated.

    The play count and rate are snapshotted so a later rate change never
    rewrites history; ``amount`` is the quantised product of the two.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        PAID = "paid", "Paid"
        FAILED = "failed", "Failed"

    class Method(models.TextChoices):
        BANK = "bank", "Bank transfer"
        PAYPAL = "paypal", "PayPal"
        STRIPE = "stripe", "Stripe"
        OTHER = "other", "Other"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    artist = models.ForeignKey("music.Artist", related_name="payouts", on_delete=models.CASCADE)
    period_start = models.DateField()
    period_end = models.DateField()
    plays = models.PositiveIntegerField(default=0)
    rate_per_play = models.DecimalField(max_digits=8, decimal_places=5, default=Decimal("0.00400"))
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    method = models.CharField(max_length=12, choices=Method.choices, default=Method.BANK)
    reference = models.CharField(max_length=120, blank=True)
    note = models.TextField(blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "analytics_artist_payout"
        ordering = ("-period_end", "-amount")
        constraints = [
            models.UniqueConstraint(
                fields=["artist", "period_start", "period_end"], name="uniq_artist_payout_period"
            )
        ]
        indexes = [models.Index(fields=["status", "-period_end"])]
        verbose_name = "artist payout"
        verbose_name_plural = "artist payouts"

    def __str__(self):
        return f"{self.artist_id} {self.period_start}..{self.period_end}: {self.amount}"


def register_play(
    track, *, user=None, request=None, source=PlayEvent.Source.WEB, listened_seconds=0, completed=True
):
    """Record a play event and bump the denormalised counter atomically.

    Uses ``F()`` so concurrent requests cannot clobber the counter, and only
    increments tracks that are actually published.
    """
    if track.status != track.Status.PUBLISHED:
        return None

    event = PlayEvent(
        track=track,
        user=user if (user and user.is_authenticated) else None,
        source=source,
        completed=bool(completed),
        listened_seconds=listened_seconds or track.duration_seconds,
        ip_address=_extract_ip(request) if request else None,
        user_agent=(request.META.get("HTTP_USER_AGENT", "")[:256] if request else ""),
        session_key=(request.session.session_key or "")[:64] if request and hasattr(request, "session") else "",
    )
    event.save()
    track.__class__._default_manager.filter(pk=track.pk).update(play_count=F("play_count") + 1)
    track.play_count += 1
    return event


def _extract_ip(request):
    from accounts.models import User

    return User.client_ip(request) or None


def total_plays():
    return PlayEvent.objects.count()


def plays_in_window(days: int = 7):
    cutoff = timezone.now() - timezone.timedelta(days=days)
    return PlayEvent.objects.filter(played_at__gte=cutoff)


def plays_series(days: int = 14):
    """Daily play counts for the last ``days`` days, oldest first, zero-filled."""
    cutoff = timezone.now() - timezone.timedelta(days=days - 1)
    rows = (
        PlayEvent.objects.filter(played_at__gte=cutoff)
        .annotate(day=TruncDate("played_at"))
        .values("day")
        .annotate(plays=Count("id"))
        .order_by("day")
    )
    by_day = {row["day"]: row["plays"] for row in rows}
    series = []
    for offset in range(days - 1, -1, -1):
        day = (timezone.now() - timezone.timedelta(days=offset)).date()
        series.append({"date": day, "plays": by_day.get(day, 0)})
    return series


def source_breakdown(days: int = 30):
    rows = (
        plays_in_window(days)
        .values("source")
        .annotate(plays=Count("id"))
        .order_by("-plays")
    )
    return [{"source": row["source"], "plays": row["plays"]} for row in rows]
