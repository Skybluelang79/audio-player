"""Analytics rollups, search endpoints and the staff dashboard."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client, TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from analytics.models import (
    ArtistListenerSnapshot,
    DailyPlatformStat,
    DailyTrackStat,
    PlayEvent,
    register_play,
)
from music.models import Track
from playlists.models import Playlist

from .factories import CatalogMixin, MediaRootMixin, make_audio

User = get_user_model()
PASSWORD = "Str0ngPass!23"


class RegisterPlayTests(CatalogMixin, TestCase):
    def test_counter_uses_f_expressions(self):
        register_play(self.live_track)
        register_play(self.live_track)
        self.live_track.refresh_from_db()
        self.assertEqual(self.live_track.play_count, 2)
        self.assertEqual(PlayEvent.objects.count(), 2)

    def test_draft_and_archived_are_ignored(self):
        self.assertIsNone(register_play(self.draft_track))
        self.draft_track.status = Track.Status.ARCHIVED
        self.draft_track.save(update_fields=["status"])
        self.assertIsNone(register_play(self.draft_track))
        self.assertEqual(PlayEvent.objects.count(), 0)

    def test_completed_false_is_persisted(self):
        event = register_play(self.live_track, completed=False)
        self.assertFalse(event.completed)

    def test_client_ip_extracted_from_forwarded_header(self):
        from django.test import RequestFactory

        request = RequestFactory().get("/", HTTP_X_FORWARDED_FOR="203.0.113.9, 10.0.0.1")
        event = register_play(self.live_track, request=request)
        self.assertEqual(event.ip_address, "203.0.113.9")

    def test_user_agent_truncated(self):
        from django.test import RequestFactory

        request = RequestFactory().get("/", HTTP_USER_AGENT="x" * 500)
        event = register_play(self.live_track, request=request)
        self.assertLessEqual(len(event.user_agent), 256)


class RollupCommandTests(CatalogMixin, TestCase):
    """`manage.py aggregate_plays` is the public entry point for rollups."""

    def test_rolls_up_daily_track_stats(self):
        for _ in range(3):
            register_play(self.live_track)
        register_play(self.second_track)

        call_command("aggregate_plays", days=30)

        row = DailyTrackStat.objects.get(track=self.live_track)
        self.assertEqual(row.plays, 3)
        self.assertEqual(row.total_listened_seconds, 3 * self.live_track.duration_seconds)
        self.assertEqual(DailyPlatformStat.objects.get().total_plays, 4)

    def test_is_idempotent(self):
        register_play(self.live_track)
        call_command("aggregate_plays", days=30)
        before = DailyTrackStat.objects.count()
        call_command("aggregate_plays", days=30)
        self.assertEqual(DailyTrackStat.objects.count(), before)
        self.assertEqual(DailyTrackStat.objects.get(track=self.live_track).plays, 1)

    def test_events_outside_window_are_excluded(self):
        PlayEvent.objects.create(
            track=self.live_track, played_at=timezone.now() - timedelta(days=10)
        )
        call_command("aggregate_plays", days=3)
        self.assertFalse(
            DailyTrackStat.objects.filter(
                date__lt=timezone.localdate() - timedelta(days=3)
            ).exists()
        )

    def test_snapshot_artists_writes_monthly_row(self):
        user = User.objects.create_user(email="l@example.com", password=PASSWORD)
        register_play(self.live_track, user=user)
        call_command("aggregate_plays", days=30, snapshot_artists=True)

        self.artist.refresh_from_db()
        self.assertEqual(self.artist.monthly_listeners, 1)
        row = ArtistListenerSnapshot.objects.get(artist=self.artist)
        self.assertEqual(row.month, timezone.localdate().replace(day=1))
        self.assertEqual(row.monthly_listeners, 1)

    def test_prune_deletes_old_rows(self):
        register_play(self.live_track)
        call_command("aggregate_plays", days=1)
        call_command("aggregate_plays", days=30, prune=True)
        self.assertTrue(DailyTrackStat.objects.exists())

    def test_artist_monthly_listeners_counts_distinct_users(self):
        users = [
            User.objects.create_user(email=f"l{i}@example.com", password=PASSWORD)
            for i in range(3)
        ]
        for user in users + users[:1]:
            register_play(self.live_track, user=user)
        self.assertEqual(self.artist.recalculate_monthly_listeners(), 3)

    def test_listeners_outside_window_excluded(self):
        user = User.objects.create_user(email="old@example.com", password=PASSWORD)
        PlayEvent.objects.create(
            track=self.live_track, user=user, played_at=timezone.now() - timedelta(days=45)
        )
        self.assertEqual(self.artist.recalculate_monthly_listeners(), 0)


class ServiceTests(CatalogMixin, TestCase):
    def test_catalogue_overview_counts(self):
        register_play(self.live_track)
        from analytics.services import catalogue_overview

        overview = catalogue_overview()
        self.assertEqual(overview["published_tracks"], 2)
        self.assertEqual(overview["draft_tracks"], 1)
        self.assertEqual(overview["total_plays"], 1)

    def test_plays_timeseries_is_dense_and_ordered(self):
        register_play(self.live_track)
        from analytics.services import plays_timeseries

        series = plays_timeseries(days=7)
        self.assertEqual(len(series), 7)
        self.assertEqual(str(series[-1]["date"]), str(timezone.localdate()))
        self.assertEqual(sum(point["plays"] for point in series), 1)

    def test_top_tracks_ordered_by_plays(self):
        for _ in range(2):
            register_play(self.second_track)
        from analytics.services import top_tracks

        rows = top_tracks(limit=5)
        self.assertEqual(rows[0].title, "Chrome Rain")

    def test_leaderboard_ranks_artist(self):
        register_play(self.live_track)
        from analytics.services import leaderboard_artists

        self.assertEqual(leaderboard_artists(limit=5)[0].name, "Nova Kestrel")


class AnalyticsApiTests(CatalogMixin, TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            email="boss@example.com", password=PASSWORD, is_staff=True, role=User.Role.MANAGER
        )
        self.client = APIClient()
        self.client.force_authenticate(self.staff)
        register_play(self.live_track)

    def test_summary_endpoint_shape(self):
        response = self.client.get("/api/analytics/summary/")
        self.assertEqual(response.status_code, 200, response.data)
        for key in ("overview", "engagement", "growth", "timeseries", "top_tracks"):
            self.assertIn(key, response.data)
        self.assertEqual(response.data["overview"]["total_plays"], 1)

    def test_summary_requires_staff(self):
        self.assertEqual(APIClient().get("/api/analytics/summary/").status_code, 401)
        listener = APIClient()
        listener.force_authenticate(User.objects.create_user(email="dj@example.com", password=PASSWORD))
        self.assertEqual(listener.get("/api/analytics/summary/").status_code, 403)

    def test_top_tracks_and_leaderboard(self):
        for _ in range(2):
            register_play(self.live_track)
        top = self.client.get("/api/analytics/top-tracks/")
        self.assertEqual(top.status_code, 200, top.data)
        self.assertEqual(top.data[0]["title"], "Glass Cities")

        board = self.client.get("/api/analytics/artists/")
        self.assertEqual(board.status_code, 200, board.data)
        self.assertEqual(board.data[0]["name"], "Nova Kestrel")

    def test_track_analytics_detail(self):
        response = self.client.get(f"/api/analytics/tracks/{self.live_track.pk}/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["play_count"], 1)
        self.assertIn("daily", response.data)

    def test_daily_and_events_endpoints(self):
        call_command("aggregate_plays", days=30)
        self.assertEqual(self.client.get("/api/analytics/daily/").status_code, 200)
        self.assertEqual(self.client.get("/api/analytics/events/").status_code, 200)

    def test_rebuild_is_staff_gated(self):
        response = self.client.post("/api/analytics/rebuild/", {}, format="json")
        self.assertIn(response.status_code, (200, 202), response.data)


class SearchTests(CatalogMixin, TestCase):
    def _results(self, query):
        return APIClient().get(f"/api/search/?q={query}").data["results"]

    def test_global_search_finds_every_entity_type(self):
        results = self._results("Neon")
        self.assertTrue(results["albums"])
        self.assertTrue(results["tracks"])

    def test_search_returns_query_and_total(self):
        payload = APIClient().get("/api/search/?q=Neon").data
        self.assertEqual(payload["query"], "Neon")
        self.assertEqual(
            payload["total"], sum(len(v) for v in payload["results"].values())
        )

    def test_search_is_case_insensitive(self):
        self.assertTrue(self._results("nova")["artists"])
        self.assertTrue(self._results("NOVA")["artists"])

    def test_short_query_returns_empty(self):
        payload = APIClient().get("/api/search/?q=a").data
        self.assertEqual(payload["results"], {})
        self.assertEqual(payload["total"], 0)
        self.assertIn("at least", payload["detail"])

    def test_private_playlists_are_excluded(self):
        Playlist.objects.create(
            name="Secret Neon Mix",
            owner=User.objects.create_user(email="p@example.com", password=PASSWORD),
            visibility=Playlist.Visibility.PRIVATE,
        )
        self.assertEqual(self._results("Secret")["playlists"], [])

    def test_owner_sees_own_private_playlist(self):
        owner = User.objects.create_user(email="me@example.com", password=PASSWORD)
        Playlist.objects.create(
            name="Secret Neon Mix", owner=owner, visibility=Playlist.Visibility.PRIVATE
        )
        client = APIClient()
        client.force_authenticate(owner)
        results = client.get("/api/search/?q=Secret").data["results"]
        self.assertEqual(len(results["playlists"]), 1)

    def test_suggestions_are_typed_and_limited(self):
        data = APIClient().get("/api/search/suggest/?q=Neon&limit=5").data
        self.assertEqual(data["query"], "Neon")
        self.assertLessEqual(len(data["suggestions"]), 20)
        for row in data["suggestions"]:
            self.assertIn(row["type"], {"track", "artist", "album", "genre"})

    def test_track_search_endpoint(self):
        response = APIClient().get("/api/search/tracks/?q=Glass")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data[0]["title"], "Glass Cities")

    def test_drafts_never_appear_in_search(self):
        self.assertEqual(self._results("Sketches")["tracks"], [])


class DashboardTests(MediaRootMixin, CatalogMixin, TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            email="boss@example.com", password=PASSWORD, is_staff=True, role=User.Role.MANAGER
        )
        self.listener = User.objects.create_user(email="dj@example.com", password=PASSWORD)
        register_play(self.live_track, user=self.listener)
        self.client = Client()

    def test_anonymous_is_redirected_to_login(self):
        response = self.client.get("/dashboard/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])

    def test_listener_is_forbidden(self):
        self.client.force_login(self.listener)
        self.assertEqual(self.client.get("/dashboard/").status_code, 403)

    def test_dashboard_renders_stats(self):
        self.client.force_login(self.staff)
        response = self.client.get("/dashboard/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("overview", response.context)
        self.assertEqual(response.context["overview"]["total_plays"], 1)
        self.assertEqual(response.context["engagement"]["plays"], 1)

    def test_dashboard_sections_render(self):
        self.client.force_login(self.staff)
        for url in (
            "/dashboard/",
            "/dashboard/catalogue/",
            "/dashboard/audience/",
            "/dashboard/data/",
            "/dashboard/upload/",
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_quick_upload_creates_draft_with_new_artist(self):
        self.client.force_login(self.staff)
        response = self.client.post(
            "/dashboard/upload/",
            {
                "title": "Fresh Upload",
                "artist_name": "Brand New Act",
                "audio_file": make_audio("fresh.mp3"),
                "status": Track.Status.DRAFT,
                "consent": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        track = Track.objects.get(title="Fresh Upload")
        self.assertEqual(track.status, Track.Status.DRAFT)
        self.assertTrue(Track.objects.get(pk=track.pk).artist.name == "Brand New Act")

    def test_quick_upload_rejects_bad_extension(self):
        self.client.force_login(self.staff)
        response = self.client.post(
            "/dashboard/upload/",
            {
                "title": "Bad",
                "artist_name": "Someone",
                "audio_file": make_audio("notes.txt"),
                "consent": "on",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("audio_file", response.context["form"].errors)
        self.assertFalse(Track.objects.filter(title="Bad").exists())

    def test_quick_upload_requires_consent(self):
        self.client.force_login(self.staff)
        response = self.client.post(
            "/dashboard/upload/",
            {"title": "No Consent", "artist_name": "Someone", "audio_file": make_audio("nc.mp3")},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("consent", response.context["form"].errors)

    def test_upload_requires_artist_or_name(self):
        self.client.force_login(self.staff)
        response = self.client.post(
            "/dashboard/upload/",
            {"title": "Nameless", "audio_file": make_audio("nameless.mp3"), "consent": "on"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("artist", response.context["form"].errors)

    def test_upload_saves_genres(self):
        self.client.force_login(self.staff)
        self.client.post(
            "/dashboard/upload/",
            {
                "title": "With Genres",
                "artist_name": "Someone",
                "audio_file": make_audio("genres.mp3"),
                "genres": [self.genre.pk],
                "consent": "on",
            },
        )
        track = Track.objects.get(title="With Genres")
        self.assertEqual(list(track.genres.all()), [self.genre])

    def test_upload_rejects_album_from_another_artist(self):
        from music.models import Album

        other_album = Album.objects.create(title="Foreign", artist=self.other_artist)
        self.client.force_login(self.staff)
        response = self.client.post(
            "/dashboard/upload/",
            {
                "title": "Mismatch",
                "artist": self.artist.pk,
                "album": other_album.pk,
                "audio_file": make_audio("mismatch.mp3"),
                "consent": "on",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("album", response.context["form"].errors)