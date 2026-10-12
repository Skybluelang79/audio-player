"""Artist studio onboarding, self-service profile/upload, and new chart series."""

from django.contrib.auth import get_user_model
from django.test import Client, TestCase

from analytics.models import register_play
from analytics.services import completion_funnel, listens_by_hour
from music.models import Artist, Track

from .factories import CatalogMixin, MediaRootMixin, make_audio

User = get_user_model()
PASSWORD = "Str0ngPass!23"


class ChartServiceTests(CatalogMixin, TestCase):
    def test_listens_by_hour_has_24_zero_filled_buckets(self):
        register_play(self.live_track)
        register_play(self.second_track)

        series = listens_by_hour(days=30)

        self.assertEqual(len(series), 24)
        self.assertEqual(series[0]["label"], "00:00")
        self.assertEqual(series[-1]["label"], "23:00")
        self.assertEqual(sum(row["plays"] for row in series), 2)

    def test_completion_funnel_stages(self):
        register_play(self.live_track, completed=False)
        register_play(self.second_track, completed=True)

        funnel = completion_funnel(days=30)
        stages = {row["stage"]: row["value"] for row in funnel["stages"]}

        self.assertEqual(stages["Plays"], 2)
        self.assertEqual(stages["Completed"], 1)
        self.assertIn("Signed-in", stages)
        self.assertIn("Distinct tracks", stages)


class StudioTests(MediaRootMixin, CatalogMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.owner = User.objects.create_user(
            email="nova@example.com", password=PASSWORD, display_name="Nova"
        )
        cls.artist.owner = cls.owner
        cls.artist.save(update_fields=["owner"])
        cls.fresh = User.objects.create_user(email="fresh@example.com", password=PASSWORD)
        cls.staff = User.objects.create_user(
            email="boss@example.com", password=PASSWORD, is_staff=True
        )

    def setUp(self):
        self.client = Client()

    def test_anonymous_is_redirected_to_login(self):
        response = self.client.get("/dashboard/studio/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])

    def test_user_without_profile_sees_onboarding_call(self):
        self.client.force_login(self.fresh)
        response = self.client.get("/dashboard/studio/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Welcome to your studio")
        self.assertContains(response, "Set up your profile")

    def test_claim_creates_a_new_profile(self):
        self.client.force_login(self.fresh)
        response = self.client.post(
            "/dashboard/studio/claim/",
            {"mode": "create", "name": "Aurora Dawn", "country": "SE", "bio": "Synthwave."},
        )
        self.assertEqual(response.status_code, 302)
        artist = Artist.objects.get(owner=self.fresh)
        self.assertEqual(artist.name, "Aurora Dawn")
        self.assertEqual(artist.country, "SE")

    def test_claim_takes_ownership_of_an_unclaimed_profile(self):
        self.client.force_login(self.fresh)
        response = self.client.post(
            "/dashboard/studio/claim/",
            {"mode": "claim", "existing_artist": self.other_artist.pk},
        )
        self.assertEqual(response.status_code, 302)
        self.other_artist.refresh_from_db()
        self.assertEqual(self.other_artist.owner, self.fresh)

    def test_claim_requires_a_name_for_new_profiles(self):
        self.client.force_login(self.fresh)
        response = self.client.post("/dashboard/studio/claim/", {"mode": "create"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Enter a name")

    def test_owner_home_shows_profile_tiles_and_tracks(self):
        self.client.force_login(self.owner)
        response = self.client.get("/dashboard/studio/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Nova Kestrel")
        self.assertContains(response, "Glass Cities")
        self.assertContains(response, "Earnings pending")

    def test_profile_edit_saves_changes(self):
        self.client.force_login(self.owner)
        response = self.client.post(
            "/dashboard/studio/profile/",
            {"bio": "Updated bio.", "website": "https://nova.dev", "country": "DE"},
        )
        self.assertEqual(response.status_code, 302)
        self.artist.refresh_from_db()
        self.assertEqual(self.artist.bio, "Updated bio.")
        self.assertEqual(self.artist.country, "DE")

    def test_upload_is_pinned_to_owned_artist_and_forced_to_draft(self):
        self.client.force_login(self.owner)
        response = self.client.post(
            "/dashboard/studio/upload/",
            {
                "title": "Neon Heart",
                "audio_file": make_audio("neon-heart.mp3"),
                "consent": "on",
                "status": "published",
            },
        )
        self.assertEqual(response.status_code, 302)
        track = Track.objects.get(title="Neon Heart")
        self.assertEqual(track.artist, self.artist)
        self.assertEqual(track.status, Track.Status.DRAFT)

    def test_upload_requires_consent(self):
        self.client.force_login(self.owner)
        response = self.client.post(
            "/dashboard/studio/upload/",
            {
                "title": "No Consent",
                "audio_file": make_audio("no-consent.mp3"),
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "rights")

    def test_user_without_profile_cannot_upload(self):
        self.client.force_login(self.fresh)
        response = self.client.get("/dashboard/studio/upload/")
        self.assertEqual(response.status_code, 403)

    def test_staff_can_open_any_artist(self):
        self.client.force_login(self.staff)
        response = self.client.get(f"/dashboard/studio/?artist={self.artist.pk}")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Glass Cities")

    def test_payout_summary_is_scoped_to_the_artist(self):
        from decimal import Decimal

        from analytics.models import ArtistPayout

        ArtistPayout.objects.create(
            artist=self.artist,
            period_start="2026-01-01",
            period_end="2026-01-31",
            plays=10,
            amount=Decimal("0.04"),
        )
        self.client.force_login(self.owner)
        response = self.client.get("/dashboard/studio/")
        self.assertContains(response, "0.04")