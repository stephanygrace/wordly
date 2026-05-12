"""Shift ASS/SSA dialogue events to align with a trimmed clip (0-based timeline)."""

from __future__ import annotations

import copy
from pathlib import Path


def shift_ass_for_trim(
    *,
    source_ass: Path,
    clip_start_s: float,
    clip_end_s: float,
    dest_ass: Path,
    min_gap_ms: int = 50,
) -> int:
    """
    Write a new ASS/SSA file whose dialogue is relative to ``clip_start_s``.

    Non-dialogue events (comments, etc.) are dropped. Styles and script info
    from the source are preserved so FFmpeg + libass can still render karaoke
    tags (e.g. ``\\k``) when present in cue text.

    Returns the number of dialogue lines written.
    """
    try:
        import pysubs2
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "pysubs2 is required for ASS/SSA subtitles. Install requirements.txt."
        ) from exc

    clip_start_ms = int(round(clip_start_s * 1000))
    clip_end_ms = int(round(clip_end_s * 1000))
    duration_ms = max(0, clip_end_ms - clip_start_ms)
    if duration_ms <= 0:
        dest_ass.write_text("", encoding="utf-8-sig")
        return 0

    subs = pysubs2.load(str(source_ass))
    out = pysubs2.SSAFile()
    out.info = subs.info
    out.styles = subs.styles
    out.events = []

    for ev in subs.events:
        if ev.is_comment:
            continue
        if ev.end <= clip_start_ms or ev.start >= clip_end_ms:
            continue
        new_start = max(0, ev.start - clip_start_ms)
        new_end = min(duration_ms, ev.end - clip_start_ms)
        if new_end <= new_start + min_gap_ms:
            continue
        ne = copy.deepcopy(ev)
        ne.start = new_start
        ne.end = new_end
        out.events.append(ne)

    if not out.events:
        dest_ass.parent.mkdir(parents=True, exist_ok=True)
        dest_ass.write_text("", encoding="utf-8-sig")
        return 0

    dest_ass.parent.mkdir(parents=True, exist_ok=True)
    out.save(str(dest_ass))
    return len(out.events)
