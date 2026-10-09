"""Listener engagement APIs: track comments, recommendations, saved queue.

Covers the access rules that are easy to get wrong and expensive to get wrong:
who may moderate a comment, what a private queue must not leak, and that
recommendation queries de-duplicate after a many-to-many join.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase

from analytics.recommend import recommended_for, similar_tracks
from music.models import Track, TrackComment
from playlists.models import Favorite, PlayHistory, PlaybackQueue, PlaybackQueueItem

from .factories import CatalogMixin, MediaRootMixin, auth_header, client_for

User = get_user_model()
PASSWORD = "Str0ngPass!23"


class CommentModerationTests(CatalogMixin, MediaRootMixin, TestCase):
    def setUp(self):
        self.author = User.objects.create_user(email="author@example.com", password=PASSWORD)
        self.stranger = User.objects.create_user(email="stranger@example.com", password=PASSWORD)
        self.staff = User.objects.create_user(
            email="boss@example.com", password=PASSWORD,
            is_staff=True, role=User.Role.MANAGER,
        )
        self.comment = TrackComment.objects.create(
            track=self.live_track, user=self.author, body="Solid opener."
        )

    def list_url(self):
        return f"/api/tracks/{self.live_track.pk}/comments/"

    def test_listing_is_public(self):
        response = self.client.get(self.list_url())
        self.assertEqual(response.status_code, 200)
        rows = response.json()["results"] if isinstance(response.json(), dict) else response.json()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["body"], "Solid opener.")

    def test_author_display_name_falls_back_to_email_local_part(self):
        self.author.display_name = ""
        self.author.save(update_fields=["display_name"])
        rows = self.client.get(self.list_url()).json()
        rows = rows["results"] if isinstance(rows, dict) else rows
        self.assertEqual(rows[0]["author"]["display_name"], "author")

    def test_posting_requires_authentication(self):
        response = self.client.post(self.list_url(), {"body": "hi"}, content_type="application/json")
        self.assertEqual(response.status_code, 401)

    def test_author_can_post(self):
        client = client_for(self.author)
        response = client.post(self.list_url(), {"body": "Great track."}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["author"]["id"], str(self.author.pk))

    def test_blank_body_is_rejected(self):
        client = client_for(self.author)
        response = client.post(self.list_url(), {"body": "   "}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("body", response.data["error"]["details"])

    def test_author_cannot_moderate_own_comment(self):
        client = client_for(self.author)
        response = client.patch(
            f"/api/comments/{self.comment.pk}/", {"status": "hidden"}, format="json"
        )
        self.assertEqual(response.status_code, 403)

    def test_author_can_edit_own_body(self):
        client = client_for(self.author)
        response = client.patch(
            f"/api/comments/{self.comment.pk}/", {"body": "Better opener."}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        self.comment.refresh_from_db()
        self.assertEqual(self.comment.body, "Better opener.")

    def test_stranger_cannot_edit_or_delete(self):
        client = client_for(self.stranger)
        edit = client.patch(
            f"/api/comments/{self.comment.pk}/", {"body": "hacked"}, format="json"
        )
        self.assertEqual(edit.status_code, 403)
        delete = client.delete(f"/api/comments/{self.comment.pk}/")
        self.assertEqual(delete.status_code, 403)
        self.comment.refresh_from_db()
        self.assertEqual(self.comment.body, "Solid opener.")

    def test_staff_can_hide_and_pin(self):
        client = client_for(self.staff)
        response = client.patch(
            f"/api/comments/{self.comment.pk}/",
            {"status": "hidden", "is_pinned": True},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.comment.refresh_from_db()
        self.assertEqual(self.comment.status, TrackComment.Status.HIDDEN)
        self.assertTrue(self.comment.is_pinned)

    def test_hidden_comment_disappears_from_public_listing(self):
        self.comment.status = TrackComment.Status.HIDDEN
        self.comment.save(update_fields=["status"])
        rows = self.client.get(self.list_url()).json()
        rows = rows["results"] if isinstance(rows, dict) else rows
        self.assertEqual(rows, [])

    def test_staff_still_sees_hidden_comments(self):
        self.comment.status = TrackComment.Status.HIDDEN
        self.comment.save(update_fields=["status"])
        client = client_for(self.staff)
        rows = client.get(self.list_url()).json()
        rows = rows["results"] if isinstance(rows, dict) else rows
        self.assertEqual(len(rows), 1)

    def test_author_can_delete_own_comment(self):
        client = client_for(self.author)
        self.assertEqual(client.delete(f"/api/comments/{self.comment.pk}/").status_code, 204)
        self.assertFalse(TrackComment.objects.filter(pk=self.comment.pk).exists())

    def test_reply_must_share_the_parent_track(self):
        other = TrackComment.objects.create(
            track=self.second_track, user=self.author, body="elsewhere"
        )
        client = client_for(self.author)
        response = client.post(
            self.list_url(), {"body": "reply", "parent": other.pk}, format="json"
        )
        self.assertEqual(response.status_code, 400)

    def test_replies_are_nested_under_the_parent(self):
        client = client_for(self.author)
        client.post(
            self.list_url(), {"body": "Agreed.", "parent": self.comment.pk}, format="json"
        )
        rows = client.get(self.list_url()).json()
        rows = rows["results"] if isinstance(rows, dict) else rows
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]["replies"]), 1)
        self.assertEqual(rows[0]["replies"][0]["body"], "Agreed.")

    def test_commenting_on_a_draft_track_is_404_for_public(self):
        response = self.client.get(f"/api/tracks/{self.draft_track.pk}/comments/")
        self.assertEqual(response.status_code, 404)


class RecommendationTests(CatalogMixin, MediaRootMixin, TestCase):
    def setUp(self):
        self.listener = User.objects.create_user(email="fan@example.com", password=PASSWORD)

    def test_similar_excludes_the_seed_track(self):
        results = similar_tracks(self.live_track)
        self.assertNotIn(self.live_track.pk, [t.pk for t in results])

    def test_similar_returns_no_duplicates(self):
        """The genres join fans out; without distinct() the same track repeats."""
        self.second_track.genres.add(self.genre, self.other_genre)
        results = similar_tracks(self.live_track)
        ids = [t.pk for t in results]
        self.assertEqual(len(ids), len(set(ids)))

    def test_similar_only_returns_published_tracks(self):
        results = similar_tracks(self.live_track)
        self.assertNotIn(self.draft_track.pk, [t.pk for t in results])

    def test_similar_prefers_shared_genres(self):
        neighbour = Track.objects.create(
            title="Same Genre", artist=self.other_artist, status=Track.Status.PUBLISHED,
            audio_file=__import__("tests.factories", fromlist=["make_audio"]).make_audio("a.mp3"),
        )
        neighbour.genres.add(self.genre)
        distant = Track.objects.create(
            title="Other Genre", artist=self.other_artist, status=Track.Status.PUBLISHED,
            audio_file=__import__("tests.factories", fromlist=["make_audio"]).make_audio("b.mp3"),
        )
        distant.genres.add(self.other_genre)
        results = similar_tracks(self.live_track)
        ids = [t.pk for t in results]
        self.assertIn(neighbour.pk, ids)
        if distant.pk in ids:
            self.assertLess(ids.index(neighbour.pk), ids.index(distant.pk))

    def test_similar_endpoint(self):
        response = self.client.get(f"/api/tracks/{self.live_track.pk}/similar/")
        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(response.json(), list)

    def test_anonymous_recommendations_fall_back_to_trending(self):
        response = self.client.get("/api/recommendations/")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["personalised"])
        self.assertTrue(payload["tracks"])

    def test_feed_uses_favourites(self):
        Favorite.objects.create(user=self.listener, track=self.live_track)
        results = recommended_for(self.listener)
        self.assertNotIn(self.live_track.pk, [t.pk for t in results])

    def test_feed_falls_back_when_no_signal(self):
        results = recommended_for(self.listener)
        self.assertTrue(results)

    def test_personalised_flag_matches_the_feed_source(self):
        """Favourites-only users are personalised; play events alone also count."""
        headers, _ = auth_header(self.listener)
        before = self.client.get("/api/recommendations/").json()["personalised"]
        self.assertFalse(before)

        Favorite.objects.create(user=self.listener, track=self.live_track)
        response = self.client.get("/api/recommendations/", **headers)
        self.assertTrue(response.json()["personalised"])

    def test_recommendations_are_unique(self):
        Favorite.objects.create(user=self.listener, track=self.live_track)
        headers, _ = auth_header(self.listener)
        response = self.client.get("/api/recommendations/", **headers)
        ids = [row["id"] for row in response.json()["tracks"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_history_counts_as_signal(self):
        PlayHistory.objects.create(user=self.listener, track=self.live_track)
        results = recommended_for(self.listener)
        self.assertNotIn(self.live_track.pk, [t.pk for t in results])


class PlaybackQueueTests(CatalogMixin, MediaRootMixin, TestCase):
    def setUp(self):
        self.listener = User.objects.create_user(email="fan@example.com", password=PASSWORD)
        self.other = User.objects.create_user(email="other@example.com", password=PASSWORD)
        self.ids = [self.live_track.pk, self.second_track.pk]

    def test_queue_requires_authentication(self):
        for method in ("get", "put", "delete"):
            response = getattr(self.client, method)("/api/queue/")
            self.assertIn(response.status_code, (401, 405), method)

    def test_get_returns_empty_shape_when_nothing_saved(self):
        client = client_for(self.listener)
        response = client.get("/api/queue/")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["items"], [])
        self.assertEqual(payload["current_position"], 0)
        self.assertFalse(payload["shuffle"])
        self.assertEqual(payload["repeat"], "off")

    def test_put_then_get_round_trips(self):
        client = client_for(self.listener)
        response = client.put(
            "/api/queue/",
            {"track_ids": self.ids, "current_position": 1, "shuffle": True, "repeat": "all"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.json()["items"]), 2)

        restored = client.get("/api/queue/").json()
        self.assertEqual([i["track"]["id"] for i in restored["items"]], self.ids)
        self.assertEqual(restored["current_position"], 1)
        self.assertTrue(restored["shuffle"])
        self.assertEqual(restored["repeat"], "all")

    def test_put_is_a_replace_not_an_append(self):
        client = client_for(self.listener)
        client.put("/api/queue/", {"track_ids": self.ids}, format="json")
        client.put("/api/queue/", {"track_ids": [self.live_track.pk]}, format="json")
        self.assertEqual(len(client.get("/api/queue/").json()["items"]), 1)

    def test_drafts_are_dropped_and_reported(self):
        client = client_for(self.listener)
        response = client.put(
            "/api/queue/",
            {"track_ids": [self.live_track.pk, self.draft_track.pk]},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["dropped_track_ids"], [self.draft_track.pk])
        self.assertEqual(len(response.json()["items"]), 1)

    def test_duplicate_ids_are_rejected(self):
        client = client_for(self.listener)
        response = client.put(
            "/api/queue/", {"track_ids": [self.live_track.pk, self.live_track.pk]}, format="json"
        )
        self.assertEqual(response.status_code, 400)

    def test_position_past_the_end_is_rejected(self):
        client = client_for(self.listener)
        response = client.put(
            "/api/queue/", {"track_ids": [self.live_track.pk], "current_position": 5}, format="json"
        )
        self.assertEqual(response.status_code, 400)

    def test_empty_queue_is_allowed(self):
        client = client_for(self.listener)
        client.put("/api/queue/", {"track_ids": self.ids}, format="json")
        response = client.put("/api/queue/", {"track_ids": []}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(client.get("/api/queue/").json()["items"], [])

    def test_queues_are_private_to_their_owner(self):
        client_for(self.listener).put("/api/queue/", {"track_ids": self.ids}, format="json")
        intruder = client_for(self.other)
        self.assertEqual(intruder.get("/api/queue/").json()["items"], [])

    def test_delete_clears_the_queue(self):
        client = client_for(self.listener)
        client.put("/api/queue/", {"track_ids": self.ids}, format="json")
        self.assertEqual(client.delete("/api/queue/").status_code, 204)
        self.assertFalse(PlaybackQueue.objects.filter(user=self.listener).exists())
        self.assertEqual(client.get("/api/queue/").json()["items"], [])

    def test_replacing_a_queue_does_not_leak_items(self):
        client = client_for(self.listener)
        client.put("/api/queue/", {"track_ids": self.ids}, format="json")
        client.put("/api/queue/", {"track_ids": [self.second_track.pk]}, format="json")
        self.assertEqual(PlaybackQueueItem.objects.filter(queue__user=self.listener).count(), 1)
