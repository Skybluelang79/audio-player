"""Registration, JWT issue/refresh, and profile management."""

import re

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from tests.factories import MediaRootMixin

User = get_user_model()

PASSWORD = "Str0ngPass!23"


class RegistrationTests(TestCase):
    url = "/api/auth/register/"

    def test_creates_account_and_returns_token_pair(self):
        response = APIClient().post(
            self.url,
            {
                "email": "Listener@Example.com",
                "password": PASSWORD,
                "password_confirm": PASSWORD,
                "display_name": "Nova",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertIn("access", response.data)
        self.assertIn("refresh", response.data)
        self.assertEqual(response.data["user"]["email"], "listener@example.com")

        user = User.objects.get(email="listener@example.com")
        self.assertTrue(user.check_password(PASSWORD))
        self.assertEqual(user.display_name, "Nova")
        self.assertEqual(user.role, User.Role.LISTENER)
        self.assertIsNotNone(user.last_login)

    def test_rejects_duplicate_email_case_insensitively(self):
        User.objects.create_user(email="taken@example.com", password=PASSWORD)
        response = APIClient().post(
            self.url,
            {"email": "TAKEN@example.com", "password": PASSWORD, "password_confirm": PASSWORD},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("email", response.data["error"]["details"])

    def test_rejects_password_mismatch(self):
        response = APIClient().post(
            self.url,
            {"email": "a@example.com", "password": PASSWORD, "password_confirm": "different"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("password_confirm", response.data["error"]["details"])

    def test_rejects_weak_password(self):
        response = APIClient().post(
            self.url,
            {"email": "weak@example.com", "password": "1234", "password_confirm": "1234"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)


class LoginTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(email="dj@example.com", password=PASSWORD)
        cls.staff = User.objects.create_user(
            email="boss@example.com", password=PASSWORD, is_staff=True, role=User.Role.MANAGER
        )

    def test_login_returns_tokens(self):
        response = APIClient().post(
            "/api/auth/login/", {"email": "dj@example.com", "password": PASSWORD}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn("access", response.data)
        self.assertIn("refresh", response.data)

    def test_login_records_client_ip(self):
        client = APIClient()
        client.post(
            "/api/auth/login/",
            {"email": "dj@example.com", "password": PASSWORD},
            format="json",
            REMOTE_ADDR="203.0.113.9",
        )
        self.user.refresh_from_db()
        self.assertEqual(self.user.last_login_ip, "203.0.113.9")

    def test_wrong_password_is_401(self):
        response = APIClient().post(
            "/api/auth/login/", {"email": "dj@example.com", "password": "nope"}, format="json"
        )
        self.assertEqual(response.status_code, 401)

    def test_inactive_account_cannot_log_in(self):
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        response = APIClient().post(
            "/api/auth/login/", {"email": "dj@example.com", "password": PASSWORD}, format="json"
        )
        self.assertEqual(response.status_code, 401)


class TokenLifecycleTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(email="cycle@example.com", password=PASSWORD)

    def test_refresh_rotates_and_blacklists_old_token(self):
        from rest_framework_simplejwt.tokens import RefreshToken

        login = APIClient().post(
            "/api/auth/login/", {"email": "cycle@example.com", "password": PASSWORD}, format="json"
        )
        first_refresh = login.data["refresh"]

        refreshed = APIClient().post(
            "/api/auth/refresh/", {"refresh": first_refresh}, format="json"
        )
        self.assertEqual(refreshed.status_code, 200, refreshed.data)
        self.assertIn("access", refreshed.data)
        self.assertIn("refresh", refreshed.data)
        self.assertNotEqual(refreshed.data["refresh"], first_refresh)

        replayed = APIClient().post("/api/auth/refresh/", {"refresh": first_refresh}, format="json")
        self.assertEqual(replayed.status_code, 401)

    def test_logout_blacklists_refresh_token(self):
        login = APIClient().post(
            "/api/auth/login/", {"email": "cycle@example.com", "password": PASSWORD}, format="json"
        )
        refresh = login.data["refresh"]
        out = APIClient().post("/api/auth/logout/", {"refresh": refresh}, format="json")
        self.assertEqual(out.status_code, 205)
        again = APIClient().post("/api/auth/logout/", {"refresh": refresh}, format="json")
        self.assertEqual(again.status_code, 400)

    def test_logout_requires_token(self):
        self.assertEqual(APIClient().post("/api/auth/logout/", {}, format="json").status_code, 400)

    def test_access_token_works_until_expiry(self):
        from rest_framework_simplejwt.tokens import RefreshToken

        token = RefreshToken.for_user(self.user)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")
        self.assertEqual(client.get("/api/auth/me/").status_code, 200)


class MeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(email="me@example.com", password=PASSWORD)

    def test_requires_authentication(self):
        self.assertEqual(APIClient().get("/api/auth/me/").status_code, 401)

    def test_returns_own_profile(self):
        client = APIClient()
        client.force_authenticate(self.user)
        response = client.get("/api/auth/me/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["email"], "me@example.com")
        self.assertEqual(response.data["initials"], "ME")

    def test_profile_update_cannot_escalate_privileges(self):
        client = APIClient()
        client.force_authenticate(self.user)
        response = client.patch(
            "/api/auth/me/",
            {"display_name": "Renamed", "is_staff": True, "is_superuser": True, "role": "manager"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.user.refresh_from_db()
        self.assertEqual(self.user.display_name, "Renamed")
        self.assertFalse(self.user.is_staff)
        self.assertFalse(self.user.is_superuser)
        self.assertEqual(self.user.role, User.Role.LISTENER)

    def test_password_change_revokes_sessions(self):
        from rest_framework_simplejwt.tokens import RefreshToken

        token = RefreshToken.for_user(self.user)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")

        response = client.post(
            "/api/auth/password/change/",
            {"current_password": PASSWORD, "new_password": "An0therPass!77"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)

        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("An0therPass!77"))

        blocked = APIClient()
        blocked.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")
        from rest_framework_simplejwt.exceptions import TokenError

        with self.assertRaises(TokenError):
            RefreshToken(str(token))

    def test_password_change_requires_current_password(self):
        client = APIClient()
        client.force_authenticate(self.user)
        response = client.post(
            "/api/auth/password/change/",
            {"current_password": "wrong", "new_password": "An0therPass!77"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)


class StaffDirectoryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            email="staff@example.com", password=PASSWORD, is_staff=True, role=User.Role.MANAGER
        )
        cls.regular = User.objects.create_user(email="reg@example.com", password=PASSWORD)

    def test_regular_user_cannot_list_staff_directory(self):
        client = APIClient()
        client.force_authenticate(self.regular)
        self.assertEqual(client.get("/api/staff/users/").status_code, 403)

    def test_staff_can_list_staff_directory(self):
        client = APIClient()
        client.force_authenticate(self.staff)
        self.assertEqual(client.get("/api/staff/users/").status_code, 200)

    def test_staff_can_set_role_but_not_their_own(self):
        client = APIClient()
        client.force_authenticate(self.staff)
        target = User.objects.create(email="target@example.com")
        response = client.post(f"/api/staff/users/{target.pk}/set_role/", {"role": "artist"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        target.refresh_from_db()
        self.assertEqual(target.role, User.Role.ARTIST)

        own = client.post(f"/api/staff/users/{self.staff.pk}/set_role/", {"role": "listener"}, format="json")
        self.assertEqual(own.status_code, 400)

    def test_set_role_rejects_unknown_value(self):
        client = APIClient()
        client.force_authenticate(self.staff)
        target = User.objects.create(email="t2@example.com")
        response = client.post(f"/api/staff/users/{target.pk}/set_role/", {"role": "wizard"}, format="json")
        self.assertEqual(response.status_code, 400)


class UserModelTests(TestCase):
    def test_auto_derives_username_and_display_name(self):
        user = User.objects.create_user(email="auto@example.com", password=PASSWORD)
        self.assertEqual(user.username, "auto")
        self.assertEqual(user.display_name, "auto")

    def test_initials_from_full_name(self):
        user = User.objects.create(
            email="full@example.com", first_name="Ada", last_name="Lovelace"
        )
        self.assertEqual(user.full_name, "Ada Lovelace")
        self.assertEqual(user.initials, "AL")

    def test_is_admin_staff_requires_active(self):
        staff = User.objects.create_user(
            email="s@example.com", password=PASSWORD, is_staff=True, is_active=False
        )
        self.assertFalse(staff.is_admin_staff)

    def test_invalid_client_ip_returns_empty_string(self):
        self.assertEqual(User.client_ip(type("R", (), {"META": {"REMOTE_ADDR": "not-an-ip"}})()), "")


class FollowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.me = User.objects.create_user(email="me@example.com", password=PASSWORD)
        cls.target = User.objects.create_user(email="target@example.com", password=PASSWORD)
        cls.other = User.objects.create_user(email="other@example.com", password=PASSWORD)

    def test_follow_and_unfollow(self):
        from accounts.models import UserFollow

        client = APIClient()
        client.force_authenticate(self.me)
        response = client.post(f"/api/users/{self.target.pk}/follow/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["is_following"])
        self.assertEqual(response.data["follower_count"], 1)
        self.assertTrue(
            UserFollow.objects.filter(follower=self.me, following=self.target).exists()
        )

        response = client.delete(f"/api/users/{self.target.pk}/follow/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(response.data["is_following"])
        self.assertFalse(
            UserFollow.objects.filter(follower=self.me, following=self.target).exists()
        )

    def test_cannot_follow_yourself(self):
        client = APIClient()
        client.force_authenticate(self.me)
        self.assertEqual(client.post(f"/api/users/{self.me.pk}/follow/").status_code, 400)

    def test_follow_requires_authentication(self):
        self.assertEqual(APIClient().post(f"/api/users/{self.target.pk}/follow/").status_code, 401)

    def test_followers_and_following_lists(self):
        from accounts.models import UserFollow

        UserFollow.objects.create(follower=self.other, following=self.target)
        UserFollow.objects.create(follower=self.me, following=self.target)

        followers = APIClient().get(f"/api/users/{self.target.pk}/followers/")
        self.assertEqual(followers.status_code, 200)
        self.assertEqual(
            {row["id"] for row in followers.data["results"]},
            {str(self.other.pk), str(self.me.pk)},
        )

        following = APIClient().get(f"/api/users/{self.me.pk}/following/")
        self.assertEqual(following.status_code, 200)
        self.assertEqual(
            [row["id"] for row in following.data["results"]], [str(self.target.pk)]
        )

    def test_serializer_reports_followed_state(self):
        from accounts.models import UserFollow

        UserFollow.objects.create(follower=self.me, following=self.target)
        client = APIClient()
        client.force_authenticate(self.me)
        response = client.get(f"/api/users/{self.target.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["is_following"])
        self.assertEqual(response.data["follower_count"], 1)
        self.assertEqual(response.data["following_count"], 0)


class PasswordResetTests(TestCase):
    URL_REQUEST = "/api/auth/password/reset/"
    URL_CONFIRM = "/api/auth/password/reset/confirm/"

    def test_reset_request_sends_email_and_confirm_rotates_password(self):
        from django.core import mail

        user = User.objects.create_user(email="reset@example.com", password=PASSWORD)
        response = APIClient().post(
            self.URL_REQUEST, {"email": "reset@example.com"}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(mail.outbox), 1)

        match = re.search(r"uid:\s*(\S+)\s+token:\s*(\S+)", mail.outbox[0].body)
        self.assertIsNotNone(match)
        uid, token = match.group(1), match.group(2)

        confirmed = APIClient().post(
            self.URL_CONFIRM,
            {
                "uid": uid,
                "token": token,
                "new_password": "FreshPass!88",
                "new_password_confirm": "FreshPass!88",
            },
            format="json",
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.data)
        user.refresh_from_db()
        self.assertTrue(user.check_password("FreshPass!88"))

    def test_confirm_rejects_mismatched_passwords(self):
        from accounts.emails import make_token, make_uid

        user = User.objects.create_user(email="r2@example.com", password=PASSWORD)
        response = APIClient().post(
            self.URL_CONFIRM,
            {
                "uid": make_uid(user),
                "token": make_token(user),
                "new_password": "FreshPass!88",
                "new_password_confirm": "DifferentPass!1",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("new_password_confirm", response.data["error"]["details"])

    def test_confirm_rejects_expired_token(self):
        from datetime import datetime, timedelta
        from unittest.mock import patch

        from accounts.emails import make_token, make_uid

        user = User.objects.create_user(email="r3@example.com", password=PASSWORD)

        with patch(
            "django.contrib.auth.tokens.PasswordResetTokenGenerator._now",
            return_value=datetime.now() - timedelta(days=8),
        ):
            stale = make_token(user)

        response = APIClient().post(
            self.URL_CONFIRM,
            {
                "uid": make_uid(user),
                "token": stale,
                "new_password": "FreshPass!88",
                "new_password_confirm": "FreshPass!88",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("token", response.data["error"]["details"])

    def test_request_does_not_enumerate_accounts(self):
        response = APIClient().post(
            self.URL_REQUEST, {"email": "missing@example.com"}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("has been sent", response.data["detail"])


class EmailVerificationTests(TestCase):
    URL_SEND = "/api/auth/email/verify/"
    URL_CONFIRM = "/api/auth/email/verify/confirm/"

    def test_verify_flow(self):
        from django.core import mail

        user = User.objects.create_user(email="v@example.com", password=PASSWORD)
        client = APIClient()
        client.force_authenticate(user)

        sent = client.post(self.URL_SEND)
        self.assertEqual(sent.status_code, 200, sent.data)
        self.assertEqual(len(mail.outbox), 1)

        match = re.search(r"uid:\s*(\S+)\s+token:\s*(\S+)", mail.outbox[0].body)
        confirmed = APIClient().post(
            self.URL_CONFIRM,
            {"uid": match.group(1), "token": match.group(2)},
            format="json",
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.data)
        user.refresh_from_db()
        self.assertTrue(user.is_verified)

    def test_sending_requires_authentication(self):
        self.assertEqual(APIClient().post(self.URL_SEND).status_code, 401)

    def test_confirm_is_open_to_anonymous_clients(self):
        # The confirm endpoint must be reachable without a session/JWT because
        # it is only ever reached from an emailed link.
        response = APIClient().post(
            self.URL_CONFIRM, {"uid": "x", "token": "x"}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("uid", response.data["error"]["details"])

    def test_bad_token_rejected(self):
        from accounts.emails import make_uid

        user = User.objects.create_user(email="v2@example.com", password=PASSWORD)
        response = APIClient().post(
            self.URL_CONFIRM, {"uid": make_uid(user), "token": "bogus"}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("token", response.data["error"]["details"])


class ProfileSiteTests(MediaRootMixin, TestCase):
    def test_public_profile_renders(self):
        user = User.objects.create_user(
            email="pixel@example.com", password=PASSWORD, display_name="Pixel"
        )
        response = self.client.get(reverse("accounts:profile", kwargs={"pk": user.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Pixel")

    def test_own_profile_shows_favourites(self):
        from tests.factories import make_audio
        from music.models import Artist, Track
        from playlists.models import Favorite

        user = User.objects.create_user(email="own@example.com", password=PASSWORD)
        artist = Artist.objects.create(name="Nova Kestrel")
        track = Track.objects.create(
            title="Glass Cities",
            artist=artist,
            status=Track.Status.PUBLISHED,
            duration_seconds=214,
            audio_file=make_audio("glass-cities.mp3"),
        )
        Favorite.objects.create(user=user, track=track)

        self.client.force_login(user)
        response = self.client.get(reverse("accounts:profile", kwargs={"pk": user.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, track.title)

    def test_feed_requires_login(self):
        response = self.client.get(reverse("accounts:feed"))
        self.assertEqual(response.status_code, 302)

    def test_feed_renders_for_logged_in_user(self):
        user = User.objects.create_user(email="feed@example.com", password=PASSWORD)
        self.client.force_login(user)
        response = self.client.get(reverse("accounts:feed"))
        self.assertEqual(response.status_code, 200)

    def test_password_reset_page_renders(self):
        response = self.client.get(reverse("accounts:password-reset"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Reset your password")


class MediaLoginTests(MediaRootMixin, TestCase):
    def test_login_page_renders(self):
        response = self.client.get(reverse("login"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Staff sign in")
