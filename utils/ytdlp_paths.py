from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

_YTDLP_BIN_NAMES = ("yt-dlp.exe", "yt-dlp")

_MAC_CANDIDATE_DIRS: tuple[Path, ...] = (
    Path.home() / "bin",
    Path("/opt/homebrew/bin"),
    Path("/usr/local/bin"),
)

_WINDOWS_CANDIDATE_DIRS: tuple[Path, ...] = (
    Path.home() / "bin",
    Path(os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links")),
)

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")

_UNSET = object()

# yt-dlp's PyInstaller build can take 10+ seconds to cold-start; never spawn it
# repeatedly just to read --version during a download.
_cached_executable: str | None | object = _UNSET
_cached_version: tuple[int, int, int] | None | object = _UNSET


def _resolve_bin(names: tuple[str, ...]) -> str | None:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


def _scan_dirs(dirs: tuple[Path, ...], names: tuple[str, ...]) -> str | None:
    for folder in dirs:
        if not folder.is_dir():
            continue
        for name in names:
            candidate = folder / name
            if candidate.is_file():
                return str(candidate.resolve())
    return None


def find_yt_dlp_executable() -> str | None:
    """Return the yt-dlp executable path, preferring ~/bin over PATH."""
    if sys.platform == "darwin":
        return _scan_dirs(_MAC_CANDIDATE_DIRS, _YTDLP_BIN_NAMES) or _resolve_bin(_YTDLP_BIN_NAMES)
    if os.name == "nt":
        return _scan_dirs(_WINDOWS_CANDIDATE_DIRS, _YTDLP_BIN_NAMES) or _resolve_bin(_YTDLP_BIN_NAMES)
    return _resolve_bin(_YTDLP_BIN_NAMES)


def preferred_yt_dlp_executable() -> str | None:
    """Cached system yt-dlp path for hot paths (downloads, UI labels)."""
    global _cached_executable
    if _cached_executable is _UNSET:
        _cached_executable = find_yt_dlp_executable()
    return _cached_executable  # type: ignore[return-value]


def parse_yt_dlp_version(text: str) -> tuple[int, int, int] | None:
    match = _VERSION_RE.search(text.strip())
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def yt_dlp_executable_version(exe: str) -> tuple[int, int, int] | None:
    try:
        proc = subprocess.run(
            [exe, "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = (proc.stdout or proc.stderr or "").strip()
    return parse_yt_dlp_version(output)


def packaged_yt_dlp_version() -> tuple[int, int, int] | None:
    try:
        from yt_dlp.version import __version__ as version_text
    except ImportError:
        return None
    return parse_yt_dlp_version(str(version_text))


def cached_yt_dlp_executable_version() -> tuple[int, int, int] | None:
    """Lazy, once-per-process version lookup for the preferred executable."""
    global _cached_version
    if _cached_version is not _UNSET:
        return _cached_version  # type: ignore[return-value]

    exe = preferred_yt_dlp_executable()
    _cached_version = yt_dlp_executable_version(exe) if exe else None
    return _cached_version  # type: ignore[return-value]


def yt_dlp_backend_label() -> str:
    """Short label for UI without spawning yt-dlp on every wizard paint."""
    exe = preferred_yt_dlp_executable()
    if exe:
        return f"system yt-dlp ({exe})"

    packaged = packaged_yt_dlp_version()
    if packaged is not None:
        version_label = ".".join(str(part) for part in packaged)
        return f"venv yt-dlp {version_label}"
    return "venv yt-dlp"
