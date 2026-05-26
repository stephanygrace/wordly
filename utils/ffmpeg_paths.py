from __future__ import annotations

import os
import shutil
from pathlib import Path

_FFMPEG_BIN_NAMES = ("ffmpeg.exe", "ffmpeg")
_FFPROBE_BIN_NAMES = ("ffprobe.exe", "ffprobe")

_WINDOWS_CANDIDATE_DIRS: tuple[Path, ...] = (
    Path(r"C:\ffmpeg\bin"),
    Path(r"C:\Program Files\ffmpeg\bin"),
    Path(r"C:\Program Files (x86)\ffmpeg\bin"),
    Path(os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links")),
)


def _path_from_registry() -> str | None:
    """Merge User+Machine PATH from registry (Cursor/Git Bash may be stale)."""
    if os.name != "nt":
        return None
    parts: list[str] = []
    try:
        import winreg  # type: ignore[import-untyped]

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as key:
            raw, _ = winreg.QueryValueEx(key, "Path")
            if raw:
                parts.append(str(raw))
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        ) as key:
            raw, _ = winreg.QueryValueEx(key, "Path")
            if raw:
                parts.append(str(raw))
    except OSError:
        return None
    return ";".join(parts) if parts else None


def _resolve_bin(names: tuple[str, ...]) -> str | None:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    registry_path = _path_from_registry()
    if registry_path:
        for name in names:
            found = shutil.which(name, path=registry_path)
            if found:
                return found
    return None


def _scan_windows_dirs(names: tuple[str, ...]) -> str | None:
    if os.name != "nt":
        return None
    for folder in _WINDOWS_CANDIDATE_DIRS:
        if not folder.is_dir():
            continue
        for name in names:
            candidate = folder / name
            if candidate.is_file():
                return str(candidate.resolve())
    # WinGet: .../Packages/Gyan.FFmpeg_.../ffmpeg-8.x-full_build/bin
    packages = Path(os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages"))
    if packages.is_dir():
        for pattern in ("*ffmpeg*/bin", "Gyan.FFmpeg*/**/bin"):
            for folder in packages.glob(pattern):
                for name in names:
                    candidate = folder / name
                    if candidate.is_file():
                        return str(candidate.resolve())
        for exe in packages.glob(f"**/{names[0]}"):
            if exe.is_file():
                return str(exe.resolve())
    return None


def find_ffmpeg() -> str | None:
    return _resolve_bin(_FFMPEG_BIN_NAMES) or _scan_windows_dirs(_FFMPEG_BIN_NAMES)


def find_ffprobe() -> str | None:
    return _resolve_bin(_FFPROBE_BIN_NAMES) or _scan_windows_dirs(_FFPROBE_BIN_NAMES)


def ffmpeg_bin_dir() -> Path | None:
    ffmpeg = find_ffmpeg()
    if ffmpeg:
        return Path(ffmpeg).parent
    ffprobe = find_ffprobe()
    if ffprobe:
        return Path(ffprobe).parent
    return None


def require_ffmpeg() -> str:
    """Return ffmpeg executable path or raise with install instructions."""
    path = find_ffmpeg()
    if path:
        return path
    raise RuntimeError(_install_message("ffmpeg"))


def require_ffprobe() -> str:
    """Return ffprobe executable path or raise with install instructions."""
    path = find_ffprobe()
    if path:
        return path
    raise RuntimeError(_install_message("ffprobe"))


def _install_message(tool: str) -> str:
    if os.name == "nt":
        return (
            f"{tool} not found. Install FFmpeg and restart Wordly.\n\n"
            "Options:\n"
            "  winget install Gyan.FFmpeg\n"
            "  choco install ffmpeg\n"
            "Or download from https://ffmpeg.org/download.html and add the bin folder to PATH."
        )
    return f"{tool} not found on PATH. Install FFmpeg (e.g. sudo apt install ffmpeg)."
