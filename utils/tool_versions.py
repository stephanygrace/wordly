"""Best-effort version strings for CLI tools on PATH (startup status line)."""

from __future__ import annotations

import shutil
import subprocess


def cli_version_token(executable: str) -> str | None:
    """
    Return a short version token (e.g. ``n6.1.1``) or ``None`` if missing.

    Uses ``-version`` with a short timeout; falls back to ``\"OK\"`` on errors.
    """
    path = shutil.which(executable)
    if not path:
        return None
    try:
        r = subprocess.run(
            [path, "-version"],
            capture_output=True,
            text=True,
            timeout=2.5,
            check=False,
        )
        first = (r.stdout or "").split("\n", 1)[0].strip()
        if not first:
            return "OK"
        parts = first.split()
        for i, p in enumerate(parts):
            if p.lower() == "version" and i + 1 < len(parts):
                return parts[i + 1].rstrip(",").strip()[:24]
        return first[:24]
    except (OSError, subprocess.TimeoutExpired):
        return "OK"
