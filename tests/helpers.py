"""Shared helpers: generate small audio fixtures and verify converter output."""

import json
import subprocess
from pathlib import Path

from audio_converter.ffmpeg_utils import find_ffmpeg, find_ffprobe

# output format -> (audio codec, container name reported by ffprobe)
EXPECTED = {
    "mp3": ("mp3", "mp3"),
    "aac": ("aac", "aac"),
    "m4a": ("aac", "mov,mp4,m4a,3gp,3g2,mj2"),
    "ogg": ("vorbis", "ogg"),
    "opus": ("opus", "ogg"),
    "flac": ("flac", "flac"),
    "wav": ("pcm_s16le", "wav"),
    "aiff": ("pcm_s16be", "aiff"),
}
ALL_FORMATS = list(EXPECTED)


def _run(cmd):
    return subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )


def make_audio(path, duration=2.0, source="sine", channels=2, metadata=None, cover=False):
    """Create a real audio file; the container/codec follow the file extension."""
    path = Path(path)
    if source == "sine":
        src = f"sine=frequency=440:duration={duration}:sample_rate=44100"
    else:  # noise gives encoders something realistic to compress
        src = f"anoisesrc=duration={duration}:sample_rate=44100:amplitude=0.3"
    cmd = [find_ffmpeg(), "-v", "error", "-y", "-f", "lavfi", "-i", src]
    if cover:
        png = path.parent / "cover_fixture.png"
        r = _run([find_ffmpeg(), "-v", "error", "-y", "-f", "lavfi", "-i",
                  "color=c=red:s=64x64", "-frames:v", "1", str(png)])
        assert r.returncode == 0, r.stderr
        cmd += ["-i", str(png), "-map", "0:a", "-map", "1:v", "-c:v", "copy",
                "-id3v2_version", "3", "-disposition:v", "attached_pic"]
    cmd += ["-ac", str(channels)]
    for k, v in (metadata or {}).items():
        cmd += ["-metadata", f"{k}={v}"]
    cmd.append(str(path))
    r = _run(cmd)
    assert r.returncode == 0, r.stderr
    return path


def make_video_only(path, duration=1.0):
    r = _run([find_ffmpeg(), "-v", "error", "-y", "-f", "lavfi", "-i",
              f"testsrc=duration={duration}:size=64x64:rate=10", str(path)])
    assert r.returncode == 0, r.stderr
    return path


def probe(path):
    r = _run([find_ffprobe(), "-v", "error", "-show_format", "-show_streams",
              "-of", "json", str(path)])
    assert r.returncode == 0, f"ffprobe cannot read {path}: {r.stderr}"
    return json.loads(r.stdout)


def audio_stream(path):
    streams = [s for s in probe(path)["streams"] if s["codec_type"] == "audio"]
    assert len(streams) == 1, f"expected exactly one audio stream, got {len(streams)}"
    return streams[0]


def attached_pictures(path):
    return [s for s in probe(path)["streams"]
            if s["codec_type"] == "video" and s.get("disposition", {}).get("attached_pic")]


def video_streams(path):
    return [s for s in probe(path)["streams"] if s["codec_type"] == "video"]


def assert_valid_output(path, fmt, duration=2.0, tolerance=0.25):
    """The file is readable, has the requested format and the expected length."""
    path = Path(path)
    assert path.exists() and path.stat().st_size > 0, f"{path} missing or empty"
    codec, container = EXPECTED[fmt]
    info = probe(path)
    assert info["format"]["format_name"] == container, info["format"]["format_name"]
    assert audio_stream(path)["codec_name"] == codec
    got = float(info["format"]["duration"])
    assert abs(got - duration) <= tolerance, f"duration {got:.3f}s, expected {duration}s"
    dec = _run([find_ffmpeg(), "-v", "error", "-i", str(path), "-f", "null", "-"])
    assert dec.returncode == 0 and not dec.stderr.strip(), f"decode errors: {dec.stderr}"


def leftover_temp_files(folder):
    return [p.name for p in Path(folder).iterdir() if p.name.startswith("audioconv_tmp_")]
