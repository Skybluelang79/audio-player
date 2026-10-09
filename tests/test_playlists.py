"""Playlist permissions, ordering, follows, favourites and play history."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from music.models import Track

from .factories import CatalogMixin

User = get_user_model()
PASSWORD = "Str0ngPass!23"


class PlaylistFixtureMixin(CatalogMixin):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.owner = User.objects.create_user(email="owner@example.com", password=PASSWORD)
        cls.other = User.objects.create_user(email="other@example.com", password=PASSWORD)
        cls.staff = User.objects.create_user(
            email="boss@example.com", password=PASSWORD, is_staff=True, role=User.Role.MANAGER
        )


class PlaylistCreationTests(PlaylistFixtureMixin, TestCase):
    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.owner)

    def test_create_defaults_to_private_and_owned(self):
        response = self.client.post(
            "/api/playlists/", {"name": "Road Trip", "visibility": "private"}, format="json"
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["owner"]["id"], str(self.owner.pk))
        self.assertEqual(response.data["visibility"], "private")

    def test_creation_requires_login(self):
        response = APIClient().post("/api/playlists/", {"name": "Anon"}, format="json")
        self.assertEqual(response.status_code, 401)

    def test_owner_cannot_be_forced_onto_someone_else(self):
        response = self.client.post(
            "/api/playlists/",
            {"name": "Fake", "owner": self.other.pk, "visibility": "public"},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["owner"]["id"], str(self.owner.pk))

    def test_public_playlist_is_listable_by_anyone(self):
        from playlists.models import Playlist

        Playlist.objects.create(
            name="Open", owner=self.owner, visibility=Playlist.Visibility.PUBLIC
        )
        response = APIClient().get("/api/playlists/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)

    def test_private_playlist_hidden_from_strangers(self):
        from playlists.models import Playlist

        Playlist.objects.create(
            name="Secret", owner=self.owner, visibility=Playlist.Visibility.PRIVATE
        )
        client = APIClient()
        client.force_authenticate(self.other)
        self.assertEqual(client.get("/api/playlists/").data["count"], 0)
        self.assertEqual(APIClient().get("/api/playlists/").data["count"], 0)


class PlaylistAuthorizationTests(PlaylistFixtureMixin, TestCase):
    def setUp(self):
        from playlists.models import Playlist

        self.playlist = Playlist.objects.create(
            name="Mine", owner=self.owner, visibility=Playlist.Visibility.PRIVATE
        )
        self.client = APIClient()
        self.client.force_authenticate(self.other)

    def test_stranger_cannot_rename(self):
        response = self.client.patch(
            f"/api/playlists/{self.playlist.pk}/", {"name": "Hijacked"}, format="json"
        )
        self.assertEqual(response.status_code, 403)

    def test_stranger_cannot_delete(self):
        response = self.client.delete(f"/api/playlists/{self.playlist.pk}/")
        self.assertEqual(response.status_code, 403)

    def test_stranger_cannot_add_tracks(self):
        response = self.client.post(
            f"/api/playlists/{self.playlist.pk}/add_track/",
            {"track_id": self.live_track.pk},
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_stranger_cannot_reorder(self):
        response = self.client.post(
            f"/api/playlists/{self.playlist.pk}/reorder/",
            {"track_ids": [self.live_track.pk]},
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_stranger_cannot_follow_private_playlist(self):
        response = self.client.post(f"/api/playlists/{self.playlist.pk}/follow/", {}, format="json")
        self.assertEqual(response.status_code, 404)

    def test_stranger_cannot_see_private_playlist_detail(self):
        self.assertEqual(self.client.get(f"/api/playlists/{self.playlist.pk}/").status_code, 404)

    def test_collaborator_may_mutate(self):
        self.playlist.collaborators.add(self.other)
        response = self.client.patch(
            f"/api/playlists/{self.playlist.pk}/", {"name": "Shared"}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.data)

    def test_owner_may_mutate(self):
        client = APIClient()
        client.force_authenticate(self.owner)
        response = client.patch(
            f"/api/playlists/{self.playlist.pk}/", {"name": "Renamed"}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.data)

    def test_collaborator_cannot_delete(self):
        self.playlist.collaborators.add(self.other)
        self.assertEqual(self.client.delete(f"/api/playlists/{self.playlist.pk}/").status_code, 403)


class PlaylistMembershipTests(PlaylistFixtureMixin, TestCase):
    def setUp(self):
        from playlists.models import Playlist

        self.playlist = Playlist.objects.create(
            name="Mix", owner=self.owner, visibility=Playlist.Visibility.PUBLIC
        )
        self.client = APIClient()
        self.client.force_authenticate(self.owner)

    def test_add_track_appends_with_next_position(self):
        response = self.client.post(
            f"/api/playlists/{self.playlist.pk}/add_track/",
            {"track_id": self.live_track.pk},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(self.playlist.track_count, 1)

        self.client.post(
            f"/api/playlists/{self.playlist.pk}/add_track/",
            {"track_id": self.second_track.pk},
            format="json",
        )
        self.assertEqual(
            [p.track.title for p in self.playlist.playlist_tracks.order_by("position")],
            ["Glass Cities", "Chrome Rain"],
        )

    def test_adding_same_track_twice_is_rejected(self):
        self.client.post(
            f"/api/playlists/{self.playlist.pk}/add_track/",
            {"track_id": self.live_track.pk},
            format="json",
        )
        response = self.client.post(
            f"/api/playlists/{self.playlist.pk}/add_track/",
            {"track_id": self.live_track.pk},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_draft_track_cannot_be_added(self):
        response = self.client.post(
            f"/api/playlists/{self.playlist.pk}/add_track/",
            {"track_id": self.draft_track.pk},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_bulk_add_then_remove(self):
        response = self.client.post(
            f"/api/playlists/{self.playlist.pk}/add-tracks/",
            {"track_ids": [self.live_track.pk, self.second_track.pk]},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["added"], 2)
        self.assertEqual(self.playlist.track_count, 2)

        self.assertEqual(
            self.client.delete(
                f"/api/playlists/{self.playlist.pk}/tracks/{self.live_track.pk}/"
            ).status_code,
            204,
        )
        self.assertEqual(self.playlist.track_count, 1)

    def test_reorder_persists_without_negative_positions(self):
        for track in (self.live_track, self.second_track):
            self.client.post(
                f"/api/playlists/{self.playlist.pk}/add_track/",
                {"track_id": track.pk},
                format="json",
            )
        response = self.client.post(
            f"/api/playlists/{self.playlist.pk}/reorder/",
            {"track_ids": [self.second_track.pk, self.live_track.pk]},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            [p.track.title for p in self.playlist.playlist_tracks.order_by("position")],
            ["Chrome Rain", "Glass Cities"],
        )
        positions = list(
            self.playlist.playlist_tracks.values_list("position", flat=True)
        )
        self.assertTrue(all(p >= 1 for p in positions), positions)

    def test_remove_track_closes_position_gap(self):
        for track in (self.live_track, self.second_track):
            self.client.post(
                f"/api/playlists/{self.playlist.pk}/add_track/",
                {"track_id": track.pk},
                format="json",
            )
        self.client.delete(f"/api/playlists/{self.playlist.pk}/tracks/{self.live_track.pk}/")
        self.assertEqual(
            [p.position for p in self.playlist.playlist_tracks.all()], [1]
        )

    def test_detail_lists_tracks_in_order(self):
        for track in (self.live_track, self.second_track):
            self.client.post(
                f"/api/playlists/{self.playlist.pk}/add_track/",
                {"track_id": track.pk},
                format="json",
            )
        response = self.client.get(f"/api/playlists/{self.playlist.pk}/")
        self.assertEqual(
            [t["title"] for t in response.data["tracks"]], ["Glass Cities", "Chrome Rain"]
        )
        self.assertEqual(response.data["duration_display"], self.playlist.duration_display)


class PlaylistFollowTests(PlaylistFixtureMixin, TestCase):
    def setUp(self):
        from playlists.models import Playlist

        self.playlist = Playlist.objects.create(
            name="Open", owner=self.owner, visibility=Playlist.Visibility.PUBLIC
        )
        self.client = APIClient()
        self.client.force_authenticate(self.other)

    def test_follow_and_unfollow(self):
        response = self.client.post(f"/api/playlists/{self.playlist.pk}/follow/", {}, format="json")
        self.assertTrue(response.data["is_following"])
        self.assertEqual(response.data["follower_count"], 1)

        response = self.client.delete(f"/api/playlists/{self.playlist.pk}/follow/")
        self.assertFalse(response.data["is_following"])
        self.assertEqual(response.data["follower_count"], 0)

    def test_follow_requires_login(self):
        response = APIClient().post(f"/api/playlists/{self.playlist.pk}/follow/", {}, format="json")
        self.assertEqual(response.status_code, 401)

    def test_public_and_trending_endpoints(self):
        from playlists.models import PlaylistFollow

        PlaylistFollow.objects.create(user=self.other, playlist=self.playlist)
        self.assertEqual(len(APIClient().get("/api/playlists/public/").data), 1)
        self.assertEqual(APIClient().get("/api/playlists/trending/").status_code, 200)

    def test_trending_ignores_out_of_window_plays(self):
        from datetime import timedelta

        from django.utils import timezone

        from analytics.models import PlayEvent
        from playlists.models import PlaylistTrack

        PlaylistTrack.objects.create(playlist=self.playlist, track=self.live_track, position=1)
        PlayEvent.objects.create(
            track=self.live_track, played_at=timezone.now() - timedelta(days=20)
        )
        self.assertEqual(len(APIClient().get("/api/playlists/trending/?days=7").data), 0)


class FavoriteTests(PlaylistFixtureMixin, TestCase):
    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.other)

    def test_toggle_adds_then_removes(self):
        response = self.client.post(
            f"/api/favorites/tracks/{self.live_track.pk}/", {}, format="json"
        )
        self.assertTrue(response.data["is_favorite"])
        self.assertEqual(response.data["favorite_count"], 1)

        # Repeating the POST is idempotent, not a second toggle-off.
        response = self.client.post(
            f"/api/favorites/tracks/{self.live_track.pk}/", {}, format="json"
        )
        self.assertTrue(response.data["is_favorite"])
        self.assertEqual(response.data["favorite_count"], 1)

        response = self.client.delete(f"/api/favorites/tracks/{self.live_track.pk}/")
        self.assertFalse(response.data["is_favorite"])
        self.assertEqual(response.data["favorite_count"], 0)

        # Deleting again is harmless and still reports the same shape.
        response = self.client.delete(f"/api/favorites/tracks/{self.live_track.pk}/")
        self.assertFalse(response.data["is_favorite"])

    def test_bulk_add_is_idempotent(self):
        response = self.client.post(
            "/api/favorites/bulk_add/",
            {"track_ids": [self.live_track.pk, self.second_track.pk]},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["added"], 2)
        self.assertEqual(response.data["already_present"], 0)

        response = self.client.post(
            "/api/favorites/bulk_add/",
            {"track_ids": [self.live_track.pk, self.second_track.pk]},
            format="json",
        )
        self.assertEqual(response.data["added"], 0)
        self.assertEqual(response.data["already_present"], 2)

    def test_favorites_are_scoped_to_the_user(self):
        self.client.post(f"/api/favorites/tracks/{self.live_track.pk}/", {}, format="json")
        stranger = APIClient()
        stranger.force_authenticate(self.owner)
        self.assertEqual(stranger.get("/api/favorites/").data["count"], 0)

    def test_cannot_favorite_draft_track(self):
        response = self.client.post(f"/api/favorites/tracks/{self.draft_track.pk}/", {}, format="json")
        self.assertEqual(response.status_code, 404)

    def test_listing_requires_login(self):
        self.assertEqual(APIClient().get("/api/favorites/").status_code, 401)


class PlayHistoryApiTests(PlaylistFixtureMixin, TestCase):
    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.other)
        for _ in range(2):
            self.client.post(f"/api/tracks/{self.live_track.pk}/play/", {}, format="json")

    def test_listing_groups_by_track_with_total_plays(self):
        response = self.client.get("/api/history/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["play_count"], 2)

    def test_history_is_private(self):
        self.assertEqual(APIClient().get("/api/history/").status_code, 401)

    def test_summary_aggregates_totals(self):
        response = self.client.get("/api/history/stats/summary/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["total_plays"], 2)
        self.assertEqual(response.data["distinct_tracks"], 1)

    def test_remove_and_clear(self):
        self.assertEqual(self.client.delete(f"/api/history/tracks/{self.live_track.pk}/").status_code, 204)
        self.assertEqual(self.client.get("/api/history/").data["count"], 0)

        self.client.post(f"/api/tracks/{self.live_track.pk}/play/", {}, format="json")
        response = self.client.delete("/api/history/clear/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["deleted"], 1)


class PlayEventModelTests(PlaylistFixtureMixin, TestCase):
    def test_register_play_is_idempotent_for_drafts(self):
        from analytics.models import register_play

        self.assertIsNone(register_play(self.draft_track))

    def test_completed_flag_is_recorded(self):
        from analytics.models import register_play

        event = register_play(self.live_track, user=self.owner, completed=False)
        self.assertFalse(event.completed)
