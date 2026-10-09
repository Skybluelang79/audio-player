"""Track / artist / album / genre endpoints, uploads, filtering."""

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from music.models import Album, Artist, Genre, Track

from .factories import CatalogMixin, MediaRootMixin, make_audio, make_image

User = get_user_model()
PASSWORD = "Str0ngPass!23"


class TrackListingTests(CatalogMixin, TestCase):
    def test_list_hides_drafts_from_anonymous_users(self):
        response = APIClient().get("/api/tracks/")
        self.assertEqual(response.status_code, 200)
        titles = [row["title"] for row in response.data["results"]]
        self.assertIn("Glass Cities", titles)
        self.assertNotIn("Unreleased Sketches", titles)

    def test_list_exposes_drafts_to_staff(self):
        staff = User.objects.create_user(
            email="boss@example.com", password=PASSWORD, is_staff=True, role=User.Role.MANAGER
        )
        client = APIClient()
        client.force_authenticate(staff)
        titles = [row["title"] for row in client.get("/api/tracks/").data["results"]]
        self.assertIn("Unreleased Sketches", titles)

    def test_detail_returns_audio_url_and_derived_fields(self):
        response = APIClient().get(f"/api/tracks/{self.live_track.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["duration_display"], "3:34")
        self.assertIn("audio_url", response.data)
        self.assertTrue(response.data["is_playable"])
        self.assertEqual(response.data["file_size_display"], "2.0 KB")

    def test_draft_detail_is_404_for_public(self):
        response = APIClient().get(f"/api/tracks/{self.draft_track.pk}/")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data["error"]["code"], "not_found")

    def test_ordering_by_play_count(self):
        Track.objects.filter(pk=self.live_track.pk).update(play_count=5)
        Track.objects.filter(pk=self.second_track.pk).update(play_count=50)
        results = APIClient().get("/api/tracks/?ordering=-play_count").data["results"]
        self.assertEqual(results[0]["title"], "Chrome Rain")

    def test_filter_by_genre_and_artist(self):
        by_genre = APIClient().get(f"/api/tracks/?genre={self.genre.pk}").data["results"]
        self.assertEqual(len(by_genre), 1)
        self.assertEqual(by_genre[0]["title"], "Glass Cities")

        by_artist = APIClient().get(f"/api/tracks/?artist={self.other_artist.pk}").data["results"]
        self.assertEqual(by_artist, [])

    def test_filter_by_duration_range_and_year(self):
        results = APIClient().get("/api/tracks/?min_duration=200").data["results"]
        self.assertEqual([row["title"] for row in results], ["Glass Cities"])

        by_year = APIClient().get("/api/tracks/?release_year=2026").data["results"]
        self.assertEqual(len(by_year), 2)

    def test_search_matches_artist_and_album(self):
        # "Neon" is the album title and both tracks belong to that album.
        results = APIClient().get("/api/tracks/?search=Neon").data["results"]
        self.assertEqual(
            sorted(row["title"] for row in results), ["Chrome Rain", "Glass Cities"]
        )

        # Artist name search.
        results = APIClient().get("/api/tracks/?search=Kestrel").data["results"]
        self.assertEqual(len(results), 2)

        # No match.
        self.assertEqual(APIClient().get("/api/tracks/?search=Rift").data["results"], [])

    def test_pagination_envelope(self):
        payload = APIClient().get("/api/tracks/?page_size=1").data
        self.assertEqual(payload["count"], 2)
        self.assertEqual(payload["page_size"], 1)
        self.assertEqual(len(payload["results"]), 1)
        self.assertIsNotNone(payload["next"])


class TrackPopularTrendingTests(CatalogMixin, TestCase):
    def test_popular_orders_by_play_count_and_honours_limit(self):
        Track.objects.filter(pk=self.second_track.pk).update(play_count=99)
        results = APIClient().get("/api/catalog/popular/?limit=1").data
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["title"], "Chrome Rain")

    def test_trending_ranks_by_window_volume(self):
        from analytics.models import PlayEvent

        for _ in range(3):
            PlayEvent.objects.create(track=self.second_track)
        titles = [row["title"] for row in APIClient().get("/api/catalog/trending/").data]
        self.assertEqual(titles[0], "Chrome Rain")

    def test_trending_ignores_out_of_window_events(self):
        from datetime import timedelta

        from django.utils import timezone

        from analytics.models import PlayEvent

        PlayEvent.objects.create(
            track=self.draft_track, played_at=timezone.now() - timedelta(days=30)
        )
        rows = APIClient().get("/api/catalog/trending/?days=7").data
        self.assertNotIn("Unreleased Sketches", [row["title"] for row in rows])

    def test_bad_limit_falls_back_to_default(self):
        results = APIClient().get("/api/catalog/popular/?limit=abc").data
        self.assertEqual(len(results), 2)


class TrackUploadTests(MediaRootMixin, CatalogMixin, TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="dj@example.com", password=PASSWORD)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _payload(self, **overrides):
        data = {
            "title": "Midnight Circuit",
            "artist": self.artist.pk,
            "audio_file": make_audio("midnight.mp3"),
        }
        data.update(overrides)
        return data

    def test_upload_creates_draft_track_with_audio(self):
        response = self.client.post("/api/tracks/", self._payload(), format="multipart")
        self.assertEqual(response.status_code, 201, response.data)

        track = Track.objects.get(title="Midnight Circuit")
        self.assertEqual(track.status, Track.Status.DRAFT)
        self.assertTrue(track.audio_file.name.startswith("audio/"))
        self.assertTrue(track.audio_file.name.endswith(".mp3"))
        self.assertGreater(track.file_size_bytes, 0)

    def test_draft_upload_is_hidden_from_public_listing(self):
        self.client.post("/api/tracks/", self._payload(), format="multipart")
        titles = [row["title"] for row in APIClient().get("/api/tracks/").data["results"]]
        self.assertNotIn("Midnight Circuit", titles)

    def test_upload_with_cover_art_and_genres(self):
        response = self.client.post(
            "/api/tracks/",
            self._payload(cover_image=make_image("art.png"), genres=[self.genre.pk]),
            format="multipart",
        )
        self.assertEqual(response.status_code, 201, response.data)
        track = Track.objects.get(title="Midnight Circuit")
        self.assertTrue(track.cover_image.name.startswith("tracks/"))
        self.assertEqual(list(track.genres.all()), [self.genre])

    def test_upload_rejects_non_audio_extension(self):
        response = self.client.post(
            "/api/tracks/", self._payload(audio_file=make_audio("notes.txt")), format="multipart"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("audio_file", response.data["error"]["details"])

    def test_upload_rejects_oversized_file(self):
        big = make_audio("huge.mp3", size=2 * 1024 * 1024)
        with self.settings(MAX_UPLOAD_SIZE_MB=1):
            response = self.client.post("/api/tracks/", self._payload(audio_file=big), format="multipart")
        self.assertEqual(response.status_code, 400)
        self.assertIn("audio_file", response.data["error"]["details"])

    def test_upload_requires_authentication(self):
        response = APIClient().post("/api/tracks/", self._payload(), format="multipart")
        self.assertEqual(response.status_code, 401)

    def test_album_from_another_artist_is_rejected(self):
        other_album = Album.objects.create(title="Foreign", artist=self.other_artist)
        response = self.client.post(
            "/api/tracks/", self._payload(album=other_album.pk), format="multipart"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("album", response.data["error"]["details"])

    def test_staff_upload_can_publish_directly(self):
        self.user.is_staff = True
        self.user.save(update_fields=["is_staff"])
        response = self.client.post(
            "/api/tracks/", self._payload(status=Track.Status.PUBLISHED), format="multipart"
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Track.objects.get(title="Midnight Circuit").status, Track.Status.PUBLISHED)

    def test_isrc_is_normalised_to_uppercase(self):
        response = self.client.post("/api/tracks/", self._payload(isrc="usabc12345678"), format="multipart")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Track.objects.get(title="Midnight Circuit").isrc, "USABC12345678")

    def test_duplicate_isrc_is_rejected(self):
        Track.objects.filter(pk=self.live_track.pk).update(isrc="USABC12345678")
        response = self.client.post("/api/tracks/", self._payload(isrc="USABC12345678"), format="multipart")
        self.assertEqual(response.status_code, 400)

    def test_release_date_inherits_from_album(self):
        response = self.client.post(
            "/api/tracks/", self._payload(album=self.album.pk), format="multipart"
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(str(Track.objects.get(title="Midnight Circuit").release_date), "2026-01-15")


class TrackStaffManageTests(MediaRootMixin, CatalogMixin, TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            email="boss@example.com", password=PASSWORD, is_staff=True, role=User.Role.MANAGER
        )
        self.user = User.objects.create_user(email="dj@example.com", password=PASSWORD)

    def test_regular_user_cannot_use_manage_action(self):
        client = APIClient()
        client.force_authenticate(self.user)
        response = client.patch(f"/api/tracks/{self.draft_track.pk}/manage/", {"title": "Hijack"}, format="json")
        self.assertEqual(response.status_code, 403)

    def test_staff_can_publish_via_manage_action(self):
        client = APIClient()
        client.force_authenticate(self.staff)
        response = client.patch(
            f"/api/tracks/{self.draft_track.pk}/manage/",
            {"status": Track.Status.PUBLISHED},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.draft_track.refresh_from_db()
        self.assertEqual(self.draft_track.status, Track.Status.PUBLISHED)


class PlayCountingTests(CatalogMixin, TestCase):
    def test_play_increments_counter_and_writes_event(self):
        from analytics.models import PlayEvent

        response = APIClient().post(f"/api/tracks/{self.live_track.pk}/play/", {}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["play_count"], 1)
        self.assertEqual(PlayEvent.objects.filter(track=self.live_track).count(), 1)
        self.live_track.refresh_from_db()
        self.assertEqual(self.live_track.play_count, 1)

    def test_anonymous_play_is_attributed_to_nobody(self):
        from analytics.models import PlayEvent

        APIClient().post(f"/api/tracks/{self.live_track.pk}/play/", {}, format="json")
        self.assertIsNone(PlayEvent.objects.get().user)

    def test_draft_track_play_is_ignored(self):
        response = APIClient().post(f"/api/tracks/{self.draft_track.pk}/play/", {}, format="json")
        self.assertEqual(response.status_code, 404)

    def test_authenticated_play_updates_history(self):
        from playlists.models import PlayHistory

        user = User.objects.create_user(email="dj@example.com", password=PASSWORD)
        client = APIClient()
        client.force_authenticate(user)
        client.post(f"/api/tracks/{self.live_track.pk}/play/", {}, format="json")
        client.post(f"/api/tracks/{self.live_track.pk}/play/", {}, format="json")

        entry = PlayHistory.objects.get(user=user, track=self.live_track)
        self.assertEqual(entry.play_count, 2)

    def test_play_captures_source_and_client_ip(self):
        from analytics.models import PlayEvent

        APIClient().post(
            f"/api/tracks/{self.live_track.pk}/play/",
            {"source": "mobile", "listened_seconds": 30},
            format="json",
            REMOTE_ADDR="198.51.100.7",
        )
        event = PlayEvent.objects.get()
        self.assertEqual(event.source, "mobile")
        self.assertEqual(event.listened_seconds, 30)
        self.assertEqual(event.ip_address, "198.51.100.7")

    def test_invalid_listened_seconds_is_rejected(self):
        response = APIClient().post(
            f"/api/tracks/{self.live_track.pk}/play/", {"listened_seconds": -5}, format="json"
        )
        self.assertEqual(response.status_code, 400)

    def test_like_increments_counter(self):
        response = APIClient().post(f"/api/tracks/{self.live_track.pk}/like/", {}, format="json")
        self.assertEqual(response.data["like_count"], 1)

    def test_stream_returns_metadata(self):
        response = APIClient().get(f"/api/tracks/{self.live_track.pk}/stream/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("/media/audio/", response.data["url"])
        self.assertEqual(response.data["duration_seconds"], 214)


class ArtistAlbumGenreTests(CatalogMixin, TestCase):
    def test_artist_list_includes_counts(self):
        results = APIClient().get("/api/artists/").data["results"]
        nova = next(row for row in results if row["name"] == "Nova Kestrel")
        self.assertEqual(nova["track_count"], 2)
        self.assertEqual(nova["album_count"], 1)

    def test_artist_detail_includes_top_tracks_and_albums(self):
        response = APIClient().get(f"/api/artists/{self.artist.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["top_tracks"]), 2)
        self.assertEqual(response.data["albums"][0]["title"], "Neon Horizon")
        self.assertEqual([g["name"] for g in response.data["genres"]], ["Synthwave"])

    def test_artist_sub_resources(self):
        self.assertEqual(APIClient().get(f"/api/artists/{self.artist.pk}/tracks/").data["count"], 2)
        self.assertEqual(APIClient().get(f"/api/artists/{self.artist.pk}/albums/").data["count"], 1)

    def test_follow_and_unfollow_require_login(self):
        response = APIClient().post(f"/api/artists/{self.artist.pk}/follow/", {}, format="json")
        self.assertEqual(response.status_code, 401)

    def test_follow_toggle(self):
        user = User.objects.create_user(email="fan@example.com", password=PASSWORD)
        client = APIClient()
        client.force_authenticate(user)

        response = client.post(f"/api/artists/{self.artist.pk}/follow/", {}, format="json")
        self.assertTrue(response.data["is_following"])
        self.assertEqual(response.data["follower_count"], 1)

        response = client.delete(f"/api/artists/{self.artist.pk}/follow/")
        self.assertFalse(response.data["is_following"])
        self.assertEqual(response.data["follower_count"], 0)

    def test_is_following_reflects_request_user(self):
        user = User.objects.create_user(email="fan2@example.com", password=PASSWORD)
        self.artist.followers.add(user)
        client = APIClient()
        client.force_authenticate(user)
        results = client.get("/api/artists/").data["results"]
        nova = next(row for row in results if row["name"] == "Nova Kestrel")
        self.assertTrue(nova["is_following"])

    def test_refresh_listeners_is_staff_only(self):
        # Anonymous -> 401 (auth runs first); logged-in non-staff -> 403.
        self.assertEqual(
            APIClient().post(f"/api/artists/{self.artist.pk}/refresh_listeners/", {}, format="json").status_code,
            401,
        )
        client = APIClient()
        client.force_authenticate(User.objects.create_user(email="dj@example.com", password=PASSWORD))
        self.assertEqual(
            client.post(f"/api/artists/{self.artist.pk}/refresh_listeners/", {}, format="json").status_code,
            403,
        )
        staff = User.objects.create_user(
            email="boss@example.com", password=PASSWORD, is_staff=True, role=User.Role.MANAGER
        )
        client = APIClient()
        client.force_authenticate(staff)
        response = client.post(f"/api/artists/{self.artist.pk}/refresh_listeners/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["monthly_listeners"], 0)

    def test_recalculate_monthly_listeners_counts_distinct_users(self):
        from analytics.models import PlayEvent

        a = User.objects.create_user(email="l1@example.com", password=PASSWORD)
        b = User.objects.create_user(email="l2@example.com", password=PASSWORD)
        for user in (a, a, b):
            PlayEvent.objects.create(track=self.live_track, user=user)
        self.assertEqual(self.artist.recalculate_monthly_listeners(), 2)

    def test_album_detail_orders_tracks_by_track_number(self):
        Track.objects.filter(pk=self.live_track.pk).update(track_number=2)
        Track.objects.filter(pk=self.second_track.pk).update(track_number=1)
        response = APIClient().get(f"/api/albums/{self.album.pk}/")
        self.assertEqual([t["title"] for t in response.data["tracks"]], ["Chrome Rain", "Glass Cities"])
        self.assertEqual(response.data["track_count"], 2)

    def test_album_tracks_subresource(self):
        self.assertEqual(APIClient().get(f"/api/albums/{self.album.pk}/tracks/").data["count"], 2)

    def test_album_creation_requires_staff(self):
        client = APIClient()
        client.force_authenticate(User.objects.create_user(email="dj@example.com", password=PASSWORD))
        self.assertEqual(
            client.post("/api/albums/", {"title": "Sneaky", "artist": self.artist.pk}, format="json").status_code,
            403,
        )

    def test_album_filter_by_type_and_year(self):
        self.assertEqual(APIClient().get("/api/albums/?album_type=album").data["count"], 1)
        self.assertEqual(APIClient().get("/api/albums/?release_year=2026").data["count"], 1)
        self.assertEqual(APIClient().get("/api/albums/?album_type=ep").data["count"], 0)

    def test_genre_list_and_filter(self):
        self.assertEqual(APIClient().get("/api/genres/").data["count"], 2)
        self.assertEqual(APIClient().get("/api/genres/?name=Synth").data["count"], 1)

    def test_genre_creation_requires_staff(self):
        client = APIClient()
        client.force_authenticate(User.objects.create_user(email="dj@example.com", password=PASSWORD))
        self.assertEqual(client.post("/api/genres/", {"name": "Grime"}, format="json").status_code, 403)

    def test_artist_slug_is_generated_and_unique(self):
        first = Artist.objects.create(name="Echo Vault")
        second = Artist.objects.create(name="Echo Vault")
        self.assertEqual(first.slug, "echo-vault")
        self.assertEqual(second.slug, "echo-vault-2")

    def test_track_slug_is_unique_across_identical_titles(self):
        other = Artist.objects.create(name="Twin Artist")
        clone = Track.objects.create(
            title="Glass Cities", artist=other, audio_file=make_audio("clone.mp3")
        )
        self.assertEqual(clone.slug, "glass-cities-2")


class ModelBehaviourTests(MediaRootMixin, CatalogMixin, TestCase):
    def test_deleting_track_removes_files_from_storage(self):
        import os

        track = Track.objects.create(
            title="Disposable", artist=self.artist, audio_file=make_audio("disposable.mp3")
        )
        path = track.audio_file.path
        self.assertTrue(os.path.exists(path))
        track.delete()
        self.assertFalse(os.path.exists(path))

    def test_is_playable_requires_published_and_file(self):
        self.assertTrue(self.live_track.is_playable)
        self.assertFalse(self.draft_track.is_playable)

    def test_trending_score_decays_with_age(self):
        from datetime import timedelta

        from django.utils import timezone

        from analytics.models import PlayEvent

        PlayEvent.objects.create(track=self.live_track, played_at=timezone.now())
        fresh = self.live_track.trending_score
        PlayEvent.objects.filter(track=self.live_track).update(
            played_at=timezone.now() - timedelta(days=6)
        )
        self.assertLess(self.live_track.trending_score, fresh)

    def test_album_totals(self):
        self.assertEqual(self.album.track_count, 2)
        self.assertEqual(self.album.total_duration_seconds, 401)
        self.assertEqual(self.album.total_plays, 0)

    def test_artist_totals(self):
        self.assertEqual(self.artist.track_count, 2)
        self.assertEqual(self.artist.total_plays, 0)


class TrackLyricsTests(CatalogMixin, TestCase):
    def test_plain_lyrics_payload(self):
        self.live_track.lyrics = "Hello\nWorld"
        self.live_track.save(update_fields=["lyrics"])
        response = APIClient().get(f"/api/tracks/{self.live_track.pk}/lyrics/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["has_lyrics"])
        self.assertFalse(response.data["synced"])
        self.assertEqual(response.data["lyrics"], "Hello\nWorld")
        self.assertEqual(response.data["lines"], [])

    def test_synced_lrc_is_parsed_into_lines(self):
        self.live_track.lyrics = "[00:01.00]Hello\n[00:04.50]World"
        self.live_track.save(update_fields=["lyrics"])
        response = APIClient().get(f"/api/tracks/{self.live_track.pk}/lyrics/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["synced"])
        self.assertEqual(
            response.data["lines"],
            [
                {"time": 1.0, "text": "Hello"},
                {"time": 4.5, "text": "World"},
            ],
        )

    def test_track_without_lyrics_reports_has_lyrics_false(self):
        response = APIClient().get(f"/api/tracks/{self.second_track.pk}/lyrics/")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["has_lyrics"])
        self.assertFalse(response.data["synced"])

    def test_detail_exposes_has_lyrics_and_download_url(self):
        self.live_track.lyrics = "Hello"
        self.live_track.save(update_fields=["lyrics"])
        response = APIClient().get(f"/api/tracks/{self.live_track.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["has_lyrics"])
        self.assertIn("/download/", response.data["download_url"])


class TrackStreamTokenTests(CatalogMixin, MediaRootMixin, TestCase):
    def test_stream_advertises_signed_endpoint_when_gated(self):
        with override_settings(STREAM_REQUIRE_AUTH=True):
            response = APIClient().get(f"/api/tracks/{self.live_track.pk}/stream/")
            self.assertEqual(response.status_code, 200, response.data)
            self.assertNotIn("/media/audio/", response.data["url"])
            self.assertIn("/api/tracks/", response.data["url"])

    def test_stream_keeps_raw_media_url_when_open(self):
        response = APIClient().get(f"/api/tracks/{self.live_track.pk}/stream/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("/media/audio/", response.data["url"])
        self.assertIn("download_url", response.data)

    def test_audio_without_token_is_403_when_gated(self):
        with override_settings(STREAM_REQUIRE_AUTH=True):
            response = APIClient().get(f"/api/tracks/{self.live_track.pk}/audio/")
            self.assertEqual(response.status_code, 403)

    def test_audio_with_valid_token_serves_file(self):
        from music.streaming import stream_url

        with override_settings(STREAM_REQUIRE_AUTH=True):
            response = APIClient().get(stream_url(self.live_track))
            try:
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response["Content-Type"], "audio/mpeg")
            finally:
                response.close()

    def test_audio_rejects_token_for_another_track(self):
        from music.streaming import stream_url

        other = self.second_track
        with override_settings(STREAM_REQUIRE_AUTH=True):
            token_url = stream_url(other)
            response = APIClient().get(
                f"/api/tracks/{self.live_track.pk}/audio/?token={token_url.split('token=', 1)[1]}"
            )
            self.assertEqual(response.status_code, 403)


class TrackDownloadTests(CatalogMixin, MediaRootMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.fan = User.objects.create_user(email="fan@example.com", password=PASSWORD)

    def test_download_requires_auth_when_gated(self):
        with override_settings(DOWNLOADS_REQUIRE_AUTH=True):
            response = APIClient().get(f"/api/tracks/{self.live_track.pk}/download/")
            self.assertEqual(response.status_code, 401)

    def test_download_allows_anonymous_when_disabled(self):
        with override_settings(DOWNLOADS_REQUIRE_AUTH=False):
            response = APIClient().get(f"/api/tracks/{self.live_track.pk}/download/")
            try:
                self.assertEqual(response.status_code, 200)
            finally:
                response.close()

    def test_download_serves_attachment_and_increments_count(self):
        client = APIClient()
        client.force_authenticate(self.fan)
        with override_settings(DOWNLOADS_REQUIRE_AUTH=True):
            response = client.get(f"/api/tracks/{self.live_track.pk}/download/")
            try:
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response["Content-Disposition"].startswith("attachment"))
                self.assertIn("Nova Kestrel - Glass Cities", response["Content-Disposition"])
            finally:
                response.close()
        self.live_track.refresh_from_db()
        self.assertEqual(self.live_track.download_count, 1)

    def test_cannot_download_draft(self):
        client = APIClient()
        client.force_authenticate(self.fan)
        with override_settings(DOWNLOADS_REQUIRE_AUTH=True):
            response = client.get(f"/api/tracks/{self.draft_track.pk}/download/")
            self.assertEqual(response.status_code, 404)
