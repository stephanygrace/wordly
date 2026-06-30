from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ParsedTimecode:
    total_seconds: float


def parse_timecode(value: str) -> ParsedTimecode:
    """
    Parse flexible time strings into seconds.

    Supported:
    - HH:MM:SS or HH:MM:SS.mmm
    - MM:SS or MM:SS.mmm (treated as minutes:seconds when two segments)
    """
    raw = value.strip()
    if not raw:
        raise ValueError("Timecode is empty.")

    # Allow optional fractional seconds
    pattern = re.compile(
        r"^"
        r"(?:(?P<h>\d+):)?"
        r"(?P<m>\d+):"
        r"(?P<s>\d+)"
        r"(?:\.(?P<ms>\d+))?"
        r"$"
    )
    match = pattern.match(raw)
    if not match:
        raise ValueError(f"Invalid timecode: {value!r}")

    h = match.group("h")
    m = int(match.group("m"))
    s = int(match.group("s"))
    ms = match.group("ms")
    frac = 0.0
    if ms is not None:
        frac = int(ms.ljust(3, "0")[:3]) / 1000.0

    if h is None:
        # Two-part MM:SS
        total = m * 60 + s + frac
    else:
        hours = int(h)
        total = hours * 3600 + m * 60 + s + frac

    if total < 0:
        raise ValueError("Timecode must be non-negative.")

    return ParsedTimecode(total_seconds=total)


def format_timecode(seconds: float) -> str:
    """Format seconds as HH:MM:SS for display and editing."""
    if seconds < 0:
        seconds = 0.0
    whole = int(seconds)
    frac = seconds - whole
    h, rem = divmod(whole, 3600)
    m, s = divmod(rem, 60)
    if frac > 0.001:
        return f"{h:02d}:{m:02d}:{s:02d}.{int(frac * 1000):03d}"
    return f"{h:02d}:{m:02d}:{s:02d}"


def validate_range(start_s: float, end_s: float) -> None:
    if end_s <= start_s:
        raise ValueError("End time must be greater than start time.")


def validate_segment_times(
    start_text: str,
    end_text: str,
    *,
    media_duration_s: float | None = None,
) -> tuple[float, float]:
    """Parse start/end timecodes and ensure they form a valid in-bounds segment."""
    start = parse_timecode(start_text).total_seconds
    end = parse_timecode(end_text).total_seconds
    validate_range(start, end)
    if media_duration_s is not None and media_duration_s > 0:
        dur_label = format_timecode(media_duration_s)
        if start > media_duration_s:
            raise ValueError(f"Start is after the sermon ends ({dur_label}).")
        if end > media_duration_s:
            raise ValueError(f"End is after the sermon ends ({dur_label}).")
    return start, end
