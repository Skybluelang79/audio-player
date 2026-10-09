"""Roll raw play events up into per-day statistics.

Idempotent and safe to re-run: each day is recomputed from the source events
and written with ``update_or_create``.

    python manage.py aggregate_plays --days 30
    python manage.py aggregate_plays --days 90 --snapshot-artists
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone


class Command(BaseCommand):
    help = "Aggregate PlayEvent rows into DailyTrackStat / DailyPlatformStat."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=30,
            help="How many days back to (re)compute. Default 30.",
        )
        parser.add_argument(
            "--snapshot-artists",
            action="store_true",
            help="Also refresh Artist.monthly_listeners from the last 30 days.",
        )
        parser.add_argument(
            "--prune",
            action="store_true",
            help="Delete rollup rows older than the window.",
        )

    def handle(self, *args, **options):
        days = max(options["days"], 1)
        cutoff = timezone.now() - timedelta(days=days)
        today = timezone.localdate()

        from analytics.models import (
            ArtistListenerSnapshot,
            DailyPlatformStat,
            DailyTrackStat,
            PlayEvent,
        )

        rows = self._track_rows(cutoff)
        written = 0
        for stat in rows:
            DailyTrackStat.objects.update_or_create(
                track_id=stat["track_id"],
                date=stat["day"],
                defaults={
                    "plays": stat["plays"],
                    "unique_listeners": stat["listeners"],
                    "total_listened_seconds": stat["seconds"],
                },
            )
            written += 1

        platform = self._platform_row(cutoff, today)
        DailyPlatformStat.objects.update_or_create(
            date=today, defaults=platform
        )

        if options["prune"]:
            stale = DailyTrackStat.objects.filter(date__lt=cutoff.date()).delete()
            self.stdout.write(f"Pruned {stale[0]} stale rollup rows.")

        self.stdout.write(
            self.style.SUCCESS(
                f"Aggregated {sum(r['plays'] for r in rows)} plays into "
                f"{written} daily track stats ({cutoff:%Y-%m-%d} → {today})."
            )
        )

        if options["snapshot_artists"]:
            count = self._snapshot_artists()
            self.stdout.write(self.style.SUCCESS(f"Refreshed monthly listeners for {count} artists."))

    def _track_rows(self, cutoff):
        from analytics.models import PlayEvent

        return list(
            PlayEvent.objects.filter(played_at__gte=cutoff)
            .annotate(day=TruncDate("played_at"))
            .values("track_id", "day")
            .annotate(
                plays=Count("id"),
                listeners=Count("user", filter=Q(user__isnull=False), distinct=True),
                seconds=Sum("listened_seconds"),
            )
        )

    def _platform_row(self, cutoff, today):
        from analytics.models import PlayEvent

        base = PlayEvent.objects.filter(played_at__gte=cutoff)
        by_source = {
            row["source"]: row["plays"]
            for row in base.values("source").annotate(plays=Count("id"))
        }
        return {
            "total_plays": base.count(),
            "unique_listeners": base.exclude(user__isnull=True).values("user").distinct().count(),
            "unique_tracks": base.values("track").distinct().count(),
            "by_source": by_source,
        }

    def _snapshot_artists(self):
        from analytics.models import ArtistListenerSnapshot, PlayEvent
        from music.models import Artist

        since = timezone.now() - timedelta(days=30)
        month = timezone.localdate().replace(day=1)

        counts = dict(
            PlayEvent.objects.filter(played_at__gte=since)
            .exclude(user__isnull=True)
            .values_list("track__artist_id")
            .annotate(listeners=Count("user", distinct=True))
            .values_list("track__artist_id", "listeners")
        )

        updated = 0
        for artist in Artist.objects.all():
            listeners = counts.get(artist.id, 0)
            artist.monthly_listeners = listeners
            artist.save(update_fields=["monthly_listeners"])
            ArtistListenerSnapshot.objects.update_or_create(
                artist=artist,
                month=month,
                defaults={"monthly_listeners": listeners, "total_plays": artist.total_plays},
            )
            updated += 1
        return updated
