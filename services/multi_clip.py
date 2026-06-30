from __future__ import annotations

import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Optional

from models.project import ClipSegment
from services.trimmer import parse_trim_times
from utils.ffmpeg_paths import require_ffmpeg
from utils.ffmpeg_progress import parse_ffmpeg_progress_seconds
from utils.subprocess_win import background_creationflags

ProgressCallback = Callable[[float, str], None]
ShouldCancel = Callable[[], bool]

# -loglevel info: lets ffmpeg print "time=HH:MM:SS" progress lines to stderr
# which are unbuffered unlike stdout pipes.
_FFMPEG_GLOBAL = ("-nostdin", "-hide_banner", "-loglevel", "info")


def _ffmpeg() -> str:
    return require_ffmpeg()


def _format_clip_progress(
    clip_index: int,
    clip_total: int,
    clip_ratio: float,
    detail: str,
) -> tuple[float, str]:
    """Map per-clip progress into an overall 0..1 ratio and UI-friendly text."""
    overall = ((clip_index - 1) + max(0.0, min(1.0, clip_ratio))) / max(1, clip_total)
    pct = int(min(100, max(0, round(overall * 100))))
    return overall, f"Trimming — {pct}% [{clip_index}/{clip_total}] {detail}"


def _run_ffmpeg(
    cmd: list[str],
    *,
    total_duration_s: float,
    progress_cb: Optional[ProgressCallback],
    should_cancel: Optional[ShouldCancel],
    status: str,
) -> None:
    """Run an ffmpeg command with live progress from stderr.

    ffmpeg writes ``time=HH:MM:SS`` progress to stderr with ``-loglevel info``.
    stderr pipes are unbuffered in ffmpeg so we get updates every ~0.5 s.
    stdout is discarded; we never deadlock on a full pipe.
    """
    popen_kw: dict = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.PIPE,
        "text": True,
        "bufsize": 1,   # line-buffered on Python side
    }
    flags = background_creationflags()
    if flags:
        popen_kw["creationflags"] = flags

    proc = subprocess.Popen(cmd, **popen_kw)
    assert proc.stderr is not None

    error_tail: deque[str] = deque(maxlen=40)
    lock = threading.Lock()
    last_ratio = [-1.0]
    last_emit_t = [time.monotonic()]

    def emit(ratio: float, message: str) -> None:
        if progress_cb is None:
            return
        with lock:
            last_ratio[0] = ratio
            last_emit_t[0] = time.monotonic()
        progress_cb(ratio, message)

    def read_stderr() -> None:
        for line in proc.stderr:
            stripped = line.rstrip()
            # Always collect for error reporting
            if stripped:
                error_tail.append(stripped[:500])
            t = parse_ffmpeg_progress_seconds(stripped)
            if t is None:
                continue
            ratio = max(0.0, min(1.0, t / max(0.05, total_duration_s)))
            pct = int(min(100, max(0, round(ratio * 100))))
            emit(ratio, f"{status} — {pct}%")

    t_err = threading.Thread(target=read_stderr, daemon=True)
    t_err.start()

    try:
        while True:
            if should_cancel and should_cancel():
                proc.kill()
                proc.wait(timeout=10)
                raise RuntimeError("Cancelled")
            if proc.poll() is not None:
                break
            # Heartbeat: keep the UI label alive if ffmpeg is quiet for a moment
            with lock:
                stale = time.monotonic() - last_emit_t[0]
                ratio = last_ratio[0]
            if progress_cb is not None and stale >= 0.6:
                if ratio >= 0:
                    pct = int(min(100, max(0, round(ratio * 100))))
                    emit(ratio, f"{status} — {pct}%")
                else:
                    emit(-1.0, f"{status}…")
            time.sleep(0.10)
        proc.wait(timeout=15)
    finally:
        t_err.join(timeout=5.0)

    if proc.returncode != 0:
        detail = "\n".join(error_tail).strip()
        msg = f"ffmpeg failed (code {proc.returncode})"
        if detail:
            msg += "\n\n" + detail
        raise RuntimeError(msg)


def _build_copy_cmd(
    sermon_path: Path,
    *,
    start_s: float,
    end_s: float,
    output_path: Path,
) -> list[str]:
    """Fast stream-copy.

    Both ``-ss`` and ``-to`` are placed BEFORE ``-i`` so ffmpeg uses fast
    keyframe seeking and only touches the bytes inside the clip window.

    ``0:v:0?`` and ``0:a:0?`` use optional specifiers so the command works
    whether or not the file has an audio track — no ffprobe needed.
    """
    return [
        _ffmpeg(),
        *_FFMPEG_GLOBAL,
        "-y",
        "-ss", f"{start_s:.3f}",
        "-to", f"{end_s:.3f}",
        "-i", str(sermon_path),
        "-map", "0:v:0?",
        "-map", "0:a:0?",
        "-c", "copy",
        "-avoid_negative_ts", "make_zero",
        str(output_path),
    ]


def _build_reencode_cmd(
    sermon_path: Path,
    *,
    start_s: float,
    end_s: float,
    output_path: Path,
) -> list[str]:
    return [
        _ffmpeg(),
        *_FFMPEG_GLOBAL,
        "-y",
        "-ss", f"{start_s:.3f}",
        "-to", f"{end_s:.3f}",
        "-i", str(sermon_path),
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        "-threads", "0",
        str(output_path),
    ]


def _output_looks_valid(output_path: Path, expected_duration_s: float) -> bool:
    """Quick size-only check — avoids running ffprobe on large files."""
    if not output_path.is_file():
        return False
    size = output_path.stat().st_size
    if size <= 4096:
        return False
    # Stream copies at 3–4 Mb/s produce ≥ 375 KB/s.  Accept if size is in range.
    return size >= max(32_000, expected_duration_s * 375)


def export_segment_clip(
    sermon_path: Path,
    segment: ClipSegment,
    output_path: Path,
    *,
    progress_cb: Optional[ProgressCallback] = None,
    should_cancel: Optional[ShouldCancel] = None,
) -> Path:
    spec = parse_trim_times(segment.start_text, segment.end_text)
    start_s, end_s = spec.start_seconds, spec.end_seconds
    duration_s = spec.duration_seconds
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if progress_cb:
        progress_cb(-1.0, f"Trimming {segment.display_name}…")

    copy_cmd = _build_copy_cmd(
        sermon_path,
        start_s=start_s,
        end_s=end_s,
        output_path=output_path,
    )

    copy_failed = False
    try:
        _run_ffmpeg(
            copy_cmd,
            total_duration_s=duration_s,
            progress_cb=progress_cb,
            should_cancel=should_cancel,
            status=f"Clip {segment.display_name}",
        )
    except RuntimeError as exc:
        if "Cancelled" in str(exc):
            raise
        copy_failed = True

    if not copy_failed and _output_looks_valid(output_path, duration_s):
        if progress_cb:
            progress_cb(1.0, f"Done — {segment.display_name}")
        return output_path.resolve()

    # Stream copy failed (rare): fall back to re-encode
    if output_path.is_file():
        try:
            output_path.unlink()
        except OSError:
            pass

    if progress_cb:
        progress_cb(0.0, f"Re-encoding {segment.display_name}…")

    reencode_cmd = _build_reencode_cmd(
        sermon_path,
        start_s=start_s,
        end_s=end_s,
        output_path=output_path,
    )
    _run_ffmpeg(
        reencode_cmd,
        total_duration_s=duration_s,
        progress_cb=progress_cb,
        should_cancel=should_cancel,
        status=f"Encoding {segment.display_name}",
    )
    return output_path.resolve()


def export_clips(
    sermon_path: Path,
    segments: list[ClipSegment],
    output_dir: Path,
    *,
    progress_cb: Optional[ProgressCallback] = None,
    should_cancel: Optional[ShouldCancel] = None,
) -> list[Path]:
    """Export each segment as an individual clip (Clip001.mp4, Clip002.mp4 …).

    Stream-copy directly from the original sermon — no whole-file pre-processing.
    Each clip starts within seconds.
    """
    if not segments:
        raise ValueError("Add at least one timestamp range.")

    output_dir.mkdir(parents=True, exist_ok=True)
    total = len(segments)
    clip_paths: list[Path] = []

    n_label = f"{total} clip{'s' if total != 1 else ''}"
    if progress_cb:
        progress_cb(-1.0, f"Starting — {n_label}…")

    for idx, segment in enumerate(segments, start=1):
        if should_cancel and should_cancel():
            raise RuntimeError("Cancelled")

        out = output_dir / f"Clip{idx:03d}.mp4"

        def part_progress(ratio: float, msg: str, _i: int = idx) -> None:
            if progress_cb is None:
                return
            if ratio < 0:
                progress_cb(ratio, f"[{_i}/{total}] {msg}")
                return
            overall = ((_i - 1) + max(0.0, min(1.0, ratio))) / max(1, total)
            pct = int(min(100, max(0, round(overall * 100))))
            progress_cb(overall, f"Trimming — {pct}% (clip {_i}/{total})")

        export_segment_clip(
            sermon_path,
            segment,
            out,
            progress_cb=part_progress,
            should_cancel=should_cancel,
        )
        clip_paths.append(out.resolve())

    if progress_cb:
        progress_cb(1.0, f"Done — exported {n_label}")

    return clip_paths
