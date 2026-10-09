"""Populate a browsable demo catalogue.

The repository ships a large ``media/`` tree of audio files but no matching
database rows, so a fresh checkout renders an empty site. This command builds a
coherent catalogue around those files: artists, albums, genres, tracks with
real audio attached, playlists, accounts, and enough play history for the
analytics pages and the trending shelves to have something to show.

Idempotent by slug/title: re-running tops up missing rows instead of
duplicating the catalogue.

    python manage.py seed_demo                    # full catalogue
    python manage.py seed_demo --tracks 40        # smaller
    python manage.py seed_demo --flush            # wipe seeded data first
    python manage.py seed_demo --plays 5000       # heavier analytics
"""

import random
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from music.models import Album, Artist, Genre, Track

GENRES = [
    ("Synthwave", "#00e5ff", "Retro-futurist arpeggios and neon drum machines."),
    ("Ambient", "#b47cff", "Slow-moving textures for focus and sleep."),
    ("Techno", "#ff4ecd", "Driving club music built for long sets."),
    ("Downtempo", "#00e5a0", "Laid-back grooves with warm low-end."),
    ("Lo-fi", "#4da3ff", "Dusty, tape-saturated beats for study."),
    ("Drum & Bass", "#ffb020", "Breakbeats at 174 and above."),
]

ALBUM_TYPES = [Album.AlbumType.ALBUM, Album.AlbumType.ALBUM, Album.AlbumType.EP, Album.AlbumType.SINGLE]

TITLE_HEADS = [
    "Neon", "Midnight", "Glass", "Silent", "Velvet", "Electric", "Crimson", "Hollow",
    "Lunar", "Static", "Golden", "Paper", "Iron", "Crystal", "Fading", "Northern",
]
TITLE_TAILS = [
    "Horizon", "Circuit", "Harbour", "Tide", "Bloom", "Signal", "Drift", "Parade",
    "Mirror", "Corridor", "Season", "Machine", "Lantern", "Avenue", "Weather", "Pulse",
]

BIOS = [
    "Producer and sound designer working between {city} and the rest of the world.",
    "{city}-based artist blending analogue hardware with generative software.",
    "Three-time local champion, now releasing records on their own imprint.",
    "Long-form ambient composer. Performs occasionally, records constantly.",
]

CITIES = [
    "Berlin", "Tokyo", "Lagos", "Lisbon", "Montreal", "Seoul", "Manchester",
    "São Paulo", "Reykjavík", "Melbourne", "Nairobi", "Rotterdam",
]

PLAYLIST_NAMES = [
    "Late Shift", "Focus Flow", "Rainy Window", "Long Drive", "Deep Work",
    "Warm Up", "After Hours", "Static Bloom", "Night Transit", "Slow Morning",
]


class Command(BaseCommand):
    help = "Seed a demo catalogue (artists, albums, tracks, playlists, accounts, plays)."

    def add_arguments(self, parser):
        parser.add_argument("--artists", type=int, default=12)
        parser.add_argument("--tracks", type=int, default=60)
        parser.add_argument("--albums", type=int, default=0, help="0 = one per artist.")
        parser.add_argument("--plays", type=int, default=2000)
        parser.add_argument("--listeners", type=int, default=15)
        parser.add_argument(
            "--audio",
            choices=["auto", "media", "synth"],
            default="auto",
            help="auto: reuse media/ files then synthesise; media: only existing files; synth: generated silence.",
        )
        parser.add_argument("--flush", action="store_true", help="Delete the demo catalogue first.")
        parser.add_argument("--seed", type=int, default=20260902, help="RNG seed for reproducibility.")

    # -- entry point --------------------------------------------------------

    @transaction.atomic
    def handle(self, *args, **options):
        rng = random.Random(options["seed"])
        self.rng = rng
        self.audio_mode = options["audio"]
        self.playlists = []

        if options["flush"]:
            self._flush()

        genres = self._genres()
        pool = self._audio_pool()
        self.stdout.write(
            f"Audio pool: {len(pool)} file(s) from MEDIA_ROOT "
            f"({'synthesising the rest' if self.audio_mode != 'media' else 'media-only mode'})."
        )

        artists = self._artists(genres, options["artists"])
        albums = self._albums(artists, genres, options["albums"])
        tracks = self._tracks(artists, albums, genres, pool, options["tracks"])
        self._number_album_tracks(tracks)
        accounts = self._accounts(options["listeners"])
        self._playlists(accounts, tracks)

        plays = self._play_events(tracks, accounts, options["plays"])

        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded {len(genres)} genres, {len(artists)} artists, {len(albums)} albums, "
                f"{len(tracks)} tracks, {len(self.playlists)} playlists, {len(accounts)} accounts, "
                f"{plays} play events."
            )
        )
        self.stdout.write(
            self.style.WARNING(
                "Demo logins: admin@auralis.test / Admin!2345  |  "
                "listener@auralis.test / Listen!2345"
            )
        )

    # -- steps ---------------------------------------------------------------

    def _flush(self):
        from playlists.models import Playlist, PlaylistTrack
        from analytics.models import PlayEvent

        PlayEvent.objects.all().delete()
        PlaylistTrack.objects.all().delete()
        Playlist.objects.all().delete()
        Track.objects.all().delete()
        Album.objects.all().delete()
        Artist.objects.all().delete()
        Genre.objects.all().delete()
        self.stdout.write(self.style.WARNING("Flushed existing catalogue."))

    def _genres(self):
        made = []
        for name, color, description in GENRES:
            genre, _ = Genre.objects.get_or_create(
                name=name, defaults={"color": color, "description": description}
            )
            made.append(genre)
        return made

    def _artists(self, genres, count):
        made = []
        for index in range(count):
            city = self.rng.choice(CITIES)
            name = self._artist_name()
            artist, created = Artist.objects.get_or_create(
                name=name,
                defaults={
                    "bio": self.rng.choice(BIOS).format(city=city),
                    "country": city,
                    "verified": self.rng.random() < 0.25,
                    "website": f"https://{name.split()[0].lower()}.example" if self.rng.random() < 0.4 else "",
                },
            )
            if created or not artist.genres.exists():
                artist.genres.add(*self.rng.sample(genres, k=min(3, len(genres))))
            made.append(artist)
        return made

    def _artist_name(self):
        first = self.rng.choice(
            ["Nova", "Rift", "Aster", "Kite", "Vela", "Onyx", "Lyra", "Ember", "Sable",
             "Halo", "Zephyr", "Cobalt", "Mirage", "Pallas", "Sable", "Orbit"]
        )
        second = self.rng.choice(
            ["Kestrel", "Meridian", "Drift", "Vellum", "Solstice", "Harbor", "Nine",
             "Circuit", "Atlas", "Verge", "Prism", "Wren"]
        )
        return f"{first} {second}"

    def _albums(self, artists, genres, count):
        made = []
        today = timezone.localdate()
        for artist in artists:
            how_many = count or self.rng.randint(1, 3)
            for _ in range(how_many):
                title = self._title()
                release = today - timedelta(days=self.rng.randint(1, 1500))
                album, created = Album.objects.get_or_create(
                    artist=artist,
                    title=title,
                    defaults={
                        "album_type": self.rng.choice(ALBUM_TYPES),
                        "release_date": release,
                        "label": f"{self.rng.choice(['North', 'Static', 'Blue Hour', 'Ember'])} "
                                 f"{self.rng.choice(['Tapes', 'Records', 'Sound', 'Audio'])}",
                        "description": f"{title} — recorded across {artist.country or 'several cities'}.",
                    },
                )
                if created:
                    album.genres.add(*self.rng.sample(genres, k=min(2, len(genres))))
                made.append(album)
        return made

    def _tracks(self, artists, albums, genres, pool, count):
        made = []
        today = timezone.localdate()
        # get_or_create(artist=, title=) raises MultipleObjectsReturned if the
        # same pair is generated twice, so titles are deduped per artist.
        used: dict[int, set] = {}

        def fresh_title(artist):
            seen = used.setdefault(artist.pk, set())
            existing = {t.title for t in made if t.artist_id == artist.pk}
            for _ in range(50):
                candidate = self._title()
                if candidate not in seen and candidate not in existing:
                    seen.add(candidate)
                    return candidate
            candidate = f"{self._title()} {len(seen) + 1}"
            seen.add(candidate)
            return candidate

        for index in range(count):
            artist = artists[index % len(artists)]
            album = self.rng.choice([a for a in albums if a.artist_id == artist.pk] or albums)
            title = fresh_title(artist)
            duration = self.rng.randint(95, 420)

            track, created = Track.objects.get_or_create(
                artist=artist,
                title=title,
                defaults={
                    "album": album,
                    "duration_seconds": duration,
                    "bitrate_kbps": self.rng.choice([128, 192, 256, 320]),
                    "sample_rate_hz": 44100,
                    "release_date": album.release_date or (today - timedelta(days=self.rng.randint(1, 900))),
                    "status": Track.Status.PUBLISHED,
                    "explicit": self.rng.random() < 0.12,
                    "track_number": None,
                },
            )
            if created:
                # Publish ~88% and leave the rest as drafts.
                if self.rng.random() < 0.12:
                    track.status = Track.Status.DRAFT
                    track.save(update_fields=["status"])

                if track.status == Track.Status.PUBLISHED:
                    track.genres.add(*self.rng.sample(genres, k=min(2, len(genres))))
                    self._attach_audio(track, pool, index)
                    Track.objects.filter(pk=track.pk).update(
                        play_count=self.rng.randint(0, 900),
                        like_count=self.rng.randint(0, 240),
                    )
            made.append(track)
        return made

    def _title(self):
        return f"{self.rng.choice(TITLE_HEADS)} {self.rng.choice(TITLE_TAILS)}"

    def _number_album_tracks(self, tracks):
        """Assign sequential track numbers per album.

        "Play album" orders by ``track_number``, so leaving every seeded track
        NULL would make the queue fall back to alphabetical title order.
        """
        by_album: dict[int, list] = {}
        for track in tracks:
            if track.album_id:
                by_album.setdefault(track.album_id, []).append(track)

        updated = 0
        for rows in by_album.values():
            for position, track in enumerate(sorted(rows, key=lambda t: t.title), start=1):
                if track.track_number != position:
                    track.track_number = position
                    track.save(update_fields=["track_number"])
                    updated += 1
        if updated:
            self.stdout.write(f"Numbered {updated} track(s) within their album.")

    def _attach_audio(self, track, pool, index):
        """Point the track at a real file so <audio> has something to stream.

        The orphan files already live under MEDIA_ROOT, so the field is assigned
        by name rather than copied -- seeding thousands of tracks would otherwise
        double the disk footprint for no benefit.
        """
        if self.audio_mode in ("auto", "media") and pool:
            name = pool[index % len(pool)]
            track.audio_file.name = name
            try:
                track.file_size_bytes = (Path(settings.MEDIA_ROOT) / name).stat().st_size
            except OSError:
                track.file_size_bytes = 0
            track.save(update_fields=["audio_file", "file_size_bytes"])
            return

        if self.audio_mode == "media":
            return  # media-only: leave tracks without a source

        # Synthesise a short silent MP3-shaped blob. Not decodable, but it makes
        # the player, the manifest endpoint and cover-art flows exercisable.
        name = f"{track.slug or track.pk}.mp3"
        payload = b"\xff\xfb\x90\x00" + b"\x00" * 4096
        from django.core.files.base import ContentFile

        track.audio_file.save(name, ContentFile(payload), save=False)
        track.save(update_fields=["audio_file", "file_size_bytes"])

    def _audio_pool(self):
        """Existing audio under MEDIA_ROOT as MEDIA_ROOT-relative names.

        Newest first, capped so the manifest/queue stays quick to hydrate.
        """
        if self.audio_mode == "synth":
            return []
        root = Path(settings.MEDIA_ROOT)
        audio_dir = root / "audio"
        search_root = audio_dir if audio_dir.exists() else root
        if not search_root.exists():
            return []
        found = []
        for path in search_root.rglob("*.mp3"):
            if not path.is_file():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_size > 0:
                found.append((stat.st_mtime, path.relative_to(root).as_posix()))
        found.sort(reverse=True)
        return [name for _, name in found[:500]]

    def _accounts(self, count):
        from accounts.models import User

        admin, created = User.objects.get_or_create(
            email="admin@auralis.test",
            defaults={
                "is_staff": True,
                "is_superuser": True,
                "role": User.Role.MANAGER,
                "display_name": "Station Manager",
            },
        )
        if created:
            admin.set_password("Admin!2345")
            admin.save()

        listener, created = User.objects.get_or_create(
            email="listener@auralis.test",
            defaults={"display_name": "Demo Listener", "role": User.Role.LISTENER},
        )
        if created:
            listener.set_password("Listen!2345")
            listener.save()

        made = [admin, listener]
        for index in range(max(0, count - 2)):
            email = f"listener{index + 1}@auralis.test"
            user, created = User.objects.get_or_create(
                email=email,
                defaults={
                    "display_name": self.rng.choice(
                        ["Ada", "Rex", "Mika", "Sol", "Juno", "Kai", "Wren", "Nix"]
                    )
                    + f" {index + 1}",
                    "role": User.Role.LISTENER,
                },
            )
            if created:
                user.set_password("Listen!2345")
                user.save()
            made.append(user)
        return made

    def _playlists(self, accounts, tracks):
        from playlists.models import Playlist, PlaylistTrack

        published = [t for t in tracks if t.status == Track.Status.PUBLISHED]
        if not published:
            return

        for index in range(len(PLAYLIST_NAMES)):
            owner = accounts[index % len(accounts)]
            playlist, created = Playlist.objects.get_or_create(
                name=PLAYLIST_NAMES[index], owner=owner
            )
            if not created:
                continue

            # Public, plus a couple of private ones so the privacy rules have
            # real rows to hide on the site.
            if index < 6:
                visibility = Playlist.Visibility.PUBLIC
            elif index < 8:
                visibility = Playlist.Visibility.UNLISTED
            else:
                visibility = Playlist.Visibility.PRIVATE

            chosen = self.rng.sample(published, k=min(len(published), self.rng.randint(6, 18)))
            PlaylistTrack.objects.bulk_create(
                [
                    PlaylistTrack(
                        playlist=playlist,
                        track=track,
                        position=position,
                        added_by=owner,
                    )
                    for position, track in enumerate(chosen, start=1)
                ],
                ignore_conflicts=True,
            )
            playlist.visibility = visibility
            playlist.total_plays = self.rng.randint(0, 5000)
            playlist.save(update_fields=["visibility", "total_plays"])
            self.playlists.append(playlist)

    def _play_events(self, tracks, accounts, count):
        """Spread play events over the last 30 days so trending is non-degenerate."""
        from analytics.models import PlayEvent

        published = [t for t in tracks if t.status == Track.Status.PUBLISHED]
        if not published or count <= 0:
            return 0

        now = timezone.now()
        # Weighted so a few tracks clearly dominate; otherwise trending is noise.
        weights = [1.0 / (index + 1) ** 0.6 for index in range(len(published))]
        sources = [PlayEvent.Source.WEB, PlayEvent.Source.WEB, PlayEvent.Source.MOBILE, PlayEvent.Source.API]

        rows = []
        for _ in range(count):
            track = self.rng.choices(published, weights=weights, k=1)[0]
            age = self.rng.random() ** 1.8  # recent-skewed
            rows.append(
                PlayEvent(
                    track=track,
                    user=self.rng.choice(accounts),
                    session_key=f"seed{self.rng.randint(0, 9999):04d}",
                    source=self.rng.choice(sources),
                    played_at=now - timedelta(seconds=age * 30 * 86400),
                    listened_seconds=track.duration_seconds,
                    completed=self.rng.random() < 0.86,
                )
            )
        PlayEvent.objects.bulk_create(rows, batch_size=1000)
        return len(rows)
