from __future__ import annotations

import re
from typing import Optional

# ffmpeg -progress pipe:1 emits key=value lines.  The sentinel "progress=end"
# or "progress=continue" marks the end of each block — not a time value.
_PROGRESS_SENTINEL_RE = re.compile(r"^progress=")


def parse_ffmpeg_progress_seconds(line: str) -> Optional[float]:
    """Return the number of output seconds processed, or None if not a progress line.

    Handles both ``-progress pipe:1`` output (``out_time_ms=N``) and the
    human-readable ``time=HH:MM:SS`` that appears in stderr log lines.
    """
    if _PROGRESS_SENTINEL_RE.match(line):
        return None
    m = re.search(r"out_time_ms=(\d+)", line)
    if m:
        return int(m.group(1)) / 1_000_000.0
    m = re.search(r"\bout_time=(\d+):(\d+):(\d+(?:\.\d+)?)", line)
    if m:
        h, mn, sec = int(m.group(1)), int(m.group(2)), float(m.group(3))
        return h * 3600 + mn * 60 + sec
    m = re.search(r"\btime=(\d+):(\d+):(\d+(?:\.\d+)?)", line)
    if not m:
        return None
    h = int(m.group(1))
    m_min = int(m.group(2))
    sec = float(m.group(3))
    return h * 3600 + m_min * 60 + sec
