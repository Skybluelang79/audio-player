"""Staff dashboard tests."""

import csv
from io import StringIO

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from music.models import Artist, Track, TrackComment

from tests.factories import CatalogMixin, MediaRootMixin

from . import services

PASSWORD = "Str0ngPass!23"


class StaffDashboardMixin(CatalogMixin, MediaRootMixin):
    """Adds a staff account and a helper for building comments."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        User = get_user_model()
        cls.staff = User.objects.create_user(
            email="mod-staff@example.com", password=PASSWORD, is_staff=True
        )
        cls.listener = User.objects.create_user(
            email="mod-fan@example.com", password=PASSWORD
        )

    def comment(self, body="Nice track.", **kwargs):
        kwargs.setdefault("track", self.live_track)
        kwargs.setdefault("user", self.listener)
        return TrackComment.objects.create(body=body, **kwargs)


class ModerationAccessTests(StaffDashboardMixin, TestCase):
    def test_anonymous_is_redirected_to_login(self):
        response = self.client.get(reverse("dashboard:moderation"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])

    def test_non_staff_is_forbidden(self):
        self.client.force_login(self.listener)
        response = self.client.get(reverse("dashboard:moderation"))
        self.assertEqual(response.status_code, 403)

    def test_staff_sees_the_queue(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("dashboard:moderation"))
        self.assertEqual(response.status_code, 200)


class ModerationQueueTests(StaffDashboardMixin, TestCase):
    def setUp(self):
        self.client.force_login(self.staff)

    def test_defaults_to_the_actionable_queue(self):
        """Published comments stay out of the way until something needs a call."""
        self.comment(body="Live one", status=TrackComment.Status.VISIBLE)
        self.comment(body="Awaiting review", status=TrackComment.Status.PENDING)
        self.comment(body="Reported", status=TrackComment.Status.HIDDEN, is_flagged=True)

        response = self.client.get(reverse("dashboard:moderation"))
        bodies = [c.body for c in response.context["comments"]]
        self.assertEqual(bodies, ["Reported", "Awaiting review"])
        self.assertNotIn("Live one", bodies)

    def test_flagged_filter_matches_flagged_regardless_of_status(self):
        visible_flag = self.comment(
            body="Visible but flagged", status=TrackComment.Status.VISIBLE, is_flagged=True
        )
        self.comment(body="Ordinary", status=TrackComment.Status.VISIBLE)

        response = self.client.get(reverse("dashboard:moderation"), {"filter": "flagged"})
        self.assertEqual(
            [c.pk for c in response.context["comments"]], [visible_flag.pk]
        )

    def test_all_filter_includes_every_status(self):
        self.comment(body="a", status=TrackComment.Status.VISIBLE)
        self.comment(body="b", status=TrackComment.Status.PENDING)
        self.comment(body="c", status=TrackComment.Status.HIDDEN)

        response = self.client.get(reverse("dashboard:moderation"), {"filter": "all"})
        self.assertEqual(len(response.context["comments"]), 3)

    def test_unknown_filter_falls_back_to_the_queue(self):
        self.comment(body="Pending", status=TrackComment.Status.PENDING)
        self.comment(body="Live", status=TrackComment.Status.VISIBLE)

        response = self.client.get(reverse("dashboard:moderation"), {"filter": "nonsense"})
        self.assertEqual(response.context["scope"], "queue")
        self.assertEqual(len(response.context["comments"]), 1)

    def test_search_narrows_by_body(self):
        target = self.comment(body="Distinctive phrase", status=TrackComment.Status.PENDING)
        self.comment(body="Unrelated", status=TrackComment.Status.PENDING)

        response = self.client.get(reverse("dashboard:moderation"), {"q": "Distinctive"})
        self.assertEqual([c.pk for c in response.context["comments"]], [target.pk])

    def test_search_by_author_returns_all_of_their_comments(self):
        """Author and track are OR-ed with the body, so both of this user's
        comments match even though neither body contains the term."""
        mine = [
            self.comment(body="alpha", status=TrackComment.Status.PENDING),
            self.comment(body="beta", status=TrackComment.Status.PENDING),
        ]
        other = get_user_model().objects.create_user(
            email="someone-else@example.com", password=PASSWORD
        )
        self.comment(body="gamma", status=TrackComment.Status.PENDING, user=other)

        response = self.client.get(reverse("dashboard:moderation"), {"q": "mod-fan"})
        self.assertEqual(
            sorted(c.pk for c in response.context["comments"]),
            sorted(c.pk for c in mine),
        )

    def test_search_by_track_title_spans_authors(self):
        on_track = self.comment(body="one", status=TrackComment.Status.PENDING)
        self.comment(
            body="two",
            status=TrackComment.Status.PENDING,
            track=self.second_track,
        )

        response = self.client.get(
            reverse("dashboard:moderation"), {"q": self.live_track.title}
        )
        self.assertEqual([c.pk for c in response.context["comments"]], [on_track.pk])

    def test_search_is_combined_with_the_active_filter(self):
        """The default scope is the actionable queue, so a live comment stays
        hidden even when it matches the search term."""
        pending = self.comment(body="Match me", status=TrackComment.Status.PENDING)
        live = self.comment(body="Match me too", status=TrackComment.Status.VISIBLE)

        response = self.client.get(reverse("dashboard:moderation"), {"q": "Match me"})
        self.assertEqual([c.pk for c in response.context["comments"]], [pending.pk])

    def test_search_with_the_all_filter_returns_both(self):
        pending = self.comment(body="Match me", status=TrackComment.Status.PENDING)
        live = self.comment(body="Match me too", status=TrackComment.Status.VISIBLE)

        response = self.client.get(
            reverse("dashboard:moderation"), {"filter": "all", "q": "Match me"}
        )
        self.assertEqual(
            sorted(c.pk for c in response.context["comments"]),
            sorted([pending.pk, live.pk]),
        )

    def test_blank_search_is_ignored(self):
        self.comment(body="Pending", status=TrackComment.Status.PENDING)
        response = self.client.get(reverse("dashboard:moderation"), {"q": "   "})
        self.assertEqual(len(response.context["comments"]), 1)

    def test_ordering_oldest_puts_the_backlog_first(self):
        first = self.comment(body="oldest", status=TrackComment.Status.PENDING)
        second = self.comment(body="newest", status=TrackComment.Status.PENDING)

        response = self.client.get(reverse("dashboard:moderation"), {"order": "oldest"})
        self.assertEqual([c.pk for c in response.context["comments"]], [first.pk, second.pk])
        self.assertNotEqual(first.pk, second.pk)

    def test_counts_add_up(self):
        """Tab totals must not double count a comment that is both pending and flagged."""
        self.comment(body="visible", status=TrackComment.Status.VISIBLE)
        self.comment(body="pending", status=TrackComment.Status.PENDING)
        hidden_flagged = self.comment(
            body="hidden+flagged", status=TrackComment.Status.HIDDEN, is_flagged=True
        )

        response = self.client.get(reverse("dashboard:moderation"))
        counts = response.context["counts"]
        self.assertEqual(counts["total"], 3)
        self.assertEqual(counts["pending"], 1)
        self.assertEqual(counts["hidden"], 1)
        self.assertEqual(counts["flagged"], 1)
        self.assertEqual(counts["visible"], 1)
        self.assertEqual(counts["queue"], 2)

    def test_queue_count_includes_hidden_but_flagged(self):
        self.comment(body="hidden", status=TrackComment.Status.HIDDEN, is_flagged=True)
        response = self.client.get(reverse("dashboard:moderation"))
        self.assertEqual(response.context["counts"]["queue"], 1)

    def test_pagination_slices_the_queue(self):
        for index in range(30):
            self.comment(body=f"pending {index}", status=TrackComment.Status.PENDING)

        first = self.client.get(reverse("dashboard:moderation"))
        self.assertEqual(first.context["paginator"].num_pages, 2)
        self.assertEqual(len(first.context["comments"]), 25)

        second = self.client.get(reverse("dashboard:moderation"), {"page": 2})
        self.assertEqual(len(second.context["comments"]), 5)

    def test_pagination_preserves_the_filter(self):
        for index in range(30):
            self.comment(body=f"pending {index}", status=TrackComment.Status.PENDING)

        response = self.client.get(reverse("dashboard:moderation"), {"page": 2})
        self.assertEqual(response.context["scope"], "queue")


class ModerationActionTests(StaffDashboardMixin, TestCase):
    url = reverse("dashboard:moderation-action")

    def setUp(self):
        self.client.force_login(self.staff)

    def test_approve_publishes_and_clears_the_flag(self):
        comment = self.comment(status=TrackComment.Status.PENDING, is_flagged=True)
        self.client.post(self.url, {"action": "approve", "comment_ids": [comment.pk]})
        comment.refresh_from_db()
        self.assertEqual(comment.status, TrackComment.Status.VISIBLE)
        self.assertFalse(comment.is_flagged)

    def test_hide_removes_it_from_public_reads(self):
        comment = self.comment(status=TrackComment.Status.VISIBLE)
        self.client.post(self.url, {"action": "hide", "comment_ids": [comment.pk]})
        comment.refresh_from_db()
        self.assertEqual(comment.status, TrackComment.Status.HIDDEN)
        self.assertFalse(comment.is_visible)

    def test_send_to_review_reopens_a_hidden_comment(self):
        comment = self.comment(status=TrackComment.Status.HIDDEN)
        self.client.post(self.url, {"action": "pending", "comment_ids": [comment.pk]})
        comment.refresh_from_db()
        self.assertEqual(comment.status, TrackComment.Status.PENDING)

    def test_pin_and_unpin(self):
        comment = self.comment(status=TrackComment.Status.VISIBLE)
        self.client.post(self.url, {"action": "pin", "comment_ids": [comment.pk]})
        comment.refresh_from_db()
        self.assertTrue(comment.is_pinned)

        self.client.post(self.url, {"action": "unpin", "comment_ids": [comment.pk]})
        comment.refresh_from_db()
        self.assertFalse(comment.is_pinned)

    def test_flag_and_unflag(self):
        comment = self.comment(status=TrackComment.Status.VISIBLE)
        self.client.post(self.url, {"action": "flag", "comment_ids": [comment.pk]})
        comment.refresh_from_db()
        self.assertTrue(comment.is_flagged)

        self.client.post(self.url, {"action": "unflag", "comment_ids": [comment.pk]})
        comment.refresh_from_db()
        self.assertFalse(comment.is_flagged)

    def test_bulk_applies_to_every_selected_id(self):
        comments = [
            self.comment(body=f"c{index}", status=TrackComment.Status.PENDING)
            for index in range(3)
        ]
        self.client.post(
            self.url,
            {"action": "approve", "comment_ids": [c.pk for c in comments]},
        )
        self.assertEqual(
            TrackComment.objects.filter(status=TrackComment.Status.VISIBLE).count(), 3
        )

    def test_unselected_comments_are_untouched(self):
        chosen = self.comment(status=TrackComment.Status.PENDING)
        untouched = self.comment(status=TrackComment.Status.PENDING)
        self.client.post(self.url, {"action": "approve", "comment_ids": [chosen.pk]})
        untouched.refresh_from_db()
        self.assertEqual(untouched.status, TrackComment.Status.PENDING)

    def test_non_digit_ids_are_ignored_rather_than_erroring(self):
        comment = self.comment(status=TrackComment.Status.PENDING)
        response = self.client.post(
            self.url,
            {"action": "approve", "comment_ids": [str(comment.pk), "not-a-pk", ""]},
        )
        self.assertEqual(response.status_code, 302)
        comment.refresh_from_db()
        self.assertEqual(comment.status, TrackComment.Status.VISIBLE)

    def test_unknown_action_changes_nothing(self):
        comment = self.comment(status=TrackComment.Status.PENDING)
        response = self.client.post(
            self.url, {"action": "rm -rf", "comment_ids": [comment.pk]}
        )
        self.assertEqual(response.status_code, 302)
        comment.refresh_from_db()
        self.assertEqual(comment.status, TrackComment.Status.PENDING)

    def test_empty_selection_is_reported_not_silently_accepted(self):
        response = self.client.post(self.url, {"action": "approve"}, follow=True)
        self.assertEqual(response.status_code, 200)
        messages = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("Select at least one" in m for m in messages))

    def test_returns_to_the_queue_the_user_came_from(self):
        response = self.client.post(
            self.url,
            {
                "action": "approve",
                "comment_ids": [],
                "next": "/dashboard/moderation/?filter=flagged",
            },
        )
        self.assertEqual(response["Location"], "/dashboard/moderation/?filter=flagged")

    def test_offsite_return_url_is_refused(self):
        """An open redirect here would hand staff a phishing page after a POST."""
        response = self.client.post(
            self.url,
            {"action": "approve", "comment_ids": [], "next": "https://evil.example.com/"},
        )
        self.assertEqual(response["Location"], reverse("dashboard:moderation"))

    def test_non_staff_cannot_moderate(self):
        comment = self.comment(status=TrackComment.Status.PENDING)
        self.client.force_login(self.listener)
        self.client.post(self.url, {"action": "hide", "comment_ids": [comment.pk]})
        comment.refresh_from_db()
        self.assertEqual(comment.status, TrackComment.Status.PENDING)

    def test_anonymous_cannot_moderate(self):
        self.client.logout()
        response = self.client.post(self.url, {"action": "hide", "comment_ids": [1]})
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])


class ModerationReflectsInTheApiTests(StaffDashboardMixin, TestCase):
    """A dashboard decision and the public API must agree."""

    def setUp(self):
        self.client.force_login(self.staff)

    def public_comments(self, track=None):
        response = self.client.get(
            reverse("api:track-comments", args=[(track or self.live_track).pk]),
            headers={"x-requested-with": "XMLHttpRequest"},
        )
        self.assertEqual(response.status_code, 200)
        return response.json()["results"]

    def test_hidden_comment_disappears_from_the_public_api(self):
        comment = self.comment(status=TrackComment.Status.VISIBLE)
        self.client.post(
            reverse("dashboard:moderation-action"),
            {"action": "hide", "comment_ids": [comment.pk]},
        )
        self.client.logout()

        self.assertEqual(self.public_comments(), [])

    def test_approved_comment_becomes_public(self):
        comment = self.comment(status=TrackComment.Status.PENDING)
        self.client.post(
            reverse("dashboard:moderation-action"),
            {"action": "approve", "comment_ids": [comment.pk]},
        )
        self.client.logout()

        self.assertEqual(
            [row["body"] for row in self.public_comments()], [comment.body]
        )

    def test_pending_comment_is_never_public(self):
        self.comment(status=TrackComment.Status.PENDING)
        self.client.logout()
        self.assertEqual(self.public_comments(), [])


class BulkServiceTests(StaffDashboardMixin, TestCase):
    """The service layer, exercised without HTTP."""

    def setUp(self):
        self.ids = [self.live_track.pk, self.second_track.pk]

    def test_set_status_updates_every_track(self):
        result = services.set_status(self.ids, Track.Status.PUBLISHED)
        self.assertEqual(result.updated, 2)
        self.assertEqual(
            Track.objects.filter(status=Track.Status.PUBLISHED).count(), 2
        )

    def test_set_status_rejects_an_unknown_value(self):
        with self.assertRaises(services.BulkActionError):
            services.set_status(self.ids, "banana")

    def test_publishing_skips_tracks_with_no_audio(self):
        orphan = Track.objects.create(
            title="No audio",
            artist=self.artist,
            status=Track.Status.DRAFT,
            audio_file="audio/does-not-exist.mp3",
        )
        result = services.set_status([orphan.pk], Track.Status.PUBLISHED)
        orphan.refresh_from_db()
        self.assertEqual(orphan.status, Track.Status.DRAFT)
        self.assertEqual(result.skipped, 1)
        self.assertTrue(result.notes)

    def test_set_album_reassigns_and_clears_track_numbers(self):
        result = services.set_album(self.ids, self.album.pk)
        self.assertEqual(result.updated, 2)
        for track in (self.live_track, self.second_track):
            track.refresh_from_db()
            self.assertEqual(track.album_id, self.album.pk)
            self.assertIsNone(track.track_number)

    def test_set_album_none_detaches(self):
        services.set_album(self.ids, None)
        self.assertFalse(
            Track.objects.filter(pk__in=self.ids, album__isnull=False).exists()
        )

    def test_set_album_rejects_a_missing_album(self):
        with self.assertRaises(services.BulkActionError):
            services.set_album(self.ids, 999999)

    def test_set_artist_clears_an_orphaned_album(self):
        """A track whose album belongs to someone else must not keep it."""
        other_artist = Artist.objects.create(name="Stranger")
        result = services.set_artist([self.live_track.pk], other_artist.pk)
        self.live_track.refresh_from_db()
        self.assertEqual(self.live_track.artist_id, other_artist.pk)
        self.assertIsNone(self.live_track.album_id)
        self.assertTrue(any("album" in note for note in result.notes))

    def test_set_artist_keeps_a_matching_album(self):
        result = services.set_artist([self.live_track.pk], self.artist.pk)
        self.live_track.refresh_from_db()
        self.assertEqual(self.live_track.album_id, self.album.pk)

    def test_set_genres_add_mode_keeps_existing(self):
        other = self.other_genre
        self.live_track.genres.add(self.genre)
        services.set_genres([self.live_track.pk], [other.pk], "add")
        self.live_track.refresh_from_db()
        self.assertEqual(
            set(self.live_track.genres.values_list("name", flat=True)),
            {self.genre.name, other.name},
        )

    def test_set_genres_replace_mode_overwrites(self):
        other = self.other_genre
        self.live_track.genres.add(self.genre)
        services.set_genres([self.live_track.pk], [other.pk], "replace")
        self.live_track.refresh_from_db()
        self.assertEqual(
            list(self.live_track.genres.values_list("name", flat=True)), [other.name]
        )

    def test_set_genres_rejects_an_unknown_mode(self):
        with self.assertRaises(services.BulkActionError):
            services.set_genres(self.ids, [], "toggle")

    def test_set_flag_only_touches_explicit(self):
        services.set_flag(self.ids, "explicit", True)
        self.assertEqual(Track.objects.filter(explicit=True).count(), 2)
        with self.assertRaises(services.BulkActionError):
            services.set_flag(self.ids, "play_count", True)

    def test_reset_counters_requires_named_counters(self):
        with self.assertRaises(services.BulkActionError):
            services.reset_counters(self.ids, [])
        with self.assertRaises(services.BulkActionError):
            services.reset_counters(self.ids, ["bogus"])

    def test_reset_counters_zeroes_the_named_columns(self):
        self.live_track.play_count = 50
        self.live_track.save(update_fields=["play_count"])
        services.reset_counters([self.live_track.pk], ["play_count"])
        self.live_track.refresh_from_db()
        self.assertEqual(self.live_track.play_count, 0)


class CsvExportTests(StaffDashboardMixin, TestCase):
    """``services.tracks_csv`` output shape."""

    def parse(self, text):
        # Strip the UTF-8 BOM Excel needs before DictReader sees the header.
        return list(csv.DictReader(StringIO(text.lstrip("﻿"))))

    def test_header_and_row_count(self):
        body = services.tracks_csv(Track.objects.all())
        rows = self.parse(body)
        self.assertEqual(
            list(rows[0].keys()), list(services.TRACK_CSV_COLUMNS)
        )
        self.assertEqual(len(rows), Track.objects.count())

    def test_starts_with_a_bom(self):
        body = services.tracks_csv(Track.objects.all())
        self.assertTrue(body.startswith("﻿"))

    def test_uses_crlf_line_endings(self):
        body = services.tracks_csv(Track.objects.all())
        self.assertIn("\r\n", body)

    def test_row_carries_related_names_not_ids(self):
        rows = self.parse(services.tracks_csv(Track.objects.filter(pk=self.live_track.pk)))
        row = rows[0]
        self.assertEqual(row["artist"], self.artist.name)
        self.assertEqual(row["album"], self.album.title)
        self.assertEqual(row["genres"], self.genre.name)

    def test_missing_relations_render_blank(self):
        rows = self.parse(services.tracks_csv(Track.objects.filter(pk=self.draft_track.pk)))
        self.assertEqual(rows[0]["album"], "")
        self.assertEqual(rows[0]["genres"], "")

    def test_explicit_and_track_number_render_cleanly(self):
        self.live_track.explicit = True
        self.live_track.track_number = None
        self.live_track.save(update_fields=["explicit", "track_number"])
        rows = self.parse(services.tracks_csv(Track.objects.filter(pk=self.live_track.pk)))
        self.assertEqual(rows[0]["explicit"], "yes")
        self.assertEqual(rows[0]["track_number"], "")

    def test_unpublished_tracks_are_included_for_staff(self):
        rows = self.parse(services.tracks_csv(Track.objects.all()))
        statuses = {row["status"] for row in rows}
        self.assertIn(Track.Status.DRAFT, statuses)

    def test_filter_tracks_by_status(self):
        queryset = services.filter_tracks(
            Track.objects.all(), status=Track.Status.DRAFT
        )
        self.assertEqual([t.pk for t in queryset], [self.draft_track.pk])

    def test_filter_tracks_by_artist_and_genre(self):
        self.assertEqual(
            sorted(t.pk for t in services.filter_tracks(Track.objects.all(), artist_id=str(self.artist.pk))),
            sorted([self.live_track.pk, self.second_track.pk]),
        )
        self.assertEqual(
            [t.pk for t in services.filter_tracks(Track.objects.all(), genre_id=str(self.genre.pk))],
            [self.live_track.pk],
        )

    def test_filter_tracks_search_spans_title_artist_and_isrc(self):
        self.live_track.isrc = "GBAAA1234567"
        self.live_track.save(update_fields=["isrc"])
        for term in ("Glass", "GBAAA"):
            with self.subTest(term=term):
                found = services.filter_tracks(Track.objects.all(), search=term)
                self.assertEqual([t.pk for t in found], [self.live_track.pk])

    def test_filter_tracks_search_by_artist_spans_that_artists_tracks(self):
        found = services.filter_tracks(Track.objects.all(), search=self.artist.name)
        self.assertEqual(
            sorted(t.pk for t in found),
            sorted([self.live_track.pk, self.second_track.pk]),
        )

    def test_filter_tracks_by_missing_audio(self):
        """A row whose file vanished from disk counts as missing even though
        the database still has a filename for it."""
        orphan = Track.objects.create(
            title="No audio",
            artist=self.artist,
            audio_file="audio/gone.mp3",
        )
        no_audio = services.filter_tracks(Track.objects.all(), has_audio="no")
        self.assertEqual([t.pk for t in no_audio], [orphan.pk])

        with_audio = services.filter_tracks(Track.objects.all(), has_audio="yes")
        self.assertNotIn(orphan.pk, [t.pk for t in with_audio])
        self.assertIn(self.live_track.pk, [t.pk for t in with_audio])

    def test_missing_audio_ids_ignores_the_real_catalogue(self):
        """Probes storage, so every existing file must come back present."""
        self.assertEqual(services.missing_audio_ids(), [])


class BulkActionViewTests(StaffDashboardMixin, TestCase):
    url = reverse("dashboard:bulk-action")

    def setUp(self):
        self.client.force_login(self.staff)

    def test_publish_selected(self):
        self.client.post(self.url, {"action": "publish", "track_ids": [self.draft_track.pk]})
        self.draft_track.refresh_from_db()
        self.assertEqual(self.draft_track.status, Track.Status.PUBLISHED)

    def test_action_with_no_selection_uses_the_filters(self):
        """The point of filter-driven bulk actions: act on everything matched."""
        self.client.post(self.url, {"action": "publish", "status": Track.Status.DRAFT})
        self.draft_track.refresh_from_db()
        self.live_track.refresh_from_db()
        self.assertEqual(self.draft_track.status, Track.Status.PUBLISHED)
        self.assertEqual(self.live_track.status, Track.Status.PUBLISHED)

    def test_selection_beats_the_filter(self):
        self.client.post(
            self.url,
            {"action": "archive", "track_ids": [self.draft_track.pk], "status": Track.Status.PUBLISHED},
        )
        self.draft_track.refresh_from_db()
        self.live_track.refresh_from_db()
        self.assertEqual(self.draft_track.status, Track.Status.ARCHIVED)
        self.assertEqual(self.live_track.status, Track.Status.PUBLISHED)

    def test_unknown_action_changes_nothing(self):
        response = self.client.post(self.url, {"action": "drop_tables", "track_ids": [self.live_track.pk]})
        self.assertEqual(response.status_code, 302)
        self.live_track.refresh_from_db()
        self.assertEqual(self.live_track.status, Track.Status.PUBLISHED)

    def test_set_artist_requires_an_artist(self):
        self.client.post(self.url, {"action": "set_artist", "track_ids": [self.live_track.pk], "artist_id": ""})
        self.live_track.refresh_from_db()
        self.assertEqual(self.live_track.artist_id, self.artist.pk)

    def test_delete_requires_confirmation(self):
        self.client.post(self.url, {"action": "delete", "track_ids": [self.live_track.pk]})
        self.assertTrue(Track.objects.filter(pk=self.live_track.pk).exists())

    def test_delete_with_confirmation(self):
        self.client.post(
            self.url,
            {"action": "delete", "track_ids": [self.live_track.pk], "confirm_delete": "yes"},
        )
        self.assertFalse(Track.objects.filter(pk=self.live_track.pk).exists())

    def test_offsite_return_url_is_refused(self):
        response = self.client.post(
            self.url,
            {"action": "publish", "track_ids": [], "next": "https://evil.example.com/"},
        )
        self.assertEqual(response["Location"], reverse("dashboard:bulk"))

    def test_non_staff_cannot_bulk_edit(self):
        self.client.force_login(self.listener)
        self.client.post(self.url, {"action": "archive", "track_ids": [self.live_track.pk]})
        self.live_track.refresh_from_db()
        self.assertEqual(self.live_track.status, Track.Status.PUBLISHED)


class BulkPageTests(StaffDashboardMixin, TestCase):
    def test_staff_sees_the_page(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("dashboard:bulk"))
        self.assertEqual(response.status_code, 200)

    def test_anonymous_is_redirected(self):
        response = self.client.get(reverse("dashboard:bulk"))
        self.assertEqual(response.status_code, 302)

    def test_export_link_mirrors_the_filters(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("dashboard:bulk"), {"status": Track.Status.DRAFT})
        self.assertIn("status=draft", response.context["export_query"])


class CatalogueExportViewTests(StaffDashboardMixin, TestCase):
    url = reverse("dashboard:catalogue-export")

    def test_downloads_csv_as_an_attachment(self):
        self.client.force_login(self.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("attachment;", response["Content-Disposition"])
        self.assertIn("tracks-", response["Content-Disposition"])
        self.assertIn(".csv", response["Content-Disposition"])

    def test_respects_the_status_filter(self):
        self.client.force_login(self.staff)
        response = self.client.get(self.url, {"status": Track.Status.DRAFT})
        rows = list(csv.DictReader(StringIO(response.content.decode().lstrip("﻿"))))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], Track.Status.DRAFT)

    def test_search_filter_narrows_the_download(self):
        self.client.force_login(self.staff)
        response = self.client.get(self.url, {"search": "Glass"})
        rows = list(csv.DictReader(StringIO(response.content.decode().lstrip("﻿"))))
        self.assertEqual([row["title"] for row in rows], ["Glass Cities"])

    def test_anonymous_cannot_export(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)

    def test_non_staff_cannot_export(self):
        self.client.force_login(self.listener)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 403)
