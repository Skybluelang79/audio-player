"""HLS packaging: playlist helpers, the transcode job and the serving routes."""

from pathlib import Path
from unittest import mock

from django.core.files.storage import default_storage
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from music import hls
from music.hls import (
    TranscodeError,
    build_master,
    content_type_for,
    master_rel,
    parse_variants,
    rewrite_playlist_uris,
    safe_hls_path,
    transcode_track,
)
from music.models import Track
from music.streaming import sign_stream_token

from .factories import CatalogMixin, MediaRootMixin


class HelperTests(TestCase):
    def test_parse_variants_accepts_strings_and_lists(self):
        self.assertEqual(parse_variants("128,192"), [128, 192])
        self.assertEqual(parse_variants(["96k", "160K"]), [96, 160])
        self.assertEqual(parse_variants("128,128,192"), [128, 192])

    def test_parse_variants_defaults_to_settings(self):
        with override_settings(HLS_VARIANTS="64,128"):
            self.assertEqual(parse_variants(), [64, 128])

    def test_parse_variants_rejects_junk(self):
        with self.assertRaises(ValueError):
            parse_variants("fast")
        with self.assertRaises(ValueError):
            parse_variants("0")

    def test_build_master_lists_every_variant(self):
        master = build_master([128, 320])
        self.assertTrue(master.startswith("#EXTM3U"))
        self.assertIn("BANDWIDTH=128000", master)
        self.assertIn("BANDWIDTH=320000", master)
        self.assertIn("stream_320k.m3u8", master)

    def test_rewrite_playlist_stamps_query_on_relative_uris_only(self):
        body = "#EXTM3U\nseg_000.ts\nhttps://cdn.example/abs.ts\n#EXTINF:6.0,\nseg_001.ts\n"
        rewritten = rewrite_playlist_uris(body, "token=abc")
        self.assertIn("seg_000.ts?token=abc", rewritten)
        self.assertIn("seg_001.ts?token=abc", rewritten)
        self.assertIn("https://cdn.example/abs.ts\n", rewritten)
        self.assertNotIn("#EXTINF:6.0,?token", rewritten)

    def test_rewrite_playlist_is_a_noop_without_query(self):
        body = "#EXTM3U\nseg.ts\n"
        self.assertEqual(rewrite_playlist_uris(body, ""), body)

    def test_content_types(self):
        self.assertEqual(content_type_for("master.m3u8"), "application/vnd.apple.mpegurl")
        self.assertEqual(content_type_for("seg.ts"), "video/mp2t")
        self.assertEqual(content_type_for("x.bin"), "application/octet-stream")


class TranscodeTests(MediaRootMixin, CatalogMixin, TestCase):
    def _runner(self, calls):
        def runner(command):
            calls.append(command)
            playlist = Path(command[-1])
            playlist.parent.mkdir(parents=True, exist_ok=True)
            playlist.write_text("#EXTM3U\n#EXTINF:6.0,\nseg_000.ts\n", encoding="utf-8")
            template = command[command.index("-hls_segment_filename") + 1]
            Path(template.replace("%03d", "000")).write_bytes(b"\x00")
        return runner

    def test_transcode_writes_master_and_records_it(self):
        calls = []
        result = transcode_track(
            self.live_track, variants=[128, 192], runner=self._runner(calls)
        )
        self.live_track.refresh_from_db()

        self.assertEqual(len(calls), 2)
        self.assertEqual(result["playlist"], master_rel(self.live_track))
        self.assertEqual(self.live_track.hls_playlist, master_rel(self.live_track))
        self.assertTrue(self.live_track.has_hls)
        self.assertIsNotNone(self.live_track.hls_generated_at)

        master = Path(default_storage.path(self.live_track.hls_playlist)).read_text("utf-8")
        self.assertIn("BANDWIDTH=128000", master)
        self.assertIn("BANDWIDTH=192000", master)
        self.assertTrue(any("-b:a" in command for command in calls))

    def test_transcode_requires_audio(self):
        empty = Track(title="Silence", artist=self.artist, audio_file="")
        with self.assertRaises(TranscodeError):
            transcode_track(empty, runner=lambda command: None)

    def test_transcode_requires_ffmpeg(self):
        with mock.patch("music.hls.ffmpeg_available", return_value=False):
            with self.assertRaises(TranscodeError):
                transcode_track(self.live_track)

    def test_transcode_reports_missing_source(self):
        ghost = Track.objects.create(
            title="Ghost", artist=self.artist, audio_file="audio/does-not-exist.mp3"
        )
        with self.assertRaises(TranscodeError):
            transcode_track(ghost, runner=lambda command: None)

    def test_remove_hls_clears_field_and_directory(self):
        transcode_track(self.live_track, variants=[128], runner=self._runner([]))
        directory = Path(default_storage.path(hls.hls_dir_rel(self.live_track)))
        self.assertTrue(directory.exists())

        hls.remove_hls(self.live_track)
        self.live_track.refresh_from_db()
        self.assertEqual(self.live_track.hls_playlist, "")
        self.assertFalse(directory.exists())

    def test_safe_hls_path_rejects_traversal(self):
        transcode_track(self.live_track, variants=[128], runner=self._runner([]))
        self.assertIsNone(safe_hls_path(self.live_track, "../secret.txt"))
        self.assertIsNone(safe_hls_path(self.live_track, "missing.ts"))
        self.assertIsNotNone(safe_hls_path(self.live_track, "master.m3u8"))

    def test_needs_existing_bitrate(self):
        with self.assertRaises(TranscodeError):
            transcode_track(self.live_track, variants=[])


class BuildHlsCommandTests(MediaRootMixin, CatalogMixin, TestCase):
    def test_requires_a_selection(self):
        with self.assertRaises(CommandError):
            call_command("build_hls")

    def test_reports_missing_ffmpeg(self):
        with mock.patch("music.management.commands.build_hls.ffmpeg_available", return_value=False):
            with self.assertRaises(CommandError):
                call_command("build_hls", "--all")

    def test_dry_run_lists_tracks(self):
        from io import StringIO

        out = StringIO()
        call_command("build_hls", "--published", "--dry-run", stdout=out)
        self.assertIn("would package", out.getvalue())
        self.assertIn("Glass Cities", out.getvalue())

    def test_packages_published_tracks(self):
        from io import StringIO

        out = StringIO()
        fake = {"track_id": 1, "variants": [128], "playlist": "hls/1/master.m3u8"}
        with mock.patch(
            "music.management.commands.build_hls.ffmpeg_available", return_value=True
        ), mock.patch(
            "music.management.commands.build_hls.transcode_track", return_value=fake
        ):
            call_command("build_hls", "--published", stdout=out)
        self.assertIn("packaged 2", out.getvalue())


class HlsApiTests(MediaRootMixin, CatalogMixin, TestCase):
    def setUp(self):
        self.client = APIClient()

    def _package(self, track, variants=(128,)):
        base = Path(default_storage.path(hls.hls_dir_rel(track)))
        base.mkdir(parents=True, exist_ok=True)
        for bitrate in variants:
            (base / f"stream_{bitrate}k.m3u8").write_text(
                "#EXTM3U\n#EXTINF:6.0,\nseg_000.ts\n", encoding="utf-8"
            )
            (base / "seg_000.ts").write_bytes(b"\x00")
        (base / "master.m3u8").write_text(build_master(variants), encoding="utf-8")
        track.hls_playlist = master_rel(track)
        track.save(update_fields=["hls_playlist"])
        return base

    def test_action_reports_not_ready_when_disabled(self):
        response = self.client.get(f"/api/tracks/{self.live_track.pk}/hls/")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["enabled"])
        self.assertFalse(response.data["ready"])
        self.assertIsNone(response.data["master_url"])

    @override_settings(HLS_ENABLED=True)
    def test_action_reports_ready_when_packaged(self):
        self._package(self.live_track)
        response = self.client.get(f"/api/tracks/{self.live_track.pk}/hls/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["ready"])
        self.assertIn("/hls/master.m3u8", response.data["master_url"])

    @override_settings(HLS_ENABLED=True)
    def test_serves_master_playlist(self):
        self._package(self.live_track)
        response = self.client.get(f"/api/tracks/{self.live_track.pk}/hls/master.m3u8")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response["Content-Type"], "application/vnd.apple.mpegurl"
        )
        self.assertIn(b"BANDWIDTH=128000", response.content)

    @override_settings(HLS_ENABLED=True)
    def test_serves_segment_with_binary_content_type(self):
        self._package(self.live_track)
        response = self.client.get(f"/api/tracks/{self.live_track.pk}/hls/seg_000.ts")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "video/mp2t")

    @override_settings(HLS_ENABLED=True)
    def test_traversal_is_not_served(self):
        self._package(self.live_track)
        response = self.client.get(f"/api/tracks/{self.live_track.pk}/hls/..%2fsecret.txt")
        self.assertEqual(response.status_code, 404)

    @override_settings(HLS_ENABLED=True, STREAM_REQUIRE_AUTH=True)
    def test_signed_token_is_stamped_on_relative_uris(self):
        self._package(self.live_track)
        token = sign_stream_token(self.live_track)
        response = self.client.get(
            f"/api/tracks/{self.live_track.pk}/hls/stream_128k.m3u8?token={token}"
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(f"seg_000.ts?token={token}".encode(), response.content)

    @override_settings(HLS_ENABLED=True, STREAM_REQUIRE_AUTH=True)
    def test_missing_token_is_forbidden(self):
        self._package(self.live_track)
        response = self.client.get(f"/api/tracks/{self.live_track.pk}/hls/master.m3u8")
        self.assertEqual(response.status_code, 403)

    @override_settings(HLS_ENABLED=True)
    def test_unpublished_packaged_track_is_hidden(self):
        base = self._package(self.live_track)
        self.live_track.status = Track.Status.DRAFT
        self.live_track.save(update_fields=["status"])
        self.assertTrue(base.exists())
        response = self.client.get(f"/api/tracks/{self.live_track.pk}/hls/master.m3u8")
        self.assertEqual(response.status_code, 404)
