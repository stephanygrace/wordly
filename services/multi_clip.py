from __future__ import annotations

import shutil
import subprocess
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Optional

from models.project import ClipSegment
from services.trimmer import ffprobe_has_audio, parse_trim_times
from utils.ffmpeg_paths import require_ffmpeg
from utils.ffmpeg_progress import parse_ffmpeg_progress_seconds
from utils.paths import CLIPS, TEMP
from utils.subprocess_win import background_creationflags

ProgressCallback = Callable[[float, str], None]
ShouldCancel = Callable[[], bool]


def _ffmpeg() -> str:
    return require_ffmpeg()


def _run_ffmpeg(
    cmd: list[str],
    *,
    total_duration_s: float,
    progress_cb: Optional[ProgressCallback],
    should_cancel: Optional[ShouldCancel],
    status: str,
) -> None:
    popen_kw: dict = {
        "stderr": subprocess.PIPE,
        "stdout": subprocess.DEVNULL,
        "text": True,
        "bufsize": 1,
    }
    flags = background_creationflags()
    if flags:
        popen_kw["creationflags"] = flags
    proc = subprocess.Popen(cmd, **popen_kw)
    assert proc.stderr is not None
    tail: deque[str] = deque(maxlen=30)

    def reader() -> None:
        for line in proc.stderr:
            tail.append(line.rstrip()[:500])
            if progress_cb is None:
                continue
            t = parse_ffmpeg_progress_seconds(line)
            if t is None:
                continue
            ratio = max(0.0, min(1.0, t / max(0.05, total_duration_s)))
            progress_cb(ratio, status)

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
        detail = "\n".join(tail).strip()
        msg = f"ffmpeg failed (code {proc.returncode})"
        if detail:
            msg += "\n\n" + detail
        raise RuntimeError(msg)


def export_segment_clip(
    sermon_path: Path,
    segment: ClipSegment,
    output_path: Path,
    *,
    progress_cb: Optional[ProgressCallback] = None,
    should_cancel: Optional[ShouldCancel] = None,
) -> Path:
    spec = parse_trim_times(segment.start_text, segment.end_text)
    has_audio = ffprobe_has_audio(sermon_path)
    start_s, end_s = spec.start_seconds, spec.end_seconds
    duration_s = spec.duration_seconds
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Fast path: seek before input + stream copy (no full-sermon decode/re-encode).
    copy_cmd = [
        _ffmpeg(),
        "-hide_banner",
        "-y",
        "-ss",
        f"{start_s:.3f}",
        "-to",
        f"{end_s:.3f}",
        "-i",
        str(sermon_path),
        "-map",
        "0:v:0?",
        "-map",
        "0:a:0?",
        "-c",
        "copy",
        "-avoid_negative_ts",
        "make_zero",
        str(output_path),
    ]
    if not has_audio:
        copy_cmd = [
            _ffmpeg(),
            "-hide_banner",
            "-y",
            "-ss",
            f"{start_s:.3f}",
            "-to",
            f"{end_s:.3f}",
            "-i",
            str(sermon_path),
            "-map",
            "0:v:0?",
            "-c",
            "copy",
            "-avoid_negative_ts",
            "make_zero",
            str(output_path),
        ]

    if progress_cb:
        progress_cb(0.0, f"Trimming {segment.display_name} (fast copy)…")

    proc = subprocess.run(copy_cmd, capture_output=True, text=True, check=False)
    if proc.returncode == 0 and output_path.is_file() and output_path.stat().st_size > 1024:
        try:
            from services.trimmer import ffprobe_duration_seconds

            got = ffprobe_duration_seconds(output_path)
            if got >= duration_s * 0.85:
                if progress_cb:
                    progress_cb(1.0, f"Trimmed {segment.display_name}")
                return output_path.resolve()
        except Exception:
            pass

    # Fallback: seek before input, then encode only the highlight window.
    if progress_cb:
        progress_cb(0.0, f"Trimming {segment.display_name} (encoding)…")

    cmd = [
        _ffmpeg(),
        "-hide_banner",
        "-y",
        "-ss",
        f"{start_s:.3f}",
        "-to",
        f"{end_s:.3f}",
        "-i",
        str(sermon_path),
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
    ]
    if has_audio:
        cmd += ["-c:a", "aac", "-b:a", "192k"]
    else:
        cmd.append("-an")
    cmd.append(str(output_path))

    _run_ffmpeg(
        cmd,
        total_duration_s=duration_s,
        progress_cb=progress_cb,
        should_cancel=should_cancel,
        status=f"Encoding {segment.display_name}…",
    )
    return output_path.resolve()


def concat_clips(
    clip_paths: list[Path],
    output_path: Path,
    *,
    progress_cb: Optional[ProgressCallback] = None,
    should_cancel: Optional[ShouldCancel] = None,
) -> Path:
    if not clip_paths:
        raise ValueError("No clips to join.")
    if len(clip_paths) == 1:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(clip_paths[0], output_path)
        if progress_cb:
            progress_cb(1.0, "Joined highlight reel ready")
        return output_path.resolve()

    TEMP.mkdir(parents=True, exist_ok=True)
    list_file = TEMP / f"concat_{int(time.time())}.txt"
    lines = [f"file '{p.resolve().as_posix()}'" for p in clip_paths]
    list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        _ffmpeg(),
        "-hide_banner",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_file),
        "-c",
        "copy",
        str(output_path),
    ]
    _run_ffmpeg(
        cmd,
        total_duration_s=1.0,
        progress_cb=progress_cb,
        should_cancel=should_cancel,
        status="Joining highlight clips…",
    )
    if progress_cb:
        progress_cb(1.0, "Joined highlight reel ready")
    return output_path.resolve()


def trim_and_join_segments(
    sermon_path: Path,
    segments: list[ClipSegment],
    *,
    output_path: Optional[Path] = None,
    progress_cb: Optional[ProgressCallback] = None,
    should_cancel: Optional[ShouldCancel] = None,
) -> Path:
    if not segments:
        raise ValueError("Add at least one timestamp range.")

    CLIPS.mkdir(parents=True, exist_ok=True)
    stem = sermon_path.stem[:40]
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in stem)
    joined = output_path or (CLIPS / f"{safe}_highlights_joined.mp4")

    part_paths: list[Path] = []
    total = len(segments)
    for idx, segment in enumerate(segments, start=1):
        if should_cancel and should_cancel():
            raise RuntimeError("Cancelled")
        part = CLIPS / f"{safe}_part{idx:02d}.mp4"

        def part_progress(ratio: float, msg: str, base=idx - 1) -> None:
            if progress_cb is None:
                return
            overall = (base + max(0.0, min(1.0, ratio))) / total
            progress_cb(overall, f"[{idx}/{total}] {msg}")

        export_segment_clip(
            sermon_path,
            segment,
            part,
            progress_cb=part_progress,
            should_cancel=should_cancel,
        )
        part_paths.append(part)

    return concat_clips(
        part_paths,
        joined,
        progress_cb=lambda r, m: progress_cb((total - 1 + r) / total, m) if progress_cb else None,
        should_cancel=should_cancel,
    )
