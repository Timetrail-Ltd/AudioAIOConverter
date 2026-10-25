"""Tests for converter.convert_audio (real FFmpeg, real files in tmp dirs)."""

import hashlib
import shutil
from pathlib import Path

import pytest

from audio_converter import converter
from audio_converter.converter import (
    ConversionCancelled,
    ConversionError,
    ConversionSettings,
    convert_audio,
)
from audio_converter.ffmpeg_utils import FFmpegNotFoundError

from .helpers import (
    ALL_FORMATS,
    assert_valid_output,
    attached_pictures,
    audio_stream,
    leftover_temp_files,
    make_audio,
    make_video_only,
    probe,
    video_streams,
)


def settings(src, out, fmt, **kw):
    return ConversionSettings(input_path=str(src), output_path=str(out), output_format=fmt, **kw)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@pytest.fixture(scope="session")
def sources(tmp_path_factory):
    """One 2.0 s stereo sine file per supported format."""
    d = tmp_path_factory.mktemp("sources")
    return {fmt: make_audio(d / f"source.{fmt}") for fmt in ALL_FORMATS}


@pytest.fixture
def out_dir(tmp_path):
    d = tmp_path / "out"
    d.mkdir()
    return d


# --------------------------------------------------------------------------
# Normal conversion between all supported formats
# --------------------------------------------------------------------------

@pytest.mark.parametrize("fmt", ALL_FORMATS)
def test_wav_to_every_format(sources, out_dir, fmt):
    result = convert_audio(settings(sources["wav"], out_dir / f"o.{fmt}", fmt))
    assert Path(result) == out_dir / f"o.{fmt}"
    assert_valid_output(result, fmt)
    assert not leftover_temp_files(out_dir)


@pytest.mark.parametrize("src_fmt", ALL_FORMATS)
def test_every_format_to_flac(sources, out_dir, src_fmt):
    result = convert_audio(settings(sources[src_fmt], out_dir / "o.flac", "flac"))
    assert_valid_output(result, "flac")


@pytest.mark.parametrize("src_fmt,dst_fmt", [("mp3", "ogg"), ("flac", "m4a"), ("opus", "mp3"), ("aiff", "wav")])
def test_lossy_and_lossless_cross_conversions(sources, out_dir, src_fmt, dst_fmt):
    result = convert_audio(settings(sources[src_fmt], out_dir / f"o.{dst_fmt}", dst_fmt))
    assert_valid_output(result, dst_fmt)


# --------------------------------------------------------------------------
# Settings really reach the output
# --------------------------------------------------------------------------

def test_sample_rate_and_channels_applied(sources, out_dir):
    result = convert_audio(settings(sources["wav"], out_dir / "o.mp3", "mp3", sample_rate=22050, channels=1))
    a = audio_stream(result)
    assert int(a["sample_rate"]) == 22050
    assert a["channels"] == 1
    assert_valid_output(result, "mp3")


@pytest.mark.parametrize("fmt", ["mp3", "ogg", "aac", "opus"])
def test_bitrate_changes_output_size(tmp_path, out_dir, fmt):
    noisy = make_audio(tmp_path / "noise.wav", duration=4.0, source="noise")
    small = convert_audio(settings(noisy, out_dir / f"small.{fmt}", fmt, bitrate=64, vbr_quality=4))
    large = convert_audio(settings(noisy, out_dir / f"large.{fmt}", fmt, bitrate=256, vbr_quality=4))
    assert Path(large).stat().st_size > 2 * Path(small).stat().st_size, (
        f"{fmt}: bitrate setting has no effect"
    )


def test_ogg_quality_mode_when_vbr_requested(tmp_path, out_dir):
    noisy = make_audio(tmp_path / "noise.wav", duration=4.0, source="noise")
    low = convert_audio(settings(noisy, out_dir / "q0.ogg", "ogg", vbr=True, vbr_quality=0))
    high = convert_audio(settings(noisy, out_dir / "q9.ogg", "ogg", vbr=True, vbr_quality=9))
    assert Path(high).stat().st_size > Path(low).stat().st_size


@pytest.mark.parametrize("bit_depth,expected_bits", [(16, 16), (24, 24)])
def test_flac_bit_depth(sources, out_dir, bit_depth, expected_bits):
    """Regression: 24-bit FLAC used to fail ('-sample_fmt s24' is invalid)."""
    result = convert_audio(settings(sources["wav"], out_dir / "o.flac", "flac", bit_depth=bit_depth))
    a = audio_stream(result)
    bits = int(a.get("bits_per_raw_sample") or 0) or int(a["bits_per_sample"])
    assert bits == expected_bits
    assert_valid_output(result, "flac")


@pytest.mark.parametrize("fmt", ["wav", "aiff"])
@pytest.mark.parametrize("bit_depth,codec_bits", [(16, 16), (24, 24), (32, 32)])
def test_pcm_bit_depth(sources, out_dir, fmt, bit_depth, codec_bits):
    result = convert_audio(settings(sources["wav"], out_dir / f"o.{fmt}", fmt, bit_depth=bit_depth))
    assert str(codec_bits) in audio_stream(result)["codec_name"]


@pytest.mark.parametrize("fmt,rate", [("mp3", 96000), ("opus", 44100), ("opus", 96000), ("opus", 22050)])
def test_unsupported_sample_rate_gives_clear_error(sources, out_dir, fmt, rate):
    """Regression: used to end in an unreadable 'Conversion failed!' from FFmpeg."""
    with pytest.raises(ConversionError) as exc:
        convert_audio(settings(sources["wav"], out_dir / f"o.{fmt}", fmt, sample_rate=rate))
    assert str(rate) in str(exc.value) and "Supported sample rates" in str(exc.value)
    assert list(out_dir.iterdir()) == []


@pytest.mark.parametrize("rate", [8000, 16000, 48000])
def test_opus_supported_sample_rates(sources, out_dir, rate):
    result = convert_audio(settings(sources["wav"], out_dir / "o.opus", "opus", sample_rate=rate))
    assert_valid_output(result, "opus")


def test_encoder_rejecting_settings_gives_readable_error(sources, out_dir):
    with pytest.raises(ConversionError) as exc:
        convert_audio(settings(sources["wav"], out_dir / "o.ogg", "ogg", sample_rate=96000, bitrate=192))
    assert "rejected the selected settings" in str(exc.value)
    assert not leftover_temp_files(out_dir)
    assert list(out_dir.iterdir()) == []


# --------------------------------------------------------------------------
# Metadata and cover art
# --------------------------------------------------------------------------

TAGS = {"title": "Árvíztűrő tükörfúrógép", "artist": "日本語 & \"idézet\"", "album": "a=b;c", "year": "1999"}


@pytest.mark.parametrize("fmt", ["mp3", "flac", "m4a", "ogg", "opus"])
def test_metadata_written_and_readable(sources, out_dir, fmt):
    from audio_converter.metadata import read_metadata

    result = convert_audio(settings(sources["wav"], out_dir / f"o.{fmt}", fmt, metadata=dict(TAGS)))
    meta = read_metadata(result)
    assert meta["title"] == TAGS["title"]
    assert meta["artist"] == TAGS["artist"]
    assert meta["album"] == TAGS["album"]
    assert meta["year"] == "1999"


def test_source_metadata_preserved_and_can_be_dropped(tmp_path, out_dir):
    from audio_converter.metadata import read_metadata

    src = make_audio(tmp_path / "tagged.flac", metadata={"title": "Eredeti cím", "artist": "Előadó"})
    kept = convert_audio(settings(src, out_dir / "kept.mp3", "mp3"))
    assert read_metadata(kept)["title"] == "Eredeti cím"
    dropped = convert_audio(settings(src, out_dir / "dropped.mp3", "mp3", preserve_metadata=False))
    assert "title" not in read_metadata(dropped)


@pytest.mark.parametrize("fmt", ALL_FORMATS)
def test_mp3_with_cover_art_converts_to_every_format(tmp_path, out_dir, fmt):
    """Regression: cover art made mp3->m4a fail and became a theora stream in ogg."""
    src = make_audio(tmp_path / "cover.mp3", cover=True)
    result = convert_audio(settings(src, out_dir / f"o.{fmt}", fmt))
    assert_valid_output(result, fmt)
    if fmt in ("mp3", "flac", "m4a"):
        assert len(attached_pictures(result)) == 1, "cover should be preserved"
    else:
        assert video_streams(result) == []


def test_cover_art_dropped_when_metadata_not_preserved(tmp_path, out_dir):
    src = make_audio(tmp_path / "cover.mp3", cover=True)
    result = convert_audio(settings(src, out_dir / "o.mp3", "mp3", preserve_metadata=False))
    assert video_streams(result) == []


def test_input_without_audio_stream_gives_clear_error(tmp_path, out_dir):
    src = make_video_only(tmp_path / "video_only.mp4")
    with pytest.raises(ConversionError) as exc:
        convert_audio(settings(src, out_dir / "o.mp3", "mp3"))
    assert "no audio stream" in str(exc.value)
    assert list(out_dir.iterdir()) == []


# --------------------------------------------------------------------------
# File names and paths
# --------------------------------------------------------------------------

NAMES = {
    "spaces": "hello world  with   spaces",
    "accents": "Árvíztűrő tükörfúrógép – ünnep",
    "cjk": "音楽ファイル",
    "special": "a&b;c (1) [x] #y %z ' !",
    "dots": "my.song.v2",
    "long200": "L" * 200,
    "long250": "M" * 250,  # 254 chars with extension, close to the 255 limit
}


@pytest.mark.parametrize("fmt", ["mp3", "flac"])
@pytest.mark.parametrize("stem", NAMES.values(), ids=NAMES.keys())
def test_unusual_file_names(sources, tmp_path, fmt, stem):
    """Regression: a 250-character name used to crash on the temp file name."""
    d = tmp_path / "in"
    d.mkdir()
    src = d / f"{stem}.wav"
    shutil.copy(sources["wav"], src)
    result = convert_audio(settings(src, d / f"{stem}.{fmt}", fmt))
    assert Path(result).name == f"{stem}.{fmt}"
    assert_valid_output(result, fmt)
    assert not leftover_temp_files(d)


def test_unusual_folder_name_and_very_deep_path(sources, tmp_path):
    deep = tmp_path / "Zenék és más ünnepek"
    for _ in range(6):
        deep = deep / ("könyvtár " + "x" * 40)
    deep.mkdir(parents=True)
    assert len(str(deep)) > 260
    src = deep / "dal.wav"
    shutil.copy(sources["wav"], src)
    result = convert_audio(settings(src, deep / "dal.mp3", "mp3"))
    assert_valid_output(result, "mp3")


def test_missing_output_folder_gives_clear_error(sources, tmp_path):
    target = tmp_path / "nofolder" / "sub" / "o.mp3"
    with pytest.raises(ConversionError, match="Output folder does not exist"):
        convert_audio(settings(sources["wav"], target, "mp3"))
    assert not (tmp_path / "nofolder").exists()


def test_relative_output_path(sources, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = convert_audio(settings(sources["wav"], "relative.mp3", "mp3"))
    assert_valid_output(tmp_path / "relative.mp3", "mp3")
    assert Path(result).name == "relative.mp3"


# --------------------------------------------------------------------------
# Broken, empty and unsupported inputs
# --------------------------------------------------------------------------

def test_missing_input(out_dir, tmp_path):
    with pytest.raises(ConversionError, match="Input file not found"):
        convert_audio(settings(tmp_path / "nope.mp3", out_dir / "o.flac", "flac"))
    assert list(out_dir.iterdir()) == []


def test_empty_input(out_dir, tmp_path):
    zero = tmp_path / "zero_bytes.mp3"
    zero.write_bytes(b"")
    with pytest.raises(ConversionError, match="Input file is empty"):
        convert_audio(settings(zero, out_dir / "o.flac", "flac"))
    assert list(out_dir.iterdir()) == []


@pytest.mark.parametrize("name,content", [
    ("garbage.mp3", bytes(range(256)) * 40),
    ("text.wav", b"this is not audio at all\n" * 20),
    ("fake.flac", b"fLaC" + b"\x00" * 100),
])
def test_corrupt_or_unsupported_input(out_dir, tmp_path, name, content):
    bad = tmp_path / name
    bad.write_bytes(content)
    with pytest.raises(ConversionError) as exc:
        convert_audio(settings(bad, out_dir / "o.mp3", "mp3"))
    assert "FFmpeg failed" in str(exc.value)
    assert list(out_dir.iterdir()) == [], "no output or temp file may remain"


def test_garbage_input_message_is_readable(out_dir, tmp_path):
    bad = tmp_path / "garbage.mp3"
    bad.write_bytes(bytes(range(256)) * 40)
    with pytest.raises(ConversionError) as exc:
        convert_audio(settings(bad, out_dir / "o.mp3", "mp3"))
    assert "damaged, empty or not a supported audio file" in str(exc.value)


def test_truncated_input_is_converted_up_to_the_cut(sources, out_dir, tmp_path):
    cut = tmp_path / "cut.mp3"
    data = sources["mp3"].read_bytes()
    cut.write_bytes(data[: len(data) // 3])
    result = convert_audio(settings(cut, out_dir / "o.flac", "flac"))
    length = float(probe(result)["format"]["duration"])
    assert 0.1 < length < 1.5


def test_unsupported_output_format(sources, out_dir):
    with pytest.raises(ConversionError, match="Unsupported output format"):
        convert_audio(settings(sources["wav"], out_dir / "o.xyz", "xyz"))
    assert list(out_dir.iterdir()) == []


# --------------------------------------------------------------------------
# Existing output files / overwrite policies
# --------------------------------------------------------------------------

ORIGINAL = b"ORIGINAL USER DATA"


@pytest.fixture
def existing(out_dir):
    f = out_dir / "o.mp3"
    f.write_bytes(ORIGINAL)
    return f


def test_rename_policy_never_touches_existing(sources, out_dir, existing):
    r1 = convert_audio(settings(sources["wav"], existing, "mp3", overwrite_policy="rename"))
    r2 = convert_audio(settings(sources["wav"], existing, "mp3", overwrite_policy="rename"))
    assert Path(r1).name == "o_1.mp3" and Path(r2).name == "o_2.mp3"
    assert existing.read_bytes() == ORIGINAL
    assert_valid_output(r1, "mp3")
    assert_valid_output(r2, "mp3")


@pytest.mark.parametrize("policy", ["skip", "prompt"])
def test_skip_and_prompt_raise_and_keep_existing(sources, out_dir, existing, policy):
    """Regression: 'prompt' used to overwrite silently."""
    with pytest.raises(FileExistsError):
        convert_audio(settings(sources["wav"], existing, "mp3", overwrite_policy=policy))
    assert existing.read_bytes() == ORIGINAL
    assert sorted(p.name for p in out_dir.iterdir()) == ["o.mp3"]


def test_unknown_policy_is_rejected_not_treated_as_overwrite(sources, out_dir, existing):
    """Regression: any unknown policy used to overwrite silently."""
    with pytest.raises(ValueError, match="Unknown overwrite policy"):
        convert_audio(settings(sources["wav"], existing, "mp3", overwrite_policy="bogus"))
    assert existing.read_bytes() == ORIGINAL


def test_overwrite_policy_replaces_existing(sources, out_dir, existing):
    result = convert_audio(settings(sources["wav"], existing, "mp3", overwrite_policy="overwrite"))
    assert Path(result) == existing
    assert_valid_output(existing, "mp3")
    assert sorted(p.name for p in out_dir.iterdir()) == ["o.mp3"]


def test_failed_overwrite_keeps_existing_file(out_dir, tmp_path, existing):
    bad = tmp_path / "bad.mp3"
    bad.write_bytes(bytes(range(256)) * 40)
    with pytest.raises(ConversionError):
        convert_audio(settings(bad, existing, "mp3", overwrite_policy="overwrite"))
    assert existing.read_bytes() == ORIGINAL
    assert not leftover_temp_files(out_dir)


def test_overwrite_never_destroys_the_source_file(sources, tmp_path):
    """Regression: input == output with 'overwrite' replaced the user's original."""
    src = tmp_path / "song.wav"
    shutil.copy(sources["wav"], src)
    before = sha(src)
    with pytest.raises(ConversionError, match="same as the input"):
        convert_audio(settings(src, src, "wav", overwrite_policy="overwrite", sample_rate=8000))
    assert sha(src) == before
    assert not leftover_temp_files(tmp_path)


def test_same_format_conversion_does_not_touch_source(sources, tmp_path):
    src = tmp_path / "song.mp3"
    shutil.copy(sources["mp3"], src)
    before = sha(src)
    result = convert_audio(settings(src, src, "mp3", overwrite_policy="rename"))
    assert Path(result).name == "song_1.mp3"
    assert sha(src) == before
    assert_valid_output(result, "mp3")


# --------------------------------------------------------------------------
# FFmpeg missing, cancel, progress
# --------------------------------------------------------------------------

def test_missing_ffmpeg(sources, out_dir, monkeypatch):
    def not_found():
        raise FFmpegNotFoundError("FFmpeg not found")

    monkeypatch.setattr(converter, "find_ffmpeg", not_found)
    with pytest.raises(FFmpegNotFoundError):
        convert_audio(settings(sources["wav"], out_dir / "o.mp3", "mp3"))
    assert list(out_dir.iterdir()) == [], "no temp file may be left behind"


def test_cancel_stops_conversion_and_cleans_up(tmp_path, out_dir):
    long_src = make_audio(tmp_path / "long.wav", duration=120.0)
    with pytest.raises(ConversionCancelled) as exc:
        convert_audio(settings(long_src, out_dir / "o.flac", "flac"), cancel_flag=lambda: True)
    assert isinstance(exc.value, ConversionError)
    assert list(out_dir.iterdir()) == []


def test_cancel_keeps_existing_output(tmp_path, out_dir, existing):
    long_src = make_audio(tmp_path / "long.wav", duration=120.0)
    with pytest.raises(ConversionCancelled):
        convert_audio(
            settings(long_src, existing, "mp3", overwrite_policy="overwrite"),
            cancel_flag=lambda: True,
        )
    assert existing.read_bytes() == ORIGINAL
    assert not leftover_temp_files(out_dir)


def test_retry_after_cancel_succeeds(tmp_path, out_dir):
    src = make_audio(tmp_path / "s.wav")
    with pytest.raises(ConversionCancelled):
        convert_audio(settings(src, out_dir / "o.mp3", "mp3"), cancel_flag=lambda: True)
    result = convert_audio(settings(src, out_dir / "o.mp3", "mp3"))
    assert Path(result).name == "o.mp3"
    assert_valid_output(result, "mp3")


def test_progress_is_monotonic_and_completes(tmp_path, out_dir):
    src = make_audio(tmp_path / "p.wav", duration=30.0, source="noise")
    values = []
    convert_audio(settings(src, out_dir / "o.flac", "flac"), progress_callback=values.append)
    assert values, "no progress reported"
    assert all(0.0 <= v <= 1.0 for v in values)
    assert values == sorted(values)
    assert values[-1] == pytest.approx(1.0, abs=0.01)


def test_repeated_conversion_of_same_file(sources, out_dir):
    results = [convert_audio(settings(sources["wav"], out_dir / "o.mp3", "mp3")) for _ in range(3)]
    assert [Path(r).name for r in results] == ["o.mp3", "o_1.mp3", "o_2.mp3"]
    for r in results:
        assert_valid_output(r, "mp3")



# --------------------------------------------------------------------------
# Trim, Timecode parsing, and Cover Art toggle
# --------------------------------------------------------------------------

def test_parse_timecode_formats():
    from audio_converter.converter import parse_timecode
    assert parse_timecode("") is None
    assert parse_timecode("  ") is None
    assert parse_timecode("12.5") == pytest.approx(12.5)
    assert parse_timecode("01:30") == pytest.approx(90.0)
    assert parse_timecode("01:02:03.5") == pytest.approx(3723.5)
    with pytest.raises(ConversionError):
        parse_timecode("invalid")
    with pytest.raises(ConversionError):
        parse_timecode("-5")


def test_trim_audio_and_invalid_trim_range(tmp_path, out_dir):
    src = make_audio(tmp_path / "full.wav", duration=4.0)
    out = convert_audio(settings(src, out_dir / "cut.mp3", "mp3", trim_start=1.0, trim_end=2.5))
    assert_valid_output(out, "mp3", duration=1.5, tolerance=0.25)

    with pytest.raises(ConversionError, match="greater than start time"):
        convert_audio(settings(src, out_dir / "bad_cut.mp3", "mp3", trim_start=3.0, trim_end=2.0))


def test_preserve_cover_art_can_be_disabled_independently_of_metadata(tmp_path, out_dir):
    from audio_converter.metadata import read_metadata
    src = make_audio(tmp_path / "cover_tagged.mp3", metadata={"title": "KeepMe"}, cover=True)
    out = convert_audio(
        settings(src, out_dir / "no_cover.mp3", "mp3", preserve_metadata=True, preserve_cover_art=False)
    )
    assert read_metadata(out).get("title") == "KeepMe"
    assert video_streams(out) == []
