"""Shift SubRip (.srt) cues so they align with a trimmed clip (0-based timeline)."""

from __future__ import annotations

import re
from pathlib import Path

_TIME_LINE = re.compile(
    r"^(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})",
)


def _srt_timestamp_to_seconds(ts: str) -> float:
    h, m, rest = ts.split(":")
    s, ms = rest.split(",")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def _seconds_to_srt_timestamp(sec: float) -> str:
    if sec < 0:
        sec = 0.0
    ms = int(round((sec - int(sec)) * 1000))
    whole = int(sec)
    if ms >= 1000:
        ms = 0
        whole += 1
    h, rem = divmod(whole, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def shift_srt_for_trim(
    *,
    source_srt: Path,
    clip_start_s: float,
    clip_end_s: float,
    dest_srt: Path,
    min_gap_s: float = 0.05,
) -> int:
    """
    Write a new SRT whose cues are relative to ``clip_start_s`` (first frame = 0).

    Cues are clipped to ``[0, clip_end_s - clip_start_s]``. Overlapping ranges are
    preserved; cues entirely outside the trim window are dropped.

    Returns the number of cues written.
    """
    raw = source_srt.read_text(encoding="utf-8-sig", errors="replace")
    blocks = re.split(r"\n\s*\n", raw.strip())
    duration = max(0.0, clip_end_s - clip_start_s)
    if duration <= 0:
        dest_srt.write_text("", encoding="utf-8")
        return 0

    out_blocks: list[str] = []
    idx = 1
    for block in blocks:
        lines = [ln.rstrip() for ln in block.strip().splitlines()]
        if not lines:
            continue
        i = 0
        if i < len(lines) and lines[i].strip().isdigit():
            i += 1
        if i >= len(lines):
            continue
        m = _TIME_LINE.match(lines[i].strip())
        if not m:
            continue
        try:
            t0 = _srt_timestamp_to_seconds(m.group(1))
            t1 = _srt_timestamp_to_seconds(m.group(2))
        except (ValueError, IndexError):
            continue
        if t1 <= t0:
            continue
        text = "\n".join(lines[i + 1 :]).strip()
        if t1 <= clip_start_s or t0 >= clip_end_s:
            continue
        n0 = max(0.0, t0 - clip_start_s)
        n1 = min(duration, t1 - clip_start_s)
        if n1 <= n0 + min_gap_s:
            continue
        out_blocks.append(
            f"{idx}\n"
            f"{_seconds_to_srt_timestamp(n0)} --> {_seconds_to_srt_timestamp(n1)}\n"
            f"{text if text else ' '}"
        )
        idx += 1

    dest_srt.parent.mkdir(parents=True, exist_ok=True)
    dest_srt.write_text("\n\n".join(out_blocks) + ("\n" if out_blocks else ""), encoding="utf-8")
    return len(out_blocks)
