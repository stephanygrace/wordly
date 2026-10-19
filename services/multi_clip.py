from __future__ import annotations

import json
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Optional

from models.project import ClipSegment
from services.trimmer import parse_trim_times
from utils.ffmpeg_paths import find_ffprobe, require_ffmpeg
from utils.ffmpeg_progress import parse_ffmpeg_progress_seconds
from utils.subprocess_win import background_creationflags

ProgressCallback = Callable[[float, str], None]
ShouldCancel = Callable[[], bool]

# Every clip keeps half a second on each side of the chosen range that stays
# hidden on the timeline: the media Resolve's 1 s cross-fade between clips draws on.
CLIP_HANDLE_S = 0.5
CLIP_PAD_S = CLIP_HANDLE_S

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


def _source_video_codec(video_path: Path) -> str:
    """Best-effort primary video codec name from ffprobe (empty if unknown)."""
    ffprobe = find_ffprobe()
    if not ffprobe:
        return ""
    cmd = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_name",
        "-of",
        "json",
        str(video_path),
    ]
    try:
        out = subprocess.check_output(cmd, text=True)
        data = json.loads(out)
        streams = data.get("streams") or []
        if streams:
            return str(streams[0].get("codec_name") or "").strip().lower()
    except Exception:
        return ""
    return ""


def _prefer_reencode_for_editing(video_path: Path) -> bool:
    """True when source codec is known to be sluggish in NLE timelines."""
    codec = _source_video_codec(video_path)
    return codec in {"vp8", "vp9", "av1", "hevc", "h265"}


def export_segment_clip(
    sermon_path: Path,
    segment: ClipSegment,
    output_path: Path,
    *,
    progress_cb: Optional[ProgressCallback] = None,
    should_cancel: Optional[ShouldCancel] = None,
) -> Path:
    spec = parse_trim_times(segment.start_text, segment.end_text)
    start_s = max(0.0, spec.start_seconds - CLIP_PAD_S)
    end_s = spec.end_seconds + CLIP_PAD_S  # ffmpeg stops at the end of the video
    duration_s = end_s - start_s
    output_path.parent.mkdir(parents=True, exist_ok=True)
    prefer_reencode = _prefer_reencode_for_editing(sermon_path)

    if progress_cb:
        if prefer_reencode:
            progress_cb(-1.0, f"Preparing edit-friendly encode for {segment.display_name}…")
        else:
            progress_cb(-1.0, f"Trimming {segment.display_name}…")

    copy_cmd = _build_copy_cmd(
        sermon_path,
        start_s=start_s,
        end_s=end_s,
        output_path=output_path,
    )

    copy_failed = prefer_reencode
    if not prefer_reencode:
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


def _write_concat_list_file(list_path: Path, clip_paths: list[Path]) -> None:
    lines: list[str] = []
    for p in clip_paths:
        # concat demuxer expects: file '/abs/path'
        # Escape single quotes for ffmpeg concat syntax.
        s = str(p.resolve()).replace("'", r"'\''")
        lines.append(f"file '{s}'")
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def merge_clips(
    clip_paths: list[Path],
    output_path: Path,
    *,
    progress_cb: Optional[ProgressCallback] = None,
    should_cancel: Optional[ShouldCancel] = None,
) -> Path:
    """Merge already-trimmed clips into a single highlights video."""
    if not clip_paths:
        raise ValueError("No clips to merge.")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Fast path: concat demuxer + stream copy.
    # Falls back to re-encode when sources are not bitstream-compatible.
    list_file = output_path.with_suffix(".concat.txt")
    _write_concat_list_file(list_file, clip_paths)

    if progress_cb:
        progress_cb(-1.0, "Merging clips…")

    copy_cmd = [
        _ffmpeg(),
        *_FFMPEG_GLOBAL,
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
    try:
        # Unknown overall duration; still show heartbeat progress.
        _run_ffmpeg(
            copy_cmd,
            total_duration_s=60.0,
            progress_cb=progress_cb,
            should_cancel=should_cancel,
            status="Merging",
        )
        if progress_cb:
            progress_cb(1.0, f"Done — merged to {output_path.name}")
        return output_path.resolve()
    except RuntimeError as exc:
        if "Cancelled" in str(exc):
            raise
        # Re-encode fallback (still uses concat demuxer).
        if output_path.is_file():
            try:
                output_path.unlink()
            except OSError:
                pass

    if progress_cb:
        progress_cb(0.0, "Re-encoding merged video…")

    reencode_cmd = [
        _ffmpeg(),
        *_FFMPEG_GLOBAL,
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_file),
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
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
        "-threads",
        "0",
        str(output_path),
    ]
    _run_ffmpeg(
        reencode_cmd,
        total_duration_s=120.0,
        progress_cb=progress_cb,
        should_cancel=should_cancel,
        status="Merging",
    )
    if progress_cb:
        progress_cb(1.0, f"Done — merged to {output_path.name}")
    return output_path.resolve()
