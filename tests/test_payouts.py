"""Artist payouts: earnings computation, generation and the API surface."""

from datetime import date, datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from analytics.models import ArtistPayout, PlayEvent
from analytics.payouts import earnings_by_artist, generate_payouts, rate_per_play

from .factories import CatalogMixin

User = get_user_model()
PASSWORD = "Str0ngPass!23"

PERIOD_START = date(2026, 1, 1)
PERIOD_END = date(2026, 1, 31)


def _play(track, day=15, *, completed=True):
    return PlayEvent.objects.create(
        track=track,
        played_at=timezone.make_aware(datetime(2026, 1, day, 12, 0)),
        completed=completed,
    )


class PayoutServiceTests(CatalogMixin, TestCase):
    def test_rate_defaults_to_setting(self):
        with override_settings(PAYOUT_RATE_PER_PLAY="0.009"):
            self.assertEqual(rate_per_play(), Decimal("0.009"))

    def test_earnings_count_only_completed_plays(self):
        _play(self.live_track)
        _play(self.second_track)
        _play(self.live_track, completed=False)

        rows = earnings_by_artist(PERIOD_START, PERIOD_END, rate="0.01")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["artist_id"], self.artist.id)
        self.assertEqual(rows[0]["plays"], 2)
        self.assertEqual(rows[0]["amount"], Decimal("0.02"))

    def test_earnings_exclude_plays_outside_the_period(self):
        _play(self.live_track, day=1)
        PlayEvent.objects.create(
            track=self.live_track,
            played_at=timezone.make_aware(datetime(2025, 12, 20, 12, 0)),
            completed=True,
        )

        rows = earnings_by_artist(PERIOD_START, PERIOD_END, rate="0.01")

        self.assertEqual(rows[0]["plays"], 1)

    def test_generate_creates_one_row_per_artist(self):
        _play(self.live_track)
        _play(self.second_track)

        created = generate_payouts(PERIOD_START, PERIOD_END, rate="0.01")

        self.assertEqual(len(created), 1)
        payout = ArtistPayout.objects.get()
        self.assertEqual(payout.artist_id, self.artist.id)
        self.assertEqual(payout.plays, 2)
        self.assertEqual(payout.amount, Decimal("0.02"))
        self.assertEqual(payout.status, ArtistPayout.Status.PENDING)

    def test_generate_is_idempotent_without_force(self):
        _play(self.live_track)
        generate_payouts(PERIOD_START, PERIOD_END, rate="0.01")
        _play(self.second_track)

        generate_payouts(PERIOD_START, PERIOD_END, rate="0.01")

        payout = ArtistPayout.objects.get()
        self.assertEqual(payout.plays, 1, "existing payout must not be rewritten")

    def test_force_refreshes_existing_rows(self):
        _play(self.live_track)
        generate_payouts(PERIOD_START, PERIOD_END, rate="0.01")
        _play(self.second_track)

        generate_payouts(PERIOD_START, PERIOD_END, rate="0.01", force=True)

        payout = ArtistPayout.objects.get()
        self.assertEqual(payout.plays, 2)
        self.assertEqual(payout.amount, Decimal("0.02"))

    def test_minimum_amount_filters_trivial_payouts(self):
        _play(self.live_track)

        created = generate_payouts(PERIOD_START, PERIOD_END, rate="0.001")

        self.assertEqual(created, [])
        self.assertFalse(ArtistPayout.objects.exists())

    def test_reversed_period_is_rejected(self):
        _play(self.live_track)
        with self.assertRaises(ValueError):
            generate_payouts(PERIOD_END, PERIOD_START)

    def test_missing_period_is_rejected(self):
        with self.assertRaises(ValueError):
            generate_payouts(None, PERIOD_END)


class PayoutAPITests(CatalogMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.staff = User.objects.create_user(
            email="staff@example.com", password=PASSWORD, is_staff=True
        )
        cls.owner = User.objects.create_user(email="artist@example.com", password=PASSWORD)
        cls.outsider = User.objects.create_user(email="fan@example.com", password=PASSWORD)
        cls.artist.owner = cls.owner
        cls.artist.save(update_fields=["owner"])
        cls.other_artist.owner = cls.outsider
        cls.other_artist.save(update_fields=["owner"])

    def setUp(self):
        self.client = APIClient()

    def _generate(self):
        _play(self.live_track)
        _play(self.second_track)
        return generate_payouts(PERIOD_START, PERIOD_END, rate="0.01")

    def test_generate_requires_staff(self):
        self.client.force_authenticate(self.owner)
        response = self.client.post(
            "/api/payouts/generate/",
            {"period_start": "2026-01-01", "period_end": "2026-01-31"},
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_generate_creates_payouts_for_staff(self):
        _play(self.live_track)
        self.client.force_authenticate(self.staff)
        response = self.client.post(
            "/api/payouts/generate/",
            {"period_start": "2026-01-01", "period_end": "2026-01-31", "rate": "0.01"},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(ArtistPayout.objects.count(), 1)

    def test_generate_rejects_bad_dates(self):
        self.client.force_authenticate(self.staff)
        response = self.client.post(
            "/api/payouts/generate/",
            {"period_start": "not-a-date", "period_end": "2026-01-31"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_artist_sees_only_own_payouts(self):
        self._generate()
        other_payout = ArtistPayout.objects.create(
            artist=self.other_artist,
            period_start=PERIOD_START,
            period_end=PERIOD_END,
            plays=5,
            amount=Decimal("0.05"),
        )

        self.client.force_authenticate(self.owner)
        response = self.client.get("/api/payouts/")

        self.assertEqual(response.status_code, 200)
        ids = [row["id"] for row in response.data["results"]]
        self.assertNotIn(str(other_payout.id), ids)
        self.assertEqual(len(ids), 1)

    def test_staff_sees_every_payout(self):
        self._generate()
        ArtistPayout.objects.create(
            artist=self.other_artist,
            period_start=PERIOD_START,
            period_end=PERIOD_END,
            plays=5,
            amount=Decimal("0.05"),
        )

        self.client.force_authenticate(self.staff)
        response = self.client.get("/api/payouts/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["results"]), 2)

    def test_anonymous_is_rejected(self):
        response = self.client.get("/api/payouts/")
        self.assertIn(response.status_code, (401, 403))

    def test_summary_totals_by_status(self):
        payout = self._generate()[0]
        payout.status = ArtistPayout.Status.PAID
        payout.save(update_fields=["status"])

        self.client.force_authenticate(self.staff)
        response = self.client.get("/api/payouts/summary/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["total"], "0.02")
        self.assertEqual(response.data["pending"], "0.00")
        self.assertEqual(response.data["by_status"]["paid"]["count"], 1)

    def test_staff_can_mark_a_payout_paid(self):
        payout = self._generate()[0]
        self.client.force_authenticate(self.staff)
        response = self.client.patch(
            f"/api/payouts/{payout.id}/", {"status": "paid"}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.data)
        payout.refresh_from_db()
        self.assertEqual(payout.status, ArtistPayout.Status.PAID)

    def test_artist_cannot_adjudicate(self):
        payout = self._generate()[0]
        self.client.force_authenticate(self.owner)
        response = self.client.patch(
            f"/api/payouts/{payout.id}/", {"status": "paid"}, format="json"
        )
        self.assertEqual(response.status_code, 403)
