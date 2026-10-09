"""Signed streaming and download helpers.

Uploaded audio lives at guessable ``/media/`` paths, so the API never has to
hand out a permanently valid URL. Instead it signs a short-lived token that the
``/audio/`` endpoint verifies; when ``STREAM_REQUIRE_AUTH`` is on, the raw media
path is withheld and only the signed endpoint is advertised.

The same module builds the download URL so the serializer has one source of
truth for both.
"""

import mimetypes
import os
from urllib.parse import urlencode

from django.conf import settings
from django.core import signing
from django.http import FileResponse
from django.urls import reverse

_STREAM_SALT = "auralis.stream"


def _ttl() -> int:
    return int(getattr(settings, "STREAM_URL_TTL_SECONDS", 3600))


def sign_stream_token(track, *, ttl=None) -> str:
    """Return a signed, expiring token for ``track``'s audio."""
    return signing.dumps({"t": str(track.pk), "ttl": ttl or _ttl()}, salt=_STREAM_SALT)


def verify_stream_token(token: str) -> str:
    """Return the track id inside ``token``.

    Raises ``signing.SignatureExpired`` for a stale token and
    ``signing.BadSignature`` for a forged one, so the caller can answer 403 for
    both without leaking which case it was.
    """
    data = signing.loads(token, salt=_STREAM_SALT, max_age=_ttl())
    return data["t"]


def _absolute(request, path: str) -> str:
    return request.build_absolute_uri(path) if request is not None else path


def stream_url(track, request=None) -> str:
    token = sign_stream_token(track)
    path = reverse("api:track-audio", kwargs={"pk": track.pk})
    return _absolute(request, f"{path}?{urlencode({'token': token})}")


def download_url(track, request=None) -> str:
    return _absolute(request, reverse("api:track-download", kwargs={"pk": track.pk}))


def download_filename(track) -> str:
    """A human filename for the attachment: ``Artist - Title.mp3``."""
    extension = os.path.splitext(track.audio_file.name)[1] or ".mp3"
    stem = f"{track.artist.name} - {track.title}" if track.artist_id else track.title
    # Strip characters that break Content-Disposition headers on some clients.
    safe = "".join("_" if char in '\\/:*?"<>|' else char for char in stem).strip()
    return f"{safe}{extension}"


def audio_file_response(track, *, as_attachment=False, filename=None):
    """Wrap a track's audio in a range-capable ``FileResponse``.

    Returns ``None`` when the file is missing from storage, so the view can
    answer 404 instead of raising ``FileNotFoundError`` mid-response.
    """
    field_file = track.audio_file
    if not field_file:
        return None
    try:
        handle = field_file.open("rb")
    except (FileNotFoundError, OSError, ValueError):
        return None

    name = filename or os.path.basename(field_file.name) or "audio"
    content_type = mimetypes.guess_type(name)[0] or "audio/mpeg"
    response = FileResponse(
        handle, as_attachment=as_attachment, filename=name, content_type=content_type
    )
    response["Accept-Ranges"] = "bytes"
    return response
