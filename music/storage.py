"""Upload helpers shared by audio and cover-art fields."""

import os
import uuid
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils.deconstruct import deconstructible

AUDIO_EXTENSIONS = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".oga", ".opus", ".wma", ".aiff"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif"}

AUDIO_MIME_PREFIXES = ("audio/", "application/ogg", "video/mp4", "application/vnd.apple.mpegurl")


def extension_of(value) -> str:
    """Best-effort file extension for a path, FieldFile or uploaded file object.

    These validators run in three different worlds: Django model validation
    hands them a ``FieldFile``, DRF hands them the raw ``InMemoryUploadedFile``
    / ``TemporaryUploadedFile``, and a Django *form* field may hand them a bare
    filename string (``forms.FileField.to_python`` returns ``data.name``).
    Handle all three.
    """
    if isinstance(value, str):
        name = value
    else:
        name = getattr(value, "name", None)
    if not name:
        return ""
    return Path(str(name)).suffix.lower()


def validate_audio_extension(value):
    ext = extension_of(value) or "unknown"
    if ext not in AUDIO_EXTENSIONS:
        raise ValidationError(
            f"Unsupported audio format '{ext}'. Allowed: {', '.join(sorted(AUDIO_EXTENSIONS))}"
        )


def validate_image_extension(value):
    ext = extension_of(value) or "unknown"
    if ext not in IMAGE_EXTENSIONS:
        raise ValidationError(
            f"Unsupported image format '{ext}'. Allowed: {', '.join(sorted(IMAGE_EXTENSIONS))}"
        )


def validate_upload_size(value):
    limit = settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024
    size = getattr(value, "size", None)
    if size is not None and size > limit:
        raise ValidationError(
            f"File is {size / 1024 / 1024:.1f} MB, limit is {settings.MAX_UPLOAD_SIZE_MB} MB."
        )


@deconstructible
class AudioUploadPath:
    """Sharded, collision-proof storage path: audio/2026/09/<track>/<uuid>.mp3"""

    def __init__(self, subdir="audio"):
        self.subdir = subdir

    def __call__(self, instance, filename):
        ext = Path(filename).suffix.lower()
        if ext not in AUDIO_EXTENSIONS:
            ext = ".mp3"
        owner = ""
        track = getattr(instance, "pk", None)
        if track:
            owner = f"{track}/"
        stamp = uuid.uuid4().hex[:12]
        return f"{self.subdir}/{timezone_path()}/{owner}{stamp}{ext}"


@deconstructible
class CoverArtUploadPath:
    """Sharded cover-art path: covers/2026/09/<owner>/<uuid>.jpg"""

    def __init__(self, subdir="covers"):
        self.subdir = subdir

    def __call__(self, instance, filename):
        ext = Path(filename).suffix.lower()
        if ext not in IMAGE_EXTENSIONS:
            ext = ".jpg"
        owner = ""
        pk = getattr(instance, "pk", None)
        if pk:
            owner = f"{str(pk)[:12]}/"
        return f"{self.subdir}/{timezone_path()}/{owner}{uuid.uuid4().hex[:12]}{ext}"


def timezone_path() -> str:
    from django.utils import timezone

    return timezone.now().strftime("%Y/%m")


def delete_file(field_file):
    """Best-effort removal of an uploaded file from storage."""
    if not field_file:
        return False
    try:
        name = field_file.name
        if name and os.path.isfile(field_file.path):
            field_file.storage.delete(name)
            return True
    except (OSError, ValueError, NotImplementedError):
        return False
    return False


def human_size(num_bytes) -> str:
    if not num_bytes:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    index = min(int(num_bytes.bit_length() // 10) if num_bytes else 0, len(units) - 1)
    size = num_bytes / (1024**index)
    return f"{size:.0f} {units[index]}" if index == 0 else f"{size:.1f} {units[index]}"
