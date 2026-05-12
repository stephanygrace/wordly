"""Convert WebVTT (.vtt) cues to SubRip (.srt) text for the existing burn-in pipeline."""

from __future__ import annotations

import re
from pathlib import Path

from utils.srt_clip import _seconds_to_srt_timestamp


def _vtt_timestamp_to_seconds(ts: str) -> float:
    """Parse a WebVTT media timestamp (comma or dot ms) into seconds."""
    ts = ts.strip().replace(",", ".")
    if not ts:
        raise ValueError("empty timestamp")
    parts = ts.split(":")
    if len(parts) == 2:
        minutes, sec = parts
        return int(minutes, 10) * 60 + float(sec)
    if len(parts) == 3:
        hours, minutes, sec = parts
        return int(hours, 10) * 3600 + int(minutes, 10) * 60 + float(sec)
    raise ValueError(f"unsupported timestamp: {ts!r}")


def _strip_vtt_markup(text: str) -> str:
    """Remove simple WebVTT / HTML-style tags for plain SRT."""
    text = re.sub(r"<[^>]+>", "", text)
    return text.replace("&nbsp;", " ").replace("&lrm;", "").replace("&rlm;", "").strip()


def _split_timestamp_line(line: str) -> tuple[str, str] | None:
    if "-->" not in line:
        return None
    left, _, right = line.partition("-->")
    t1 = left.strip().split()[-1] if left.strip() else ""
    t2 = right.strip().split()[0] if right.strip() else ""
    if not t1 or not t2:
        return None
    return t1, t2


def parse_vtt_cues(vtt_content: str) -> list[tuple[float, float, str]]:
    """
    Parse WebVTT into (start_s, end_s, plain_text) cues.

    Skips WEBVTT header blocks, NOTE, STYLE, and REGION sections.
    """
    raw = vtt_content.strip()
    if not raw.upper().startswith("WEBVTT"):
        raise ValueError("Not a WebVTT file (expected header starting with WEBVTT).")

    blocks = re.split(r"\n\s*\n", raw)
    cues: list[tuple[float, float, str]] = []

    for block in blocks:
        lines = [ln.rstrip() for ln in block.splitlines()]
        non_empty = [ln for ln in lines if ln.strip()]
        if not non_empty:
            continue
        head = non_empty[0].strip()
        if head.upper().startswith("WEBVTT"):
            continue
        if head.startswith("NOTE") or head.startswith("STYLE") or head.startswith("REGION"):
            continue

        ts_i = next((i for i, ln in enumerate(non_empty) if "-->" in ln), None)
        if ts_i is None:
            continue
        pair = _split_timestamp_line(non_empty[ts_i].strip())
        if pair is None:
            continue
        t1s, t2s = pair
        try:
            t0 = _vtt_timestamp_to_seconds(t1s)
            t1 = _vtt_timestamp_to_seconds(t2s)
        except (ValueError, TypeError):
            continue
        if t1 <= t0:
            continue
        text = "\n".join(non_empty[ts_i + 1 :]).strip()
        text = _strip_vtt_markup(text)
        cues.append((t0, t1, text if text else " "))

    return cues


def vtt_to_srt_text(vtt_content: str) -> str:
    """Convert WebVTT body to SubRip text (pass UTF-8 decoded string)."""
    return _cues_to_srt_body(parse_vtt_cues(vtt_content))


def _cues_to_srt_body(cues: list[tuple[float, float, str]]) -> str:
    out: list[str] = []
    for i, (t0, t1, text) in enumerate(cues, start=1):
        out.append(
            f"{i}\n{_seconds_to_srt_timestamp(t0)} --> {_seconds_to_srt_timestamp(t1)}\n{text}"
        )
    return "\n\n".join(out) + ("\n" if out else "")


def vtt_file_to_srt_file(src_vtt: Path, dest_srt: Path) -> int:
    """
    Read ``src_vtt``, write SubRip to ``dest_srt``.

    Returns the number of cues written (0 if none).
    """
    raw = src_vtt.read_text(encoding="utf-8-sig", errors="replace")
    cues = parse_vtt_cues(raw)
    body = _cues_to_srt_body(cues)
    dest_srt.parent.mkdir(parents=True, exist_ok=True)
    dest_srt.write_text(body, encoding="utf-8")
    return len(cues)
