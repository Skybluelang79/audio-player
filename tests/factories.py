"""Shared factories and helpers for the test suite."""

import io
import os
import tempfile

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

from music.models import Album, Artist, Genre, Track

User = get_user_model()

# 1x1 transparent PNG — smallest valid image Pillow will accept.
PNG_PIXELS = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082"
)


def make_image(name="cover.png", content_type="image/png"):
    return SimpleUploadedFile(name, PNG_PIXELS, content_type=content_type)


def make_audio(name="demo.mp3", size=2048):
    """A small file with a valid audio extension; contents are never parsed."""
    return SimpleUploadedFile(name, b"\x00" * size, content_type="audio/mpeg")


class MediaRootMixin:
    """Redirect MEDIA_ROOT to a temp dir so tests never touch the real one."""

    @classmethod
    def setUpClass(cls):
        cls._media_tmp = tempfile.TemporaryDirectory()
        cls._media_override = override_settings(MEDIA_ROOT=cls._media_tmp.name)

        # Cleanups run LIFO, so registering in this order means the temp dir is
        # emptied first, then the override is lifted, then storage is left to
        # re-resolve itself against the real MEDIA_ROOT.
        cls.addClassCleanup(cls.reset_default_storage)
        cls.addClassCleanup(cls._media_override.disable)
        cls.addClassCleanup(cls._media_tmp.cleanup)

        # Both of these must happen *before* super().setUpClass(), because
        # Django invokes setUpTestData() from inside TestCase.setUpClass() and
        # that is what creates the audio files.
        cls._media_override.enable()
        cls.reset_default_storage()

        super().setUpClass()

    @staticmethod
    def reset_default_storage():
        """Force ``default_storage`` to re-resolve against the current MEDIA_ROOT.

        Django's own ``setting_changed`` receiver clears the storage caches for
        STORAGES/STATIC_ROOT/STATIC_URL/DEFAULT_FILE_STORAGE but not for
        MEDIA_ROOT, and in this version ``default_storage`` resolves through the
        cached ``storages`` registry. Without this, both caches stay bound to
        the real media directory and every uploaded test file lands there.
        """
        from django.core.files.storage import default_storage, storages
        from django.utils.functional import empty

        default_storage._wrapped = empty
        try:
            del storages.backends
        except AttributeError:
            pass
        storages._backends = None
        storages._storages = {}


class CatalogMixin:
    """Builds a small, deterministic catalogue shared by API tests."""

    @classmethod
    def setUpTestData(cls):
        cls.artist = Artist.objects.create(name="Nova Kestrel", bio="Synthwave producer.", country="DE")
        cls.other_artist = Artist.objects.create(name="Rift Meridian", country="JP")
        cls.genre = Genre.objects.create(name="Synthwave", color="#00e5ff")
        cls.other_genre = Genre.objects.create(name="Ambient", color="#b47cff")
        cls.artist.genres.add(cls.genre)

        cls.album = Album.objects.create(
            title="Neon Horizon",
            artist=cls.artist,
            album_type=Album.AlbumType.ALBUM,
            release_date="2026-01-15",
        )
        cls.live_track = Track.objects.create(
            title="Glass Cities",
            artist=cls.artist,
            album=cls.album,
            status=Track.Status.PUBLISHED,
            duration_seconds=214,
            audio_file=make_audio("glass-cities.mp3"),
            track_number=1,
        )
        cls.live_track.genres.add(cls.genre)
        cls.second_track = Track.objects.create(
            title="Chrome Rain",
            artist=cls.artist,
            album=cls.album,
            status=Track.Status.PUBLISHED,
            duration_seconds=187,
            audio_file=make_audio("chrome-rain.mp3"),
            track_number=2,
        )
        cls.draft_track = Track.objects.create(
            title="Unreleased Sketches",
            artist=cls.other_artist,
            status=Track.Status.DRAFT,
            audio_file=make_audio("sketches.mp3"),
        )


def auth_header(user, password="Str0ngPass!23"):
    """Log in through the API and return the bearer header plus the token pair."""
    from rest_framework.test import APIClient

    client = APIClient()
    response = client.post(
        "/api/auth/login/",
        {"email": user.email, "password": password},
        format="json",
    )
    assert response.status_code == 200, response.data
    access = response.data["access"]
    return {"HTTP_AUTHORIZATION": f"Bearer {access}"}, response.data


def client_for(user=None, password="Str0ngPass!23"):
    from rest_framework.test import APIClient

    client = APIClient()
    if user is not None:
        header, _tokens = auth_header(user, password)
        client.credentials(**header)
    return client
