"""HTTP Live Streaming (HLS) packaging for track audio.

ffmpeg slices a track's master file into short segments plus one playlist per
bitrate; this module owns the on-disk layout, the master playlist, the URL
scheme and the optional transcode job. Every path/playlist helper is pure, so
the layout can be reasoned about and tested without ffmpeg installed.

Layout for a single track under ``MEDIA_ROOT``::

    hls/<track_id>/master.m3u8
    hls/<track_id>/stream_128k.m3u8
    hls/<track_id>/stream_128k_000.ts
    ...
"""

import os
import shutil
import subprocess
from pathlib import Path

from django.conf import settings
from django.core.files.storage import default_storage
from django.utils import timezone

#: Directory (below MEDIA_ROOT) that holds every track's HLS output.
HLS_DIR = "hls"
MASTER_NAME = "master.m3u8"


class TranscodeError(RuntimeError):
    """Raised when a track cannot be packaged (missing ffmpeg, bad source, ...)."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def hls_enabled() -> bool:
    return bool(getattr(settings, "HLS_ENABLED", False))


def ffmpeg_binary() -> str:
    return getattr(settings, "FFMPEG_BINARY", "ffmpeg") or "ffmpeg"


def ffmpeg_available(binary: str | None = None) -> bool:
    return shutil.which(binary or ffmpeg_binary()) is not None


def segment_seconds() -> int:
    return max(1, int(getattr(settings, "HLS_SEGMENT_SECONDS", 6)))


def parse_variants(value=None) -> list[int]:
    """Parse a bitrate ladder such as ``"128,192"`` or ``["96k", "160k"]``."""
    if value is None:
        value = getattr(settings, "HLS_VARIANTS", "128,192")
    if isinstance(value, (list, tuple)):
        items = list(value)
    else:
        items = str(value).replace(";", ",").split(",")

    variants: list[int] = []
    for item in items:
        token = str(item).strip().lower().removesuffix("k").strip()
        if not token:
            continue
        try:
            bitrate = int(token)
        except ValueError as exc:
            raise ValueError(f"Invalid HLS bitrate: {item!r}") from exc
        if bitrate <= 0:
            raise ValueError("HLS bitrates must be positive.")
        if bitrate not in variants:
            variants.append(bitrate)
    return sorted(variants)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def hls_dir_rel(track) -> str:
    return f"{HLS_DIR}/{track.pk}"


def master_rel(track) -> str:
    return f"{hls_dir_rel(track)}/{MASTER_NAME}"


def variant_rel(track, bitrate: int) -> str:
    return f"{hls_dir_rel(track)}/stream_{int(bitrate)}k.m3u8"


def hls_abs_dir(track) -> Path:
    return Path(default_storage.path(hls_dir_rel(track)))


def master_exists(track) -> bool:
    name = getattr(track, "hls_playlist", "")
    return bool(name) and default_storage.exists(name)


def safe_hls_path(track, relpath: str) -> Path | None:
    """Resolve a client-supplied subpath inside the track's HLS directory.

    Returns ``None`` when the path escapes the directory (``../`` traversal) or
    the file does not exist, so the view can answer 403/404 without ever
    exposing the surrounding tree.
    """
    root = Path(default_storage.path(hls_dir_rel(track))).resolve()
    try:
        candidate = (root / relpath).resolve()
    except (OSError, ValueError):
        return None
    if candidate != root and root not in candidate.parents:
        return None
    if not candidate.is_file():
        return None
    return candidate


#: Content types for the file flavours HLS produces.
CONTENT_TYPES = {
    ".m3u8": "application/vnd.apple.mpegurl",
    ".ts": "video/mp2t",
    ".m4s": "video/iso.segment",
    ".mp4": "video/mp4",
    ".aac": "audio/aac",
    ".vtt": "text/vtt",
}


def content_type_for(name: str) -> str:
    return CONTENT_TYPES.get(os.path.splitext(name)[1].lower(), "application/octet-stream")


# ---------------------------------------------------------------------------
# Playlists
# ---------------------------------------------------------------------------


def build_master(variants, *, uri_for=None, codecs="mp4a.40.2") -> str:
    """Render the master playlist that advertises each variant stream."""
    lines = ["#EXTM3U", "#EXT-X-VERSION:3"]
    for bitrate in variants:
        lines.append(f'#EXT-X-STREAM-INF:BANDWIDTH={int(bitrate) * 1000},CODECS="{codecs}"')
        lines.append(uri_for(bitrate) if uri_for else f"stream_{int(bitrate)}k.m3u8")
    return "\n".join(lines) + "\n"


def rewrite_playlist_uris(text: str, query: str) -> str:
    """Append ``query`` to every relative URI in a playlist body.

    Relative references in an HLS playlist have no way to carry the signed
    token on their own, so the serving view stamps the caller's query string
    onto each segment/variant line. Absolute URIs and tags are left untouched.
    """
    if not query:
        return text
    out = []
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "://" not in stripped:
            newline = ""
            core = line
            if line.endswith("\r\n"):
                core, newline = line[:-2], "\r\n"
            elif line.endswith("\n"):
                core, newline = line[:-1], "\n"
            joiner = "&" if "?" in core else "?"
            line = f"{core}{joiner}{query}{newline}"
        out.append(line)
    return "".join(out)


# ---------------------------------------------------------------------------
# Transcoding
# ---------------------------------------------------------------------------


def build_ffmpeg_command(
    source,
    out_dir,
    bitrate: int,
    *,
    segment: int | None = None,
    ffmpeg: str | None = None,
    codecs: str = "aac",
) -> list[str]:
    """The argv that produces one VOD variant playlist."""
    out_dir = Path(out_dir)
    return [
        ffmpeg or ffmpeg_binary(),
        "-y",
        "-i",
        str(source),
        "-vn",
        "-c:a",
        codecs,
        "-b:a",
        f"{int(bitrate)}k",
        "-f",
        "hls",
        "-hls_time",
        str(segment or segment_seconds()),
        "-hls_playlist_type",
        "vod",
        "-hls_segment_filename",
        str(out_dir / f"stream_{int(bitrate)}k_%03d.ts"),
        str(out_dir / f"stream_{int(bitrate)}k.m3u8"),
    ]


def _run_ffmpeg(command: list[str]) -> None:
    try:
        subprocess.run(command, check=True, capture_output=True)
    except FileNotFoundError as exc:
        raise TranscodeError("ffmpeg executable not found.") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"").decode("utf-8", "replace").strip()
        raise TranscodeError(detail[-500:] or "ffmpeg failed.") from exc


def transcode_track(
    track,
    *,
    variants=None,
    segment: int | None = None,
    force: bool = False,
    runner=None,
):
    """Package ``track`` as HLS and record the master playlist on the row.

    ``runner`` executes one ffmpeg command and is injectable so the
    orchestration can be tested end-to-end without ffmpeg installed.
    """
    if not track.audio_file:
        raise TranscodeError("Track has no audio file to transcode.")

    variants = parse_variants(variants)
    if not variants:
        raise TranscodeError("No HLS bitrate variants configured.")
    segment = segment or segment_seconds()

    external_runner = runner is not None
    runner = runner or _run_ffmpeg
    if not external_runner and not ffmpeg_available():
        raise TranscodeError("ffmpeg is not available on this host.")

    source = Path(default_storage.path(track.audio_file.name))
    if not source.is_file():
        raise TranscodeError("Source audio file is missing from storage.")

    out_dir = hls_abs_dir(track)
    if force and out_dir.exists():
        shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    for bitrate in variants:
        runner(build_ffmpeg_command(source, out_dir, bitrate, segment=segment))

    master_path = out_dir / MASTER_NAME
    master_path.write_text(build_master(variants), encoding="utf-8")

    rel = master_rel(track)
    track.hls_playlist = rel
    track.hls_generated_at = timezone.now()
    track.save(update_fields=["hls_playlist", "hls_generated_at"])
    return {"track_id": track.pk, "variants": variants, "playlist": rel}


def remove_hls(track) -> None:
    """Delete a track's packaged output and clear the recorded playlist."""
    try:
        directory = hls_abs_dir(track)
        if directory.exists():
            shutil.rmtree(directory, ignore_errors=True)
    except (NotImplementedError, OSError):
        # Remote storage without local paths: still clear the DB pointer.
        pass
    finally:
        if getattr(track, "hls_playlist", "") or getattr(track, "hls_generated_at", None):
            track.hls_playlist = ""
            track.hls_generated_at = None
            track.save(update_fields=["hls_playlist", "hls_generated_at"])
