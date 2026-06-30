from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from utils.ffmpeg_paths import find_ffprobe, require_ffmpeg, require_ffprobe
from utils.timecode import parse_timecode, validate_range

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


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


# ---------------------------------------------------------------------------
# Internal probe helpers
# ---------------------------------------------------------------------------

class _ProbeResult(NamedTuple):
    duration_s: float
    has_audio: bool


# Cache: (resolved_path_str, mtime_ns) → _ProbeResult
_probe_cache: dict[tuple[str, int], _ProbeResult] = {}


def _duration_from_ffmpeg_stderr(stderr: str) -> float:
    match = _DURATION_RE.search(stderr)
    if not match:
        raise ValueError("Could not read duration from the media file.")
    hours, minutes, seconds = match.groups()
    dur = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    if dur <= 0:
        raise ValueError("Could not read a positive duration from the media file.")
    return dur


def _ffmpeg_probe_stderr(video_path: Path) -> str:
    """Read container metadata via ``ffmpeg -i`` only — never decode the full file."""
    cmd = [
        require_ffmpeg(),
        "-hide_banner",
        "-i",
        str(video_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return proc.stderr


def _probe_with_ffprobe(video_path: Path) -> _ProbeResult:
    """Single ffprobe call that returns both duration and audio presence."""
    cmd = [
        require_ffprobe(),
        "-v", "error",
        "-show_entries", "format=duration:stream=codec_type",
        "-of", "json",
        str(video_path),
    ]
    out = subprocess.check_output(cmd, text=True)
    data = json.loads(out)
    dur = float(data["format"]["duration"])
    if dur <= 0:
        raise ValueError("Could not read a positive duration from the media file.")
    streams = data.get("streams", [])
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    return _ProbeResult(duration_s=dur, has_audio=has_audio)


def _probe_with_ffmpeg(video_path: Path) -> _ProbeResult:
    """Fallback when ffprobe is unavailable: parse ffmpeg -i stderr."""
    stderr = _ffmpeg_probe_stderr(video_path)
    dur = _duration_from_ffmpeg_stderr(stderr)
    has_audio = bool(re.search(r"Audio:\s", stderr))
    return _ProbeResult(duration_s=dur, has_audio=has_audio)


def _get_probe(video_path: Path) -> _ProbeResult:
    """Return cached probe result; runs ffprobe (or ffmpeg fallback) on first call."""
    resolved = video_path.resolve()
    try:
        mtime_ns = resolved.stat().st_mtime_ns
    except OSError:
        mtime_ns = 0
    key = (str(resolved), mtime_ns)
    if key not in _probe_cache:
        if find_ffprobe() is not None:
            _probe_cache[key] = _probe_with_ffprobe(resolved)
        else:
            _probe_cache[key] = _probe_with_ffmpeg(resolved)
    return _probe_cache[key]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def ffprobe_duration_seconds(video_path: Path) -> float:
    """Return container duration in seconds (cached per file mtime)."""
    return _get_probe(video_path).duration_s


def ffprobe_has_audio(video_path: Path) -> bool:
    """Return True if the file has at least one audio stream (cached per file mtime)."""
    return _get_probe(video_path).has_audio
