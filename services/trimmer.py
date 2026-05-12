from __future__ import annotations

from dataclasses import dataclass

from utils.timecode import parse_timecode, validate_range


@dataclass(frozen=True)
class TrimSpec:
    start_seconds: float
    end_seconds: float
    duration_seconds: float


def parse_trim_times(start_text: str, end_text: str) -> TrimSpec:
    start = parse_timecode(start_text).total_seconds
    end = parse_timecode(end_text).total_seconds
    validate_range(start, end)
    return TrimSpec(
        start_seconds=start,
        end_seconds=end,
        duration_seconds=end - start,
    )


def trim_ffmpeg_args(start_s: float, end_s: float) -> tuple[str, str]:
    """Return (trim_start, trim_end) as strings for FFmpeg -ss/-to style filters."""
    return f"{start_s:.3f}", f"{end_s:.3f}"


def ffprobe_duration_seconds(video_path: Path) -> float:
    """Return container duration in seconds using ffprobe."""
    import json
    import shutil
    import subprocess

    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe not found on PATH. Install FFmpeg.")

    cmd = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(video_path),
    ]
    out = subprocess.check_output(cmd, text=True)
    data = json.loads(out)
    dur = float(data["format"]["duration"])
    if dur <= 0:
        raise ValueError("Could not read a positive duration from the media file.")
    return dur
