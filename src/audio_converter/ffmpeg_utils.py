"""FFmpeg és FFprobe utility függvények."""

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


class FFmpegNotFoundError(Exception):
    """FFmpeg nincs telepítve vagy nem található."""
    pass


@dataclass
class MediaInfo:
    """Médiafájl alapvető információi."""
    path: str
    format_name: str
    duration: float  # másodperc
    bitrate: int  # bps
    audio_codec: str
    sample_rate: int  # Hz
    channels: int
    bit_depth: Optional[int]
    title: Optional[str] = None
    artist: Optional[str] = None
    album: Optional[str] = None


def find_ffmpeg() -> str:
    """Megkeresi az ffmpeg futtatható fájlt."""
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path:
        return ffmpeg_path
    
    env_path = os.environ.get("AUDIOCONVERTER_FFMPEG_DIR")
    if env_path:
        candidate = Path(env_path) / "ffmpeg.exe"
        if candidate.exists():
            return str(candidate)
    
    local_bin = Path(__file__).parent.parent.parent / "bin" / "ffmpeg.exe"
    if local_bin.exists():
        return str(local_bin)
    
    raise FFmpegNotFoundError(
        "FFmpeg nem található. Telepítsd: winget install Gyan.FFmpeg"
    )


def find_ffprobe() -> str:
    """Megkeresi az ffprobe futtatható fájlt."""
    ffprobe_path = shutil.which("ffprobe")
    if ffprobe_path:
        return ffprobe_path
    
    env_path = os.environ.get("AUDIOCONVERTER_FFMPEG_DIR")
    if env_path:
        candidate = Path(env_path) / "ffprobe.exe"
        if candidate.exists():
            return str(candidate)
    
    local_bin = Path(__file__).parent.parent.parent / "bin" / "ffprobe.exe"
    if local_bin.exists():
        return str(local_bin)
    
    raise FFmpegNotFoundError("FFprobe nem található.")


def get_available_encoders() -> dict[str, list[str]]:
    """Lekéri az elérhető FFmpeg encoder-eket."""
    ffmpeg = find_ffmpeg()
    
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-encoders"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    
    encoders = {}
    for line in result.stdout.splitlines():
        match = re.match(r"^\s*([A-Z\.]+)\s+(\S+)\s+(.+)$", line)
        if not match:
            continue
        
        flags, codec_name, description = match.groups()
        if not flags.startswith("A"):
            continue
        
        formats = []
        desc_lower = description.lower()
        
        if "mp3" in desc_lower or "lame" in desc_lower:
            formats.append("mp3")
        if "aac" in desc_lower:
            formats.append("aac")
            formats.append("m4a")
        if "flac" in desc_lower:
            formats.append("flac")
        if "vorbis" in desc_lower:
            formats.append("ogg")
        if "opus" in desc_lower:
            formats.append("opus")
        if "pcm" in desc_lower or "wav" in desc_lower:
            formats.append("wav")
        if "aiff" in desc_lower:
            formats.append("aiff")
        
        if codec_name == "libmp3lame":
            formats = ["mp3"]
        elif codec_name == "aac":
            formats = ["aac", "m4a"]
        elif codec_name == "flac":
            formats = ["flac"]
        elif codec_name == "libvorbis":
            formats = ["ogg"]
        elif codec_name == "libopus":
            formats = ["opus"]
        elif codec_name in ("pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_f32le"):
            formats = ["wav"]
        elif codec_name == "pcm_s16be":
            formats = ["aiff"]
        
        if formats:
            encoders[codec_name] = list(set(formats))
    
    return encoders


def probe_media_info(file_path: str) -> MediaInfo:
    """Kiolvassa egy médiafájl információit FFprobe-bal."""
    ffprobe = find_ffprobe()
    
    cmd = [
        ffprobe,
        "-v", "error",
        "-show_entries", "format=duration,bit_rate:format_tags=title,artist,album:streams=codec_name,sample_rate,channels,bits_per_raw_sample",
        "-of", "default=noprint_wrappers=1",
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
        raise RuntimeError(f"FFprobe hiba: {result.stderr}")
    
    data = {
        "duration": 0.0,
        "bit_rate": 0,
        "codec_name": "",
        "sample_rate": 0,
        "channels": 0,
        "bits_per_raw_sample": None,
        "title": None,
        "artist": None,
        "album": None,
    }
    
    for line in result.stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        
        if key == "duration":
            try:
                data["duration"] = float(value)
            except ValueError:
                data["duration"] = 0.0
        elif key == "bit_rate":
            try:
                data["bit_rate"] = int(value)
            except ValueError:
                data["bit_rate"] = 0
        elif key == "codec_name":
            data["codec_name"] = value
        elif key == "sample_rate":
            try:
                data["sample_rate"] = int(value)
            except ValueError:
                data["sample_rate"] = 0
        elif key == "channels":
            try:
                data["channels"] = int(value)
            except ValueError:
                data["channels"] = 0
        elif key == "bits_per_raw_sample":
            try:
                data["bits_per_raw_sample"] = int(value) if value != "N/A" else None
            except ValueError:
                data["bits_per_raw_sample"] = None
        elif key == "TAG:title":
            data["title"] = value or None
        elif key == "TAG:artist":
            data["artist"] = value or None
        elif key == "TAG:album":
            data["album"] = value or None
    
    return MediaInfo(
        path=file_path,
        format_name=Path(file_path).suffix.lstrip(".").lower(),
        duration=data["duration"],
        bitrate=data["bit_rate"],
        audio_codec=data["codec_name"],
        sample_rate=data["sample_rate"],
        channels=data["channels"],
        bit_depth=data["bits_per_raw_sample"],
        title=data["title"],
        artist=data["artist"],
        album=data["album"],
    )


def get_supported_input_formats() -> list[str]:
    """Visszaadja a támogatott bemeneti formátumok listáját."""
    return ["mp3", "wav", "flac", "aac", "m4a", "ogg", "opus", "aiff"]


def get_supported_output_formats() -> list[str]:
    """Visszaadja a támogatott kimeneti formátumok listáját."""
    return ["mp3", "wav", "flac", "aac", "m4a", "ogg", "opus", "aiff"]