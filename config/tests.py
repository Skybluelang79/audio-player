"""Tests for the platform health probe."""

from unittest import mock

from django.db import connection
from django.test import TestCase
from django.urls import reverse


class HealthzTests(TestCase):
    def test_healthz_returns_ok(self):
        response = self.client.get(reverse("healthz"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_healthz_is_public(self):
        """The platform probes anonymously; requiring a login would fail it."""

        self.assertEqual(self.client.get("/healthz/").status_code, 200)

    def test_healthz_rejects_non_get(self):
        self.assertEqual(self.client.post(reverse("healthz")).status_code, 405)

    def test_healthz_is_never_cached(self):
        response = self.client.get(reverse("healthz"))

        self.assertIn("no-cache", response.headers.get("Cache-Control", ""))

    def test_healthz_reports_503_when_database_is_unreachable(self):
        """A process that cannot reach the DB is up but not ready."""

        with mock.patch.object(connection, "cursor", side_effect=Exception("down")):
            response = self.client.get(reverse("healthz"))

        self.assertEqual(response.status_code, 503)

    def test_healthz_does_not_leak_details(self):
        body = self.client.get(reverse("healthz")).content.decode()

        self.assertNotIn("sqlite", body)
        self.assertNotIn("postgres", body)
