from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

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


def clamp_trim_to_duration(spec: TrimSpec, media_duration_s: float) -> TrimSpec:
    """Clamp start/end so they stay inside [0, media_duration] with positive length."""
    if media_duration_s <= 0:
        raise ValueError("Media duration must be positive.")
    start = max(0.0, min(spec.start_seconds, media_duration_s - 0.05))
    end = max(0.0, min(spec.end_seconds, media_duration_s))
    if end <= start:
        end = min(media_duration_s, start + min(1.0, media_duration_s - start))
    if end <= start:
        raise ValueError("Clip length is too short after clamping to file duration.")
    return TrimSpec(
        start_seconds=start,
        end_seconds=end,
        duration_seconds=end - start,
    )


def ffprobe_duration_seconds(video_path: Path) -> float:
    """Return container duration in seconds using ffprobe."""
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


def ffprobe_has_audio(video_path: Path) -> bool:
    """Return True if the file has at least one audio stream."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return False

    cmd = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "a",
        "-show_entries",
        "stream=index",
        "-of",
        "csv=p=0",
        str(video_path),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        return False
    return bool(r.stdout.strip())
