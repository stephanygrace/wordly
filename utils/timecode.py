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


def format_timecode_digits(digits: str) -> str:
    """Format up to six digits as HH:MM:SS while typing."""
    clean = re.sub(r"\D", "", digits)[:6]
    if len(clean) <= 2:
        return clean
    if len(clean) <= 4:
        return f"{clean[:2]}:{clean[2:]}"
    return f"{clean[:2]}:{clean[2:4]}:{clean[4:]}"


def normalize_four_digit_timecode(value: str) -> str:
    """Expand a four-digit HH:MM entry to HH:MM:00.

    Typing ``0145`` is shown as ``01:45``. Before +30s / +60s math, treat that as
    one hour forty-five minutes, not one minute forty-five seconds.
    """
    raw = value.strip()
    if not raw:
        return raw
    digits = re.sub(r"\D", "", raw)
    if len(digits) != 4 or raw.count(":") != 1:
        return raw
    hours, minutes = raw.split(":", 1)
    if len(minutes) != 2:
        return raw
    return f"{hours}:{minutes}:00"


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


def end_timecode_from_start_offset(
    start_text: str,
    offset_s: float,
    *,
    media_duration_s: float | None = None,
) -> str:
    """Return a formatted end timecode at start + offset, clamped to media duration."""
    if offset_s <= 0:
        raise ValueError("Offset must be positive.")
    start = parse_timecode(start_text).total_seconds
    end = start + offset_s
    if media_duration_s is not None and media_duration_s > 0:
        end = min(end, media_duration_s)
    validate_range(start, end)
    if media_duration_s is not None and media_duration_s > 0 and end > media_duration_s:
        raise ValueError(f"End is after the sermon ends ({format_timecode(media_duration_s)}).")
    return format_timecode(end)
