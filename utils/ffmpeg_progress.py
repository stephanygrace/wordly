from __future__ import annotations

import re
from typing import Optional


def parse_ffmpeg_progress_seconds(line: str) -> Optional[float]:
    m = re.search(r"out_time_ms=(\d+)", line)
    if m:
        return int(m.group(1)) / 1_000_000.0
    m = re.search(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)", line)
    if not m:
        return None
    h = int(m.group(1))
    m_min = int(m.group(2))
    sec = float(m.group(3))
    return h * 3600 + m_min * 60 + sec
