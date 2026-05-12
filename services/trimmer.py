from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from utils.ffmpeg_progress import parse_ffmpeg_progress_seconds
from utils.paths import CLIPS
from utils.timecode import parse_timecode, validate_range

ProgressCallback = Callable[[float, str], None]
CancelCheck = Callable[[], bool]


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


def trim_ffmpeg_args(start_s: float, end_s: float) -> tuple[str, str]:
    """Return (trim_start, trim_end) as strings for FFmpeg -ss/-to style filters."""
    return f"{start_s:.3f}", f"{end_s:.3f}"


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


def export_trimmed_clip(
    sermon_path: Path,
    output_path: Path,
    start_s: float,
    end_s: float,
    *,
    has_audio: bool,
    progress_cb: Optional[ProgressCallback] = None,
    should_cancel: Optional[CancelCheck] = None,
) -> Path:
    """
    Write a trimmed sermon segment to disk (H.264 + AAC or video-only).

    Saves under ``clips/`` when a relative name is used; callers should pass an absolute path.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found on PATH.")

    duration_s = max(0.05, end_s - start_s)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    vf = f"trim=start={start_s:.6f}:end={end_s:.6f},setpts=PTS-STARTPTS"
    cmd: list[str] = [
        ffmpeg,
        "-hide_banner",
        "-y",
        "-i",
        str(sermon_path),
        "-vf",
        vf,
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
    ]
    if has_audio:
        af = f"atrim=start={start_s:.6f}:end={end_s:.6f},asetpts=PTS-STARTPTS"
        cmd += ["-af", af, "-c:a", "aac", "-b:a", "192k"]
    else:
        cmd.append("-an")

    cmd.append(str(output_path))

    proc = subprocess.Popen(
        cmd,
        stderr=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )
    assert proc.stderr is not None
    stderr_tail: deque[str] = deque(maxlen=35)

    def reader() -> None:
        for line in proc.stderr:
            stderr_tail.append(line.rstrip()[:500])
            if progress_cb is None:
                continue
            t = parse_ffmpeg_progress_seconds(line)
            if t is None:
                continue
            ratio = max(0.0, min(1.0, t / duration_s))
            progress_cb(ratio, "Saving clip…")

    th = threading.Thread(target=reader, daemon=True)
    th.start()

    try:
        while True:
            if should_cancel and should_cancel():
                proc.kill()
                proc.wait(timeout=30)
                raise RuntimeError("Cancelled")
            if proc.poll() is not None:
                break
            time.sleep(0.15)
        proc.wait(timeout=30)
    finally:
        th.join(timeout=5.0)

    if proc.returncode != 0:
        tail = "\n".join(stderr_tail).strip()
        msg = f"ffmpeg clip export failed (code {proc.returncode})"
        if tail:
            msg += "\n\nLast FFmpeg log lines:\n" + tail
        raise RuntimeError(msg)

    if progress_cb:
        progress_cb(1.0, "Clip saved")
    return output_path.resolve()


def default_clip_output_path(sermon_path: Path, start_s: float, end_s: float) -> Path:
    CLIPS.mkdir(parents=True, exist_ok=True)
    stem = sermon_path.stem[:40]
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in stem)
    return CLIPS / f"{safe}_{int(start_s)}_{int(end_s)}.mp4"
