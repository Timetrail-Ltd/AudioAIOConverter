"""Metadata reading and writing utilities."""

import json
import os
import subprocess

from .ffmpeg_utils import find_ffprobe


def read_metadata(file_path: str) -> dict:
    """Read metadata from audio file using ffprobe."""
    ffprobe = find_ffprobe()

    cmd = [
        ffprobe,
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        file_path,
    ]

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )

    if result.returncode != 0:
        return {}

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}

    # Tags are stored in the container for most formats, but in the audio
    # stream for ogg/opus. Container tags take priority.
    tags = {}
    sources = [data.get("format", {}).get("tags") or {}]
    for stream in data.get("streams") or []:
        if stream.get("codec_type") == "audio":
            sources.append(stream.get("tags") or {})
            break
    for source in sources:
        for key, value in source.items():
            tags.setdefault(key.lower(), value)

    metadata = {}
    key_map = {
        "title": "title",
        "artist": "artist",
        "album": "album",
        "album_artist": "album_artist",
        "track": "track",
        "tracknumber": "track",
        "year": "year",
        "date": "year",
        "genre": "genre",
        "comment": "comment",
    }

    for key, value in tags.items():
        key_lower = key.lower()
        mapped = key_map.get(key_lower, key_lower)
        metadata[mapped] = value

    return metadata


def extract_cover_art(file_path: str, output_path: str) -> bool:
    """Extract embedded cover art to a file.

    Returns:
        True if cover art was extracted, False otherwise.
    """
    ffmpeg = find_ffprobe()
    # Use ffmpeg to extract
    import shutil
    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"

    cmd = [
        ffmpeg,
        "-y",
        "-i", file_path,
        "-an",
        "-vcodec", "copy",
        "-frames:v", "1",
        output_path,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        return result.returncode == 0 and os.path.exists(output_path)
    except Exception:
        return False