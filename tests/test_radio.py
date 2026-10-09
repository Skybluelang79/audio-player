"""Radio station endpoint: seed-by-track/artist/genre/playlist and my_mix."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from playlists.models import Favorite, Playlist

from .factories import CatalogMixin, MediaRootMixin

User = get_user_model()
PASSWORD = "Str0ngPass!23"


class RadioStationTests(CatalogMixin, TestCase):
    def test_seed_by_track_starts_with_the_seed(self):
        response = APIClient().get(
            "/api/radio/",
            {"seed_type": "track", "seed_id": self.live_track.pk, "limit": 5},
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["seed_type"], "track")
        self.assertIn("Glass Cities", response.data["seed_label"])
        self.assertGreater(response.data["count"], 0)
        self.assertEqual(response.data["tracks"][0]["title"], "Glass Cities")

    def test_seed_by_artist_returns_their_catalogue(self):
        response = APIClient().get(
            "/api/radio/", {"seed_type": "artist", "seed_id": self.artist.pk}
        )
        self.assertEqual(response.status_code, 200, response.data)
        titles = {row["title"] for row in response.data["tracks"]}
        self.assertIn("Glass Cities", titles)
        self.assertIn("Chrome Rain", titles)
        self.assertNotIn("Unreleased Sketches", titles)

    def test_seed_by_genre(self):
        response = APIClient().get(
            "/api/radio/", {"seed_type": "genre", "seed_id": self.genre.pk}
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["seed_label"], "Synthwave")
        self.assertGreater(response.data["count"], 0)

    def test_seed_by_playlist(self):
        from playlists.models import Playlist, PlaylistTrack

        playlist = Playlist.objects.create(
            name="Road Trip", owner=self.owner_for_radio(), visibility=Playlist.Visibility.PUBLIC
        )
        PlaylistTrack.objects.create(playlist=playlist, track=self.live_track, position=1)
        PlaylistTrack.objects.create(playlist=playlist, track=self.second_track, position=2)
        response = APIClient().get(
            "/api/radio/", {"seed_type": "playlist", "seed_id": playlist.pk}
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["seed_label"], "Road Trip")

    def test_unknown_seed_type_is_404(self):
        response = APIClient().get("/api/radio/", {"seed_type": "cassette"})
        self.assertEqual(response.status_code, 404)

    def test_missing_seed_is_404(self):
        response = APIClient().get("/api/radio/", {"seed_type": "track", "seed_id": "missing"})
        self.assertEqual(response.status_code, 404)

    def test_limit_is_bounded(self):
        response = APIClient().get(
            "/api/radio/", {"seed_type": "track", "seed_id": self.live_track.pk, "limit": 9999}
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertLessEqual(response.data["count"], 100)

    @staticmethod
    def owner_for_radio():
        return User.objects.create_user(email="radio-owner@example.com", password=PASSWORD)


class RadioMyMixTests(CatalogMixin, TestCase):
    def test_my_mix_requires_login(self):
        response = APIClient().get("/api/radio/", {"seed_type": "my_mix"})
        self.assertEqual(response.status_code, 404)

    def test_my_mix_builds_from_favourites(self):
        user = User.objects.create_user(email="mix@example.com", password=PASSWORD)
        Favorite.objects.create(user=user, track=self.live_track)
        client = APIClient()
        client.force_authenticate(user)
        response = client.get("/api/radio/", {"seed_type": "my_mix"})
        self.assertEqual(response.status_code, 200, response.data)
        titles = {row["title"] for row in response.data["tracks"]}
        self.assertIn("Glass Cities", titles)
        self.assertEqual(response.data["seed_type"], "my_mix")