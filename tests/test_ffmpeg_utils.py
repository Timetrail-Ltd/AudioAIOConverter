"""Tesztek az ffmpeg_utils modulhoz."""

import os
import pytest
from pathlib import Path

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from audio_converter.ffmpeg_utils import (
    find_ffmpeg,
    find_ffprobe,
    get_available_encoders,
    FFmpegNotFoundError,
)


def test_find_ffmpeg_exists():
    """Ellenőrzi, hogy az ffmpeg megtalálható-e."""
    path = find_ffmpeg()
    assert os.path.exists(path)
    assert "ffmpeg" in path.lower()


def test_find_ffprobe_exists():
    """Ellenőrzi, hogy az ffprobe megtalálható-e."""
    path = find_ffprobe()
    assert os.path.exists(path)
    assert "ffprobe" in path.lower()


def test_get_available_encoders():
    """Ellenőrzi, hogy az encoder lista nem üres."""
    encoders = get_available_encoders()
    assert len(encoders) > 0
    assert any("mp3" in formats for formats in encoders.values())


if __name__ == "__main__":
    pytest.main([__file__, "-v"])