"""Listener-facing pages: home, track, artist, album, genre, search, library.

These guard the two things the API tests cannot: that the HTML surface only
ever shows published tracks, and that private playlists stay unguessable.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from music.models import Album, Artist, Genre, Track
from playlists.models import Favorite, Playlist, PlaylistTrack

from .factories import CatalogMixin, MediaRootMixin, make_audio

PASSWORD = "Str0ngPass!23"

User = get_user_model()
PASSWORD = "Str0ngPass!23"


class SiteSmokeTests(CatalogMixin, MediaRootMixin, TestCase):
    """Every public page returns 200 against a populated catalogue."""

    def test_home_page_renders(self):
        response = self.client.get(reverse("music:home"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "music/home.html")

    def test_track_page_renders_and_links_to_artist(self):
        response = self.client.get(self.live_track.get_absolute_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Glass Cities")
        self.assertContains(response, self.artist.name)

    def test_artist_album_genre_pages_render(self):
        for obj in (self.artist, self.album, self.genre):
            with self.subTest(obj=obj):
                response = self.client.get(obj.get_absolute_url())
                self.assertEqual(response.status_code, 200)

    def test_playlist_index_and_detail_render(self):
        owner = User.objects.create_user(email="owner@example.com", password=PASSWORD)
        playlist = Playlist.objects.create(
            name="Late Shift", owner=owner, visibility=Playlist.Visibility.PUBLIC
        )
        PlaylistTrack.objects.create(playlist=playlist, track=self.live_track, position=1)

        self.assertEqual(self.client.get(reverse("playlists:index")).status_code, 200)
        response = self.client.get(playlist.get_absolute_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Glass Cities")

    def test_search_page_renders_results(self):
        response = self.client.get(reverse("music:search"), {"q": "Glass"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Glass Cities")

    def test_library_page_renders_for_anonymous(self):
        response = self.client.get(reverse("music:library"))
        self.assertEqual(response.status_code, 200)

    def test_library_page_renders_for_signed_in_listener(self):
        """The signed-in branch builds `following`, `history` and `playlists`.

        Anonymous requests short-circuit before those querysets are touched, so
        only a real login exercises them. Regression guard for the follow M2M
        being select_related'd against a non-relational ImageField.
        """
        from playlists.models import Favorite

        listener = User.objects.create_user(email="fan@example.com", password=PASSWORD)
        self.client.force_login(listener)

        Favorite.objects.get_or_create(user=listener, track=self.live_track)
        self.artist.followers.add(listener)

        response = self.client.get(reverse("music:library"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.live_track.title)
        self.assertContains(response, self.artist.name)


class PublishedVisibilityTests(CatalogMixin, MediaRootMixin, TestCase):
    """Drafts must not leak onto public pages."""

    def test_home_hides_drafts(self):
        response = self.client.get(reverse("music:home"))
        self.assertNotContains(response, "Unreleased Sketches")

    def test_draft_track_page_is_404_for_public(self):
        self.assertEqual(self.client.get(self.draft_track.get_absolute_url()).status_code, 404)

    def test_staff_can_preview_draft_track_page(self):
        staff = User.objects.create_user(
            email="boss@example.com", password=PASSWORD, is_staff=True, role=User.Role.MANAGER
        )
        self.client.force_login(staff)
        response = self.client.get(self.draft_track.get_absolute_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Unreleased Sketches")

    def test_playlist_detail_hides_unpublished_tracks(self):
        owner = User.objects.create_user(email="owner@example.com", password=PASSWORD)
        playlist = Playlist.objects.create(
            name="Mixed", owner=owner, visibility=Playlist.Visibility.PUBLIC
        )
        PlaylistTrack.objects.create(playlist=playlist, track=self.live_track, position=1)
        PlaylistTrack.objects.create(playlist=playlist, track=self.draft_track, position=2)

        response = self.client.get(playlist.get_absolute_url())
        self.assertNotContains(response, "Unreleased Sketches")
        self.assertContains(response, "1 unpublished track")

    def test_search_never_returns_drafts(self):
        response = self.client.get(reverse("music:search"), {"q": "Unreleased"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total"], 0)


class PlaylistPrivacyTests(CatalogMixin, MediaRootMixin, TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(email="owner@example.com", password=PASSWORD)
        self.stranger = User.objects.create_user(email="nosy@example.com", password=PASSWORD)
        self.staff = User.objects.create_user(
            email="boss@example.com", password=PASSWORD, is_staff=True, role=User.Role.MANAGER
        )
        self.private = Playlist.objects.create(
            name="Secret", owner=self.owner, visibility=Playlist.Visibility.PRIVATE
        )
        self.unlisted = Playlist.objects.create(
            name="Unlisted", owner=self.owner, visibility=Playlist.Visibility.UNLISTED
        )
        self.public = Playlist.objects.create(
            name="Open", owner=self.owner, visibility=Playlist.Visibility.PUBLIC
        )

    def test_anonymous_cannot_see_private_playlist(self):
        self.assertEqual(self.client.get(self.private.get_absolute_url()).status_code, 404)

    def test_stranger_cannot_see_private_playlist(self):
        self.client.force_login(self.stranger)
        self.assertEqual(self.client.get(self.private.get_absolute_url()).status_code, 404)

    def test_owner_sees_their_private_playlist(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(self.private.get_absolute_url()).status_code, 200)

    def test_collaborator_sees_private_playlist(self):
        self.private.collaborators.add(self.stranger)
        self.client.force_login(self.stranger)
        self.assertEqual(self.client.get(self.private.get_absolute_url()).status_code, 200)

    def test_unlisted_playlist_is_not_listed_in_the_index(self):
        index = self.client.get(reverse("playlists:index"))
        self.assertNotContains(index, "Unlisted")
        self.assertContains(index, "Open")

    def test_owner_reaches_unlisted_playlist_by_link(self):
        # `is_visible_to` treats UNLISTED exactly like PRIVATE: only the owner
        # and collaborators pass. See the note on Playlist.is_visible_to.
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(self.unlisted.get_absolute_url()).status_code, 200)

    def test_unlisted_playlist_is_404_for_anonymous(self):
        self.assertEqual(self.client.get(self.unlisted.get_absolute_url()).status_code, 404)

    def test_site_uses_is_visible_to_even_for_staff(self):
        # The API lets staff list every playlist; the listener site deliberately
        # does not, so the two surfaces share one privacy rule and staff use the
        # dashboard for moderation.
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(self.private.get_absolute_url()).status_code, 404)


class RelatedTracksTests(CatalogMixin, MediaRootMixin, TestCase):
    def test_related_excludes_the_current_track(self):
        response = self.client.get(self.live_track.get_absolute_url())
        related = response.context["related"]
        self.assertNotIn(self.live_track, related)

    def test_related_falls_back_to_same_artist(self):
        # Neither track is tagged with a genre here beyond live_track, so the
        # filler path must still return the artist's other track.
        response = self.client.get(self.live_track.get_absolute_url())
        titles = [t.title for t in response.context["related"]]
        self.assertIn("Chrome Rain", titles)

    def test_track_without_genres_still_renders(self):
        lonely = Track.objects.create(
            title="Untitled Sketch",
            artist=self.other_artist,
            status=Track.Status.PUBLISHED,
            audio_file=make_audio("untitled.mp3"),
        )
        response = self.client.get(lonely.get_absolute_url())
        self.assertEqual(response.status_code, 200)


class ManifestTests(CatalogMixin, MediaRootMixin, TestCase):
    """``/player/manifest/`` feeds the <audio> element its source URLs."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        User = get_user_model()
        cls.listener = User.objects.create_user(
            email="manifest-fan@example.com", password=PASSWORD
        )
        cls.other_user = User.objects.create_user(
            email="manifest-other@example.com", password=PASSWORD
        )

    def test_manifest_returns_ordered_tracks(self):
        ids = f"{self.second_track.pk},{self.live_track.pk}"
        response = self.client.get(reverse("music:track-manifest"), {"ids": ids})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual([row["id"] for row in payload["tracks"]],
                         [str(self.second_track.pk), str(self.live_track.pk)])

    def test_manifest_includes_absolute_audio_url(self):
        response = self.client.get(reverse("music:track-manifest"), {"ids": str(self.live_track.pk)})
        row = response.json()["tracks"][0]
        self.assertTrue(row["url"].startswith("http"))
        self.assertTrue(row["url"].endswith(".mp3"))
        self.assertEqual(row["duration_seconds"], 214)

    def test_manifest_survives_malformed_ids(self):
        ids = f"{self.live_track.pk},not-a-uuid,,123"
        response = self.client.get(reverse("music:track-manifest"), {"ids": ids})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["tracks"]), 1)

    def test_manifest_with_no_ids_is_empty(self):
        response = self.client.get(reverse("music:track-manifest"))
        self.assertEqual(response.json()["tracks"], [])

    def test_manifest_omits_drafts(self):
        response = self.client.get(reverse("music:track-manifest"), {"ids": str(self.draft_track.pk)})
        self.assertEqual(response.json()["tracks"], [])

    def test_manifest_reports_favourite_state_to_signed_in_user(self):
        """The player renders the heart from this field on first play."""
        Favorite.objects.create(user=self.listener, track=self.live_track)
        self.client.force_login(self.listener)
        response = self.client.get(reverse("music:track-manifest"), {"ids": str(self.live_track.pk)})
        rows = {row["id"]: row for row in response.json()["tracks"]}
        self.assertTrue(rows[str(self.live_track.pk)]["is_favorite"])

    def test_manifest_favourite_state_is_per_user(self):
        """One listener's favourite must not mark the track for another."""
        Favorite.objects.create(user=self.listener, track=self.live_track)
        self.client.force_login(self.other_user)
        response = self.client.get(reverse("music:track-manifest"), {"ids": str(self.live_track.pk)})
        rows = {row["id"]: row for row in response.json()["tracks"]}
        self.assertFalse(rows[str(self.live_track.pk)]["is_favorite"])

    def test_manifest_marks_unfavourited_track_for_signed_in_user(self):
        self.client.force_login(self.listener)
        response = self.client.get(reverse("music:track-manifest"), {"ids": str(self.live_track.pk)})
        rows = {row["id"]: row for row in response.json()["tracks"]}
        self.assertFalse(rows[str(self.live_track.pk)]["is_favorite"])

    def test_manifest_favourite_state_is_false_for_anonymous(self):
        response = self.client.get(reverse("music:track-manifest"), {"ids": str(self.live_track.pk)})
        rows = {row["id"]: row for row in response.json()["tracks"]}
        self.assertFalse(rows[str(self.live_track.pk)]["is_favorite"])

    def test_player_markup_exposes_auth_state(self):
        """player.js reads #player's data-authenticated to show the heart."""
        response = self.client.get(reverse("music:home"))
        self.assertContains(response, 'data-authenticated="0"')
        self.client.force_login(self.listener)
        response = self.client.get(reverse("music:home"))
        self.assertContains(response, 'data-authenticated="1"')


class CollectionManifestTests(CatalogMixin, MediaRootMixin, TestCase):
    """``/player/collection/`` backs the Play button on album/playlist cards."""

    url = reverse("music:player-collection")

    def test_album_returns_its_tracks_in_track_number_order(self):
        response = self.client.get(self.url, {"kind": "album", "slug": self.album.slug})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["kind"], "album")
        # live_track is track_number=1, second_track is 2.
        self.assertEqual(
            [row["id"] for row in payload["tracks"]],
            [str(self.live_track.pk), str(self.second_track.pk)],
        )

    def test_album_with_unnumbered_tracks_still_returns_them(self):
        # track_number is nullable; a NULL must not drop the track from the queue.
        self.live_track.track_number = None
        self.live_track.save(update_fields=["track_number"])
        response = self.client.get(self.url, {"kind": "album", "slug": self.album.slug})
        ids = [row["id"] for row in response.json()["tracks"]]
        self.assertIn(str(self.live_track.pk), ids)

    def test_album_omits_drafts(self):
        self.draft_track.album = self.album
        self.draft_track.save(update_fields=["album"])
        response = self.client.get(self.url, {"kind": "album", "slug": self.album.slug})
        ids = [row["id"] for row in response.json()["tracks"]]
        self.assertNotIn(str(self.draft_track.pk), ids)

    def test_public_playlist_returns_entries_in_position_order(self):
        owner = User.objects.create_user(email="curator@example.com", password=PASSWORD)
        playlist = Playlist.objects.create(
            name="Set", owner=owner, visibility=Playlist.Visibility.PUBLIC
        )
        PlaylistTrack.objects.create(playlist=playlist, track=self.second_track, position=2)
        PlaylistTrack.objects.create(playlist=playlist, track=self.live_track, position=1)

        response = self.client.get(self.url, {"kind": "playlist", "slug": playlist.slug})
        self.assertEqual(
            [row["id"] for row in response.json()["tracks"]],
            [str(self.live_track.pk), str(self.second_track.pk)],
        )

    def test_private_playlist_leaks_nothing_to_anonymous(self):
        owner = User.objects.create_user(email="owner@example.com", password=PASSWORD)
        playlist = Playlist.objects.create(
            name="Hidden", owner=owner, visibility=Playlist.Visibility.PRIVATE
        )
        PlaylistTrack.objects.create(playlist=playlist, track=self.live_track, position=1)

        response = self.client.get(self.url, {"kind": "playlist", "slug": playlist.slug})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["tracks"], [])

    def test_private_playlist_is_visible_to_its_owner(self):
        owner = User.objects.create_user(email="owner2@example.com", password=PASSWORD)
        playlist = Playlist.objects.create(
            name="Mine", owner=owner, visibility=Playlist.Visibility.PRIVATE
        )
        PlaylistTrack.objects.create(playlist=playlist, track=self.live_track, position=1)

        self.client.force_login(owner)
        response = self.client.get(self.url, {"kind": "playlist", "slug": playlist.slug})
        self.assertEqual(len(response.json()["tracks"]), 1)

    def test_unknown_kind_and_missing_slug_are_empty_not_errors(self):
        for params in ({"kind": "artist", "slug": "x"}, {"kind": "album"}, {}):
            with self.subTest(params=params):
                response = self.client.get(self.url, params)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["tracks"], [])


class QueueMarkupTests(CatalogMixin, MediaRootMixin, TestCase):
    """player.js builds its queue by scraping data-track-* off these rows."""

    def test_track_rows_expose_queue_metadata(self):
        response = self.client.get(reverse("music:home"))
        self.assertContains(response, f'data-track-id="{self.live_track.pk}"')
        self.assertContains(response, 'data-track-title="Glass Cities"')
        self.assertContains(response, f'data-track-duration="{self.live_track.duration_seconds}"')


class NavigationTests(CatalogMixin, MediaRootMixin, TestCase):
    def test_nav_marks_active_section(self):
        response = self.client.get(reverse("music:home"))
        self.assertEqual(response.context["nav_active"], "home")

    def test_get_absolute_url_round_trips_for_every_model(self):
        for obj in (self.live_track, self.artist, self.album, self.genre):
            with self.subTest(obj=obj):
                self.assertEqual(self.client.get(obj.get_absolute_url()).status_code, 200)
