"""Audio conversion logic using FFmpeg."""

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .ffmpeg_utils import find_ffmpeg, FFmpegNotFoundError


class ConversionError(Exception):
    """Conversion failed."""
    pass


class ConversionCancelled(ConversionError):
    """Conversion was cancelled by the user (not a real failure)."""
    pass


OVERWRITE_POLICIES = ("prompt", "overwrite", "rename", "skip")

# Output formats that can carry the embedded cover picture as a plain copy.
_COVER_ART_FORMATS = ("mp3", "flac", "m4a")

# Sample rates the encoders accept; other formats are not restricted here.
_ALLOWED_SAMPLE_RATES = {
    "mp3": (8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000),
    "opus": (8000, 12000, 16000, 24000, 48000),
}

_OUTPUT_FORMATS = ("mp3", "aac", "m4a", "ogg", "opus", "flac", "wav", "aiff")


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
    preserve_cover_art: bool = True
    metadata: dict = field(default_factory=dict)

    trim_start: Optional[float] = None  # seconds
    trim_end: Optional[float] = None  # seconds

    # prompt/skip: raise FileExistsError if the output exists (the caller
    # decides what to do); overwrite: replace it; rename: add _1, _2, ...
    overwrite_policy: str = "rename"


def parse_timecode(value: str) -> Optional[float]:
    """Parse a time string ('SS', 'MM:SS', or 'HH:MM:SS(.ms)') into seconds.

    Returns None for empty/whitespace strings. Raises ConversionError if invalid.
    """
    text = (value or "").strip()
    if not text:
        return None
    parts = text.split(":")
    try:
        if len(parts) == 1:
            seconds = float(parts[0])
        elif len(parts) == 2:
            seconds = int(parts[0]) * 60 + float(parts[1])
        elif len(parts) == 3:
            seconds = int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        else:
            raise ValueError("too many colons")
    except ValueError as e:
        raise ConversionError(
            f"Invalid time format '{value}'. Use seconds (e.g. 12.5) or MM:SS / HH:MM:SS."
        ) from e

    if seconds < 0:
        raise ConversionError(f"Time cannot be negative: '{value}'")
    return seconds


def format_duration(seconds: float) -> str:
    """Format seconds as MM:SS or HH:MM:SS."""
    if seconds is None or seconds < 0:
        return "00:00"
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _get_temp_path(output_path: str) -> str:
    """Generate a temporary file path next to the final output.

    The name is deliberately short and independent of the output name, so a
    very long (but valid) output file name never makes the temp name invalid.
    """
    parent = Path(output_path).parent
    try:
        fd, temp_path = tempfile.mkstemp(
            prefix="audioconv_tmp_", suffix=Path(output_path).suffix, dir=str(parent)
        )
    except OSError as e:
        raise ConversionError(f"Cannot create the output file in '{parent}': {e}") from e
    os.close(fd)
    return temp_path


def _same_file(a: str, b: str) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _validate_settings(settings: "ConversionSettings") -> None:
    """Reject unusable settings before any file is touched."""
    if settings.overwrite_policy not in OVERWRITE_POLICIES:
        raise ValueError(
            f"Unknown overwrite policy '{settings.overwrite_policy}' "
            f"(expected one of: {', '.join(OVERWRITE_POLICIES)})"
        )

    fmt = settings.output_format.lower()
    if fmt not in _OUTPUT_FORMATS:
        raise ConversionError(f"Unsupported output format: {fmt}")

    allowed = _ALLOWED_SAMPLE_RATES.get(fmt)
    if settings.sample_rate and allowed and settings.sample_rate not in allowed:
        raise ConversionError(
            f"{fmt.upper()} does not support {settings.sample_rate} Hz. "
            f"Supported sample rates: {', '.join(str(r) for r in allowed)} Hz "
            f"(or choose 'Keep source')."
        )

    if settings.trim_start is not None and settings.trim_start < 0:
        raise ConversionError("Trim start time cannot be negative.")
    if settings.trim_end is not None and settings.trim_end <= 0:
        raise ConversionError("Trim end time must be greater than 0.")
    if (
        settings.trim_start is not None
        and settings.trim_end is not None
        and settings.trim_end <= settings.trim_start
    ):
        raise ConversionError(
            f"Trim end time ({settings.trim_end}s) must be greater than start time ({settings.trim_start}s)."
        )


def _summarize_ffmpeg_error(lines: list[str]) -> str:
    """Turn raw FFmpeg stderr into a readable message."""
    useful = [
        l for l in lines
        if l and not l.startswith(("size=", "frame=")) and "Conversion failed!" not in l
    ]
    tail = "\n".join(useful[-6:])
    text = "\n".join(lines)

    reason = None
    if "Invalid data found when processing input" in text:
        reason = "The input file is damaged, empty or not a supported audio file."
    elif "matches no streams" in text:
        reason = "The input file contains no audio stream."
    elif "Invalid argument" in text:
        reason = (
            "The encoder rejected the selected settings "
            "(for example an unsupported sample rate / bitrate combination)."
        )

    head = f"FFmpeg failed: {reason}" if reason else "FFmpeg failed."
    return f"{head}\n\nFFmpeg output:\n{tail}"


def _build_ffmpeg_command(settings: "ConversionSettings", temp_path: str) -> list[str]:
    """Build FFmpeg command list from settings."""
    ffmpeg = find_ffmpeg()
    cmd = [ffmpeg, "-y", "-hide_banner", "-nostdin"]

    # Input
    cmd.extend(["-i", settings.input_path])

    if settings.trim_start is not None and settings.trim_start > 0:
        cmd.extend(["-ss", f"{settings.trim_start:.3f}"])
    if settings.trim_end is not None and settings.trim_end > 0:
        cmd.extend(["-to", f"{settings.trim_end:.3f}"])

    if not settings.preserve_metadata:
        cmd.extend(["-map_metadata", "-1", "-map_chapters", "-1"])

    fmt = settings.output_format.lower()

    # Audio only. Embedded cover art is copied (not re-encoded) where the
    # target container supports it; otherwise it is dropped. Without explicit
    # mapping FFmpeg tries to encode the cover into a video stream, which
    # fails for m4a and produces a bogus theora stream for ogg.
    cmd.extend(["-map", "0:a:0"])
    if (
        settings.preserve_metadata
        and settings.preserve_cover_art
        and fmt in _COVER_ART_FORMATS
    ):
        cmd.extend(["-map", "0:disp:attached_pic?", "-c:v", "copy"])

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
        # An explicitly chosen bitrate wins, unless VBR was requested.
        # (The GUI always passes a quality value, which used to override
        # the selected bitrate.)
        if settings.vbr_quality is not None and (settings.vbr or not settings.bitrate):
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
        if settings.bit_depth == 16:
            cmd.extend(["-sample_fmt", "s16"])
        elif settings.bit_depth == 24:
            # FLAC has no 24-bit sample format: it is s32 with 24 significant bits.
            cmd.extend(["-sample_fmt", "s32", "-bits_per_raw_sample", "24"])
        elif settings.bit_depth == 32:
            cmd.extend(["-sample_fmt", "s32"])
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
            # "year" is not a standard tag name; "date" is mapped by FFmpeg to
            # the right field of every container (ID3 TDRC, MP4 ©day, ...).
            if key.lower() == "year":
                key = "date"
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
        ConversionCancelled: (a ConversionError) If the user cancelled.
        FileExistsError: If output exists and policy is 'skip' or 'prompt'.
        ValueError: If the overwrite policy is unknown.
    """
    _validate_settings(settings)

    if not os.path.exists(settings.input_path):
        raise ConversionError(f"Input file not found: {settings.input_path}")
    if os.path.isfile(settings.input_path) and os.path.getsize(settings.input_path) == 0:
        raise ConversionError(f"Input file is empty: {settings.input_path}")

    final_output = settings.output_path
    output_dir = Path(final_output).parent
    if not output_dir.is_dir():
        raise ConversionError(f"Output folder does not exist: {output_dir}")

    if os.path.exists(final_output):
        if settings.overwrite_policy == "rename":
            counter = 1
            stem = Path(final_output).stem
            suffix = Path(final_output).suffix
            parent = Path(final_output).parent
            while os.path.exists(final_output):
                final_output = str(parent / f"{stem}_{counter}{suffix}")
                counter += 1
        elif settings.overwrite_policy in ("skip", "prompt"):
            raise FileExistsError(f"Output file already exists: {final_output}")
        elif _same_file(settings.input_path, final_output):
            raise ConversionError(
                "The output file is the same as the input file; "
                f"refusing to overwrite the source: {final_output}"
            )

    temp_path = _get_temp_path(final_output)
    process = None

    try:
        cmd = _build_ffmpeg_command(settings, temp_path)

        process = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
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
                    raw_duration = int(h) * 3600 + int(m) * 60 + float(s)
                    start = settings.trim_start or 0.0
                    end = min(settings.trim_end, raw_duration) if settings.trim_end else raw_duration
                    duration = max(0.0, end - start)

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
                raise ConversionCancelled("Conversion cancelled by user.")

        process.wait()

        if process.returncode != 0:
            raise ConversionError(_summarize_ffmpeg_error(stderr_lines))

        try:
            # Atomic replace (also overwrites an existing file on Windows).
            os.replace(temp_path, final_output)
        except OSError as e:
            raise ConversionError(f"Could not save the output file '{final_output}': {e}") from e
        return final_output

    except BaseException:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise
