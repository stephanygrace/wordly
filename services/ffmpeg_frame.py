from __future__ import annotations

import shutil
import subprocess
import uuid
from pathlib import Path

from utils.paths import TEMP


def extract_preview_frame(video_path: Path, time_s: float) -> Path:
    """
    Grab a single preview frame with software decoding (no CUDA/VAAPI).

    Returns path to a JPEG in ``temp/``. Raises RuntimeError on failure.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found on PATH.")

    TEMP.mkdir(parents=True, exist_ok=True)
    out = TEMP / f"preview_{uuid.uuid4().hex}.jpg"
    t = max(0.0, time_s)
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-hwaccel",
        "none",
        "-ss",
        f"{t:.3f}",
        "-i",
        str(video_path.resolve()),
        "-an",
        "-vf",
        "scale=480:-2",
        "-frames:v",
        "1",
        "-q:v",
        "5",
        "-y",
        str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0 or not out.is_file() or out.stat().st_size < 128:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(detail or f"ffmpeg preview frame failed (code {proc.returncode})")
    return out
