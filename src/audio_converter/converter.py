"""Audio conversion logic using FFmpeg."""

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .ffmpeg_utils import find_ffmpeg, FFmpegNotFoundError


class ConversionError(Exception):
    """Conversion failed."""
    pass


@dataclass
class ConversionSettings:
    """Settings for audio conversion."""
    input_path: str
    output_path: str
    output_format: str

    bitrate: Optional[int] = None  # kbps
    sample_rate: Optional[int] = None  # Hz
    channels: Optional[int] = None  # 1=mono, 2=stereo

    vbr: bool = False
    vbr_quality: Optional[int] = None  # 0-9 for MP3 VBR (lower=better)
    bit_depth: Optional[int] = None  # 16, 24, 32 for WAV/FLAC
    flac_compression: int = 5  # 0-12

    preserve_metadata: bool = True
    metadata: dict = field(default_factory=dict)

    overwrite_policy: str = "rename"  # prompt, overwrite, rename, skip


def _get_temp_path(output_path: str) -> str:
    """Generate a temporary file path next to the final output."""
    parent = Path(output_path).parent
    stem = Path(output_path).stem
    fd, temp_path = tempfile.mkstemp(prefix=f"{stem}_", suffix=Path(output_path).suffix, dir=str(parent))
    os.close(fd)
    return temp_path


def _build_ffmpeg_command(settings: "ConversionSettings", temp_path: str) -> list[str]:
    """Build FFmpeg command list from settings."""
    ffmpeg = find_ffmpeg()
    cmd = [ffmpeg, "-y", "-hide_banner"]

    # Input
    cmd.extend(["-i", settings.input_path])

    if not settings.preserve_metadata:
        cmd.extend(["-map_metadata", "-1", "-map_chapters", "-1"])

    fmt = settings.output_format.lower()

    if fmt == "mp3":
        cmd.extend(["-c:a", "libmp3lame"])
        if settings.vbr and settings.vbr_quality is not None:
            cmd.extend(["-q:a", str(settings.vbr_quality)])
        elif settings.bitrate:
            cmd.extend(["-b:a", f"{settings.bitrate}k"])
        else:
            cmd.extend(["-b:a", "192k"])
    elif fmt == "aac":
        cmd.extend(["-c:a", "aac"])
        if settings.bitrate:
            cmd.extend(["-b:a", f"{settings.bitrate}k"])
    elif fmt == "m4a":
        cmd.extend(["-c:a", "aac"])
        if settings.bitrate:
            cmd.extend(["-b:a", f"{settings.bitrate}k"])
    elif fmt == "ogg":
        cmd.extend(["-c:a", "libvorbis"])
        if settings.vbr_quality is not None:
            cmd.extend(["-q:a", str(settings.vbr_quality)])
        elif settings.bitrate:
            cmd.extend(["-b:a", f"{settings.bitrate}k"])
    elif fmt == "opus":
        cmd.extend(["-c:a", "libopus"])
        if settings.bitrate:
            cmd.extend(["-b:a", f"{settings.bitrate}k"])
        else:
            cmd.extend(["-b:a", "128k"])
    elif fmt == "flac":
        cmd.extend(["-c:a", "flac"])
        cmd.extend(["-compression_level", str(settings.flac_compression)])
        if settings.bit_depth:
            cmd.extend(["-sample_fmt", f"s{settings.bit_depth}"])
    elif fmt == "wav":
        if settings.bit_depth == 24:
            cmd.extend(["-c:a", "pcm_s24le"])
        elif settings.bit_depth == 32:
            cmd.extend(["-c:a", "pcm_s32le"])
        else:
            cmd.extend(["-c:a", "pcm_s16le"])
    elif fmt == "aiff":
        if settings.bit_depth == 24:
            cmd.extend(["-c:a", "pcm_s24be"])
        elif settings.bit_depth == 32:
            cmd.extend(["-c:a", "pcm_s32be"])
        else:
            cmd.extend(["-c:a", "pcm_s16be"])
    else:
        raise ConversionError(f"Unsupported output format: {fmt}")

    if settings.sample_rate:
        cmd.extend(["-ar", str(settings.sample_rate)])

    if settings.channels:
        cmd.extend(["-ac", str(settings.channels)])

    for key, value in settings.metadata.items():
        if value:
            cmd.extend(["-metadata", f"{key}={value}"])

    cmd.append(temp_path)
    return cmd


def convert_audio(
    settings: ConversionSettings,
    progress_callback: Optional[Callable[[float], None]] = None,
    cancel_flag: Optional[Callable[[], bool]] = None,
) -> str:
    """Convert audio file using FFmpeg.

    Returns:
        Path to output file.

    Raises:
        ConversionError: If conversion fails.
        FileExistsError: If output exists and policy is 'skip'.
    """
    if not os.path.exists(settings.input_path):
        raise ConversionError(f"Input file not found: {settings.input_path}")

    final_output = settings.output_path
    if os.path.exists(final_output):
        if settings.overwrite_policy == "rename":
            counter = 1
            stem = Path(final_output).stem
            suffix = Path(final_output).suffix
            parent = Path(final_output).parent
            while os.path.exists(final_output):
                final_output = str(parent / f"{stem}_{counter}{suffix}")
                counter += 1
        elif settings.overwrite_policy == "skip":
            raise FileExistsError(f"Output file already exists: {final_output}")

    temp_path = _get_temp_path(final_output)

    try:
        cmd = _build_ffmpeg_command(settings, temp_path)

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )

        duration = 0.0
        stderr_lines = []

        for line in process.stderr:
            line = line.strip()
            stderr_lines.append(line)

            if "Duration:" in line:
                match = re.search(r"Duration: (\d+):(\d+):([\d\.]+)", line)
                if match:
                    h, m, s = match.groups()
                    duration = int(h) * 3600 + int(m) * 60 + float(s)

            if "time=" in line and duration > 0:
                match = re.search(r"time=(\d+):(\d+):([\d\.]+)", line)
                if match:
                    h, m, s = match.groups()
                    current = int(h) * 3600 + int(m) * 60 + float(s)
                    if progress_callback:
                        progress_callback(min(current / duration, 1.0))

            if cancel_flag and cancel_flag():
                process.terminate()
                process.wait()
                raise ConversionError("Conversion cancelled by user.")

        process.wait()

        if process.returncode != 0:
            raise ConversionError(f"FFmpeg failed:\n" + "\n".join(stderr_lines[-10:]))

        shutil.move(temp_path, final_output)
        return final_output

    except Exception:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise