"""Package track audio as HLS for adaptive streaming.

    python manage.py build_hls --all
    python manage.py build_hls --published --force
    python manage.py build_hls 12 34 --variant 128 --variant 192
"""

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from music.hls import TranscodeError, ffmpeg_available, parse_variants, transcode_track
from music.models import Track


class Command(BaseCommand):
    help = "Transcode track audio into HLS segment playlists using ffmpeg."

    def add_arguments(self, parser):
        parser.add_argument("track_ids", nargs="*", type=int, help="Specific track ids.")
        parser.add_argument("--all", action="store_true", help="Every track with audio.")
        parser.add_argument(
            "--published", action="store_true", help="Only published tracks."
        )
        parser.add_argument(
            "--variant",
            action="append",
            dest="variants",
            metavar="KBPS",
            help="Bitrate variant in kbps (repeatable). Defaults to HLS_VARIANTS.",
        )
        parser.add_argument("--segment", type=int, help="Target segment length in seconds.")
        parser.add_argument("--force", action="store_true", help="Re-package existing output.")
        parser.add_argument(
            "--dry-run", action="store_true", help="List what would be transcoded."
        )

    def handle(self, *args, **options):
        variants = None
        if options.get("variants"):
            try:
                variants = parse_variants(options["variants"])
            except ValueError as exc:
                raise CommandError(str(exc)) from exc

        queryset = Track.objects.exclude(audio_file="")
        if options["track_ids"]:
            queryset = queryset.filter(pk__in=options["track_ids"])
        elif options["published"]:
            queryset = queryset.filter(status=Track.Status.PUBLISHED)
        elif not options["all"]:
            raise CommandError(
                "Choose which tracks to package: pass ids, --published or --all."
            )

        tracks = list(queryset.order_by("pk"))
        if not tracks:
            self.stdout.write("No matching tracks.")
            return

        if options["dry_run"]:
            for track in tracks:
                self.stdout.write(f"would package #{track.pk} {track.title!r}")
            return

        if not ffmpeg_available():
            raise CommandError("ffmpeg was not found on PATH (set FFMPEG_BINARY).")

        packaged = skipped = failed = 0
        for track in tracks:
            if track.hls_playlist and not options["force"]:
                skipped += 1
                continue
            try:
                result = transcode_track(
                    track,
                    variants=variants,
                    segment=options.get("segment"),
                    force=options["force"],
                )
            except TranscodeError as exc:
                failed += 1
                self.stderr.write(self.style.ERROR(f"#{track.pk} {track.title!r}: {exc}"))
                continue
            packaged += 1
            self.stdout.write(
                self.style.SUCCESS(
                    f"#{track.pk} {track.title!r} -> {result['playlist']} "
                    f"({', '.join(f'{v}k' for v in result['variants'])})"
                )
            )

        self.stdout.write(
            f"packaged {packaged}, skipped {skipped}, failed {failed} (of {len(tracks)})"
        )
