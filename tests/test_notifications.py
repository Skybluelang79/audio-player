"""Social notifications: signals, API read receipts, and the inbox page."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from accounts.models import UserFollow
from music.models import Artist, Track, TrackComment
from notifications.models import Notification, notify
from playlists.models import Playlist, PlaylistFollow

from .factories import MediaRootMixin, make_audio

User = get_user_model()
PASSWORD = "Str0ngPass!23"


class NotifyHelperTests(TestCase):
    def test_notify_skips_self_actions(self):
        user = User.objects.create_user(email="self@example.com", password=PASSWORD)
        self.assertIsNone(notify(recipient=user, actor=user, kind=Notification.Kind.FOLLOW, text="x"))
        self.assertEqual(Notification.objects.count(), 0)

    def test_notify_drops_anonymous_recipient(self):
        self.assertIsNone(notify(recipient=None, kind=Notification.Kind.SYSTEM, text="x"))


class FollowSignalsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.follower = User.objects.create_user(email="f@example.com", password=PASSWORD, display_name="Fan")
        cls.target = User.objects.create_user(email="t@example.com", password=PASSWORD)

    def test_user_follow_notifies_target(self):
        UserFollow.objects.create(follower=self.follower, following=self.target)
        notice = Notification.objects.filter(recipient=self.target).first()
        self.assertIsNotNone(notice)
        self.assertEqual(notice.kind, Notification.Kind.FOLLOW)
        self.assertIn("Fan", notice.text)
        self.assertEqual(notice.actor, self.follower)
        self.assertIn("profile", notice.url)


class ArtistFollowSignalTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(email="o@example.com", password=PASSWORD)
        cls.fan = User.objects.create_user(email="fan@example.com", password=PASSWORD, display_name="Fan")
        cls.artist = Artist.objects.create(name="Nova Kestrel", owner=cls.owner)

    def test_artist_follow_notifies_owner(self):
        self.artist.followers.add(self.fan)
        notice = Notification.objects.filter(recipient=self.owner).first()
        self.assertIsNotNone(notice)
        self.assertEqual(notice.kind, Notification.Kind.ARTIST_FOLLOW)
        self.assertIn("Nova Kestrel", notice.text)


class PlaylistFollowSignalTests(MediaRootMixin, TestCase):
    def test_playlist_follow_notifies_owner(self):
        owner = User.objects.create_user(email="p@example.com", password=PASSWORD, display_name="Curator")
        fan = User.objects.create_user(email="fan2@example.com", password=PASSWORD)
        playlist = Playlist.objects.create(
            name="Late Night", owner=owner, visibility=Playlist.Visibility.PUBLIC
        )
        PlaylistFollow.objects.create(user=fan, playlist=playlist)
        notice = Notification.objects.filter(recipient=owner).first()
        self.assertIsNotNone(notice)
        self.assertEqual(notice.kind, Notification.Kind.PLAYLIST_FOLLOW)
        self.assertIn("Late Night", notice.text)


class CommentSignalTests(MediaRootMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(email="owner@example.com", password=PASSWORD)
        cls.author = User.objects.create_user(email="author@example.com", password=PASSWORD, display_name="Ari")
        cls.replier = User.objects.create_user(email="reply@example.com", password=PASSWORD, display_name="Rey")
        cls.artist = Artist.objects.create(name="Nova Kestrel", owner=cls.owner)
        cls.track = Track.objects.create(
            title="Glass Cities",
            artist=cls.artist,
            status=Track.Status.PUBLISHED,
            duration_seconds=214,
            audio_file=make_audio("glass-cities.mp3"),
        )

    def test_top_level_comment_notifies_artist_owner(self):
        TrackComment.objects.create(track=self.track, user=self.author, body="Great tune")
        notice = Notification.objects.filter(recipient=self.owner).first()
        self.assertIsNotNone(notice)
        self.assertEqual(notice.kind, Notification.Kind.COMMENT)
        self.assertIn("Ari", notice.text)

    def test_reply_notifies_parent_author_and_owner(self):
        parent = TrackComment.objects.create(
            track=self.track, user=self.author, body="Great tune"
        )
        Notification.objects.all().delete()
        TrackComment.objects.create(
            track=self.track, user=self.replier, body="Totally", parent=parent
        )
        self.assertTrue(
            Notification.objects.filter(
                recipient=self.author, kind=Notification.Kind.COMMENT_REPLY
            ).exists()
        )
        self.assertTrue(
            Notification.objects.filter(
                recipient=self.owner, kind=Notification.Kind.COMMENT
            ).exists()
        )

    def test_hidden_comment_does_not_notify(self):
        hidden = TrackComment.Status.HIDDEN
        TrackComment.objects.create(
            track=self.track, user=self.author, body="Spam", status=hidden
        )
        self.assertEqual(Notification.objects.count(), 0)


class NotificationApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.recipient = User.objects.create_user(email="in@example.com", password=PASSWORD)
        cls.other = User.objects.create_user(email="out@example.com", password=PASSWORD)
        Notification.objects.create(
            recipient=cls.recipient, kind=Notification.Kind.FOLLOW, text="a followed you"
        )
        Notification.objects.create(
            recipient=cls.recipient, kind=Notification.Kind.SYSTEM, text="Welcome!"
        )

    def test_list_requires_auth(self):
        self.assertEqual(APIClient().get("/api/notifications/").status_code, 401)

    def test_list_returns_only_own_notifications(self):
        client = APIClient()
        client.force_authenticate(self.recipient)
        response = client.get("/api/notifications/")
        self.assertEqual(response.status_code, 200, response.data)
        items = response.data["results"]
        self.assertEqual(len(items), 2)
        self.assertEqual({row["is_read"] for row in items}, {False})

    def test_unread_count(self):
        client = APIClient()
        client.force_authenticate(self.recipient)
        self.assertEqual(client.get("/api/notifications/unread_count/").data["unread"], 2)

    def test_read_and_read_all(self):
        client = APIClient()
        client.force_authenticate(self.recipient)
        first = Notification.objects.filter(recipient=self.recipient).first()

        marked = client.post(f"/api/notifications/{first.pk}/read/")
        self.assertEqual(marked.status_code, 200)
        self.assertTrue(marked.data["is_read"])
        self.assertEqual(client.get("/api/notifications/unread_count/").data["unread"], 1)

        all_done = client.post("/api/notifications/read_all/")
        self.assertEqual(all_done.status_code, 200)
        self.assertEqual(all_done.data["marked_read"], 1)
        self.assertEqual(client.get("/api/notifications/unread_count/").data["unread"], 0)

    def test_cannot_read_someone_elses_notification(self):
        client = APIClient()
        client.force_authenticate(self.other)
        first = Notification.objects.filter(recipient=self.recipient).first()
        self.assertEqual(client.post(f"/api/notifications/{first.pk}/read/").status_code, 404)


class NotificationSiteTests(TestCase):
    def test_inbox_requires_login(self):
        response = self.client.get(reverse("notifications:list"))
        self.assertEqual(response.status_code, 302)

    def test_inbox_renders_for_logged_in_user(self):
        user = User.objects.create_user(email="site@example.com", password=PASSWORD)
        Notification.objects.create(recipient=user, kind=Notification.Kind.SYSTEM, text="Hello")
        self.client.force_login(user)
        response = self.client.get(reverse("notifications:list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Hello")