"""Regression tests for probe_media_info and read_metadata."""

import pytest

from audio_converter.ffmpeg_utils import probe_media_info
from audio_converter.metadata import read_metadata

from .helpers import ALL_FORMATS, make_audio


EXPECTED_CODEC = {
    "mp3": "mp3", "aac": "aac", "m4a": "aac", "ogg": "vorbis", "opus": "opus",
    "flac": "flac", "wav": "pcm_s16le", "aiff": "pcm_s16be",
}


@pytest.mark.parametrize("fmt", ALL_FORMATS)
def test_probe_reports_audio_stream_details(tmp_path, fmt):
    """Regression: codec/sample rate/channels were always '', 0 and 0
    (wrong ffprobe section name), so the GUI showed '| 0Hz | 0ch'."""
    f = make_audio(tmp_path / f"a.{fmt}", duration=2.0, channels=2)
    info = probe_media_info(str(f))
    assert info.audio_codec == EXPECTED_CODEC[fmt]
    assert info.sample_rate > 0
    assert info.channels == 2
    assert info.duration == pytest.approx(2.0, abs=0.25)
    assert info.format_name == fmt


def test_probe_mono(tmp_path):
    f = make_audio(tmp_path / "m.wav", channels=1)
    assert probe_media_info(str(f)).channels == 1


def test_probe_ignores_cover_art_stream(tmp_path):
    """Regression: the picture stream polluted/overrode the audio details."""
    f = make_audio(tmp_path / "c.mp3", cover=True)
    info = probe_media_info(str(f))
    assert info.audio_codec == "mp3"
    assert info.sample_rate == 44100
    assert info.channels == 2


@pytest.mark.parametrize("fmt", ["mp3", "flac", "m4a", "ogg", "opus", "wav"])
def test_probe_and_read_metadata_tags(tmp_path, fmt):
    """Regression: ogg/opus keep tags on the stream, which were never read."""
    f = make_audio(tmp_path / f"t.{fmt}", metadata={"title": "Árvíztűrő", "artist": "Őrült Ödön"})
    info = probe_media_info(str(f))
    assert info.title == "Árvíztűrő"
    assert info.artist == "Őrült Ödön"
    meta = read_metadata(str(f))
    assert meta["title"] == "Árvíztűrő"
    assert meta["artist"] == "Őrült Ödön"


def test_untagged_file_has_no_title(tmp_path):
    f = make_audio(tmp_path / "plain.wav")
    assert probe_media_info(str(f)).title is None
    assert "title" not in read_metadata(str(f))


def test_probe_rejects_garbage(tmp_path):
    bad = tmp_path / "bad.mp3"
    bad.write_bytes(bytes(range(256)) * 40)
    with pytest.raises(RuntimeError):
        probe_media_info(str(bad))
    assert read_metadata(str(bad)) == {}
