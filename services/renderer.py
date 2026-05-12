from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Optional

from services.audio_mixer import (
    AudioMixSpec,
    build_amix_filter,
    build_piano_filter,
    sermon_volume_from_ui,
    piano_volume_from_ui,
)
from utils.paths import TEMP

ProgressCallback = Callable[[float, str], None]


def _escape_filter_path(path: Path) -> str:
    """Escape path for use inside FFmpeg filter arguments."""
    s = path.as_posix()
    return s.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def _find_font_file() -> Optional[str]:
    from utils.paths import ASSETS

    fonts = ASSETS / "fonts"
    if not fonts.is_dir():
        return None
    for ext in (".ttf", ".otf"):
        for f in sorted(fonts.glob(f"*{ext}")):
            return str(f.resolve())
    return None


def build_filter_complex(
    *,
    start_s: float,
    end_s: float,
    duration_s: float,
    verse_ref_path: Path,
    verse_body_path: Path,
    sermon_volume_pct: int,
    piano_volume_pct: int,
    piano_fade_in: bool,
    piano_fade_out: bool,
    fontfile: Optional[str],
) -> str:
    spec = AudioMixSpec(
        sermon_volume=sermon_volume_from_ui(sermon_volume_pct),
        piano_volume=piano_volume_from_ui(piano_volume_pct),
        piano_fade_in=piano_fade_in,
        piano_fade_out=piano_fade_out,
        clip_duration_seconds=duration_s,
    )

    ref_esc = _escape_filter_path(verse_ref_path)
    body_esc = _escape_filter_path(verse_body_path)

    font_clause = ""
    if fontfile:
        font_clause = f":fontfile={_escape_filter_path(Path(fontfile))}"

    piano = build_piano_filter(spec)

    # Vertical layout: top ~25% overlay strip, middle 50% video, bottom ~25% reserved.
    # 1080x1920 → top h=480, video region 960px tall at y=480, bottom 480px.
    draw = (
        f"[lay]drawbox=x=0:y=0:w=iw:h=480:color=black@0.62:t=fill[strip];"
        f"[strip]drawtext=textfile='{ref_esc}'{font_clause}:fontsize=34:fontcolor=white:"
        f"x=(w-text_w)/2:y=56[vref];"
        f"[vref]drawtext=textfile='{body_esc}'{font_clause}:fontsize=38:fontcolor=white:"
        f"x=(w-text_w)/2:y=130:line_spacing=18[vout]"
    )

    piano_chain = build_piano_filter(spec)
    mix = build_amix_filter()

    segments = [
        f"[0:v]trim=start={start_s:.6f}:duration={duration_s:.6f},setpts=PTS-STARTPTS,"
        f"scale=1080:960:force_original_aspect_ratio=increase,crop=1080:960,setpts=PTS-STARTPTS[vid]",
        f"[0:a]atrim=start={start_s:.6f}:duration={duration_s:.6f},asetpts=PTS-STARTPTS[sa]",
        f"color=c=0x141418:s=1080x1920:d={duration_s:.6f}:r=30[base]",
        "[base][vid]overlay=0:480:shortest=1[lay]",
        draw,
        f"[sa]volume={spec.sermon_volume:.5f}[sermon]",
        piano_chain,
        mix,
    ]
    return ";".join(segments)


def _parse_ffmpeg_time(line: str) -> Optional[float]:
    m = re.search(r"out_time_ms=(\d+)", line)
    if m:
        return int(m.group(1)) / 1_000_000.0
    m = re.search(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)", line)
    if not m:
        return None
    h, s_sec = int(m.group(1)), m.group(3)
    m_min, sec = int(m.group(2)), float(s_sec)
    return h * 3600 + m_min * 60 + sec


def render_vertical_reel(
    *,
    sermon_path: Path,
    piano_path: Path,
    output_path: Path,
    start_s: float,
    end_s: float,
    verse_reference: str,
    verse_text: str,
    sermon_volume_pct: int,
    piano_volume_pct: int,
    piano_fade_in: bool,
    piano_fade_out: bool,
    progress_cb: Optional[ProgressCallback] = None,
) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found on PATH. Install FFmpeg and retry.")

    duration_s = max(0.01, end_s - start_s)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    TEMP.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        suffix="_ref.txt",
        dir=TEMP,
        delete=False,
    ) as ref_f:
        ref_f.write(verse_reference.strip() or " ")
        ref_path = Path(ref_f.name)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        suffix="_body.txt",
        dir=TEMP,
        delete=False,
    ) as body_f:
        body_f.write((verse_text.strip() or " ") + "\n")
        body_path = Path(body_f.name)

    font = _find_font_file()
    fc = build_filter_complex(
        start_s=start_s,
        end_s=end_s,
        duration_s=duration_s,
        verse_ref_path=ref_path,
        verse_body_path=body_path,
        sermon_volume_pct=sermon_volume_pct,
        piano_volume_pct=piano_volume_pct,
        piano_fade_in=piano_fade_in,
        piano_fade_out=piano_fade_out,
        fontfile=font,
    )

    cmd = [
        ffmpeg,
        "-hide_banner",
        "-y",
        "-i",
        str(sermon_path),
        "-i",
        str(piano_path),
        "-filter_complex",
        fc,
        "-map",
        "[vout]",
        "-map",
        "[aout]",
        "-r",
        "30",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        str(output_path),
    ]

    proc = subprocess.Popen(
        cmd,
        stderr=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )
    assert proc.stderr is not None
    try:
        for line in proc.stderr:
            if progress_cb is None:
                continue
            t = _parse_ffmpeg_time(line)
            if t is None:
                continue
            ratio = max(0.0, min(1.0, t / duration_s))
            progress_cb(ratio, "Rendering…")
    finally:
        proc.wait()
        try:
            ref_path.unlink(missing_ok=True)
            body_path.unlink(missing_ok=True)
        except OSError:
            pass

    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg exited with code {proc.returncode}")

    if progress_cb:
        progress_cb(1.0, "Done")
    return output_path.resolve()
